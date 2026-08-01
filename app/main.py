from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiohttp import web

from .bot_handlers import build_router
from .channels import ChannelRuntime, PendingChannels
from .config import Config
from .db import Database
from .hub import Hub
from .sales_api import SalesAPIClient, safe_int
from .sales_block import SalesBlockManager
from .web.server import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("channel_guard.main")

TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))


def _parse_payment_time(value: str | None) -> int:
    """Payments' own "time" field is a formatted string ("2025/06/01
    21:05:21"), unlike invoices' unix-epoch strings - assumed to already be
    in Tehran local time, matching every other timestamp convention in this
    codebase."""
    if not value:
        return 0
    try:
        dt = datetime.strptime(value, "%Y/%m/%d %H:%M:%S").replace(tzinfo=TEHRAN_TZ)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return 0


async def periodic_refresher(cfg: Config, ch: ChannelRuntime) -> None:
    """Keeps purchase/test data current for every member still in the channel,
    since a purchase can happen minutes or weeks after joining - not just
    within the analytics window."""
    interval = cfg.refresh_interval_minutes * 60
    db = ch.db
    while True:
        await asyncio.sleep(interval)
        try:
            # Re-read ch.sales fresh every cycle (rather than capturing it
            # once as a fixed parameter) so an admin editing this channel's
            # sales-bot URL/token from the settings panel takes effect on
            # this loop's very next run, no restart needed.
            sales = ch.sales
            chat_ids = await db.all_active_member_ids()
            log.info("periodic refresh: updating %d active members", len(chat_ids))
            for chat_id in chat_ids:
                profile = await sales.get_user_profile(chat_id)
                await db.upsert_sales_snapshot(profile)
                await asyncio.sleep(0.3)  # gentle pacing against the sales API
        except Exception:
            log.exception("periodic refresh failed")


async def sales_sync_loop(ch: ChannelRuntime) -> None:
    """Keeps the local sales_invoices/sales_products cache in sync with the
    sales bot's /invoice(actions=invoices) API, which is the only endpoint
    that ties a sale to its panel (Service_location). Invoices come back
    newest-first, so an incremental sync just paginates until a page has
    nothing new - except the first few pages, which are always re-fetched
    in full to pick up status changes on recently-created invoices (e.g.
    unpaid -> paid)."""
    ALWAYS_REFRESH_PAGES = 3
    PAGE_SIZE = 500
    PAGE_SIZE_USERS = 1000  # /api/users' documented max per page
    SYNC_INTERVAL_SECONDS = 600
    PRODUCT_SYNC_EVERY_N_CYCLES = 6

    db = ch.db
    cycle = 0
    while True:
        try:
            # Re-read fresh each cycle (see periodic_refresher for why).
            sales = ch.sales
            page = 1
            while True:
                result = await sales.list_invoices(page=page, limit=PAGE_SIZE)
                invoices = result["invoices"]
                if not invoices:
                    break
                rows = [
                    {
                        "id": inv["id"],
                        "chat_id": safe_int(inv.get("id_user")),
                        # Service_location is the invoice's real panel/plan
                        # category (matches the sales bot's own per-panel
                        # reports) - name_product is a generic "custom
                        # service" placeholder for ~90% of invoices and
                        # carries no useful grouping information.
                        "panel_name": inv.get("Service_location"),
                        "price": safe_int(inv.get("price")) or 0,
                        "status": inv.get("Status"),
                        "invoice_time": safe_int(inv.get("time")) or 0,
                    }
                    for inv in invoices
                ]
                new_count = await db.upsert_sales_invoices(rows)
                log.info("sales sync: page %d, %d invoices, %d new", page, len(rows), new_count)

                if new_count == 0 and page > ALWAYS_REFRESH_PAGES:
                    break
                total_pages = result["pagination"].get("total_pages")
                if total_pages and page >= total_pages:
                    break
                page += 1
                await asyncio.sleep(0.3)

            # payments is a much bigger, separate dataset (~57k rows vs ~15k
            # invoices) with no server-side filter for Payment_Method. Unlike
            # invoices, this list comes back OLDEST-first, and its
            # pagination.total_pages is unreliable (doesn't match
            # total_record/per_page) - so paginating forward from page 1
            # would just re-check the same ancient, unchanging records every
            # cycle and NEVER reach newly-created payments appended at the
            # end. Instead: probe page 1 for the current total_record, jump
            # to the computed last page, and walk BACKWARD from there.
            probe = await sales.list_payments(page=1, limit=PAGE_SIZE)
            total_record = probe["pagination"].get("total_record") or 0
            last_page = max(1, -(-total_record // PAGE_SIZE))  # ceil division

            page = last_page
            pages_checked = 0
            while page >= 1:
                result = await sales.list_payments(page=page, limit=PAGE_SIZE)
                payments = result["payments"]
                if payments:
                    rows = [
                        {
                            "id": p["id"],
                            "chat_id": safe_int(p.get("id_user")),
                            "price": safe_int(p.get("price")) or 0,
                            "payment_status": p.get("payment_status"),
                            "payment_method": p.get("Payment_Method"),
                            "payment_time": _parse_payment_time(p.get("time")),
                        }
                        for p in payments
                    ]
                    new_count = await db.upsert_sales_payments(rows)
                    log.info("sales sync: payments page %d, %d payments, %d new", page, len(rows), new_count)
                    pages_checked += 1
                    if new_count == 0 and pages_checked > ALWAYS_REFRESH_PAGES:
                        break
                page -= 1
                await asyncio.sleep(0.3)

            # users is the full sales-bot roster (~22k rows, ~22 pages at the
            # documented max page size) - cheap enough to fully re-walk every
            # cycle rather than track incrementally, and a plain re-walk is
            # the only way to pick up a user's limit_usertest/time_join
            # changing with no "what changed" signal from the API.
            page = 1
            while True:
                result = await sales.list_users(page=page, limit=PAGE_SIZE_USERS)
                users = result["users"]
                if not users:
                    break
                rows = [
                    {
                        "chat_id": safe_int(u.get("user_id")),
                        "username": u.get("username"),
                        "time_join": safe_int(u.get("time_join")),
                        "limit_usertest": safe_int(u.get("limit_usertest")),
                        "balance": safe_int(u.get("Balance")) or 0,
                        "last_message_time": safe_int(u.get("last_message_time")),
                    }
                    for u in users
                ]
                new_count = await db.upsert_sales_users(rows)
                total_pages = result["pagination"].get("total_pages")
                log.info("sales sync: users page %d/%s, %d users, %d new", page, total_pages, len(rows), new_count)
                if total_pages and page >= total_pages:
                    break
                page += 1
                await asyncio.sleep(0.3)

            if cycle % PRODUCT_SYNC_EVERY_N_CYCLES == 0:
                products = await sales.list_products(limit=500)
                rows = [
                    {
                        "id": p["id"],
                        "code": p.get("code_product"),
                        "name": p.get("name_product"),
                        "price": safe_int(p.get("price_product")),
                        "location": p.get("Location"),
                        "category": p.get("category"),
                        "status": p.get("status_product"),
                    }
                    for p in products
                ]
                await db.upsert_sales_products(rows)
                log.info("sales sync: refreshed %d products", len(rows))
        except Exception:
            log.exception("sales sync failed")

        cycle += 1
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)


async def run_web(
    cfg: Config,
    channels: dict[str, ChannelRuntime],
    pending_channels: PendingChannels,
    hub: Hub,
) -> None:
    app = create_app(cfg, channels, pending_channels, hub)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", cfg.panel_port)
    await site.start()
    log.info("web panel listening on :%d", cfg.panel_port)
    await asyncio.Event().wait()  # run forever


async def main() -> None:
    cfg = Config.load()

    # One Bot instance per *distinct* admin_bot_token - channels sharing a
    # token share the same Bot/polling connection; a channel with its own
    # token gets its own. aiogram ties one Dispatcher.start_polling() to
    # exactly one Bot, so this is the natural unit of "how many polling
    # connections do we need" once each channel can pick its own admin bot.
    bots_by_token: dict[str, Bot] = {}
    for ch_cfg in cfg.channels:
        if ch_cfg.admin_bot_token not in bots_by_token:
            bots_by_token[ch_cfg.admin_bot_token] = Bot(
                token=ch_cfg.admin_bot_token, default=DefaultBotProperties(parse_mode="Markdown")
            )

    channels: dict[str, ChannelRuntime] = {}
    for ch_cfg in cfg.channels:
        db = Database(
            host=cfg.mysql_host, port=cfg.mysql_port, user=cfg.mysql_user,
            password=cfg.mysql_password, database=ch_cfg.mysql_database,
        )
        await db.connect()
        sales = SalesAPIClient(ch_cfg.sales_api_base_url, ch_cfg.sales_api_token)
        channels[ch_cfg.id] = ChannelRuntime(
            cfg=ch_cfg, db=db, sales=sales, sales_block=SalesBlockManager(),
            bot=bots_by_token[ch_cfg.admin_bot_token],
        )
    pending_channels = PendingChannels()

    hub = Hub()

    tasks = [
        asyncio.create_task(run_web(cfg, channels, pending_channels, hub), name="web_panel"),
    ]
    # One polling task per distinct bot - each needs its own freshly-built
    # router (a Router can only ever belong to one Dispatcher), but every
    # router closes over the exact same `channels` dict, so an event for
    # any channel is handled identically regardless of which bot received
    # it (and Telegram only ever delivers a channel's events to whichever
    # bot is actually admin there, so this never double-handles anything).
    for token, bot in bots_by_token.items():
        dp = Dispatcher()
        dp.include_router(build_router(cfg, channels, pending_channels, hub, bot))
        tasks.append(
            asyncio.create_task(
                dp.start_polling(bot, allowed_updates=["chat_member", "my_chat_member"]),
                name=f"bot_polling:{token[:10]}",
            )
        )
    for ch in channels.values():
        tasks.append(
            asyncio.create_task(periodic_refresher(cfg, ch), name=f"periodic_refresher:{ch.cfg.id}")
        )
        tasks.append(
            asyncio.create_task(sales_sync_loop(ch), name=f"sales_sync_loop:{ch.cfg.id}")
        )

    try:
        # aiogram handles SIGINT/SIGTERM itself and returns from start_polling
        # once it does; as soon as ANY task finishes (gracefully or not) the
        # other long-running tasks (web server, refresher loop) are cancelled
        # so the process actually exits instead of waiting for a hard kill.
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            exc = t.exception()
            if exc:
                log.error("task %s crashed", t.get_name(), exc_info=exc)
    finally:
        for ch in channels.values():
            await ch.sales.close()
        for bot in bots_by_token.values():
            await bot.session.close()
        for ch in channels.values():
            await ch.db.close()


if __name__ == "__main__":
    asyncio.run(main())
