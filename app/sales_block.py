from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot

from .hub import Hub
from .sales_api import SalesAPIClient, SalesAPIError

log = logging.getLogger("channel_guard.sales_block")

# Each loop iteration does two outbound calls to the same recipients: a
# sales-api block_user call, and a Telegram notify to the channel's own
# admin_chat_id. Telegram's own per-chat flood limit (~1 msg/sec) is the
# binding constraint here, not the sales API - paced well under it.
PACE_SECONDS = 1.0


class SalesBlockError(Exception):
    pass


class SalesBlockManager:
    """Runs a bulk block_user pass over a precomputed user list in the
    background, publishing progress over the Hub (type: "sales_block") -
    the admin panel polls /status once on load, then follows live updates
    over the ws."""

    def __init__(self):
        self._state: dict = self._fresh_state()
        self._task: asyncio.Task | None = None

    @staticmethod
    def _fresh_state() -> dict:
        return {
            "running": False,
            "started_at": None,
            "finished_at": None,
            "total": 0,
            "done": 0,
            "ok": 0,
            "failed": 0,
            "criteria": None,
            "reason": None,
            "error": None,
            "last_result": None,
        }

    def state(self) -> dict:
        return dict(self._state)

    def start(
        self,
        sales: SalesAPIClient,
        bot: Bot,
        admin_chat_id: int,
        hub: Hub,
        users: list[dict],
        reason: str,
        criteria: dict,
    ) -> None:
        if self._state["running"]:
            raise SalesBlockError("یک عملیات مسدودسازی دیگر همین الان در حال اجراست")
        self._task = asyncio.create_task(self._run(sales, bot, admin_chat_id, hub, users, reason, criteria))

    async def _run(
        self,
        sales: SalesAPIClient,
        bot: Bot,
        admin_chat_id: int,
        hub: Hub,
        users: list[dict],
        reason: str,
        criteria: dict,
    ) -> None:
        self._state = self._fresh_state()
        self._state["running"] = True
        self._state["started_at"] = int(time.time())
        self._state["total"] = len(users)
        self._state["criteria"] = criteria
        self._state["reason"] = reason
        await hub.publish({"type": "sales_block", **self._state})

        for u in users:
            chat_id = u["chat_id"]
            username = u.get("username")
            who = f"@{username} ({chat_id})" if username else str(chat_id)

            ok = True
            err_msg = None
            try:
                await sales.block_user(chat_id, reason)
            except SalesAPIError as exc:
                ok = False
                err_msg = str(exc)
            except Exception as exc:
                ok = False
                err_msg = str(exc)
                log.exception("bulk block: unexpected error for %s", chat_id)

            if ok:
                self._state["ok"] += 1
            else:
                self._state["failed"] += 1
            self._state["done"] += 1
            self._state["last_result"] = {
                "chat_id": chat_id, "username": username, "ok": ok, "error": err_msg,
            }
            await hub.publish({"type": "sales_block", **self._state})

            try:
                icon = "✅" if ok else "❌"
                text = f"{icon} مسدودسازی خودکار ({self._state['done']}/{self._state['total']}): {who}\nدلیل: {reason}"
                if not ok:
                    text += f"\nخطا: {err_msg}"
                await bot.send_message(admin_chat_id, text)
            except Exception:
                log.exception("failed to send block-log notification for %s", chat_id)

            await asyncio.sleep(PACE_SECONDS)

        self._state["running"] = False
        self._state["finished_at"] = int(time.time())
        await hub.publish({"type": "sales_block", **self._state, "final": True})

        try:
            summary = (
                f"🚫 عملیات مسدودسازی گروهی تمام شد.\n"
                f"مجموع: {self._state['total']} · موفق: {self._state['ok']} · ناموفق: {self._state['failed']}\n"
                f"دلیل: {reason}"
            )
            await bot.send_message(admin_chat_id, summary)
        except Exception:
            log.exception("failed to send block-summary notification")
