from __future__ import annotations

import time

from . import forecasting
from .db import Database

TEHRAN_OFFSET_SECONDS = 3 * 3600 + 30 * 60


def _sales_window_start(window_days: int) -> int:
    now = int(time.time())
    if window_days == 1:
        # "امروز" (today) must mean the calendar day in Tehran time, matching
        # how the sales bot's own daily report resets at local midnight -
        # not a rolling 24h window, which would silently include hours from
        # yesterday and exclude hours already elapsed today.
        local = now + TEHRAN_OFFSET_SECONDS
        local_midnight = local - (local % 86400)
        return local_midnight - TEHRAN_OFFSET_SECONDS
    return now - window_days * 86400


def _avg(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    if not values:
        return None
    return sum(values) / len(values)



async def leaver_stats(db: Database, window_days: int, recent_limit: int = 30) -> dict:
    window_start = int(time.time()) - window_days * 86400
    rows = await db.leavers_in_window(window_start)

    time_joins = [r["time_join"] for r in rows if r["found"] and r["time_join"]]
    payment_counts = [r["count_payment"] or 0 for r in rows if r["found"]]
    payment_sums = [r["sum_payment"] or 0 for r in rows if r["found"]]

    return {
        "window_days": window_days,
        "count": len(rows),
        "avg_time_join": _avg(time_joins),
        "avg_count_payment": _avg(payment_counts),
        "avg_sum_payment": _avg(payment_sums),
        "total_sum_payment": sum(payment_sums) if payment_sums else 0,
        "recent": [
            {
                "chat_id": r["chat_id"],
                "username": r["username"],
                "event_at": r["event_at"],
                "time_join": r["time_join"],
                "count_payment": r["count_payment"] or 0,
                "sum_payment": r["sum_payment"] or 0,
                "limit_usertest": r["limit_usertest"],
                "found": bool(r["found"]),
            }
            for r in rows[:recent_limit]
        ],
    }


async def joiner_stats(db: Database, window_days: int, zero_limit: int = 200) -> dict:
    window_start = int(time.time()) - window_days * 86400
    rows = await db.joiners_in_window(window_start)

    total = len(rows)
    purchasers = [r for r in rows if (r["count_payment"] or 0) > 0]
    zero_purchase = [r for r in rows if (r["count_payment"] or 0) == 0]
    tested = [r for r in rows if (r["tested_invoice_count"] or 0) >= 1]

    purchaser_sums = [r["sum_payment"] or 0 for r in purchasers]

    return {
        "window_days": window_days,
        "count": total,
        "purchasers_count": len(purchasers),
        "purchase_rate_pct": (len(purchasers) / total * 100) if total else 0.0,
        "avg_sum_payment_purchasers": _avg(purchaser_sums),
        "total_sum_payment_purchasers": sum(purchaser_sums) if purchaser_sums else 0,
        "tested_count": len(tested),
        "tested_rate_pct": (len(tested) / total * 100) if total else 0.0,
        "zero_purchase": [
            {
                "chat_id": r["chat_id"],
                "username": r["username"],
                "event_at": r["event_at"],
            }
            for r in zero_purchase[:zero_limit]
        ],
    }


RATING_LABELS = {1: "خیلی ضعیف", 2: "ضعیف", 3: "متوسط", 4: "خوب", 5: "عالی"}
RATING_CRITERIA = ("rating_speed", "rating_stability", "rating_overall")


async def survey_overview(db: Database, limit: int = 1000) -> dict:
    rows = await db.all_surveys(limit)
    total = len(rows)

    distribution = {crit: {level: 0 for level in range(1, 6)} for crit in RATING_CRITERIA}
    items = []
    for r in rows:
        for crit in RATING_CRITERIA:
            distribution[crit][r[crit]] += 1
        avg_score = (r["rating_speed"] + r["rating_stability"] + r["rating_overall"]) / 3
        items.append(
            {
                "id": r["id"],
                "config_name": r["config_name"],
                "rating_speed": r["rating_speed"],
                "rating_stability": r["rating_stability"],
                "rating_overall": r["rating_overall"],
                "avg_score": avg_score,
                "dominant_label": RATING_LABELS[round(avg_score)],
                "comment": r["comment"],
                "submitted_at": r["submitted_at"],
                "has_screenshot": bool(r["screenshot_path"]),
            }
        )

    def crit_avg(crit: str) -> float:
        vals = [r[crit] for r in rows]
        return sum(vals) / len(vals) if vals else 0.0

    satisfied = [i for i in items if i["avg_score"] >= 4]

    return {
        "total": total,
        "avg_speed": crit_avg("rating_speed"),
        "avg_stability": crit_avg("rating_stability"),
        "avg_overall": crit_avg("rating_overall"),
        "satisfied_pct": (len(satisfied) / total * 100) if total else 0.0,
        "distribution": distribution,
        "items": items,
    }


async def overview(db: Database, window_days: int) -> dict:
    totals = await db.totals()
    daily = await db.daily_counts(window_days)
    purchasing = {r["day"]: r["purchasing_joins"] for r in await db.daily_purchasing_joins(window_days)}
    rejoins = {r["day"]: r["rejoins"] for r in await db.daily_rejoins(window_days)}
    for row in daily:
        row["purchasing_joins"] = purchasing.get(row["day"], 0)
        row["rejoins"] = rejoins.get(row["day"], 0)

    return {
        "total_joins": totals["joins"],
        "total_leaves": totals["leaves"],
        "net": totals["joins"] - totals["leaves"],
        "daily": daily,
    }


async def sales_users_tested_summary(db: Database) -> dict:
    """Percent of the sales bot's ENTIRE user roster (~22k, independent of
    channel membership - not "کل اعضای کانال") that has redeemed at least
    one free test. Explicitly NOT scoped to channel members: the channel's
    active membership is tiny next to the sales bot's full customer base,
    which is what this box is meant to represent."""
    row = await db.sales_users_tested_overall()
    total = row["total"] or 0
    tested = row["tested_count"] or 0
    return {
        "total": total,
        "tested_count": tested,
        "tested_rate_pct": (tested / total * 100) if total else 0.0,
    }


async def joined_members_summary(db: Database, window_start: int) -> dict:
    rows = await db.active_members_with_snapshot(window_start)
    total = len(rows)
    purchasers = [r for r in rows if (r["count_payment"] or 0) > 0]
    purchaser_sums = [r["sum_payment"] or 0 for r in purchasers]

    return {
        "total": total,
        "purchasers_count": len(purchasers),
        "purchase_rate_pct": (len(purchasers) / total * 100) if total else 0.0,
        "avg_sum_payment": _avg(purchaser_sums),
    }


async def returning_members_summary(db: Database, window_start: int, recent_limit: int = 100) -> dict:
    all_active = await db.active_members_with_snapshot(window_start)
    returning = await db.returning_members(window_start, limit=recent_limit)

    total_joined = len(all_active)

    def _purchase_split(r: dict) -> dict:
        before = r["count_payment_before"]
        current = r["current_count_payment"] or 0
        # before is NULL for rejoins that happened before this snapshot was
        # introduced - there's no way to retroactively know their split, so
        # fall back to just the current total with no "after" figure.
        after = max(current - before, 0) if before is not None else None
        return {"count_payment_before": before, "count_payment_after": after, "current_count_payment": current}

    members = [
        {
            "chat_id": r["chat_id"],
            "username": r["username"],
            "last_joined_at": r["last_joined_at"],
            "leave_count": r["leave_count"],
            "sales_time_join": r["sales_time_join"],
            **_purchase_split(r),
        }
        for r in returning
    ]

    # Only members whose before/after split is actually known (i.e. they
    # rejoined after the purchase-snapshot feature shipped) can count toward
    # this - a legacy returning member with no "before" figure can't be
    # judged as having bought again or not, so they're excluded from both
    # sides of this rate rather than silently counted as "no".
    measurable = [m for m in members if m["count_payment_before"] is not None]
    purchased_again = [m for m in measurable if (m["count_payment_after"] or 0) > 0]

    return {
        "count": len(returning),
        "total_joined": total_joined,
        "rate_pct": (len(returning) / total_joined * 100) if total_joined else 0.0,
        "purchased_again_count": len(purchased_again),
        "purchased_again_measurable": len(measurable),
        "purchased_again_rate_pct": (len(purchased_again) / len(measurable) * 100) if measurable else None,
        "members": members,
    }


async def sales_overview(db: Database, window_days: int, granularity: str, top_n: int = 8) -> dict:
    window_start = _sales_window_start(window_days)
    totals = await db.sales_totals(window_start)
    series = await db.sales_series(window_start, granularity)
    top_panels = await db.top_panels(window_start, limit=top_n)
    panel_names = [p["panel_name"] for p in top_panels]
    panel_series_rows = await db.panel_series(window_start, granularity, panel_names)
    sync_stats = await db.sales_sync_stats()
    # Independent of the window_days/granularity the user picked on the page
    # (forecast is always "next 30 days from today"), so it's fit on its own
    # fixed recent training window - see forecasting.py for why.
    forecast_30d = await forecasting.forecast_next_30_days(db)

    by_panel: dict[str, list[dict]] = {name: [] for name in panel_names}
    for row in panel_series_rows:
        by_panel.setdefault(row["panel_name"], []).append(
            {"bucket": row["bucket"], "count": row["count"], "revenue": row["revenue"]}
        )

    return {
        "window_days": window_days,
        "window_start": window_start,
        "granularity": granularity,
        "total_count": totals["count"],
        "total_revenue": totals["revenue"],
        "avg_price": (totals["revenue"] / totals["count"]) if totals["count"] else 0,
        "series": series,
        "top_panels": top_panels,
        "panel_series": by_panel,
        "synced_invoices": sync_stats["count"],
        "last_synced_at": sync_stats["last_synced_at"],
        "forecast_30d": forecast_30d,
    }
