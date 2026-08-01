from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ChannelConfig:
    id: str
    name: str
    channel_id: int
    sales_api_base_url: str
    sales_api_token: str
    # The bot that must be admin of this specific channel to see its
    # join/leave events, and where leave-notifications for this channel get
    # sent - both per-channel (not a single system-wide bot/chat) so
    # different channels can be run by different admins/bots.
    admin_bot_token: str
    admin_chat_id: int

    @property
    def mysql_database(self) -> str:
        # Each channel gets its own MySQL database (not a shared table with
        # a tenant_id column) - same full-isolation design the old
        # one-SQLite-file-per-channel setup used, just ported to MySQL.
        return f"channel_guard_{self.id}"


@dataclass(frozen=True)
class Config:
    panel_port: int
    panel_password: str
    admin_path: str
    refresh_interval_minutes: int
    window_days: int
    timezone: str
    public_site_url: str
    mysql_host: str
    mysql_port: int
    mysql_user: str
    mysql_password: str
    channels: list[ChannelConfig]
    # {"url": "http://127.0.0.1:7510", "token": "..."} for the shared
    # panel-backup service - optional, defaults to an empty/unreachable
    # config so a missing block degrades to a clear proxy error instead of
    # crashing the whole app.
    backup_service: dict

    @property
    def primary_channel(self) -> ChannelConfig:
        # The first configured channel is "primary" - surveys and nodes are
        # shared across all channels and always live in its database, and
        # it's the panel's default selection.
        return self.channels[0]

    @staticmethod
    def load(path: str | Path = ROOT_DIR / "config.json") -> "Config":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"Config file not found at {path}. Copy config.example.json to config.json and fill it in."
            )
        data = json.loads(path.read_text(encoding="utf-8"))

        raw_channels = data.get("channels") or []
        if not raw_channels:
            raise ValueError('config.json must have at least one entry in "channels"')
        channels = [
            ChannelConfig(
                id=c["id"],
                name=c.get("name") or c["id"],
                channel_id=int(c["channel_id"]),
                sales_api_base_url=c["sales_api_base_url"].rstrip("/"),
                sales_api_token=c["sales_api_token"],
                admin_bot_token=c["admin_bot_token"],
                admin_chat_id=int(c["admin_chat_id"]),
            )
            for c in raw_channels
        ]

        mysql = data.get("mysql") or {}
        return Config(
            panel_port=int(data.get("panel_port", 900)),
            panel_password=data["panel_password"],
            admin_path=data["admin_path"].strip("/"),
            refresh_interval_minutes=int(data.get("refresh_interval_minutes", 15)),
            window_days=int(data.get("window_days", 7)),
            timezone=data.get("timezone", "Asia/Tehran"),
            public_site_url=data.get("public_site_url", "").rstrip("/"),
            mysql_host=mysql.get("host", "127.0.0.1"),
            mysql_port=int(mysql.get("port", 3306)),
            mysql_user=mysql["user"],
            mysql_password=mysql["password"],
            channels=channels,
            backup_service=data.get("backup_service") or {},
        )
