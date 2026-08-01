from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal

import aiomysql
import pymysql.constants.FIELD_TYPE as FIELD_TYPE
from pymysql.converters import conversions

from .sales_api import SalesProfile


def _decimal_to_number(value: str):
    # PyMySQL reports SUM()/aggregate results over INT-family columns as
    # DECIMAL on the wire (confirmed empirically - even SUM(price) over a
    # plain BIGINT column comes back typed DECIMAL), which the default
    # converter turns into Decimal - and Decimal isn't JSON-serializable,
    # which would break every web.json_response() call touching an
    # aggregate. Converted once here, at the connection level, instead of
    # patching every SUM()/dict(row) call site individually.
    if value is None:
        return None
    dec = Decimal(value)
    return int(dec) if dec == dec.to_integral_value() else float(dec)


_CONVERSIONS = dict(conversions)
_CONVERSIONS[FIELD_TYPE.DECIMAL] = _decimal_to_number
_CONVERSIONS[FIELD_TYPE.NEWDECIMAL] = _decimal_to_number
# DATE(...) bucket columns (daily_counts, sales_series, etc.) default to
# PyMySQL parsing into datetime.date - also not JSON-serializable, and a
# behavioral mismatch with SQLite's date(), which always returned a plain
# 'YYYY-MM-DD' string that the rest of the codebase (analytics.py, frontend
# JS date parsing) already expects. The wire value arrives pre-decoded as
# that exact string, so the identity function reproduces SQLite's behavior.
_CONVERSIONS[FIELD_TYPE.DATE] = lambda value: value

# chat_id holds real Telegram user IDs, which already exceed INT's 2.1B
# ceiling for newer accounts (confirmed empirically against live data) - so
# every chat_id column is BIGINT, never INT/INTEGER.
SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS members (
        chat_id BIGINT PRIMARY KEY,
        username VARCHAR(255),
        first_seen_at INT NOT NULL,
        status VARCHAR(16) NOT NULL,
        last_event_at INT NOT NULL,
        CHECK (status IN ('member', 'left'))
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id INT AUTO_INCREMENT PRIMARY KEY,
        chat_id BIGINT NOT NULL,
        username VARCHAR(255),
        event_type VARCHAR(16) NOT NULL,
        event_at INT NOT NULL,
        count_payment_snapshot INT,
        sum_payment_snapshot INT,
        CHECK (event_type IN ('join', 'leave'))
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS sales_snapshot (
        chat_id BIGINT PRIMARY KEY,
        fetched_at INT NOT NULL,
        found TINYINT NOT NULL DEFAULT 0,
        time_join INT,
        count_payment INT DEFAULT 0,
        sum_payment BIGINT DEFAULT 0,
        count_invoice INT DEFAULT 0,
        sum_invoice BIGINT DEFAULT 0,
        limit_usertest INT,
        balance BIGINT DEFAULT 0,
        raw_json LONGTEXT
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS surveys (
        id INT AUTO_INCREMENT PRIMARY KEY,
        config_name VARCHAR(255) NOT NULL,
        rating_speed INT NOT NULL,
        rating_stability INT NOT NULL,
        rating_overall INT NOT NULL,
        comment TEXT,
        submitted_at INT NOT NULL,
        screenshot_path VARCHAR(255),
        CHECK (rating_speed BETWEEN 1 AND 5),
        CHECK (rating_stability BETWEEN 1 AND 5),
        CHECK (rating_overall BETWEEN 1 AND 5)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS nodes (
        id INT AUTO_INCREMENT PRIMARY KEY,
        label VARCHAR(255) NOT NULL,
        host VARCHAR(255) NOT NULL,
        ssh_port INT NOT NULL DEFAULT 22,
        ssh_user VARCHAR(255) NOT NULL,
        private_key_path VARCHAR(500) NOT NULL,
        api_token VARCHAR(255) NOT NULL,
        api_port INT NOT NULL DEFAULT 8787,
        status VARCHAR(32) NOT NULL DEFAULT 'installing',
        last_checked_at INT,
        last_error TEXT,
        installed_version VARCHAR(64),
        created_at INT NOT NULL
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS extracted_members (
        chat_id BIGINT PRIMARY KEY,
        username VARCHAR(255),
        first_name VARCHAR(255),
        last_name VARCHAR(255),
        is_bot TINYINT NOT NULL DEFAULT 0,
        extracted_at INT NOT NULL
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS sales_invoices (
        id VARCHAR(64) PRIMARY KEY,
        chat_id BIGINT,
        panel_name VARCHAR(255),
        price BIGINT NOT NULL DEFAULT 0,
        status VARCHAR(64),
        invoice_time INT NOT NULL,
        synced_at INT NOT NULL
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS sales_products (
        id INT PRIMARY KEY,
        code VARCHAR(64),
        name VARCHAR(255),
        price BIGINT,
        location VARCHAR(255),
        category VARCHAR(255),
        status VARCHAR(64),
        synced_at INT NOT NULL
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS sales_payments (
        id VARCHAR(64) PRIMARY KEY,
        chat_id BIGINT,
        price BIGINT NOT NULL DEFAULT 0,
        payment_status VARCHAR(64),
        payment_method VARCHAR(64),
        payment_time INT NOT NULL,
        synced_at INT NOT NULL
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS sales_users (
        chat_id BIGINT PRIMARY KEY,
        username VARCHAR(255),
        time_join INT,
        limit_usertest INT,
        balance BIGINT NOT NULL DEFAULT 0,
        last_message_time INT,
        synced_at INT NOT NULL
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
]

# (index_name, table, full CREATE INDEX statement) - MySQL's CREATE INDEX has
# no IF NOT EXISTS clause, so each is preceded by an information_schema
# existence check (see _create_index_if_missing) to stay idempotent across
# every process restart, same as SQLite's CREATE INDEX IF NOT EXISTS did.
INDEX_STATEMENTS = [
    ("idx_events_at", "events", "CREATE INDEX idx_events_at ON events(event_at)"),
    ("idx_events_chat", "events", "CREATE INDEX idx_events_chat ON events(chat_id)"),
    ("idx_surveys_at", "surveys", "CREATE INDEX idx_surveys_at ON surveys(submitted_at)"),
    ("idx_sales_invoices_time", "sales_invoices", "CREATE INDEX idx_sales_invoices_time ON sales_invoices(invoice_time)"),
    ("idx_sales_invoices_panel", "sales_invoices", "CREATE INDEX idx_sales_invoices_panel ON sales_invoices(panel_name)"),
    ("idx_sales_payments_time", "sales_payments", "CREATE INDEX idx_sales_payments_time ON sales_payments(payment_time)"),
    ("idx_sales_payments_method", "sales_payments", "CREATE INDEX idx_sales_payments_method ON sales_payments(payment_method)"),
    ("idx_sales_users_time_join", "sales_users", "CREATE INDEX idx_sales_users_time_join ON sales_users(time_join)"),
]


class _EagerResult:
    """A pre-fetched result set standing in for a live DB cursor. Query
    methods below call execute() then, in a later separate statement,
    cur.fetchone()/fetchall() - with a single shared connection (see
    _MySQLConnWrapper), that gap is exactly where a second coroutine
    (this app leans on asyncio.gather() a lot, e.g. handle_state()) could
    run its own query on the same connection and corrupt the first one's
    unread response stream. Fetching everything eagerly, inside the locked
    section, means the connection is only ever touched by one query at a
    time end-to-end, and callers keep using the exact same
    fetchone()/fetchall()/lastrowid shape they always did."""

    def __init__(self, rows: list[dict], lastrowid: int | None):
        self._rows = rows
        self._pos = 0
        self.lastrowid = lastrowid

    async def fetchone(self) -> dict | None:
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    async def fetchall(self) -> list[dict]:
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rows


class _MySQLConnWrapper:
    """Thin shim over an aiomysql connection that mirrors the small slice of
    aiosqlite.Connection's API this file was originally written against
    (self.conn.execute(...)/.executemany(...)/.commit()) - lets every query
    method below stay structurally unchanged; only placeholder syntax
    (%s vs ?) and upsert syntax needed to change.

    Wraps every query in a lock so concurrent callers (asyncio.gather() is
    used heavily throughout this codebase) never run two queries at once on
    the one shared connection - aiosqlite serialized this for free
    internally, a raw aiomysql connection does not. The connection itself
    is autocommit=True (set in Database.connect()), so commit() below is a
    no-op kept only so every existing "await self.conn.commit()" call site
    still works unchanged. ping(reconnect=True) before each query
    transparently recovers from a dropped/idle-timed-out MySQL connection,
    which SQLite's local-file connection never had to worry about."""

    def __init__(self, conn: aiomysql.Connection):
        self._conn = conn
        self._lock = asyncio.Lock()

    async def execute(self, query: str, params=None) -> _EagerResult:
        async with self._lock:
            await self._conn.ping(reconnect=True)
            async with self._conn.cursor(aiomysql.cursors.DictCursor) as cur:
                await cur.execute(query, params or ())
                rows = await cur.fetchall()
                lastrowid = cur.lastrowid
        return _EagerResult(rows, lastrowid)

    async def executemany(self, query: str, seq_of_params) -> _EagerResult:
        async with self._lock:
            await self._conn.ping(reconnect=True)
            async with self._conn.cursor() as cur:
                await cur.executemany(query, seq_of_params)
                lastrowid = cur.lastrowid
        return _EagerResult([], lastrowid)

    async def commit(self) -> None:
        pass

    def close(self) -> None:
        self._conn.close()


class Database:
    def __init__(self, host: str, port: int, user: str, password: str, database: str):
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._database = database
        self._conn: _MySQLConnWrapper | None = None

    async def connect(self) -> None:
        # Auto-creates its own database if missing, so a brand-new channel
        # added via the panel doesn't also require a manual MySQL step -
        # needs a connection with no database selected first.
        bootstrap = await aiomysql.connect(
            host=self._host, port=self._port, user=self._user, password=self._password, autocommit=True,
        )
        async with bootstrap.cursor() as cur:
            await cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{self._database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        bootstrap.close()

        raw_conn = await aiomysql.connect(
            host=self._host, port=self._port, user=self._user, password=self._password,
            db=self._database, autocommit=True, conv=_CONVERSIONS,
        )
        self._conn = _MySQLConnWrapper(raw_conn)

        for stmt in SCHEMA_STATEMENTS:
            await self._conn.execute(stmt)
        for index_name, table, create_sql in INDEX_STATEMENTS:
            await self._create_index_if_missing(index_name, table, create_sql)
        await self._conn.commit()

    async def _create_index_if_missing(self, index_name: str, table: str, create_sql: str) -> None:
        cur = await self._conn.execute(
            "SELECT 1 FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = %s AND index_name = %s LIMIT 1",
            (table, index_name),
        )
        if not await cur.fetchone():
            await self._conn.execute(create_sql)

    async def close(self) -> None:
        if self._conn:
            self._conn.close()

    @property
    def conn(self) -> _MySQLConnWrapper:
        assert self._conn is not None, "Database.connect() must be called first"
        return self._conn

    # ---- membership events ----

    async def record_join(
        self,
        chat_id: int,
        username: str | None,
        at: int | None = None,
        count_payment_snapshot: int | None = None,
        sum_payment_snapshot: int | None = None,
    ) -> None:
        at = at or int(time.time())
        await self.conn.execute(
            """
            INSERT INTO members (chat_id, username, first_seen_at, status, last_event_at)
            VALUES (%s, %s, %s, 'member', %s)
            ON DUPLICATE KEY UPDATE
                username=VALUES(username),
                status='member',
                last_event_at=VALUES(last_event_at)
            """,
            (chat_id, username, at, at),
        )
        await self.conn.execute(
            "INSERT INTO events (chat_id, username, event_type, event_at, count_payment_snapshot, sum_payment_snapshot) "
            "VALUES (%s, %s, 'join', %s, %s, %s)",
            (chat_id, username, at, count_payment_snapshot, sum_payment_snapshot),
        )
        await self.conn.commit()

    async def record_leave(self, chat_id: int, username: str | None, at: int | None = None) -> None:
        at = at or int(time.time())
        await self.conn.execute(
            """
            INSERT INTO members (chat_id, username, first_seen_at, status, last_event_at)
            VALUES (%s, %s, %s, 'left', %s)
            ON DUPLICATE KEY UPDATE
                username=VALUES(username),
                status='left',
                last_event_at=VALUES(last_event_at)
            """,
            (chat_id, username, at, at),
        )
        await self.conn.execute(
            "INSERT INTO events (chat_id, username, event_type, event_at) VALUES (%s, %s, 'leave', %s)",
            (chat_id, username, at),
        )
        await self.conn.commit()

    async def upsert_sales_snapshot(self, profile: SalesProfile, at: int | None = None) -> None:
        at = at or int(time.time())
        await self.conn.execute(
            """
            INSERT INTO sales_snapshot
                (chat_id, fetched_at, found, time_join, count_payment, sum_payment,
                 count_invoice, sum_invoice, limit_usertest, balance, raw_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                fetched_at=VALUES(fetched_at),
                found=VALUES(found),
                time_join=VALUES(time_join),
                count_payment=VALUES(count_payment),
                sum_payment=VALUES(sum_payment),
                count_invoice=VALUES(count_invoice),
                sum_invoice=VALUES(sum_invoice),
                limit_usertest=VALUES(limit_usertest),
                balance=VALUES(balance),
                raw_json=VALUES(raw_json)
            """,
            (
                profile.chat_id,
                at,
                1 if profile.found else 0,
                profile.time_join,
                profile.count_payment,
                profile.sum_payment,
                profile.count_invoice,
                profile.sum_invoice,
                profile.limit_usertest,
                profile.balance,
                json.dumps(profile.raw, ensure_ascii=False) if profile.raw else None,
            ),
        )
        await self.conn.commit()

    # ---- reads ----

    async def totals(self) -> dict:
        cur = await self.conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM events WHERE event_type='join') AS joins, "
            "(SELECT COUNT(*) FROM events WHERE event_type='leave') AS leaves"
        )
        row = await cur.fetchone()
        return {"joins": row["joins"], "leaves": row["leaves"]}

    async def recent_events(self, limit: int = 50) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT chat_id, username, event_type, event_at FROM events "
            "ORDER BY event_at DESC, id DESC LIMIT %s",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]

    # A member's "last_joined_at" and "leave_count" are derived from the
    # events log rather than stored redundantly on the members row, so
    # rejoin/returning-member detection always reflects true history with no
    # separate migration or risk of the two going out of sync.
    _LAST_JOINED_SQL = (
        "(SELECT MAX(event_at) FROM events e WHERE e.chat_id = m.chat_id AND e.event_type='join')"
    )
    _LEAVE_COUNT_SQL = (
        "(SELECT COUNT(*) FROM events e WHERE e.chat_id = m.chat_id AND e.event_type='leave')"
    )

    # A member who left the channel before this service ever started running
    # has no 'leave' event on record, so leave_count alone can't catch their
    # later rejoin - it looks identical to a brand-new member. The sales API's
    # own time_join (fetched fresh on every join/leave, see bot_handlers.py)
    # is a second, independent signal: if it predates our first_seen_at by
    # more than this buffer, they were already a known sales customer well
    # before we ever saw them join the channel, so this can't be a genuinely
    # new member. The buffer excludes the normal case of someone signing up
    # in the sales bot shortly before joining the channel for the first time.
    SALES_PRE_EXISTING_BUFFER_SECONDS = 2 * 86400

    # Shared by joined_members() (which must exclude these) and
    # returning_members() (which must include them) so the two lists stay
    # each other's complement within the same window - a member never shows
    # up as both "newly joined" and "returning" at once.
    _IS_RETURNING_SQL = (
        "(" + _LEAVE_COUNT_SQL + " > 0 OR (s.time_join IS NOT NULL AND s.time_join < m.first_seen_at - %s))"
    )

    async def joined_members(self, window_start: int, limit: int = 200) -> list[dict]:
        cur = await self.conn.execute(
            f"""
            SELECT m.chat_id, m.username, m.first_seen_at,
                   {self._LAST_JOINED_SQL} AS last_joined_at,
                   {self._LEAVE_COUNT_SQL} AS leave_count,
                   s.limit_usertest, s.count_payment, s.sum_payment, s.found
            FROM members m
            LEFT JOIN sales_snapshot s ON s.chat_id = m.chat_id
            WHERE m.status='member' AND {self._LAST_JOINED_SQL} >= %s
              AND NOT {self._IS_RETURNING_SQL}
            ORDER BY last_joined_at DESC LIMIT %s
            """,
            (window_start, self.SALES_PRE_EXISTING_BUFFER_SECONDS, limit),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def active_members_with_snapshot(self, window_start: int) -> list[dict]:
        cur = await self.conn.execute(
            f"""
            SELECT m.chat_id, s.count_payment, s.sum_payment, s.found
            FROM members m
            LEFT JOIN sales_snapshot s ON s.chat_id = m.chat_id
            WHERE m.status='member' AND {self._LAST_JOINED_SQL} >= %s
            """,
            (window_start,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def returning_members(self, window_start: int, limit: int = 200) -> list[dict]:
        cur = await self.conn.execute(
            f"""
            SELECT m.chat_id, m.username, m.first_seen_at,
                   {self._LAST_JOINED_SQL} AS last_joined_at,
                   {self._LEAVE_COUNT_SQL} AS leave_count,
                   s.time_join AS sales_time_join,
                   s.count_payment AS current_count_payment,
                   (SELECT es.count_payment_snapshot FROM events es
                    WHERE es.chat_id = m.chat_id AND es.event_type='join'
                    ORDER BY es.event_at DESC, es.id DESC LIMIT 1) AS count_payment_before
            FROM members m
            LEFT JOIN sales_snapshot s ON s.chat_id = m.chat_id
            WHERE m.status='member' AND {self._LAST_JOINED_SQL} >= %s
              AND {self._IS_RETURNING_SQL}
            ORDER BY last_joined_at DESC LIMIT %s
            """,
            (window_start, self.SALES_PRE_EXISTING_BUFFER_SECONDS, limit),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def all_active_member_ids(self) -> list[int]:
        cur = await self.conn.execute("SELECT chat_id FROM members WHERE status='member'")
        return [r["chat_id"] for r in await cur.fetchall()]

    # Tehran is UTC+03:30 - a fixed numeric offset, so CONVERT_TZ works with
    # no timezone-table setup on the MySQL server (only named zones like
    # 'Asia/Tehran' would need that).
    @staticmethod
    def _tehran_dt_sql(unix_col: str) -> str:
        return f"CONVERT_TZ(FROM_UNIXTIME({unix_col}), '+00:00', '+03:30')"

    async def daily_purchasing_joins(self, days: int) -> list[dict]:
        # Bucketed from the same join *events* daily_counts() uses (not
        # members.first_seen_at) so this series is always <= that day's joins.
        # A member who was already in the channel before tracking started and
        # later leaves has no join event at all, and must not be counted here.
        window_start = int(time.time()) - days * 86400
        cur = await self.conn.execute(
            f"""
            SELECT DATE({self._tehran_dt_sql('e.event_at')}) AS day,
                   SUM(CASE WHEN s.count_payment > 0 THEN 1 ELSE 0 END) AS purchasing_joins
            FROM events e
            LEFT JOIN sales_snapshot s ON s.chat_id = e.chat_id
            WHERE e.event_type='join' AND e.event_at >= %s
            GROUP BY day
            ORDER BY day ASC
            """,
            (window_start,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def daily_rejoins(self, days: int) -> list[dict]:
        # A join event counts as a rejoin if EITHER that chat_id has an
        # earlier leave event on record (independent of the members table's
        # current mutable state, so this stays accurate through any number of
        # join/leave cycles), OR - for a chat_id's first-ever tracked join,
        # which by definition can't have an earlier leave event in our log -
        # the sales API already knew them well before this join, meaning
        # their real first join predates our tracking (see
        # SALES_PRE_EXISTING_BUFFER_SECONDS / returning_members()).
        window_start = int(time.time()) - days * 86400
        cur = await self.conn.execute(
            f"""
            SELECT DATE({self._tehran_dt_sql('e.event_at')}) AS day,
                   SUM(CASE WHEN
                         EXISTS (
                           SELECT 1 FROM events e2
                           WHERE e2.chat_id = e.chat_id AND e2.event_type='leave' AND e2.event_at < e.event_at
                         )
                         OR (
                           NOT EXISTS (
                             SELECT 1 FROM events e3
                             WHERE e3.chat_id = e.chat_id AND e3.event_type='join' AND e3.event_at < e.event_at
                           )
                           AND s.time_join IS NOT NULL AND s.time_join < e.event_at - %s
                         )
                       THEN 1 ELSE 0 END) AS rejoins
            FROM events e
            LEFT JOIN sales_snapshot s ON s.chat_id = e.chat_id
            WHERE e.event_type='join' AND e.event_at >= %s
            GROUP BY day
            ORDER BY day ASC
            """,
            (self.SALES_PRE_EXISTING_BUFFER_SECONDS, window_start),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def leavers_in_window(self, window_start: int) -> list[dict]:
        # Only members who are still gone as of *now* (status='left') count
        # here - if someone left and has since rejoined, they're no longer a
        # lost customer and must drop out of these stats automatically. Also
        # keep only each chat_id's most recent leave event in the window, so
        # someone who cycled leave/rejoin/leave more than once isn't counted
        # (and their current sum_payment isn't summed) more than once.
        cur = await self.conn.execute(
            """
            SELECT e.chat_id, e.username, e.event_at,
                   s.time_join, s.count_payment, s.sum_payment, s.limit_usertest, s.found
            FROM events e
            JOIN members m ON m.chat_id = e.chat_id AND m.status = 'left'
            LEFT JOIN sales_snapshot s ON s.chat_id = e.chat_id
            WHERE e.event_type='leave' AND e.event_at >= %s
              AND e.event_at = (
                    SELECT MAX(e2.event_at) FROM events e2
                    WHERE e2.chat_id = e.chat_id AND e2.event_type='leave'
                  )
            ORDER BY e.event_at DESC
            """,
            (window_start,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def joiners_in_window(self, window_start: int) -> list[dict]:
        cur = await self.conn.execute(
            f"""
            SELECT e.chat_id, e.username, e.event_at,
                   s.count_payment, s.sum_payment, s.found,
                   COALESCE(tc.tested_invoice_count, 0) AS tested_invoice_count
            FROM events e
            LEFT JOIN sales_snapshot s ON s.chat_id = e.chat_id
            LEFT JOIN {self._TESTED_COUNT_SQL} tc ON tc.chat_id = e.chat_id
            WHERE e.event_type='join' AND e.event_at >= %s
            ORDER BY e.event_at DESC
            """,
            (window_start,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def daily_counts(self, days: int) -> list[dict]:
        window_start = int(time.time()) - days * 86400
        cur = await self.conn.execute(
            f"""
            SELECT DATE({self._tehran_dt_sql('event_at')}) AS day,
                   SUM(CASE WHEN event_type='join' THEN 1 ELSE 0 END) AS joins,
                   SUM(CASE WHEN event_type='leave' THEN 1 ELSE 0 END) AS leaves
            FROM events
            WHERE event_at >= %s
            GROUP BY day
            ORDER BY day ASC
            """,
            (window_start,),
        )
        return [dict(r) for r in await cur.fetchall()]

    # ---- surveys ----

    async def insert_survey(
        self,
        config_name: str,
        rating_speed: int,
        rating_stability: int,
        rating_overall: int,
        comment: str | None,
        screenshot_path: str | None = None,
        at: int | None = None,
    ) -> None:
        at = at or int(time.time())
        await self.conn.execute(
            """
            INSERT INTO surveys
                (config_name, rating_speed, rating_stability, rating_overall, comment, submitted_at, screenshot_path)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (config_name, rating_speed, rating_stability, rating_overall, comment, at, screenshot_path),
        )
        await self.conn.commit()

    async def get_survey_screenshot_path(self, survey_id: int) -> str | None:
        cur = await self.conn.execute("SELECT screenshot_path FROM surveys WHERE id = %s", (survey_id,))
        row = await cur.fetchone()
        return row["screenshot_path"] if row else None

    async def all_surveys(self, limit: int = 1000) -> list[dict]:
        cur = await self.conn.execute(
            """
            SELECT id, config_name, rating_speed, rating_stability, rating_overall, comment, submitted_at, screenshot_path
            FROM surveys ORDER BY submitted_at DESC LIMIT %s
            """,
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]

    # ---- sales (synced from the sales bot's /invoice?actions=invoices API) ----

    # A price of 0 marks free test services ("سرویس تست"), and 'unpaid'
    # marks an order that was never actually paid for - neither is a real
    # sale, so every aggregate query below excludes them the same way.
    _SALE_FILTER_SQL = "price > 0 AND status != 'unpaid'"

    async def upsert_sales_invoices(self, rows: list[dict], at: int | None = None) -> int:
        """rows: dicts with id, chat_id, panel_name, price, status,
        invoice_time. panel_name comes from the invoice's Service_location
        field - NOT name_product, which is a generic "custom service" label
        for ~90% of invoices and carries no useful grouping information.
        Returns how many of these ids were genuinely new, so the sync loop
        can tell when it has caught up to already-known data."""
        if not rows:
            return 0
        at = at or int(time.time())
        ids = [r["id"] for r in rows]
        placeholders = ",".join(["%s"] * len(ids))
        cur = await self.conn.execute(f"SELECT id FROM sales_invoices WHERE id IN ({placeholders})", ids)
        existing = {r["id"] for r in await cur.fetchall()}
        new_count = sum(1 for i in ids if i not in existing)

        await self.conn.executemany(
            """
            INSERT INTO sales_invoices (id, chat_id, panel_name, price, status, invoice_time, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                chat_id=VALUES(chat_id),
                panel_name=VALUES(panel_name),
                price=VALUES(price),
                status=VALUES(status),
                invoice_time=VALUES(invoice_time),
                synced_at=VALUES(synced_at)
            """,
            [
                (r["id"], r["chat_id"], r["panel_name"], r["price"], r["status"], r["invoice_time"], at)
                for r in rows
            ],
        )
        await self.conn.commit()
        return new_count

    async def sales_sync_stats(self) -> dict:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS count, MAX(synced_at) AS last_synced_at FROM sales_invoices"
        )
        return dict(await cur.fetchone())

    @staticmethod
    def _sales_bucket_sql(granularity: str) -> str:
        dt = Database._tehran_dt_sql("invoice_time")
        if granularity == "week":
            # YEARWEEK(..., 3) is ISO-8601-consistent (Monday-first, year part
            # already correctly rolls over at year-boundary weeks) - close
            # enough to SQLite's %Y-W%W for a weekly chart bucket.
            return f"CONCAT(YEARWEEK({dt}, 3) DIV 100, '-W', LPAD(YEARWEEK({dt}, 3) MOD 100, 2, '0'))"
        if granularity == "month":
            # %% escapes the literal % for PyMySQL's own query % params
            # substitution - a bare %Y here would raise "unsupported format
            # character" even though this query has no %s placeholders.
            return f"DATE_FORMAT({dt}, '%%Y-%%m')"
        return f"DATE({dt})"

    async def sales_totals(self, window_start: int) -> dict:
        cur = await self.conn.execute(
            f"""
            SELECT COUNT(*) AS count, COALESCE(SUM(price), 0) AS revenue
            FROM sales_invoices WHERE invoice_time >= %s AND {self._SALE_FILTER_SQL}
            """,
            (window_start,),
        )
        return dict(await cur.fetchone())

    async def sales_series(self, window_start: int, granularity: str) -> list[dict]:
        bucket_sql = self._sales_bucket_sql(granularity)
        cur = await self.conn.execute(
            f"""
            SELECT {bucket_sql} AS bucket, SUM(price) AS revenue, COUNT(*) AS count
            FROM sales_invoices
            WHERE invoice_time >= %s AND {self._SALE_FILTER_SQL}
            GROUP BY bucket ORDER BY bucket ASC
            """,
            (window_start,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def top_panels(self, window_start: int, limit: int = 15) -> list[dict]:
        cur = await self.conn.execute(
            f"""
            SELECT panel_name, COUNT(*) AS count, SUM(price) AS revenue
            FROM sales_invoices
            WHERE invoice_time >= %s AND {self._SALE_FILTER_SQL} AND panel_name IS NOT NULL
            GROUP BY panel_name ORDER BY count DESC LIMIT %s
            """,
            (window_start, limit),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def panel_series(self, window_start: int, granularity: str, panel_names: list[str]) -> list[dict]:
        if not panel_names:
            return []
        bucket_sql = self._sales_bucket_sql(granularity)
        placeholders = ",".join(["%s"] * len(panel_names))
        cur = await self.conn.execute(
            f"""
            SELECT {bucket_sql} AS bucket, panel_name, COUNT(*) AS count, SUM(price) AS revenue
            FROM sales_invoices
            WHERE invoice_time >= %s AND {self._SALE_FILTER_SQL} AND panel_name IN ({placeholders})
            GROUP BY bucket, panel_name ORDER BY bucket ASC
            """,
            (window_start, *panel_names),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def upsert_sales_products(self, rows: list[dict], at: int | None = None) -> None:
        if not rows:
            return
        at = at or int(time.time())
        await self.conn.executemany(
            """
            INSERT INTO sales_products (id, code, name, price, location, category, status, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                code=VALUES(code), name=VALUES(name), price=VALUES(price),
                location=VALUES(location), category=VALUES(category),
                status=VALUES(status), synced_at=VALUES(synced_at)
            """,
            [
                (r["id"], r["code"], r["name"], r["price"], r["location"], r["category"], r["status"], at)
                for r in rows
            ],
        )
        await self.conn.commit()

    async def all_sales_products(self) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT id, code, name, price, location, category, status FROM sales_products ORDER BY name"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def upsert_sales_payments(self, rows: list[dict], at: int | None = None) -> int:
        """rows: dicts with id, chat_id, price, payment_status,
        payment_method, payment_time. Returns how many ids were genuinely
        new, same pattern as upsert_sales_invoices."""
        if not rows:
            return 0
        at = at or int(time.time())
        ids = [r["id"] for r in rows]
        placeholders = ",".join(["%s"] * len(ids))
        cur = await self.conn.execute(f"SELECT id FROM sales_payments WHERE id IN ({placeholders})", ids)
        existing = {r["id"] for r in await cur.fetchall()}
        new_count = sum(1 for i in ids if i not in existing)

        await self.conn.executemany(
            """
            INSERT INTO sales_payments (id, chat_id, price, payment_status, payment_method, payment_time, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                chat_id=VALUES(chat_id),
                price=VALUES(price),
                payment_status=VALUES(payment_status),
                payment_method=VALUES(payment_method),
                payment_time=VALUES(payment_time),
                synced_at=VALUES(synced_at)
            """,
            [
                (r["id"], r["chat_id"], r["price"], r["payment_status"], r["payment_method"], r["payment_time"], at)
                for r in rows
            ],
        )
        await self.conn.commit()
        return new_count

    async def unverified_payments(self, limit: int = 3000) -> list[dict]:
        """Payments genuinely awaiting manual admin confirmation right now -
        payment_status=='waiting'. This is independent of Payment_Method
        (confirmed empirically: a real pending payment showed up with
        Payment_Method=='cart to cart', not the 'paymentnotverify' tag this
        query originally (incorrectly) filtered on). 'paid'/'expire'/'reject'
        are all settled outcomes and excluded since they need no more
        attention."""
        cur = await self.conn.execute(
            """
            SELECT id, chat_id, price, payment_status, payment_method, payment_time
            FROM sales_payments
            WHERE payment_status = 'waiting'
            ORDER BY payment_time DESC LIMIT %s
            """,
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def sales_payments_sync_stats(self) -> dict:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS count, MAX(synced_at) AS last_synced_at FROM sales_payments"
        )
        return dict(await cur.fetchone())

    # The sales bot's own count_payment/sum_payment fields only exist on the
    # single-user profile endpoint, not the paginated user list (confirmed
    # empirically - the list omits them entirely), so it's derived from our
    # own synced caches instead. IMPORTANT: this counts sales_invoices
    # (actual completed purchases), NOT sales_payments.payment_status='paid'
    # - those are two different things here. A user can pay into their
    # wallet many times (each a distinct 'paid' payment/top-up: zarinpal,
    # card-to-card, etc.) and then spend that balance on far fewer actual
    # purchases. Confirmed against a real account where sales_payments had
    # 161 'paid' rows but the sales bot's own profile said "تعداد خرید کل
    # کاربر: 12" - which matched sales_invoices' count (and its price sum)
    # exactly. sales_invoices.status='unpaid'/'Unsuccessful' are excluded
    # since those never actually got paid for (rare - ~15/~19 rows
    # system-wide).
    _PURCHASE_COUNT_SQL = (
        "(SELECT chat_id, COUNT(*) AS purchase_count FROM sales_invoices "
        "WHERE status NOT IN ('unpaid', 'Unsuccessful') GROUP BY chat_id)"
    )
    # "Has taken a test" was originally guessed from limit_usertest dropping
    # below the shop's default allowance - that undercounted badly (~607
    # system-wide vs the sales bot's own reported "اکانت‌های تست: 2045").
    # A free test is provisioned as a real, ordinary invoice with price=0
    # (confirmed empirically: ~2013-2024 distinct chat_ids have a price=0
    # invoice, matching the bot's own count almost exactly - not the
    # limit_usertest field, which apparently doesn't reliably decrement for
    # every provisioning path, e.g. admin-granted tests). 'Unsuccessful' is
    # excluded since that invoice's provisioning never actually completed.
    _TESTED_COUNT_SQL = (
        "(SELECT chat_id, COUNT(*) AS tested_invoice_count FROM sales_invoices "
        "WHERE price = 0 AND status != 'Unsuccessful' GROUP BY chat_id)"
    )
    # "Unpaid invoice" here means a payment attempt that never settled as
    # paid (expired/rejected/still-pending "waiting"/the rare capitalized
    # "Unpaid") - a much bigger, more useful-to-sort-by population than the
    # dedicated sales_invoices.status='unpaid' rows (only ~15 system-wide),
    # since this user-facing feature is about surfacing who tried to pay
    # and didn't finish, not the VPN-service lifecycle status.
    _UNPAID_COUNT_SQL = (
        "(SELECT chat_id, COUNT(*) AS unpaid_count FROM sales_payments "
        "WHERE payment_status != 'paid' GROUP BY chat_id)"
    )
    # Distinct from purchase_count above: this is how many payment
    # transactions (wallet top-ups/gateway payments) actually went through,
    # which can be far higher than the number of real purchases for anyone
    # who funds their wallet in several installments before spending it.
    _DEPOSIT_COUNT_SQL = (
        "(SELECT chat_id, COUNT(*) AS deposit_count FROM sales_payments "
        "WHERE payment_status = 'paid' GROUP BY chat_id)"
    )

    async def upsert_sales_users(self, rows: list[dict], at: int | None = None) -> int:
        """rows: dicts with chat_id, username, time_join, limit_usertest,
        balance, last_message_time. Unlike invoices/payments this isn't an
        incremental sync - the sync loop re-walks the entire roster every
        cycle (only ~22 pages for the whole user base), so this is a plain
        full replace. Still returns how many chat_ids were genuinely new,
        for logging."""
        if not rows:
            return 0
        at = at or int(time.time())
        ids = [r["chat_id"] for r in rows]
        placeholders = ",".join(["%s"] * len(ids))
        cur = await self.conn.execute(f"SELECT chat_id FROM sales_users WHERE chat_id IN ({placeholders})", ids)
        existing = {r["chat_id"] for r in await cur.fetchall()}
        new_count = sum(1 for i in ids if i not in existing)

        await self.conn.executemany(
            """
            INSERT INTO sales_users (chat_id, username, time_join, limit_usertest, balance, last_message_time, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                username=VALUES(username),
                time_join=VALUES(time_join),
                limit_usertest=VALUES(limit_usertest),
                balance=VALUES(balance),
                last_message_time=VALUES(last_message_time),
                synced_at=VALUES(synced_at)
            """,
            [
                (
                    r["chat_id"], r.get("username"), r.get("time_join"), r.get("limit_usertest"),
                    r.get("balance") or 0, r.get("last_message_time"), at,
                )
                for r in rows
            ],
        )
        await self.conn.commit()
        return new_count

    async def sales_users_sync_stats(self) -> dict:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS count, MAX(synced_at) AS last_synced_at FROM sales_users"
        )
        return dict(await cur.fetchone())

    async def sales_users_tested_overall(self) -> dict:
        """Percent of the sales bot's ENTIRE user roster (~22k, independent
        of channel membership) that has redeemed at least one free test -
        no join-age or time-window scoping."""
        cur = await self.conn.execute(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN COALESCE(tc.tested_invoice_count, 0) >= 1 THEN 1 ELSE 0 END) AS tested_count
            FROM sales_users su
            LEFT JOIN {self._TESTED_COUNT_SQL} tc ON tc.chat_id = su.chat_id
            """
        )
        row = dict(await cur.fetchone())
        row["tested_count"] = row["tested_count"] or 0
        return row

    async def has_tested_invoice(self, chat_id: int) -> bool:
        """Whether this chat_id has ever had a free (price=0) test invoice
        provisioned - the ground-truth signal for "took a test", confirmed
        against the sales bot's own reported test-account count. Used by
        the leave-survey messaging tier (bot_handlers.py)."""
        cur = await self.conn.execute(
            "SELECT 1 FROM sales_invoices WHERE chat_id = %s AND price = 0 AND status != 'Unsuccessful' LIMIT 1",
            (chat_id,),
        )
        return (await cur.fetchone()) is not None

    async def sales_users_stats(self) -> dict:
        now = int(time.time())
        d30 = now - 30 * 86400
        d60 = now - 60 * 86400
        cur = await self.conn.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN su.time_join IS NOT NULL AND su.time_join <= %s
                    AND COALESCE(pp.purchase_count, 0) = 0 AND COALESCE(dp.deposit_count, 0) = 0
                    THEN 1 ELSE 0 END) AS no_purchase_30,
                SUM(CASE WHEN su.time_join IS NOT NULL AND su.time_join <= %s
                    AND COALESCE(pp.purchase_count, 0) = 0 AND COALESCE(dp.deposit_count, 0) = 0
                    THEN 1 ELSE 0 END) AS no_purchase_60,
                SUM(CASE WHEN su.time_join IS NOT NULL AND su.time_join <= %s AND COALESCE(tc.tested_invoice_count, 0) >= 1
                    THEN 1 ELSE 0 END) AS tested_30
            FROM sales_users su
            LEFT JOIN {self._PURCHASE_COUNT_SQL} pp ON pp.chat_id = su.chat_id
            LEFT JOIN {self._DEPOSIT_COUNT_SQL} dp ON dp.chat_id = su.chat_id
            LEFT JOIN {self._TESTED_COUNT_SQL} tc ON tc.chat_id = su.chat_id
            """,
            (d30, d60, d30),
        )
        row = dict(await cur.fetchone())
        for key in ("total", "no_purchase_30", "no_purchase_60", "tested_30"):
            row[key] = row[key] or 0
        return row

    # Whitelisted so sort_by (which arrives straight from a query param) can
    # never be interpolated into raw SQL - anything not in this map is
    # ignored rather than passed through.
    _SORTABLE_COLUMNS = {
        "chat_id": "su.chat_id",
        "username": "su.username",
        "purchase_count": "purchase_count",
        "unpaid_count": "unpaid_count",
        "deposit_count": "deposit_count",
        "time_join": "su.time_join",
        "last_message_time": "su.last_message_time",
        "limit_usertest": "su.limit_usertest",
    }

    async def filtered_sales_users(
        self,
        filter_key: str | None,
        limit: int,
        offset: int,
        q: str | None = None,
        sort_by: str | None = None,
        sort_dir: str = "desc",
    ) -> tuple[list[dict], int]:
        now = int(time.time())
        where = []
        params: list = []
        if filter_key == "no_purchase_30":
            where.append(
                "su.time_join IS NOT NULL AND su.time_join <= %s "
                "AND COALESCE(pp.purchase_count, 0) = 0 AND COALESCE(dp.deposit_count, 0) = 0"
            )
            params.append(now - 30 * 86400)
        elif filter_key == "no_purchase_60":
            where.append(
                "su.time_join IS NOT NULL AND su.time_join <= %s "
                "AND COALESCE(pp.purchase_count, 0) = 0 AND COALESCE(dp.deposit_count, 0) = 0"
            )
            params.append(now - 60 * 86400)
        elif filter_key == "tested_30":
            where.append("su.time_join IS NOT NULL AND su.time_join <= %s AND COALESCE(tc.tested_invoice_count, 0) >= 1")
            params.append(now - 30 * 86400)
        if q:
            where.append("(su.username LIKE %s OR CAST(su.chat_id AS CHAR) LIKE %s)")
            like = f"%{q}%"
            params.extend([like, like])
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        count_cur = await self.conn.execute(
            f"""
            SELECT COUNT(*) AS c
            FROM sales_users su
            LEFT JOIN {self._PURCHASE_COUNT_SQL} pp ON pp.chat_id = su.chat_id
            LEFT JOIN {self._DEPOSIT_COUNT_SQL} dp ON dp.chat_id = su.chat_id
            LEFT JOIN {self._TESTED_COUNT_SQL} tc ON tc.chat_id = su.chat_id
            {where_sql}
            """,
            params,
        )
        total = (await count_cur.fetchone())["c"]

        if sort_by in self._SORTABLE_COLUMNS:
            direction = "ASC" if sort_dir == "asc" else "DESC"
            order_sql = f"{self._SORTABLE_COLUMNS[sort_by]} {direction}"
            if sort_by != "chat_id":
                order_sql += ", su.chat_id ASC"
        else:
            # A specific filter is a "who needs attention" list, so it's
            # sorted by default to surface the people with the most
            # failed/abandoned payment attempts first. "همه" (no filter) is
            # just the raw roster browse, where that sort would be
            # meaningless noise - kept on join time. An explicit column
            # click (above) always overrides this default.
            order_sql = "su.time_join DESC"
            if filter_key in ("no_purchase_30", "no_purchase_60", "tested_30"):
                order_sql = "unpaid_count DESC, su.time_join DESC"

        cur = await self.conn.execute(
            f"""
            SELECT su.chat_id, su.username, su.time_join, su.limit_usertest, su.balance,
                   su.last_message_time,
                   COALESCE(pp.purchase_count, 0) AS purchase_count,
                   COALESCE(up.unpaid_count, 0) AS unpaid_count,
                   COALESCE(dp.deposit_count, 0) AS deposit_count
            FROM sales_users su
            LEFT JOIN {self._PURCHASE_COUNT_SQL} pp ON pp.chat_id = su.chat_id
            LEFT JOIN {self._UNPAID_COUNT_SQL} up ON up.chat_id = su.chat_id
            LEFT JOIN {self._DEPOSIT_COUNT_SQL} dp ON dp.chat_id = su.chat_id
            LEFT JOIN {self._TESTED_COUNT_SQL} tc ON tc.chat_id = su.chat_id
            {where_sql}
            ORDER BY {order_sql}
            LIMIT %s OFFSET %s
            """,
            [*params, limit, offset],
        )
        rows = [dict(r) for r in await cur.fetchall()]
        return rows, total

    async def matching_sales_users(
        self,
        purchase_filter: str,
        deposit_filter: str,
        min_unpaid: int | None,
        min_join_age_days: int | None,
    ) -> list[dict]:
        """Backs both the bulk-block preview and the actual bulk-block run -
        same query, so what the admin previews is exactly who gets hit.
        purchase_filter/deposit_filter: "any" | "no_purchase"/"no_deposit" |
        "has_purchase"/"has_deposit"."""
        where = []
        params: list = []
        if purchase_filter == "no_purchase":
            where.append("COALESCE(pp.purchase_count, 0) = 0")
        elif purchase_filter == "has_purchase":
            where.append("COALESCE(pp.purchase_count, 0) >= 1")
        if deposit_filter == "no_deposit":
            where.append("COALESCE(dp.deposit_count, 0) = 0")
        elif deposit_filter == "has_deposit":
            where.append("COALESCE(dp.deposit_count, 0) >= 1")
        if min_unpaid:
            where.append("COALESCE(up.unpaid_count, 0) >= %s")
            params.append(min_unpaid)
        if min_join_age_days:
            where.append("su.time_join IS NOT NULL AND su.time_join <= %s")
            params.append(int(time.time()) - min_join_age_days * 86400)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        cur = await self.conn.execute(
            f"""
            SELECT su.chat_id, su.username, su.time_join,
                   COALESCE(pp.purchase_count, 0) AS purchase_count,
                   COALESCE(up.unpaid_count, 0) AS unpaid_count,
                   COALESCE(dp.deposit_count, 0) AS deposit_count
            FROM sales_users su
            LEFT JOIN {self._PURCHASE_COUNT_SQL} pp ON pp.chat_id = su.chat_id
            LEFT JOIN {self._UNPAID_COUNT_SQL} up ON up.chat_id = su.chat_id
            LEFT JOIN {self._DEPOSIT_COUNT_SQL} dp ON dp.chat_id = su.chat_id
            {where_sql}
            ORDER BY unpaid_count DESC, su.time_join DESC
            """,
            params,
        )
        return [dict(r) for r in await cur.fetchall()]

    async def payments_for_chat_id(self, chat_id: int) -> dict:
        cur = await self.conn.execute(
            """
            SELECT id, price, payment_status, payment_method, payment_time
            FROM sales_payments WHERE chat_id = %s
            ORDER BY payment_time DESC
            """,
            (chat_id,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        summary = {"paid": 0, "expire": 0, "reject": 0, "waiting": 0, "other": 0}
        for r in rows:
            status = (r["payment_status"] or "").strip().lower()
            if status in summary:
                summary[status] += 1
            else:
                summary["other"] += 1
        return {"payments": rows, "summary": summary}

    # ---- nodes ----

    async def insert_node(
        self,
        label: str,
        host: str,
        ssh_port: int,
        ssh_user: str,
        private_key_path: str,
        api_token: str,
        api_port: int,
    ) -> int:
        cur = await self.conn.execute(
            """
            INSERT INTO nodes
                (label, host, ssh_port, ssh_user, private_key_path, api_token, api_port, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'installing', %s)
            """,
            (label, host, ssh_port, ssh_user, private_key_path, api_token, api_port, int(time.time())),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def list_nodes(self) -> list[dict]:
        cur = await self.conn.execute("SELECT * FROM nodes ORDER BY created_at ASC")
        return [dict(r) for r in await cur.fetchall()]

    async def active_nodes(self) -> list[dict]:
        cur = await self.conn.execute("SELECT * FROM nodes WHERE status IN ('online', 'offline')")
        return [dict(r) for r in await cur.fetchall()]

    async def get_node(self, node_id: int) -> dict | None:
        cur = await self.conn.execute("SELECT * FROM nodes WHERE id = %s", (node_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def set_node_credentials(
        self, node_id: int, private_key_path: str, api_token: str, api_port: int
    ) -> None:
        await self.conn.execute(
            "UPDATE nodes SET private_key_path=%s, api_token=%s, api_port=%s WHERE id=%s",
            (private_key_path, api_token, api_port, node_id),
        )
        await self.conn.commit()

    async def update_node_status(
        self,
        node_id: int,
        status: str,
        last_error: str | None = None,
        installed_version: str | None = None,
    ) -> None:
        await self.conn.execute(
            """
            UPDATE nodes SET status=%s, last_error=%s, last_checked_at=%s,
                   installed_version=COALESCE(%s, installed_version)
            WHERE id=%s
            """,
            (status, last_error, int(time.time()), installed_version, node_id),
        )
        await self.conn.commit()

    async def delete_node(self, node_id: int) -> None:
        await self.conn.execute("DELETE FROM nodes WHERE id = %s", (node_id,))
        await self.conn.commit()
