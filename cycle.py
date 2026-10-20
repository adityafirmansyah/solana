#!/usr/bin/env python3
"""Solana's hourly cycle: the body of cronjob #2 (SOLUSDT).

Ported from Nugget's cycle.py. Structurally identical — the only real
difference is step 1: SOL's session_calendar.is_open() is always True (see
that module's docstring), so the gap branch below is dead code in practice
but kept so cycle.py stays byte-for-byte comparable to the gold engine and
survives if a future calendar nuance (exchange maintenance windows, etc.)
is ever modeled.

Order of operations, and why it is this order:

  1. calendar gate   - if the market is closed, record a GAP and stop. No fetch,
                       no prediction, no grading. A closed market is not an
                       error and must never enter the promotion gate.
  2. claim the slot  - idempotency. A duplicate or retried fire is rejected by
                       UNIQUE(job, scheduled_slot_utc) before any work happens.
  3. grade first     - score the OLDEST ungraded predictions whose target bar has
                       closed. Deliberately does not assume hour N-1 ran: a
                       missed tick simply means nothing is graded this cycle.
  4. predict         - make the call for the NEXT bar, and write it down before
                       that bar exists.
  5. finish the slot - status + detail, so the gap watchdog can diff expected
                       slots against actual rows.

Grading is threshold-aware: a move smaller than the stored noise_threshold
(0.5 x ATR14 at forecast time) is NOISE and is excluded from the hit-rate, but
counted separately. "no_call" is a legitimate recorded outcome, not a failure.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_market as fm          # noqa: E402
import ledger                      # noqa: E402
import predictor                   # noqa: E402
import session_calendar as cal     # noqa: E402
import stats                       # noqa: E402

from cycle_grading import (
    BASE_RATE_WINDOW,
    base_rate_from,
    get_active_technical,
    evaluate_barrier_trade,
    grade_due,
)
from cycle_report import (
    format_hourly_delivery,
    format_cycle_delivery,
    report,
)

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))
JOB = "hourly_predict"


def current_slot(now: datetime | None = None, horizon: str = "1h") -> datetime:
    """The top slot this run belongs to."""
    now = now or datetime.now(UTC)
    if horizon == "event":
        return now.replace(microsecond=0)
    elif horizon == "5m":
        slot_min = (now.minute // 5) * 5
        return now.replace(minute=slot_min, second=0, microsecond=0)
    elif horizon == "15m":
        slot_min = (now.minute // 15) * 15
        return now.replace(minute=slot_min, second=0, microsecond=0)
    elif horizon == "8h":
        slot_hour = (now.hour // 8) * 8
        return now.replace(hour=slot_hour, minute=0, second=0, microsecond=0)
    elif horizon == "12h":
        slot_hour = (now.hour // 12) * 12
        return now.replace(hour=slot_hour, minute=0, second=0, microsecond=0)
    return now.replace(minute=0, second=0, microsecond=0)


def run(now: datetime | None = None, *, dry_run: bool = False, horizon: str = "1h") -> dict:
    now = now or datetime.now(UTC)
    slot = current_slot(now, horizon=horizon)
    slot_iso = slot.isoformat(timespec="seconds")
    job_name = "hourly_predict" if horizon == "1h" else f"{horizon}_predict"
    out: dict[str, object] = {
        "horizon": horizon,
        "slot_utc": slot_iso,
        "slot_wib": slot.astimezone(WIB).isoformat(timespec="seconds")
    }

    # ---- 1. calendar gate -------------------------------------------------
    state = cal.session_state(now)
    out["session_state"] = state
    if state != "open":
        if not dry_run:
            con = ledger.connect()
            ledger.record_gap(con, slot, state)
        out["action"] = "gap"
        out["reason"] = state
        return out

    # ---- 2. claim the slot ------------------------------------------------
    con = ledger.connect()
    if not dry_run and not ledger.claim_slot(con, job_name, slot_iso):
        out["action"] = "duplicate_slot_rejected"
        return out

    try:
        # ---- 3. fetch -----------------------------------------------------
        try:
            snap = fm.collect(interval=horizon)
        except (fm.FetchError, fm.SchemaError) as e:
            if not dry_run:
                ledger.record_fetch_error(con, f"collect_{horizon}", f"{type(e).__name__}: {e}")
                ledger.finish_slot(con, job_name, slot_iso, "failed", str(e)[:300])
            out["action"] = "fetch_failed"
            out["error"] = f"{type(e).__name__}: {e}"
            return out

        for src, st in snap["source_status"].items():
            if st != "ok" and not dry_run:
                ledger.record_fetch_error(con, src, st)

        candles = snap["candles"]

        # ---- 4. grade what is due ----------------------------------------
        graded = grade_due(con, candles, now, horizon=horizon) if not dry_run else []
        out["graded"] = graded

        # ---- 5. evaluate active technical trade (TP / SL / Time-stop) ----
        if horizon in ("1h", "15m", "5m", "event"):
            active = get_active_technical(con, horizon=horizon)
            if active:
                ref_spot = snap["spot"]["price"]
                barrier_eval = evaluate_barrier_trade(active, candles, ref_spot, now)

                if barrier_eval["status"] == "holding":
                    entry_ref = barrier_eval["ref_price"]
                    curr = barrier_eval["current_price"]
                    if barrier_eval["direction"] == "bullish":
                        pnl = curr - entry_ref
                    else:
                        pnl = entry_ref - curr
                    pnl_pct = (pnl / entry_ref * 100) if entry_ref else 0.0

                    bars_unit = "bars" if horizon in ("15m", "5m", "event") else "h"
                    if not dry_run:
                        ledger.finish_slot(con, job_name, slot_iso, "ok",
                                           f"holding #{active['id']} ({barrier_eval['bars_held']}{bars_unit}/{barrier_eval['max_bars']}{bars_unit})")

                    out["action"] = "holding"
                    out["holding"] = {
                        "id": active["id"],
                        "direction": active["direction"],
                        "entry_price": entry_ref,
                        "current_price": curr,
                        "tp_price": barrier_eval["tp_price"],
                        "sl_price": barrier_eval["sl_price"],
                        "bars_held": barrier_eval["bars_held"],
                        "max_bars": barrier_eval["max_bars"],
                        "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl_pct, 4),
                    }
                    return out

                elif barrier_eval["status"] == "concluded":
                    exit_price = barrier_eval["exit_price"]
                    exit_reason = barrier_eval["exit_reason"]
                    ref = barrier_eval["ref_price"]
                    move = exit_price - ref
                    move_pct = (move / ref * 100) if ref else 0.0
                    thr = active["noise_threshold"]

                    if exit_reason == "tp_hit":
                        correct = 1
                        is_noise = False
                    elif exit_reason == "sl_hit":
                        correct = 0
                        is_noise = False
                    else:  # time_stop
                        is_noise = abs(move) <= thr
                        if is_noise:
                            correct = None
                        else:
                            up = move > 0
                            correct = 1 if (active["direction"] == "bullish") == up else 0

                    base = base_rate_from(candles)
                    b_high = barrier_eval.get("bar_high", exit_price)
                    b_low = barrier_eval.get("bar_low", exit_price)

                    if not dry_run:
                        ledger.record_outcome(
                            con, prediction_id=active["id"], realized_price=exit_price,
                            move=round(move, 4), move_pct=round(move_pct, 4),
                            is_noise=is_noise, correct=correct, base_rate=round(base, 6))

                        ledger.record_outcome_details(
                            con, prediction_id=active["id"],
                            target_price=barrier_eval["tp_price"],
                            sl_price=barrier_eval["sl_price"],
                            bar_high=b_high, bar_low=b_low,
                            target_reached=1 if exit_reason == "tp_hit" else 0,
                            sl_hit=1 if exit_reason == "sl_hit" else 0)

                    graded_item = {
                        "id": active["id"],
                        "status": "graded",
                        "direction": active["direction"],
                        "move": round(move, 2),
                        "move_pct": round(move_pct, 4),
                        "noise": is_noise,
                        "correct": correct,
                        "exit_reason": exit_reason,
                        "exit_price": exit_price,
                        "bars_held": barrier_eval["bars_held"],
                        "tp_price": barrier_eval["tp_price"],
                        "sl_price": barrier_eval["sl_price"],
                        "target_reached": exit_reason == "tp_hit",
                        "sl_hit": exit_reason == "sl_hit",
                        "is_bull_side": active["direction"] == "bullish",
                        "extreme_val": b_high if active["direction"] == "bullish" else b_low,
                    }
                    graded.append(graded_item)
                    out["graded"] = graded
                    # Concluded trade graded, fall through to predict step!

        # ---- 6. predict the next bar -------------------------------------
        cc_sol = (snap.get("cross_check") or {}).get("sol_usd")
        inc = ledger.get_incumbent(con, horizon=horizon)
        active_params = None
        active_version = predictor.MODEL_VERSION
        if inc:
            try:
                active_params = json.loads(inc["params_json"])
                active_version = inc["version"]
            except Exception:
                pass
        p = predictor.predict(candles, params=active_params, cross_check_sol=cc_sol, current_utc=slot, horizon=horizon)
        if inc:
            p["model_version"] = active_version

        if horizon in ("1h", "15m", "5m", "event") and p["direction"] in ("bullish", "bearish"):
            max_h = predictor.get_max_holding_hours(horizon)
            target = slot + timedelta(hours=max_h)
        else:
            horizon_hours = {"event": 0.25, "5m": 5/60, "15m": 0.25, "1h": 1, "8h": 8, "12h": 12}.get(horizon, 1)
            target = slot + timedelta(hours=horizon_hours)

        ref = snap["spot"]["price"]
        atr_val = (p.get("features") or {}).get("atr14") or ((p.get("noise_threshold") or 0) * 2.0)
        levels = predictor.compute_trade_levels(
            ref_price=ref,
            atr=atr_val,
            direction=p["direction"],
            p_bull=p.get("bullish_confidence", 0.5),
            p_bear=p.get("bearish_confidence", 0.5),
            horizon=horizon,
        )
        inputs = {
            "spot": ref,
            "spot_updated": snap["spot"]["updated_at"],
            "cross_check_sol": (snap.get("cross_check") or {}).get("sol_usd"),
            "ticker_last": (snap.get("ticker") or {}).get("last"),
            "spread": (snap.get("ticker") or {}).get("spread"),
            "source_status": snap["source_status"],
            "target_tp_price": levels["tp_price"],
            "target_tp_points": levels["tp_points"],
            "stop_loss_price": levels["sl_price"],
            "stop_loss_points": levels["sl_points"],
            "tp_price": levels["tp_price"],
            "sl_price": levels["sl_price"],
            "tp_mult": levels["tp_mult"],
            "sl_mult": levels["sl_mult"],
            "tp_atr_mult": levels["tp_mult"],
            "sl_atr_mult": levels["sl_mult"],
            "rr_ratio": levels["rr_ratio"],
            "max_holding_hours": predictor.get_max_holding_hours(horizon) if horizon in ("1h", "15m") else None,
        }
        pid = None
        if not dry_run:
            notes = p.get("reason") or f"contrib={json.dumps(p['contributions'])}"
            pid = ledger.record_prediction(
                con, horizon=horizon,
                target_bar_utc=target.isoformat(timespec="seconds"),
                scheduled_slot_utc=slot_iso,
                direction=p["direction"], confidence=p["confidence"],
                ref_price=ref, model_version=p["model_version"],
                params=p["params"], features=p["features"], inputs=inputs,
                signal_source="technical", noise_threshold=p["noise_threshold"],
                notes=notes)
            ledger.finish_slot(con, job_name, slot_iso, "ok",
                               f"graded={len([g for g in graded if g['status']=='graded'])} "
                               f"predicted={p['direction']}")

        out["action"] = "predicted"
        out["prediction"] = {
            "id": pid, "direction": p["direction"], "score": p["score"],
            "confidence": p["confidence"],
            "bullish_confidence": p.get("bullish_confidence"),
            "bearish_confidence": p.get("bearish_confidence"),
            "ref_price": ref,
            "target_bar_utc": target.isoformat(timespec="seconds"),
            "noise_threshold": p["noise_threshold"],
            "contributions": p["contributions"],
            "reason": p.get("reason"),
            "levels": levels,
        }
        return out

    except Exception as e:
        if not dry_run:
            try:
                ledger.finish_slot(con, job_name, slot_iso, "failed", f"{type(e).__name__}: {e}"[:300])
            except Exception:
                pass
        out["action"] = "error"
        out["error"] = f"{type(e).__name__}: {e}"
        return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--horizon", default="1h", choices=["1h", "8h", "12h"])
    a = ap.parse_args()

    if a.report:
        con = ledger.connect()
        print(json.dumps(report(con), indent=2, default=str))
    else:
        r = run(dry_run=a.dry_run, horizon=a.horizon)
        print(json.dumps(r, indent=2, default=str))