from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import jdatetime

jdatetime.set_locale(jdatetime.FA_LOCALE)


def fmt_amount(value: int | float | None) -> str:
    if value is None:
        return "-"
    return f"{int(value):,}"


def fmt_test_limit(limit_usertest: int | None) -> str:
    if limit_usertest is None:
        return "-"
    if limit_usertest < 0:
        return "نامحدود"
    return fmt_amount(limit_usertest)


def fmt_datetime(ts: int | None, tz: str = "Asia/Tehran") -> str:
    if not ts:
        return "-"
    dt = datetime.fromtimestamp(ts, tz=ZoneInfo(tz))
    jdt = jdatetime.datetime.fromgregorian(datetime=dt)
    return jdt.strftime("%d %B %Y - %H:%M")
