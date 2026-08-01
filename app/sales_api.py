from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger("channel_guard.sales_api")


@dataclass
class SalesProfile:
    found: bool
    chat_id: int
    username: str | None = None
    time_join: int | None = None
    count_payment: int = 0
    sum_payment: int = 0
    count_invoice: int = 0
    sum_invoice: int = 0
    limit_usertest: int | None = None
    balance: int = 0
    raw: dict | None = None

    @property
    def unlimited_test(self) -> bool:
        return self.limit_usertest is not None and self.limit_usertest < 0


class SalesAPIError(Exception):
    pass


class SalesAPIClient:
    def __init__(self, base_url: str, token: str):
        self._base_url = base_url
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Token": self._token, "Content-Type": "application/json"},
            timeout=15.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def send_message(self, chat_id: int, text: str) -> None:
        """Delivers a message to a user's chat with the sales bot itself
        (@JetPingbot), not our own channel-guard bot - useful for reaching
        someone who may have left/blocked our bot but still has theirs."""
        try:
            resp = await self._client.request(
                "POST", "/users", json={"actions": "send_message", "chat_id": str(chat_id), "text": text}
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("sales api send_message failed for chat_id=%s: %s", chat_id, exc)
            raise SalesAPIError(str(exc)) from exc

        if not data.get("status"):
            raise SalesAPIError(data.get("msg") or "send_message failed")

    async def list_invoices(self, page: int = 1, limit: int = 500) -> dict:
        """Paginated list of every invoice ever created, across all users -
        each row includes name_product, so this is the only source that lets
        sales be broken down per product (get_user_profile only has lifetime
        per-user totals with no product detail)."""
        try:
            resp = await self._client.request(
                "GET", "/invoice", json={"actions": "invoices", "page": page, "limit": limit}
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("sales api invoices list failed (page=%s): %s", page, exc)
            raise SalesAPIError(str(exc)) from exc

        if not data.get("status"):
            raise SalesAPIError(data.get("msg") or "invoices list request failed")

        obj = data.get("obj") or {}
        return {
            "invoices": obj.get("invoices") or [],
            "pagination": obj.get("pagination") or {},
        }

    async def list_payments(self, page: int = 1, limit: int = 500) -> dict:
        """Paginated list of raw payment/gateway transactions - a different
        (larger) dataset than invoices. Payment_Method=='paymentnotverify'
        marks a payment submitted through a manually-checked method (e.g. a
        bank transfer receipt); its payment_status then resolves to 'paid'
        (admin confirmed it) or 'expire'/'reject' (never confirmed)."""
        try:
            resp = await self._client.request(
                "GET", "/payment", json={"actions": "payments", "page": page, "limit": limit}
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("sales api payments list failed (page=%s): %s", page, exc)
            raise SalesAPIError(str(exc)) from exc

        if not data.get("status"):
            raise SalesAPIError(data.get("msg") or "payments list request failed")

        obj = data.get("obj") or {}
        return {
            "payments": obj.get("payments") or [],
            "pagination": obj.get("pagination") or {},
        }

    async def list_users(self, page: int = 1, limit: int = 1000, sort: str = "join_new") -> dict:
        """Paginated roster of every sales-bot user. Unlike get_user_profile,
        this list omits count_payment/count_invoice entirely (confirmed
        empirically), so purchase history for these rows has to come from
        our own synced sales_payments cache instead."""
        try:
            resp = await self._client.request(
                "GET", "/users", json={"actions": "users", "page": page, "limit": limit, "sort": sort}
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("sales api users list failed (page=%s): %s", page, exc)
            raise SalesAPIError(str(exc)) from exc

        if not data.get("status"):
            raise SalesAPIError(data.get("msg") or "users list request failed")

        obj = data.get("obj") or {}
        return {
            "users": obj.get("users") or [],
            "pagination": obj.get("pagination") or {},
        }

    async def list_products(self, limit: int = 500) -> list[dict]:
        try:
            resp = await self._client.request(
                "GET", "/product", json={"actions": "products", "limit": limit}
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("sales api products list failed: %s", exc)
            raise SalesAPIError(str(exc)) from exc

        if not data.get("status"):
            raise SalesAPIError(data.get("msg") or "products list request failed")

        return (data.get("obj") or {}).get("products") or []

    async def block_user(self, chat_id: int, reason: str) -> None:
        """Blocks a user in the sales bot itself (a separate action from
        anything on our channel bot side). `description` (the reason) is a
        required field on this action, not optional."""
        try:
            resp = await self._client.request(
                "POST",
                "/users",
                json={
                    "actions": "block_user",
                    "chat_id": str(chat_id),
                    "type_block": "block",
                    "description": reason,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("sales api block_user failed for chat_id=%s: %s", chat_id, exc)
            raise SalesAPIError(str(exc)) from exc

        if not data.get("status"):
            raise SalesAPIError(data.get("msg") or "block_user request failed")

    async def get_user_profile(self, chat_id: int) -> SalesProfile:
        try:
            resp = await self._client.request(
                "GET", "/users", json={"actions": "user", "chat_id": str(chat_id)}
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("sales api request failed for chat_id=%s: %s", chat_id, exc)
            raise SalesAPIError(str(exc)) from exc

        if not data.get("status"):
            log.info("sales api returned no data for chat_id=%s: %s", chat_id, data.get("msg"))
            return SalesProfile(found=False, chat_id=chat_id)

        users = (data.get("obj") or {}).get("users") or []
        if not users:
            return SalesProfile(found=False, chat_id=chat_id)

        u = users[0]
        return SalesProfile(
            found=True,
            chat_id=chat_id,
            username=u.get("username"),
            time_join=safe_int(u.get("time_join")),
            count_payment=safe_int(u.get("count_payment")) or 0,
            sum_payment=safe_int(u.get("sum_payment")) or 0,
            count_invoice=safe_int(u.get("count_invoice")) or 0,
            sum_invoice=safe_int(u.get("sum_invoice")) or 0,
            limit_usertest=safe_int(u.get("limit_usertest")),
            balance=safe_int(u.get("Balance")) or 0,
            raw=u,
        )


def safe_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
