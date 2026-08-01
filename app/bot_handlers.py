from __future__ import annotations

import logging
import time

from aiogram import Bot, Router
from aiogram.types import ChatMemberUpdated

from .channels import ChannelRuntime, PendingChannels
from .config import Config
from .db import Database
from .formatting import fmt_amount, fmt_datetime, fmt_test_limit
from .hub import Hub
from .sales_api import SalesAPIClient, SalesProfile

log = logging.getLogger("channel_guard.bot")

ACTIVE_STATUSES = {"member", "administrator", "creator"}
LEFT_STATUSES = {"left", "kicked"}


def build_router(
    cfg: Config,
    channels: dict[str, ChannelRuntime],
    pending_channels: PendingChannels,
    hub: Hub,
    bot: Bot,
) -> Router:
    router = Router(name="channel_guard")

    def _channel_for(chat_id: int) -> ChannelRuntime | None:
        # Recomputed on every event (not cached once at startup) so an
        # admin editing a channel's numeric chat_id from the settings panel
        # takes effect on the very next event - `channels` is the exact
        # same dict object the settings endpoint mutates in place, and this
        # is cheap enough (a handful of channels) to just re-scan.
        for ch in channels.values():
            if ch.cfg.channel_id == chat_id:
                return ch
        return None

    @router.chat_member()
    async def on_chat_member(event: ChatMemberUpdated) -> None:
        channel = _channel_for(event.chat.id)
        if channel is None:
            return
        db, sales = channel.db, channel.sales

        old_status = event.old_chat_member.status
        new_status = event.new_chat_member.status
        user = event.new_chat_member.user
        at = int(event.date.timestamp()) if event.date else int(time.time())

        is_join = new_status in ACTIVE_STATUSES and old_status not in ACTIVE_STATUSES
        is_leave = new_status in LEFT_STATUSES and old_status in ACTIVE_STATUSES

        if not is_join and not is_leave:
            return

        username = f"@{user.username}" if user.username else None

        if is_join:
            profile = await sales.get_user_profile(user.id)
            await db.record_join(
                user.id,
                username,
                at,
                count_payment_snapshot=profile.count_payment if profile.found else None,
                sum_payment_snapshot=profile.sum_payment if profile.found else None,
            )
            await db.upsert_sales_snapshot(profile)
            await hub.publish(
                {
                    "type": "join",
                    "chat_id": user.id,
                    "username": username,
                    "event_at": at,
                }
            )
            log.info("join: %s (%s) [channel=%s]", user.id, username, channel.cfg.id)
            return

        # leave
        await db.record_leave(user.id, username, at)
        profile = await sales.get_user_profile(user.id)
        await db.upsert_sales_snapshot(profile)
        await hub.publish(
            {
                "type": "leave",
                "chat_id": user.id,
                "username": username,
                "event_at": at,
            }
        )
        log.info("leave: %s (%s) [channel=%s]", user.id, username, channel.cfg.id)
        await _notify_admin(channel.bot, cfg, channel.cfg.admin_chat_id, user.id, username, profile)

    @router.my_chat_member()
    async def on_my_chat_member(event: ChatMemberUpdated) -> None:
        """Tracks our own bot's membership changes so a brand-new channel it
        gets added as admin to shows up as a suggestion in the panel's "add
        channel" flow, instead of the admin having to look up the numeric
        chat_id by hand."""
        if _channel_for(event.chat.id) is not None:
            return
        new_status = event.new_chat_member.status
        if new_status in ACTIVE_STATUSES:
            title = getattr(event.chat, "title", None)
            pending_channels.add(event.chat.id, title)
            log.info("detected candidate channel %s (%s)", event.chat.id, title)
        else:
            pending_channels.discard(event.chat.id)

    return router


async def _notify_admin(
    bot: Bot, cfg: Config, admin_chat_id: int, chat_id: int, username: str | None, profile: SalesProfile
) -> None:
    who = f"{username} ({chat_id})" if username else str(chat_id)
    lines = [f"🔴 یک کاربر از کانال لفت داد: {who}", f"چت‌آیدی: `{chat_id}`"]
    if username:
        lines.append(f"یوزرنیم: {username}")

    if not profile.found:
        lines.append("")
        lines.append("⚠️ این کاربر در ربات فروش یافت نشد.")
    else:
        test_text = fmt_test_limit(profile.limit_usertest)
        lines += [
            "",
            f"تاریخ عضویت در ربات فروش: {fmt_datetime(profile.time_join, cfg.timezone)}",
            f"تعداد کل خرید: {fmt_amount(profile.count_payment)}",
            f"مجموع مبلغ خرید: {fmt_amount(profile.sum_payment)}",
            f"تست‌های دریافتی/باقیمانده: {test_text}",
        ]

    try:
        await bot.send_message(admin_chat_id, "\n".join(lines), parse_mode="Markdown")
    except Exception:
        log.exception("failed to notify admin about leave of %s", chat_id)
