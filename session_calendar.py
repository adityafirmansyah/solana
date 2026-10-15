#!/usr/bin/env python3
"""Satoshi's session calendar: BTCUSDT trades 24/7/365. No open/close, no
daily break, no holidays, no DST anchor.

Nugget's session_calendar.py exists because spot gold has a real weekly
close + daily maintenance break, and a 24/7 crypto PROXY feed would happily
return a real-looking price while the underlying market was shut. Bitcoin
has no such problem: the exchange that prices it never closes. So this
module is a trivial shim that exposes the SAME function surface the rest of
the engine (cycle.py, watchdog.py, daily.py, improve.py, web_dashboard.py)
already calls through `cal.*` — every hour is open, every slot is expected,
there is no "gap" state to produce. This keeps those modules UNCHANGED;
only this file differs between the two engines.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
WIB = ZoneInfo("Asia/Jakarta")


def session_state(dt: datetime) -> str:
    """Always 'open' — BTCUSDT has no calendar gate."""
    if dt.tzinfo is None:
        raise ValueError("session_state requires a timezone-aware datetime")
    return "open"


def is_open(dt: datetime) -> bool:
    return True


def open_instant(d) -> datetime:
    """No concept of a weekly open; returns midnight UTC of the given date
    for callers that ask "when does the next session start" (daily.py's
    next_open fallback). BTC has no such boundary, so this is a harmless
    placeholder that is never actually reached (is_open() is always True,
    so callers never need to search forward for an opening instant)."""
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def expected_hourly_slots(start_utc: datetime, end_utc: datetime) -> list[datetime]:
    """Every top-of-hour slot in [start, end) — all of them, since BTC never
    closes. This is what the watchdog's slot-gap detector diffs against."""
    out = []
    cur = start_utc.replace(minute=0, second=0, microsecond=0)
    if cur < start_utc:
        cur += timedelta(hours=1)
    while cur < end_utc:
        out.append(cur)
        cur += timedelta(hours=1)
    return out


def holidays(year: int) -> set:
    return set()


def describe(dt: datetime) -> str:
    return (f"{dt.astimezone(UTC):%Y-%m-%d %H:%M} UTC / "
            f"{dt.astimezone(WIB):%Y-%m-%d %H:%M} WIB  ->  open (24/7 market)")


if __name__ == "__main__":
    print("=== BTCUSDT session calendar self-test (trivial 24/7) ===\n")
    now = datetime.now(UTC)
    print("now:", describe(now))
    assert is_open(now)
    assert is_open(now.replace(hour=0)) and is_open(now.replace(hour=23))
    slots = expected_hourly_slots(now, now + timedelta(days=7))
    assert len(slots) == 7 * 24, f"expected 168 slots/week, got {len(slots)}"
    print(f"7-day window -> {len(slots)} open hourly slots (100% of 168, as expected)")
    print("holidays(2026):", holidays(2026), "(always empty)")
    print("\nALL CALENDAR CHECKS DONE")
