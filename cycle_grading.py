#!/usr/bin/env python3
"""Cycle Grading & Barrier Trade Evaluation for Solana Engine.

Handles evaluation of open directional predictions against Take-Profit,
Cut-Loss, and Time-Stop thresholds across continuous price bars.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

import fetch_market as fm
import ledger
import predictor

BASE_RATE_WINDOW = 100


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

    row_horizon = active.get("horizon", "1h")
    max_bars = 4 if row_horizon in ("15m", "event") else 24
    max_holding_h = 1 if row_horizon in ("15m", "event") else 24

    if len(completed_bars) >= max_bars:
        exit_bar = completed_bars[max_bars - 1]
        exit_price = exit_bar["close"]
        return {
            "status": "concluded",
            "exit_reason": "time_stop",
            "exit_price": exit_price,
            "bars_held": max_bars,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "ref_price": ref,
            "direction": direction,
            "bar_high": overall_high or exit_price,
            "bar_low": overall_low or exit_price,
        }

    pnl = (current_spot - ref) if direction == "bullish" else (ref - current_spot)
    pnl_pct = (pnl / ref) * 100.0 if ref else 0.0

    return {
        "status": "holding",
        "id": active["id"],
        "bars_held": max(1, len(relevant_bars)),
        "current_bar": len(relevant_bars),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
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

    for row in ledger.ungraded(con, before_utc=now.isoformat(timespec="seconds"), horizon=horizon):
        if horizon in ("1h", "15m", "5m", "event") and row["signal_source"] == "technical" and row["direction"] in ("bullish", "bearish"):
            continue

        target = datetime.fromisoformat(row["target_bar_utc"])
        horizon_hours = {"event": 0.25, "5m": 5/60, "15m": 0.25, "1h": 1, "8h": 8, "12h": 12}.get(horizon, 1)
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

        feats = json.loads(row["features_json"] or "{}") if "features_json" in row.keys() else {}
        inputs = json.loads(row["inputs_json"] or "{}") if "inputs_json" in row.keys() else {}
        atr = feats.get("atr14") or (thr * 2.0)
        p_bull, p_bear = 0.5, 0.5
        notes = row["notes"] or ""
        if "Bullish" in notes and "Bearish" in notes:
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
                "ref_price": ref, "tp_price": tp_price, "sl_price": sl_price,
                "tp_points": tp_points, "sl_points": sl_points, "tp_mult": tp_mult, "sl_mult": sl_mult,
                "rr_ratio": round(tp_mult / sl_mult, 2) if sl_mult > 0 else 0.0,
                "tp_sign": tp_sign, "sl_sign": sl_sign, "is_bull_side": is_bull_side,
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
