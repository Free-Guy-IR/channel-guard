from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import replace
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

from .. import analytics
from .. import nodes as nodes_module
from ..channels import ChannelRuntime, PendingChannels
from ..config import ROOT_DIR, Config
from ..db import Database
from ..hub import Hub
from ..sales_api import SalesAPIClient
from ..sales_block import SalesBlockError

log = logging.getLogger("channel_guard.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_COOKIE = "cg_admin_session"
SESSION_TTL_SECONDS = 7 * 86400


def create_app(
    cfg: Config,
    channels: dict[str, ChannelRuntime],
    pending_channels: PendingChannels,
    hub: Hub,
) -> web.Application:
    admin_prefix = "/" + cfg.admin_path

    app = web.Application(
        middlewares=[_make_auth_middleware(admin_prefix), _no_cache_static],
        client_max_size=10 * 1024 * 1024,
    )
    app["cfg"] = cfg
    app["channels"] = channels
    app["primary_id"] = cfg.primary_channel.id
    app["pending_channels"] = pending_channels
    app["hub"] = hub
    app["admin_prefix"] = admin_prefix
    app["sessions"] = {}  # token -> expiry unix ts

    # public
    app.router.add_static("/static/", STATIC_DIR, name="static")

    # admin (hidden behind a random path segment)
    app.router.add_get(f"{admin_prefix}/", handle_admin_index)
    app.router.add_get(f"{admin_prefix}/nodes", handle_admin_nodes_page)
    app.router.add_get(f"{admin_prefix}/sales", handle_admin_sales_page)
    app.router.add_get(f"{admin_prefix}/sales-users", handle_admin_sales_users_page)
    app.router.add_post(f"{admin_prefix}/api/login", handle_login)
    app.router.add_post(f"{admin_prefix}/api/logout", handle_logout)
    app.router.add_get(f"{admin_prefix}/api/state", handle_state)
    app.router.add_get(f"{admin_prefix}/api/nodes", handle_nodes_list)
    app.router.add_post(f"{admin_prefix}/api/nodes", handle_nodes_add)
    app.router.add_delete(f"{admin_prefix}/api/nodes/{{node_id}}", handle_nodes_delete)
    app.router.add_get(f"{admin_prefix}/api/nodes/{{node_id}}/manual-script", handle_nodes_manual_script)
    app.router.add_post(f"{admin_prefix}/api/nodes/{{node_id}}/recheck", handle_nodes_recheck)
    app.router.add_get(f"{admin_prefix}/api/sales", handle_sales_data)
    app.router.add_get(f"{admin_prefix}/api/sales/unverified-payments", handle_sales_unverified_payments)
    app.router.add_get(f"{admin_prefix}/api/sales-users", handle_sales_users_data)
    app.router.add_get(f"{admin_prefix}/api/sales-users/{{chat_id}}/payments", handle_sales_user_payments)
    app.router.add_post(f"{admin_prefix}/api/sales-users/block-preview", handle_sales_users_block_preview)
    app.router.add_post(f"{admin_prefix}/api/sales-users/block", handle_sales_users_block_start)
    app.router.add_get(f"{admin_prefix}/api/sales-users/block-status", handle_sales_users_block_status)
    app.router.add_get(f"{admin_prefix}/backup", handle_backup_page)
    app.router.add_get(f"{admin_prefix}/api/backup/status", handle_backup_status)
    app.router.add_get(f"{admin_prefix}/api/backup/settings", handle_backup_settings_get)
    app.router.add_post(f"{admin_prefix}/api/backup/settings", handle_backup_settings_post)
    app.router.add_post(f"{admin_prefix}/api/backup/run", handle_backup_run)
    app.router.add_get(f"{admin_prefix}/api/channels", handle_channels_list)
    app.router.add_post(f"{admin_prefix}/api/channels", handle_channels_add)
    app.router.add_get(f"{admin_prefix}/api/channels/{{channel_id}}", handle_channel_get)
    app.router.add_post(f"{admin_prefix}/api/channels/{{channel_id}}", handle_channel_update)
    app.router.add_get(f"{admin_prefix}/ws", handle_ws)

    return app


@web.middleware
async def _no_cache_static(request: web.Request, handler):
    resp = await handler(request)
    if request.path.startswith("/static/") or request.path.endswith((".html", "/")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def _make_auth_middleware(admin_prefix: str):
    login_path = f"{admin_prefix}/api/login"
    public_shells = {
        f"{admin_prefix}/",
        f"{admin_prefix}/nodes", f"{admin_prefix}/sales",
        f"{admin_prefix}/sales-users", f"{admin_prefix}/backup",
    }
    public_apis: set[str] = set()

    @web.middleware
    async def _auth_middleware(request: web.Request, handler):
        path = request.path
        if (
            path in public_shells
            or path == login_path
            or path in public_apis
            or path.startswith("/static/")
        ):
            return await handler(request)

        if path.startswith(admin_prefix + "/"):
            token = request.cookies.get(SESSION_COOKIE)
            sessions: dict = request.app["sessions"]
            expiry = sessions.get(token) if token else None
            if not expiry or expiry < time.time():
                return web.json_response({"status": False, "msg": "unauthorized"}, status=401)
            return await handler(request)

        return await handler(request)

    return _auth_middleware


def _render_admin_page(path: Path, admin_prefix: str) -> web.Response:
    html = path.read_text(encoding="utf-8")
    html = html.replace("__ADMIN_BASE__", admin_prefix)
    return web.Response(text=html, content_type="text/html")


def _resolve_channel(request: web.Request) -> ChannelRuntime:
    """Which channel's data a dashboard/sales/sales-users request is scoped
    to - picked via ?channel=<id>, falling back to the primary channel for
    missing/unknown ids so an old/cleared selection never 404s."""
    channels: dict[str, ChannelRuntime] = request.app["channels"]
    channel_id = request.query.get("channel")
    if channel_id and channel_id in channels:
        return channels[channel_id]
    return channels[request.app["primary_id"]]


def _primary_db(request: web.Request) -> Database:
    """Surveys, nodes and the public server list are shared across every
    channel, so they always read/write the primary channel's database,
    regardless of which channel is selected in the panel."""
    channels: dict[str, ChannelRuntime] = request.app["channels"]
    return channels[request.app["primary_id"]].db


async def handle_admin_index(request: web.Request) -> web.Response:
    return _render_admin_page(STATIC_DIR / "index.html", request.app["admin_prefix"])


async def handle_admin_nodes_page(request: web.Request) -> web.Response:
    return _render_admin_page(STATIC_DIR / "nodes.html", request.app["admin_prefix"])


async def handle_admin_sales_page(request: web.Request) -> web.Response:
    return _render_admin_page(STATIC_DIR / "sales.html", request.app["admin_prefix"])


async def handle_admin_sales_users_page(request: web.Request) -> web.Response:
    return _render_admin_page(STATIC_DIR / "sales_users.html", request.app["admin_prefix"])


async def handle_login(request: web.Request) -> web.Response:
    cfg: Config = request.app["cfg"]
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": False, "msg": "bad request"}, status=400)

    password = data.get("password", "")
    if not secrets.compare_digest(password, cfg.panel_password):
        return web.json_response({"status": False, "msg": "invalid password"}, status=401)

    token = secrets.token_urlsafe(32)
    request.app["sessions"][token] = time.time() + SESSION_TTL_SECONDS

    resp = web.json_response({"status": True})
    resp.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="Lax", max_age=SESSION_TTL_SECONDS,
        path="/",
    )
    return resp


async def handle_logout(request: web.Request) -> web.Response:
    token = request.cookies.get(SESSION_COOKIE)
    request.app["sessions"].pop(token, None)
    resp = web.json_response({"status": True})
    resp.del_cookie(SESSION_COOKIE, path="/")
    return resp


async def handle_state(request: web.Request) -> web.Response:
    cfg: Config = request.app["cfg"]
    ch = _resolve_channel(request)
    db = ch.db
    bot = ch.bot

    window_days = int(request.query.get("window_days", cfg.window_days))
    window_start = int(time.time()) - window_days * 86400

    overview, leavers, joiners, joined_members, joined_summary, returning_summary, all_tested_summary = (
        await asyncio.gather(
            analytics.overview(db, window_days),
            analytics.leaver_stats(db, window_days),
            analytics.joiner_stats(db, window_days),
            db.joined_members(window_start),
            analytics.joined_members_summary(db, window_start),
            analytics.returning_members_summary(db, window_start),
            analytics.sales_users_tested_summary(db),
        )
    )

    channel_member_count = None
    try:
        channel_member_count = await bot.get_chat_member_count(ch.cfg.channel_id)
    except Exception:
        log.warning("could not fetch channel member count", exc_info=True)

    return web.json_response(
        {
            "status": True,
            "obj": {
                "overview": overview,
                "leavers": leavers,
                "joiners": joiners,
                "joined_members": joined_members,
                "joined_summary": joined_summary,
                "returning_summary": returning_summary,
                "all_tested_summary": all_tested_summary,
                "channel_member_count": channel_member_count,
                "window_days": window_days,
                "timezone": cfg.timezone,
            },
        }
    )


async def handle_sales_data(request: web.Request) -> web.Response:
    db = _resolve_channel(request).db
    try:
        window_days = int(request.query.get("window_days", 30))
    except ValueError:
        window_days = 30
    granularity = request.query.get("granularity", "day")
    if granularity not in ("day", "week", "month"):
        granularity = "day"
    data = await analytics.sales_overview(db, window_days, granularity)
    return web.json_response({"status": True, "obj": data})


async def handle_sales_unverified_payments(request: web.Request) -> web.Response:
    db = _resolve_channel(request).db
    payments = await db.unverified_payments()
    sync_stats = await db.sales_payments_sync_stats()
    return web.json_response(
        {
            "status": True,
            "obj": {
                "payments": payments,
                "synced_count": sync_stats["count"],
                "last_synced_at": sync_stats["last_synced_at"],
            },
        }
    )


SALES_USERS_FILTERS = {"no_purchase_30", "no_purchase_60", "tested_30"}
SALES_USERS_PAGE_SIZE = 50


async def handle_sales_users_data(request: web.Request) -> web.Response:
    db = _resolve_channel(request).db
    filter_key = request.query.get("filter") or None
    if filter_key not in SALES_USERS_FILTERS:
        filter_key = None
    q = (request.query.get("q") or "").strip() or None
    try:
        page = max(1, int(request.query.get("page", 1)))
    except ValueError:
        page = 1
    offset = (page - 1) * SALES_USERS_PAGE_SIZE
    sort_by = request.query.get("sort_by") or None
    sort_dir = "asc" if request.query.get("sort_dir") == "asc" else "desc"

    rows, total = await db.filtered_sales_users(
        filter_key, SALES_USERS_PAGE_SIZE, offset, q, sort_by=sort_by, sort_dir=sort_dir
    )
    stats = await db.sales_users_stats()
    sync_stats = await db.sales_users_sync_stats()
    return web.json_response(
        {
            "status": True,
            "obj": {
                "users": rows,
                "total": total,
                "page": page,
                "limit": SALES_USERS_PAGE_SIZE,
                "stats": stats,
                "synced_count": sync_stats["count"],
                "last_synced_at": sync_stats["last_synced_at"],
            },
        }
    )


async def handle_sales_user_payments(request: web.Request) -> web.Response:
    db = _resolve_channel(request).db
    try:
        chat_id = int(request.match_info["chat_id"])
    except ValueError:
        return web.json_response({"status": False, "msg": "invalid chat_id"}, status=400)
    data = await db.payments_for_chat_id(chat_id)
    return web.json_response({"status": True, "obj": data})


MAX_BLOCK_REASON_LEN = 300


def _parse_block_criteria(data: dict) -> tuple[str, str, int | None, int | None] | None:
    """Returns (purchase_filter, deposit_filter, min_unpaid,
    min_join_age_days), or None if the request has no constraint at all - a
    bulk block with zero criteria would match the entire ~22k-user roster,
    so that's rejected outright rather than silently matching everyone."""
    purchase_filter = data.get("purchase_filter") or "any"
    if purchase_filter not in ("any", "no_purchase", "has_purchase"):
        purchase_filter = "any"

    deposit_filter = data.get("deposit_filter") or "any"
    if deposit_filter not in ("any", "no_deposit", "has_deposit"):
        deposit_filter = "any"

    min_unpaid = data.get("min_unpaid")
    try:
        min_unpaid = int(min_unpaid) if min_unpaid not in (None, "") else None
    except (ValueError, TypeError):
        min_unpaid = None
    if min_unpaid is not None and min_unpaid < 1:
        min_unpaid = None

    min_join_age_days = data.get("min_join_age_days")
    try:
        min_join_age_days = int(min_join_age_days) if min_join_age_days not in (None, "") else None
    except (ValueError, TypeError):
        min_join_age_days = None
    if min_join_age_days is not None and min_join_age_days < 1:
        min_join_age_days = None

    if purchase_filter == "any" and deposit_filter == "any" and min_unpaid is None and min_join_age_days is None:
        return None
    return purchase_filter, deposit_filter, min_unpaid, min_join_age_days


async def handle_sales_users_block_preview(request: web.Request) -> web.Response:
    db = _resolve_channel(request).db
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": False, "msg": "bad request"}, status=400)

    parsed = _parse_block_criteria(data)
    if parsed is None:
        return web.json_response(
            {"status": False, "msg": "حداقل یک فیلتر باید انتخاب بشه - نمی‌شه بدون فیلتر همه رو مسدود کرد"},
            status=400,
        )
    purchase_filter, deposit_filter, min_unpaid, min_join_age_days = parsed

    users = await db.matching_sales_users(purchase_filter, deposit_filter, min_unpaid, min_join_age_days)
    return web.json_response(
        {
            "status": True,
            "obj": {
                "total": len(users),
                "sample": users[:30],
                "criteria": {
                    "purchase_filter": purchase_filter,
                    "deposit_filter": deposit_filter,
                    "min_unpaid": min_unpaid,
                    "min_join_age_days": min_join_age_days,
                },
            },
        }
    )


async def handle_sales_users_block_start(request: web.Request) -> web.Response:
    ch = _resolve_channel(request)
    db, sales, sales_block, bot = ch.db, ch.sales, ch.sales_block, ch.bot
    hub: Hub = request.app["hub"]

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": False, "msg": "bad request"}, status=400)

    parsed = _parse_block_criteria(data)
    if parsed is None:
        return web.json_response(
            {"status": False, "msg": "حداقل یک فیلتر باید انتخاب بشه - نمی‌شه بدون فیلتر همه رو مسدود کرد"},
            status=400,
        )
    purchase_filter, deposit_filter, min_unpaid, min_join_age_days = parsed

    reason = (data.get("reason") or "").strip()
    if not reason:
        return web.json_response({"status": False, "msg": "دلیل مسدودسازی الزامیه"}, status=400)
    if len(reason) > MAX_BLOCK_REASON_LEN:
        reason = reason[:MAX_BLOCK_REASON_LEN]

    users = await db.matching_sales_users(purchase_filter, deposit_filter, min_unpaid, min_join_age_days)
    if not users:
        return web.json_response({"status": False, "msg": "هیچ کاربری با این فیلترها پیدا نشد"}, status=400)

    criteria = {
        "purchase_filter": purchase_filter,
        "deposit_filter": deposit_filter,
        "min_unpaid": min_unpaid,
        "min_join_age_days": min_join_age_days,
    }
    try:
        sales_block.start(sales, bot, ch.cfg.admin_chat_id, hub, users, reason, criteria)
    except SalesBlockError as e:
        return web.json_response({"status": False, "msg": str(e)}, status=409)

    return web.json_response({"status": True, "obj": {"total": len(users)}})


async def handle_sales_users_block_status(request: web.Request) -> web.Response:
    sales_block = _resolve_channel(request).sales_block
    return web.json_response({"status": True, "obj": sales_block.state()})


async def handle_channels_list(request: web.Request) -> web.Response:
    channels: dict[str, ChannelRuntime] = request.app["channels"]
    pending: PendingChannels = request.app["pending_channels"]
    configured_ids = {ch.cfg.channel_id for ch in channels.values()}
    return web.json_response(
        {
            "status": True,
            "obj": {
                "channels": [{"id": ch.cfg.id, "name": ch.cfg.name} for ch in channels.values()],
                "primary_id": request.app["primary_id"],
                "pending": [p for p in pending.list() if p["chat_id"] not in configured_ids],
            },
        }
    )


MAX_CHANNEL_NAME_LEN = 80
_CHANNEL_ID_RE = re.compile(r"^[a-z0-9_-]+$")


async def handle_channels_add(request: web.Request) -> web.Response:
    """Appends a new (channel, sales bot) pair to config.json. Does NOT
    take effect immediately - a brand-new channel needs its own Database
    connection, SalesAPIClient and sync tasks spun up, which only happens
    on process start. The response says so; the actual restart is a
    manually-triggered deploy step, same as every other change to this
    project."""
    channels: dict[str, ChannelRuntime] = request.app["channels"]
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": False, "msg": "bad request"}, status=400)

    ch_id = str(data.get("id") or "").strip().lower()
    name = str(data.get("name") or "").strip()
    sales_api_base_url = str(data.get("sales_api_base_url") or "").strip()
    sales_api_token = str(data.get("sales_api_token") or "").strip()
    admin_bot_token = str(data.get("admin_bot_token") or "").strip()
    try:
        channel_id = int(data.get("channel_id"))
    except (TypeError, ValueError):
        return web.json_response({"status": False, "msg": "آیدی عددی کانال نامعتبره"}, status=400)
    try:
        admin_chat_id = int(data.get("admin_chat_id"))
    except (TypeError, ValueError):
        return web.json_response({"status": False, "msg": "چت‌آیدی ادمین نامعتبره"}, status=400)

    if not ch_id or not _CHANNEL_ID_RE.match(ch_id):
        return web.json_response(
            {"status": False, "msg": "شناسه کانال باید فقط حروف انگلیسی کوچک، عدد یا خط‌تیره باشه"}, status=400
        )
    if ch_id in channels:
        return web.json_response({"status": False, "msg": "این شناسه قبلاً استفاده شده"}, status=400)
    if not name:
        return web.json_response({"status": False, "msg": "اسم کانال الزامیه"}, status=400)
    if len(name) > MAX_CHANNEL_NAME_LEN:
        name = name[:MAX_CHANNEL_NAME_LEN]
    if not sales_api_base_url or not sales_api_token:
        return web.json_response({"status": False, "msg": "آدرس و توکن API ربات فروش الزامیه"}, status=400)
    if not admin_bot_token:
        return web.json_response({"status": False, "msg": "توکن ربات ادمین این کانال الزامیه"}, status=400)
    if any(ch.cfg.channel_id == channel_id for ch in channels.values()):
        return web.json_response({"status": False, "msg": "این کانال قبلاً اضافه شده"}, status=400)

    config_path = ROOT_DIR / "config.json"
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw.setdefault("channels", [])
    raw["channels"].append(
        {
            "id": ch_id,
            "name": name,
            "channel_id": channel_id,
            "sales_api_base_url": sales_api_base_url,
            "sales_api_token": sales_api_token,
            "admin_bot_token": admin_bot_token,
            "admin_chat_id": admin_chat_id,
        }
    )
    config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    pending: PendingChannels = request.app["pending_channels"]
    pending.discard(channel_id)

    return web.json_response(
        {
            "status": True,
            "obj": {"msg": "ذخیره شد - برای فعال شدن این کانال، سرویس باید ری‌استارت بشه"},
        }
    )


async def handle_channel_get(request: web.Request) -> web.Response:
    """Full current settings for one channel, for pre-filling the settings
    modal - includes the sales-bot token and admin-bot token in the clear,
    same as the add-channel form already does, since this is behind the
    same admin session auth as everything else here."""
    channels: dict[str, ChannelRuntime] = request.app["channels"]
    ch = channels.get(request.match_info["channel_id"])
    if ch is None:
        return web.json_response({"status": False, "msg": "کانال یافت نشد"}, status=404)
    return web.json_response(
        {
            "status": True,
            "obj": {
                "id": ch.cfg.id,
                "name": ch.cfg.name,
                "channel_id": ch.cfg.channel_id,
                "sales_api_base_url": ch.cfg.sales_api_base_url,
                "sales_api_token": ch.cfg.sales_api_token,
                "admin_bot_token": ch.cfg.admin_bot_token,
                "admin_chat_id": ch.cfg.admin_chat_id,
            },
        }
    )


async def handle_channel_update(request: web.Request) -> web.Response:
    """Edits an existing channel's settings, persisted to config.json.
    channel_id / sales_api_base_url / sales_api_token apply immediately -
    no restart - by mutating this exact ChannelRuntime object's `cfg` and
    `sales` fields in place, which every other part of the running process
    (bot_handlers' per-event channel lookup, the periodic sync loops, every
    web handler) reads fresh through the same shared `channels` dict rather
    than a cached copy. admin_bot_token / admin_chat_id are saved too but
    only take effect after a restart, since a token change means starting
    a whole new Telegram polling connection - not something to do
    mid-request."""
    channels: dict[str, ChannelRuntime] = request.app["channels"]
    ch = channels.get(request.match_info["channel_id"])
    if ch is None:
        return web.json_response({"status": False, "msg": "کانال یافت نشد"}, status=404)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": False, "msg": "bad request"}, status=400)

    # Only fields that actually differ from the current value end up in
    # `updates` - the settings modal always submits every field regardless
    # of what the admin touched, so without this check editing just the
    # sales-bot URL would also spuriously "change" admin_bot_token/
    # admin_chat_id (since they're always present in the payload) and
    # falsely claim a restart is needed.
    updates: dict = {}

    if "channel_id" in data:
        try:
            new_channel_id = int(data["channel_id"])
        except (TypeError, ValueError):
            return web.json_response({"status": False, "msg": "آیدی عددی کانال نامعتبره"}, status=400)
        if new_channel_id != ch.cfg.channel_id:
            if any(other.cfg.channel_id == new_channel_id for cid, other in channels.items() if cid != ch.cfg.id):
                return web.json_response({"status": False, "msg": "این آیدی کانال قبلاً برای کانال دیگه‌ای استفاده شده"}, status=400)
            updates["channel_id"] = new_channel_id

    if "sales_api_base_url" in data:
        url = str(data["sales_api_base_url"]).strip().rstrip("/")
        if not url:
            return web.json_response({"status": False, "msg": "آدرس API ربات فروش نمی‌تونه خالی باشه"}, status=400)
        if url != ch.cfg.sales_api_base_url:
            updates["sales_api_base_url"] = url

    if "sales_api_token" in data:
        token = str(data["sales_api_token"]).strip()
        if not token:
            return web.json_response({"status": False, "msg": "توکن API ربات فروش نمی‌تونه خالی باشه"}, status=400)
        if token != ch.cfg.sales_api_token:
            updates["sales_api_token"] = token

    needs_restart = False
    if "admin_bot_token" in data:
        token = str(data["admin_bot_token"]).strip()
        if not token:
            return web.json_response({"status": False, "msg": "توکن ربات ادمین نمی‌تونه خالی باشه"}, status=400)
        if token != ch.cfg.admin_bot_token:
            updates["admin_bot_token"] = token
            needs_restart = True

    if "admin_chat_id" in data:
        try:
            new_admin_chat_id = int(data["admin_chat_id"])
        except (TypeError, ValueError):
            return web.json_response({"status": False, "msg": "چت‌آیدی ادمین نامعتبره"}, status=400)
        if new_admin_chat_id != ch.cfg.admin_chat_id:
            updates["admin_chat_id"] = new_admin_chat_id
            needs_restart = True

    if "name" in data:
        name = str(data["name"]).strip()
        if name and name != ch.cfg.name:
            updates["name"] = name[:MAX_CHANNEL_NAME_LEN]

    if not updates:
        return web.json_response({"status": False, "msg": "هیچ تغییری داده نشده"}, status=400)

    # Persist to config.json first (so a crash between here and the
    # in-memory apply below still leaves the change durable for the next
    # restart), then apply live in-memory.
    config_path = ROOT_DIR / "config.json"
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    for entry in raw.get("channels", []):
        if entry.get("id") == ch.cfg.id:
            entry.update(updates)
            break
    config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    ch.cfg = replace(ch.cfg, **updates)

    if "sales_api_base_url" in updates or "sales_api_token" in updates:
        old_sales = ch.sales
        ch.sales = SalesAPIClient(ch.cfg.sales_api_base_url, ch.cfg.sales_api_token)
        await old_sales.close()

    msg = "ذخیره و اعمال شد"
    if needs_restart:
        msg = "ذخیره شد - تغییر ربات ادمین/چت‌آیدی فقط بعد از ری‌استارت سرویس اعمال می‌شه، بقیه‌ی تغییرات همین الان اعمال شدن"

    return web.json_response({"status": True, "obj": {"msg": msg, "needs_restart": needs_restart}})


_NODE_PUBLIC_FIELDS = (
    "id", "label", "host", "ssh_port", "ssh_user",
    "status", "last_checked_at", "last_error", "installed_version", "created_at",
)


async def handle_nodes_list(request: web.Request) -> web.Response:
    db = _primary_db(request)
    rows = await db.list_nodes()
    nodes_public = [{k: r[k] for k in _NODE_PUBLIC_FIELDS} for r in rows]
    return web.json_response({"status": True, "obj": {"nodes": nodes_public}})


async def handle_nodes_add(request: web.Request) -> web.Response:
    db = _primary_db(request)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": False, "msg": "bad request"}, status=400)

    label = str(data.get("label") or "").strip()
    host = str(data.get("host") or "").strip()
    ssh_user = str(data.get("ssh_user") or "root").strip()
    ssh_password = str(data.get("ssh_password") or "")
    try:
        ssh_port = int(data.get("ssh_port") or 22)
    except (TypeError, ValueError):
        return web.json_response({"status": False, "msg": "invalid ssh_port"}, status=400)

    if not label or not host or not ssh_password:
        return web.json_response(
            {"status": False, "msg": "label, host, ssh_password required"}, status=400
        )

    node_id = await nodes_module.create_pending_node(db, label, host, ssh_port, ssh_user)
    hub: Hub = request.app["hub"]
    asyncio.create_task(nodes_module.provision_node(db, node_id, ssh_password, hub))
    return web.json_response({"status": True, "obj": {"id": node_id}})


async def handle_nodes_delete(request: web.Request) -> web.Response:
    db = _primary_db(request)
    try:
        node_id = int(request.match_info["node_id"])
    except ValueError:
        return web.json_response({"status": False, "msg": "invalid node id"}, status=400)

    node = await db.get_node(node_id)
    if not node:
        return web.json_response({"status": False, "msg": "not found"}, status=404)

    await nodes_module.remove_node(db, node)
    return web.json_response({"status": True})


async def handle_nodes_manual_script(request: web.Request) -> web.Response:
    db = _primary_db(request)
    try:
        node_id = int(request.match_info["node_id"])
    except ValueError:
        return web.json_response({"status": False, "msg": "invalid node id"}, status=400)

    node = await db.get_node(node_id)
    if not node:
        return web.json_response({"status": False, "msg": "not found"}, status=404)

    info = await nodes_module.manual_setup_info(db, node)
    return web.json_response({"status": True, "obj": info})


async def handle_nodes_recheck(request: web.Request) -> web.Response:
    db = _primary_db(request)
    try:
        node_id = int(request.match_info["node_id"])
    except ValueError:
        return web.json_response({"status": False, "msg": "invalid node id"}, status=400)

    node = await db.get_node(node_id)
    if not node:
        return web.json_response({"status": False, "msg": "not found"}, status=404)

    healthy = await nodes_module.recheck_node(db, node)
    return web.json_response({"status": True, "obj": {"healthy": healthy}})


BACKUP_TIMEOUT = ClientTimeout(total=15)


def _backup_service(request: web.Request) -> tuple[str, str]:
    cfg: Config = request.app["cfg"]
    svc = cfg.backup_service
    return svc.get("url", "http://127.0.0.1:7510"), svc.get("token", "")


async def _proxy_backup(request: web.Request, method: str, path: str) -> web.Response:
    url, token = _backup_service(request)
    kwargs: dict = {"headers": {"X-Backup-Token": token}}
    if method == "POST" and request.can_read_body:
        try:
            kwargs["json"] = await request.json()
        except Exception:
            kwargs["json"] = {}
    try:
        async with ClientSession(timeout=BACKUP_TIMEOUT) as session:
            async with session.request(method, f"{url}{path}", **kwargs) as resp:
                data = await resp.json()
                return web.json_response(data, status=resp.status)
    except Exception as exc:
        return web.json_response(
            {"status": False, "msg": f"سرویس بکاپ در دسترس نیست: {exc}"}, status=502
        )


async def handle_backup_page(request: web.Request) -> web.Response:
    return _render_admin_page(STATIC_DIR / "backup.html", request.app["admin_prefix"])


async def handle_backup_status(request: web.Request) -> web.Response:
    return await _proxy_backup(request, "GET", "/status")


async def handle_backup_settings_get(request: web.Request) -> web.Response:
    return await _proxy_backup(request, "GET", "/settings")


async def handle_backup_settings_post(request: web.Request) -> web.Response:
    return await _proxy_backup(request, "POST", "/settings")


async def handle_backup_run(request: web.Request) -> web.Response:
    return await _proxy_backup(request, "POST", "/run")


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)

    hub: Hub = request.app["hub"]
    queue = hub.subscribe()
    try:
        while True:
            get_task = asyncio.create_task(queue.get())
            recv_task = asyncio.create_task(ws.receive())
            done, pending = await asyncio.wait(
                {get_task, recv_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()

            if get_task in done:
                await ws.send_str(get_task.result())

            if recv_task in done:
                msg = recv_task.result()
                if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR, WSMsgType.CLOSING):
                    break
    finally:
        hub.unsubscribe(queue)

    return ws
