from __future__ import annotations

import time
from dataclasses import dataclass

from aiogram import Bot

from .config import ChannelConfig
from .db import Database
from .sales_api import SalesAPIClient
from .sales_block import SalesBlockManager


@dataclass
class ChannelRuntime:
    """One configured (Telegram channel, sales bot) pair's live runtime
    pieces - each channel gets its own database file, sales API client and
    bulk-block manager, kept completely independent of every other
    channel's data. `cfg` and `sales` are reassigned in place (not just
    read) when an admin edits a channel's settings - see
    web/server.py:handle_channels_update - so every other piece of the app
    that holds a reference to this same ChannelRuntime object picks up the
    change immediately, no restart needed. `bot` is shared across every
    channel that happens to use the same admin_bot_token (see main.py) -
    changing which bot a channel uses does need a restart, since that
    means starting a whole new polling connection."""

    cfg: ChannelConfig
    db: Database
    sales: SalesAPIClient
    sales_block: SalesBlockManager
    bot: Bot


class PendingChannels:
    """Chats our own bot was just made admin/member of that aren't among
    the configured channels yet - surfaced in the admin panel's "add
    channel" flow as a suggestion, so the admin doesn't have to look up the
    numeric chat_id by hand. In-memory only (mirrors Hub/server_cache -
    this is a transient UX convenience, not data worth persisting)."""

    MAX_ENTRIES = 20

    def __init__(self):
        self._entries: dict[int, dict] = {}

    def add(self, chat_id: int, title: str | None) -> None:
        self._entries[chat_id] = {"chat_id": chat_id, "title": title, "detected_at": int(time.time())}
        if len(self._entries) > self.MAX_ENTRIES:
            oldest_id = min(self._entries, key=lambda k: self._entries[k]["detected_at"])
            del self._entries[oldest_id]

    def discard(self, chat_id: int) -> None:
        self._entries.pop(chat_id, None)

    def list(self) -> list[dict]:
        return sorted(self._entries.values(), key=lambda e: e["detected_at"], reverse=True)
