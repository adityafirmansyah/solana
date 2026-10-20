#!/usr/bin/env python3
"""Improvement Session Readiness Query for Solana Web Dashboard.

Provides live evaluation of training and held-out rows against promotion gates,
accrual rates across market-open hours, and model registry incumbent status.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "solana.db"

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))

from dashboard_stats import get_db


def get_improvement() -> dict:
    """Improvement-session readiness: exactly what job #4's gate will see."""
    import improve
    import ledger
    import session_calendar as cal

    now = datetime.now(UTC)
    con = get_db()
    rows = improve.load_graded(con)
    train, held = improve.split(rows)
    n = len(rows)
    ready = (len(train) >= improve.MIN_TRAIN_ROWS and len(held) >= improve.MIN_HELD_OUT_ROWS)
    unlocks = lambda k: (int(k * 0.7) >= improve.MIN_TRAIN_ROWS and k - int(k * 0.7) >= improve.MIN_HELD_OUT_ROWS)
    need_n = next(k for k in range(n, n + 500) if unlocks(k))
    n_train_ok = next(k for k in range(n, n + 500) if int(k * 0.7) >= improve.MIN_TRAIN_ROWS)
    n_held_ok = next(k for k in range(n, n + 500) if k - int(k * 0.7) >= improve.MIN_HELD_OUT_ROWS)
    binding = "held_out" if n_held_ok > n_train_ok else ("train" if n_train_ok > n_held_ok else "both")
    rows_needed = max(0, need_n - n)

    span = con.execute("""SELECT MIN(o.graded_utc) a, MAX(o.graded_utc) b
        FROM outcomes o JOIN predictions p ON p.id = o.prediction_id
        WHERE o.is_noise = 0""").fetchone()
    per_open_hour, open_hours, since_wib = 0.0, 0.0, None
    if span["a"] and span["b"] and n > 1:
        a = datetime.fromisoformat(span["a"])
        if a.tzinfo is None:
            a = a.replace(tzinfo=UTC)
        b = max(now, datetime.fromisoformat(span["b"]).replace(tzinfo=UTC))
        open_hours = len(cal.expected_hourly_slots(a, b))
        if open_hours > 0:
            per_open_hour = (n - 1) / open_hours
        since_wib = a.astimezone(WIB).strftime("%a %d %b %H:%M")
    open_hours_to_gate = (rows_needed / per_open_hour) if (rows_needed and per_open_hour) else 0.0

    def next_sessions(after, count):
        d = after.astimezone(WIB).replace(minute=0, second=0, microsecond=0)
        out = []
        while len(out) < count:
            d += timedelta(hours=1)
            if d.weekday() < 5 and d.hour == 21:
                out.append(d)
        return out

    first_eligible_wib = None
    if not ready and per_open_hour:
        for s in next_sessions(now, 12):
            open_h = len(cal.expected_hourly_slots(now, s.astimezone(UTC)))
            if n + per_open_hour * open_h >= need_n:
                first_eligible_wib = s.strftime("%a %Y-%m-%d %H:%M WIB")
                break

    next_run = next_sessions(now, 1)[0]
    inc = ledger.get_incumbent(con)
    incumbent = inc["version"] if inc else "v0.1-default (built-in weights)"

    now_iso = now.isoformat(timespec="seconds")
    backlog = [{
        "id": r["id"],
        "target_bar_utc": (r["target_bar_utc"] or "")[:16],
        "direction": r["direction"],
        "note": ("past target - grades on the next open-market hourly cycle"
                 if (r["target_bar_utc"] or "") <= now_iso
                 else "target not reached yet"),
    } for r in con.execute("""SELECT p.id, p.target_bar_utc, p.direction
        FROM predictions p WHERE NOT EXISTS
        (SELECT 1 FROM outcomes o WHERE o.prediction_id = p.id) ORDER BY p.id""")]

    recent = []
    for r in con.execute("""SELECT scheduled_slot_utc, status, detail FROM cron_runs
        WHERE job='improve' ORDER BY scheduled_slot_utc DESC LIMIT 3"""):
        try:
            det = json.loads(r["detail"] or "{}")
        except ValueError:
            det = {}
        recent.append({
            "slot_wib": datetime.fromisoformat(r["scheduled_slot_utc"]).astimezone(WIB).strftime("%a %d %b %H:%M"),
            "status": r["status"], "train": det.get("train"), "held": det.get("held_out")
        })

    chain = ledger.verify_chain(con)
    exc = con.execute("""SELECT
        SUM(CASE WHEN o.is_noise=1 THEN 1 ELSE 0 END) noise,
        SUM(CASE WHEN o.is_noise=0 AND o.correct IS NULL THEN 1 ELSE 0 END) no_call
        FROM outcomes o""").fetchone()
    con.close()

    return {
        "ready": ready, "n": n, "need_n": need_n, "rows_needed": rows_needed,
        "excluded": {"noise": exc["noise"] or 0, "no_call": exc["no_call"] or 0},
        "train": {"have": len(train), "need": improve.MIN_TRAIN_ROWS},
        "held": {"have": len(held), "need": improve.MIN_HELD_OUT_ROWS},
        "binding": binding,
        "sources": {"technical": sum(1 for r in rows if r["signal_source"] == "technical"),
                    "sentiment": sum(1 for r in rows if r["signal_source"] == "sentiment")},
        "accrual": {"qualifying_per_open_hour": round(per_open_hour, 4),
                    "open_hours_measured": round(open_hours, 1),
                    "measured_since_wib": since_wib,
                    "open_hours_to_gate": round(open_hours_to_gate, 1) if rows_needed else 0.0},
        "next_run_wib": next_run.strftime("%a %Y-%m-%d %H:%M WIB"),
        "first_eligible_wib": first_eligible_wib,
        "incumbent": incumbent,
        "recent": recent,
        "pending_backlog": backlog,
        "chain": {"ok": chain.get("ok"), "head": (chain.get("head") or "")[:12]},
        "generated_wib": now.astimezone(WIB).strftime("%Y-%m-%d %H:%M:%S WIB"),
    }
