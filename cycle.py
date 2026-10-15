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

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))
JOB = "hourly_predict"
BASE_RATE_WINDOW = 100          # bars used for the always-up base rate


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


def base_rate_from(candles: list[dict], window: int = BASE_RATE_WINDOW) -> float:
    """The always-up rate in this window. NOT 0.5 - that is the whole point."""
    closes = [c["close"] for c in candles[-window:]]
    if len(closes) < 2:
        return 0.5
    ups = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])
    return ups / (len(closes) - 1)


def get_active_technical(con, horizon: str = "1h") -> dict | None:
    """Return the oldest open (ungraded) directional technical prediction, if any."""
    r = con.execute("""
        SELECT p.* FROM predictions p
        LEFT JOIN outcomes o ON o.prediction_id = p.id
        WHERE o.prediction_id IS NULL
          AND p.signal_source = 'technical'
          AND p.horizon = ?
          AND p.direction IN ('bullish', 'bearish')
        ORDER BY p.id ASC LIMIT 1
    """, (horizon,)).fetchone()
    return dict(r) if r else None


def evaluate_barrier_trade(active: dict, candles: list[dict], current_spot: float, now: datetime) -> dict:
    """Evaluate open directional prediction against TP / Cut Loss / 24h Time-stop."""
    ref = active["ref_price"]
    direction = active["direction"]
    if active.get("scheduled_slot_utc"):
        entry_slot_ts = int(datetime.fromisoformat(active["scheduled_slot_utc"]).timestamp())
    else:
        created_utc = datetime.fromisoformat(active["created_utc"])
        entry_slot_ts = int(created_utc.replace(minute=0, second=0, microsecond=0).timestamp())
    created_utc = datetime.fromisoformat(active["created_utc"])

    try:
        features = json.loads(active["features_json"] or "{}")
    except Exception:
        features = {}
    try:
        inputs = json.loads(active["inputs_json"] or "{}")
    except Exception:
        inputs = {}
    try:
        params = json.loads(active["params_json"] or "{}")
    except Exception:
        params = {}

    atr14 = features.get("atr14")
    if not atr14:
        thr = active.get("noise_threshold")
        atr14 = (thr / predictor.NOISE_ATR_MULT) if thr else 150.0

    tp_mult = params.get("tp_atr_mult", predictor.TP_ATR_MULT_1H)
    sl_mult = params.get("sl_atr_mult", predictor.SL_ATR_MULT)

    tp_price = inputs.get("target_tp_price") or inputs.get("tp_price")
    sl_price = inputs.get("stop_loss_price") or inputs.get("sl_price")
    if tp_price is None or sl_price is None:
        levels = predictor.compute_trade_levels(ref, atr14, direction, tp_mult=tp_mult, sl_mult=sl_mult, horizon="1h")
        tp_price = levels["tp_price"]
        sl_price = levels["sl_price"]
    else:
        tp_price = float(tp_price)
        sl_price = float(sl_price)

    relevant_bars = [c for c in candles if (c["open_time"] // 1000) >= entry_slot_ts]
    completed_bars = [c for c in candles[:-1] if (c["open_time"] // 1000) >= entry_slot_ts]

    overall_high = None
    overall_low = None

    for i, b in enumerate(relevant_bars):
        b_high = b["high"]
        b_low = b["low"]
        overall_high = b_high if overall_high is None else max(overall_high, b_high)
        overall_low = b_low if overall_low is None else min(overall_low, b_low)

        if direction == "bullish":
            hit_tp = b_high >= tp_price
            hit_sl = b_low <= sl_price
        else:  # bearish
            hit_tp = b_low <= tp_price
            hit_sl = b_high >= sl_price

        if hit_tp and hit_sl:
            # Reconstruct sequence using 1m klines
            winner = None
            try:
                b_open_ms = b["open_time"]
                b_close_ms = b_open_ms + (3600 * 1000)
                m_bars = fm.fetch_1m_range(b_open_ms, b_close_ms)
                for mb in m_bars:
                    m_tp = mb["high"] >= tp_price if direction == "bullish" else mb["low"] <= tp_price
                    m_sl = mb["low"] <= sl_price if direction == "bullish" else mb["high"] >= sl_price
                    if m_tp and not m_sl:
                        winner = "tp_hit"
                        break
                    elif m_sl and not m_tp:
                        winner = "sl_hit"
                        break
            except Exception:
                winner = None

            resolved_reason = winner or "sl_hit"
            exit_price = tp_price if resolved_reason == "tp_hit" else sl_price
            return {
                "status": "concluded",
                "exit_reason": resolved_reason,
                "exit_price": exit_price,
                "bars_held": max(1, i + 1),
                "tp_price": tp_price,
                "sl_price": sl_price,
                "ref_price": ref,
                "direction": direction,
                "bar_high": overall_high,
                "bar_low": overall_low,
            }
        elif hit_tp:
            return {
                "status": "concluded",
                "exit_reason": "tp_hit",
                "exit_price": tp_price,
                "bars_held": max(1, i + 1),
                "tp_price": tp_price,
                "sl_price": sl_price,
                "ref_price": ref,
                "direction": direction,
                "bar_high": overall_high,
                "bar_low": overall_low,
            }
        elif hit_sl:
            return {
                "status": "concluded",
                "exit_reason": "sl_hit",
                "exit_price": sl_price,
                "bars_held": max(1, i + 1),
                "tp_price": tp_price,
                "sl_price": sl_price,
                "ref_price": ref,
                "direction": direction,
                "bar_high": overall_high,
                "bar_low": overall_low,
            }

    # Time-stop check (max holding hours / bars)
    horizon = active.get("horizon", "1h")
    max_holding_h = predictor.get_max_holding_hours(horizon)
    max_bars = int(max_holding_h * 12) if horizon == "5m" else (int(max_holding_h * 4) if horizon in ("15m", "event") else int(max_holding_h))
    elapsed_seconds = (now - created_utc).total_seconds()
    bars_held = len(completed_bars)

    if elapsed_seconds >= (max_holding_h * 3600) or (bars_held >= max_bars and bars_held > 0):
        exit_price = current_spot
        return {
            "status": "concluded",
            "exit_reason": "time_stop",
            "exit_price": exit_price,
            "bars_held": max(1, bars_held),
            "tp_price": tp_price,
            "sl_price": sl_price,
            "ref_price": ref,
            "direction": direction,
            "bar_high": overall_high if overall_high is not None else current_spot,
            "bar_low": overall_low if overall_low is not None else current_spot,
        }

    return {
        "status": "holding",
        "bars_held": bars_held,
        "max_bars": max_bars,
        "max_holding_hours": max_holding_h,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "ref_price": ref,
        "current_price": current_spot,
        "direction": direction,
    }


def grade_due(con, candles: list[dict], now: datetime, horizon: str = "1h") -> list[dict]:
    """Grade every prediction whose target bar has closed. Returns summaries."""
    by_time = {c["open_time"] // 1000: c for c in candles}
    base = base_rate_from(candles)
    results = []
    horizon_hours = {"15m": 0.25, "1h": 1, "8h": 8, "12h": 12}.get(horizon, 1)

    for row in ledger.ungraded(con, before_utc=now.isoformat(timespec="seconds"), horizon=horizon):
        # Barrier lifecycle: for 1h, 15m, 5m, and event technical calls, directional predictions are evaluated
        # across bars by evaluate_barrier_trade(), NOT prematurely by bar-close grade_due.
        if horizon in ("1h", "15m", "5m", "event") and row["signal_source"] == "technical" and row["direction"] in ("bullish", "bearish"):
            continue

        target = datetime.fromisoformat(row["target_bar_utc"])
        horizon_hours = {"event": 0.25, "5m": 5/60, "15m": 0.25, "1h": 1, "8h": 8, "12h": 12}.get(horizon, 1)
        # the bar that closes at `target` opens horizon_hours earlier
        bar_open = int((target - timedelta(hours=horizon_hours)).timestamp())
        bar = by_time.get(bar_open)
        if bar is None:
            results.append({"id": row["id"], "status": "bar_not_available"})
            continue

        ref = row["ref_price"]
        realized = bar["close"]
        high = bar["high"]
        low = bar["low"]
        move = realized - ref
        move_pct = move / ref * 100 if ref else 0.0
        thr = row["noise_threshold"]
        is_noise = abs(move) <= thr

        if row["direction"] == "no_call" or is_noise:
            correct = None
        else:
            up = move > 0
            correct = 1 if (row["direction"] == "bullish") == up else 0

        # Calculate trade levels to evaluate target reached during the bar
        feats = json.loads(row["features_json"] or "{}") if "features_json" in row.keys() else {}
        inputs = json.loads(row["inputs_json"] or "{}") if "inputs_json" in row.keys() else {}
        atr = feats.get("atr14") or (thr * 2.0)
        p_bull, p_bear = 0.5, 0.5
        notes = row["notes"] or ""
        if "Bullish" in notes and "Bearish" in notes:
            import re
            m = re.search(r'([\d\.]+)%\s*Bullish.*?([\d\.]+)%\s*Bearish', notes)
            if m:
                p_bull = float(m.group(1)) / 100.0
                p_bear = float(m.group(2)) / 100.0
        elif feats:
            try:
                s, _ = predictor.score(feats)
                p_bull = round(0.5 + 0.5 * s, 4)
                p_bear = round(0.5 - 0.5 * s, 4)
            except Exception:
                pass

        row_horizon = row["horizon"] if ("horizon" in row.keys() and row["horizon"]) else horizon
        saved_tp = inputs.get("target_tp_price")
        saved_sl = inputs.get("stop_loss_price")
        if saved_tp is not None and saved_sl is not None:
            tp_price = round(float(saved_tp), 2)
            sl_price = round(float(saved_sl), 2)
            raw_tp_pts = inputs.get("target_tp_points")
            raw_sl_pts = inputs.get("stop_loss_points")
            tp_points = round(float(raw_tp_pts if raw_tp_pts is not None else abs(tp_price - ref)), 2)
            sl_points = round(float(raw_sl_pts if raw_sl_pts is not None else abs(sl_price - ref)), 2)
            is_bull_side = row["direction"] == "bullish" or (row["direction"] == "no_call" and p_bull >= p_bear)
            tp_sign = "+" if is_bull_side else "-"
            sl_sign = "-" if is_bull_side else "+"
            raw_tp_mult = inputs.get("tp_mult")
            raw_sl_mult = inputs.get("sl_mult")
            tp_mult = float(raw_tp_mult if raw_tp_mult is not None else predictor.get_tp_mult_for_horizon(row_horizon))
            sl_mult = float(raw_sl_mult if raw_sl_mult is not None else predictor.SL_ATR_MULT)
            levels = {
                "ref_price": ref,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "tp_points": tp_points,
                "sl_points": sl_points,
                "tp_mult": tp_mult,
                "sl_mult": sl_mult,
                "rr_ratio": round(tp_mult / sl_mult, 2) if sl_mult > 0 else 0.0,
                "tp_sign": tp_sign,
                "sl_sign": sl_sign,
                "is_bull_side": is_bull_side,
            }
        else:
            levels = predictor.compute_trade_levels(ref, atr, row["direction"], p_bull, p_bear, horizon=row_horizon)
        is_bull_side = levels["is_bull_side"]
        if is_bull_side:
            tp_reached = high >= levels["tp_price"]
            sl_hit = low <= levels["sl_price"]
            extreme_val = high
        else:
            tp_reached = low <= levels["tp_price"]
            sl_hit = high >= levels["sl_price"]
            extreme_val = low

        if row["direction"] in ("bullish", "bearish"):
            if tp_reached and sl_hit:
                winner = None
                try:
                    b_open_ms = bar["open_time"]
                    b_close_ms = b_open_ms + (horizon_hours * 3600 * 1000)
                    m_bars = fm.fetch_1m_range(b_open_ms, b_close_ms)
                    for mb in m_bars:
                        m_tp = mb["high"] >= levels["tp_price"] if is_bull_side else mb["low"] <= levels["tp_price"]
                        m_sl = mb["low"] <= levels["sl_price"] if is_bull_side else mb["high"] >= levels["sl_price"]
                        if m_tp and not m_sl:
                            winner = "tp_hit"
                            break
                        elif m_sl and not m_tp:
                            winner = "sl_hit"
                            break
                except Exception:
                    winner = None
                if winner == "tp_hit":
                    sl_hit = False
                else:
                    tp_reached = False

            if tp_reached:
                realized = levels["tp_price"]
                move = realized - ref
                move_pct = move / ref * 100 if ref else 0.0
                is_noise = False
                correct = 1
            elif sl_hit:
                realized = levels["sl_price"]
                move = realized - ref
                move_pct = move / ref * 100 if ref else 0.0
                is_noise = False
                correct = 0

        ledger.record_outcome(
            con, prediction_id=row["id"], realized_price=realized, move=round(move, 4),
            move_pct=round(move_pct, 4), is_noise=is_noise, correct=correct,
            base_rate=round(base, 6))

        ledger.record_outcome_details(
            con, prediction_id=row["id"], target_price=levels["tp_price"],
            sl_price=levels["sl_price"], bar_high=high, bar_low=low,
            target_reached=1 if tp_reached else 0, sl_hit=1 if sl_hit else 0)

        results.append({
            "id": row["id"], "status": "graded", "direction": row["direction"],
            "move": round(move, 2), "noise": is_noise, "correct": correct,
            "target_reached": tp_reached, "sl_hit": sl_hit,
            "tp_price": levels["tp_price"], "sl_price": levels["sl_price"],
            "extreme_val": round(extreme_val, 2), "is_bull_side": is_bull_side,
            "bar_high": round(high, 2), "bar_low": round(low, 2),
        })
    return results


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


def format_hourly_delivery(r: dict) -> str:
    """Render the hourly cycle result as the Discord message.

    Returns an EMPTY STRING for the silent cases (market closed, duplicate slot).
    The cron runner suppresses delivery on empty stdout, so returning "" is what
    keeps closed-market hours out of the channel entirely - the ledger still
    records the gap, the channel just stays quiet.

    Kept here rather than in the cron script so the delivery contract is
    unit-testable without running the scheduler.
    """
    a = r.get("action")

    if a in ("gap", "duplicate_slot_rejected"):
        return ""

    horizon = r.get("horizon", "1h")
    if a in ("fetch_failed", "error"):
        return (f"⚠️ **Solana {horizon} cycle FAILED** ({r.get('slot_wib')})\n"
                f"`{a}`: {r.get('error')}")

    if a == "holding":
        h = r.get("holding", {}) or {}
        dir_str = str(h.get("direction", ""))
        dir_badge = "🟢 **Bullish**" if dir_str == "bullish" else "🔴 **Bearish**"
        pnl = float(h.get("pnl") or 0.0)
        pnl_pct = float(h.get("pnl_pct") or 0.0)
        pnl_sign = "+" if pnl > 0 else ""

        entry = float(h.get("entry_price") or 0.0)
        curr = float(h.get("current_price") or 0.0)
        tp = float(h.get("tp_price") or 0.0)
        sl = float(h.get("sl_price") or 0.0)

        cycle_title = "⚡ **Event Position In Progress**" if horizon == "event" else ("**15-Minute cycle (15M)**" if horizon == "15m" else ("**Hourly cycle**" if horizon == "1h" else f"**{horizon.upper()} cycle**"))
        unit_str = "bars" if horizon in ("15m", "5m", "event") else "h"
        time_unit = "breakout window" if horizon == "event" else ("15 minutes" if horizon == "15m" else ("hour" if horizon == "1h" else f"{horizon} bar"))

        lines = [
            f"{cycle_title} — {str(r.get('slot_wib'))[:16]} WIB",
            "",
            f"⏳ **Position In Progress** — Holding #{h.get('id')} ({h.get('bars_held')}{unit_str} / {h.get('max_bars')}{unit_str})",
            f"• **Direction** — {dir_badge}",
            f"• **Entry** — ${entry:,.2f}  ·  **Current** — ${curr:,.2f} ({pnl_sign}${pnl:,.2f} pts / {pnl_sign}{pnl_pct:.2f}%)",
            f"• **Take Profit** — ${tp:,.2f}  ·  **Cut Loss** — ${sl:,.2f}",
            "",
            f"_Target barrier not yet reached. No new prediction this {time_unit}._",
        ]
        return "\n".join(lines)

    p = r.get("prediction", {}) or {}
    graded = [g for g in r.get("graded", []) if g.get("status") == "graded"]

    score = p.get("score", 0.0)
    p_bull = p.get("bullish_confidence")
    p_bear = p.get("bearish_confidence")
    if p_bull is None or p_bear is None:
        p_bull = round(0.5 + 0.5 * score, 4)
        p_bear = round(0.5 - 0.5 * score, 4)

    dir_str = p.get("direction", "no_call")
    if dir_str == "bullish":
        verdict_badge = "🟢 **Bullish**"
    elif dir_str == "bearish":
        verdict_badge = "🔴 **Bearish**"
    else:
        verdict_badge = "⚪ **No Call** (Abstained / Neutral)"

    reason = p.get("reason") or ""
    if "Abstained:" in reason:
        reason_display = "Abstained: " + reason.split("Abstained:", 1)[1].strip()
    elif "Market in low-conviction chop" in reason:
        reason_display = "Market in low-conviction chop " + reason.split("Market in low-conviction chop", 1)[1].strip()
    elif "Bullish bias:" in reason:
        reason_display = "Bullish bias: " + reason.split("Bullish bias:", 1)[1].strip()
    elif "Bearish bias:" in reason:
        reason_display = "Bearish bias: " + reason.split("Bearish bias:", 1)[1].strip()
    else:
        reason_display = reason.strip()

    ref_price = p.get("ref_price")
    atr_val = (p.get("features") or {}).get("atr14") or ((p.get("noise_threshold") or 0) * 2.0)
    levels = p.get("levels")
    if not levels and ref_price:
        levels = predictor.compute_trade_levels(
            ref_price=ref_price,
            atr=atr_val,
            direction=dir_str,
            p_bull=p_bull,
            p_bear=p_bear,
            horizon=horizon,
        )

    if horizon == "1h":
        cycle_title = f"**Hourly cycle** — {str(r.get('slot_wib'))[:16]} WIB"
    elif horizon == "event":
        cycle_title = f"⚡ **Event Breakout Trigger** — {str(r.get('slot_wib'))[:19]} WIB"
    elif horizon == "5m":
        cycle_title = f"**5-Minute cycle (5M)** — {str(r.get('slot_wib'))[:16]} WIB"
    elif horizon == "15m":
        cycle_title = f"**15-Minute cycle (15M)** — {str(r.get('slot_wib'))[:16]} WIB"
    elif horizon == "8h":
        cycle_title = f"**8-Hour cycle (8H)** — {str(r.get('slot_wib'))[:16]} WIB"
    elif horizon == "12h":
        cycle_title = f"**12-Hour cycle (12H)** — {str(r.get('slot_wib'))[:16]} WIB"
    else:
        cycle_title = f"**{horizon.upper()} cycle** — {str(r.get('slot_wib'))[:16]} WIB"

    lines = [
        cycle_title,
        "",
        f"**Verdict** — {verdict_badge}",
    ]
    if reason_display:
        lines.append(f"**Reason** — {reason_display}")

    lines.extend([
        f"**Confidence** — 🐂 **{p_bull*100:.1f}% Bullish** · 🐻 **{p_bear*100:.1f}% Bearish** (Conviction: {p.get('confidence', 0):.2f})",
        f"**Ref price** — ${p.get('ref_price')}  ·  "
        f"noise threshold ${p.get('noise_threshold')}",
    ])

    if levels:
        tp_sign = levels["tp_sign"]
        sl_sign = levels["sl_sign"]
        lean_suffix = ""
        if dir_str == "no_call":
            lean_side = "Bullish" if levels["is_bull_side"] else "Bearish"
            lean_suffix = f"  ·  _{lean_side} lean_"

        tp_mult_val = levels.get("tp_mult", 1.5)
        sl_mult_val = levels.get("sl_mult", 1.0)
        rr_val = levels.get("rr_ratio", round(tp_mult_val / sl_mult_val, 1) if sl_mult_val else 1.5)

        lines.extend([
            f"**Target (TP)** — ${levels['tp_price']:.2f} ({tp_sign}${levels['tp_points']:.2f} pts  ·  {tp_mult_val:.1f}× ATR · R:R 1:{rr_val:.1f}){lean_suffix}",
            f"**Stop Loss (SL)** — ${levels['sl_price']:.2f} ({sl_sign}${levels['sl_points']:.2f} pts  ·  {sl_mult_val:.1f}× ATR){lean_suffix}",
        ])

    if graded:
        lines += ["", "**Graded**"]
        for g in graded:
            exit_reason = g.get("exit_reason", "")
            if exit_reason == "tp_hit":
                mark = "✅ **WIN (Take Profit Hit)**"
            elif exit_reason == "sl_hit":
                mark = "❌ **LOSS (Cut Loss Hit)**"
            elif exit_reason == "time_stop":
                sub = "WIN" if g.get("correct") == 1 else ("LOSS" if g.get("correct") == 0 else "NOISE")
                mark = f"⏰ **TIME-STOP (24h Close)** ({sub})"
            elif g.get("noise"):
                mark = "NOISE (excluded)"
            elif g.get("correct") == 1:
                mark = "WIN"
            elif g.get("correct") == 0:
                mark = "LOSS"
            else:
                mark = "no-call"

            exit_p = g.get("exit_price")
            exit_str = f"Exit ${exit_p:,.2f} · " if exit_p is not None else ""

            target_status = ""
            if not exit_reason:
                if g.get("target_reached"):
                    extreme_label = "High" if g.get("is_bull_side") else "Low"
                    target_status = f"  ·  🎯 **Target Reached** (TP: ${g['tp_price']:.2f} · {extreme_label}: ${g['extreme_val']:.2f})"
                elif g.get("sl_hit"):
                    extreme_label = "Low" if g.get("is_bull_side") else "High"
                    target_status = f"  ·  🛑 **Stop Hit** (SL: ${g['sl_price']:.2f} · {extreme_label}: ${g['extreme_val']:.2f})"
                elif g.get("tp_price"):
                    extreme_label = "High" if g.get("is_bull_side") else "Low"
                    target_status = f"  ·  ⏳ **Target Missed** (TP: ${g['tp_price']:.2f} · {extreme_label}: ${g['extreme_val']:.2f})"

            lines.append(f"• #{g['id']} {str(g['direction']).upper()} → "
                         f"{exit_str}move {g['move']:+.2f} pts · **{mark}**{target_status}")
    else:
        time_unit = "15 minutes" if horizon == "15m" else ("hour" if horizon == "1h" else f"{horizon} bar")
        lines += ["", f"_No predictions due for grading this {time_unit}._"]

    return "\n".join(lines)


format_cycle_delivery = format_hourly_delivery


def report(con) -> dict:
    """Honest summary: hit-rate with n, CI, and the base rate it must beat."""
    rows = [dict(r) for r in con.execute(
        """SELECT o.correct, o.is_noise, o.base_rate, p.signal_source
           FROM outcomes o JOIN predictions p ON p.id = o.prediction_id""")]
    summary = stats.summarize(rows)
    summary["chain"] = ledger.verify_chain(con)
    summary["counts"] = {
        t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("predictions", "outcomes", "gaps", "fetch_errors", "cron_runs")
    }
    return summary


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