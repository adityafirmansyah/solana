#!/usr/bin/env python3
"""Database Trade Synchronization & Prediction Logging for Barrier Watcher.

Handles reading active open barrier trades from SQLite, recording trade outcomes
and detail metrics into append-only tables, and dispatching FAPI orders.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ledger
import predictor
from barrier_notifications import (
    format_duration,
    format_price,
    get_latest_base_rate,
    post_persona_discord,
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "solana.db"
UTC = timezone.utc
WIB = timezone(timedelta(hours=7))

logger = logging.getLogger("solana_barrier_db")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] barrier_db: %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

MAX_HOLDING_SECS = 360.0  # 6 minutes max holding time-stop for micro lead-lag (was 7200s / 2h)

# Try loading FapiExecutor
try:
    from fapi_executor import BinanceFuturesExecutor
    FAPI_EXECUTOR = BinanceFuturesExecutor()
except Exception as e:
    logger.warning(f"Failed to initialize BinanceFuturesExecutor: {e}")
    FAPI_EXECUTOR = None


def load_active_trades_from_db() -> dict[int, dict]:
    """Load open, ungraded predictions from the ledger."""
    con = ledger.connect()
    try:
        rows = con.execute("""
            SELECT p.id, p.symbol, p.leader_symbol, p.horizon, p.direction, p.ref_price, p.created_utc,
                   p.scheduled_slot_utc, p.features_json, p.inputs_json,
                   p.params_json, p.noise_threshold, p.notes
            FROM predictions p
            LEFT JOIN outcomes o ON o.prediction_id = p.id
            WHERE o.prediction_id IS NULL
              AND p.direction IN ('bullish', 'bearish')
            ORDER BY p.id ASC
        """).fetchall()

        active = {}
        for r in rows:
            d = dict(r)
            pid = d["id"]
            ref = float(d["ref_price"])
            direction = d["direction"]
            horizon = d["horizon"] or "lead_lag"
            symbol = d.get("symbol") or "SOLUSDT"
            leader_symbol = d.get("leader_symbol") or "BTCUSDT"

            try:
                inputs = json.loads(d["inputs_json"] or "{}")
            except Exception:
                inputs = {}
            try:
                features = json.loads(d["features_json"] or "{}")
            except Exception:
                features = {}

            tp = inputs.get("target_tp_price") or inputs.get("tp_price")
            sl = inputs.get("stop_loss_price") or inputs.get("sl_price")

            if tp is None or sl is None:
                atr = features.get("atr") or features.get("atr14")
                if not atr:
                    thr = d.get("noise_threshold")
                    atr = (thr / 0.5) if thr else (ref * 0.005)
                levels = predictor.compute_trade_levels(ref, atr, direction, horizon=horizon, tp_mult=1.5, sl_mult=1.0)
                tp = levels["tp_price"]
                sl = levels["sl_price"]
            else:
                tp = float(tp)
                sl = float(sl)

            active[pid] = {
                "id": pid,
                "symbol": symbol,
                "leader_symbol": leader_symbol,
                "horizon": horizon,
                "direction": direction,
                "ref_price": ref,
                "tp_price": tp,
                "sl_price": sl,
                "created_utc": d["created_utc"],
                "high_watermark": ref,
                "low_watermark": ref,
                "noise_threshold": d.get("noise_threshold") or (ref * 0.0025),
            }

        return active
    finally:
        con.close()


def audit_exchange_exit(pid: int, symbol: str, default_sl: float, default_tp: float) -> tuple[str, float]:
    """Check Binance algo orders to determine if SL or TP triggered natively on exchange."""
    if not FAPI_EXECUTOR:
        return "sl_hit", default_sl
    try:
        import sqlite3
        with sqlite3.connect(DB_PATH) as con:
            row = con.execute("SELECT tp_order_id, sl_order_id FROM testnet_executions WHERE prediction_id = ?", (pid,)).fetchone()
            if row:
                tp_id, sl_id = row[0], row[1]
                if sl_id:
                    sl_res = FAPI_EXECUTOR._request("GET", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": sl_id})
                    if isinstance(sl_res, dict) and sl_res.get("algoStatus") == "FINISHED":
                        px = float(sl_res.get("actualPrice") or default_sl)
                        return "sl_hit", px
                if tp_id:
                    tp_res = FAPI_EXECUTOR._request("GET", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": tp_id})
                    if isinstance(tp_res, dict) and tp_res.get("algoStatus") == "FINISHED":
                        px = float(tp_res.get("actualPrice") or default_tp)
                        return "tp_hit", px
    except Exception as e:
        logger.warning(f"Failed to audit exchange algo orders for #{pid}: {e}")
    return "sl_hit", default_sl


def sync_record_outcome(trade: dict, exit_reason: str, trigger_price: float, trigger_time: datetime) -> bool:
    """Record trade outcome in ledger, trigger Discord alert, and close testnet order."""
    pid = trade["id"]
    symbol = trade["symbol"]
    leader = trade["leader_symbol"]
    direction = trade["direction"]
    horizon = trade["horizon"]
    ref = trade["ref_price"]
    tp = trade["tp_price"]
    sl = trade["sl_price"]

    if exit_reason == "tp_hit":
        exit_price = tp
    elif exit_reason == "sl_hit":
        exit_price = sl
    else:
        exit_price = trigger_price

    move = exit_price - ref
    move_pct = (move / ref * 100) if ref else 0.0

    b_high = max(trade.get("high_watermark", ref), trigger_price, exit_price)
    b_low = min(trade.get("low_watermark", ref), trigger_price, exit_price)

    created_dt = datetime.fromisoformat(trade["created_utc"])
    elapsed_secs = (trigger_time - created_dt).total_seconds()
    duration_str = format_duration(elapsed_secs)

    logger.info(f"Triggering {exit_reason.upper()} for #{pid} ({symbol} {direction}) at {format_price(trigger_price, symbol)} "
                f"(exit: {format_price(exit_price, symbol)}, move: {move_pct:+.2f}%)")

    con = ledger.connect()
    try:
        already = con.execute("SELECT prediction_id FROM outcomes WHERE prediction_id = ?", (pid,)).fetchone()
        if already:
            logger.warning(f"Prediction #{pid} already graded. Skipping duplicate resolution.")
            return False

        base = get_latest_base_rate(con)
        is_noise = False

        if exit_reason == "tp_hit":
            correct = 1
        elif exit_reason == "sl_hit":
            correct = 0
        else:
            pnl_move = move if direction == "bullish" else -move
            correct = 1 if pnl_move > 0 else 0

        ledger.record_outcome(
            con,
            prediction_id=pid,
            realized_price=round(exit_price, 6),
            move=round(move, 6),
            move_pct=round(move_pct, 4),
            is_noise=is_noise,
            correct=correct,
            base_rate=round(base, 6),
        )

        ledger.record_outcome_details(
            con,
            prediction_id=pid,
            symbol=symbol,
            leader_symbol=leader,
            target_price=round(tp, 6),
            sl_price=round(sl, 6),
            bar_high=round(b_high, 6),
            bar_low=round(b_low, 6),
            target_reached=1 if exit_reason == "tp_hit" else 0,
            sl_hit=1 if exit_reason == "sl_hit" else 0,
        )

        logger.info(f"Successfully recorded outcome for #{pid} ({symbol}) in ledger.")
    except Exception as e:
        logger.error(f"Failed to record outcome for #{pid}: {e}", exc_info=True)
        return False
    finally:
        con.close()

    if FAPI_EXECUTOR:
        try:
            FAPI_EXECUTOR.close_position_on_exit(
                prediction_id=pid,
                symbol=symbol,
                exit_reason=exit_reason,
                realized_price=exit_price,
            )
        except Exception as e:
            logger.error(f"Failed to close testnet position for #{pid}: {e}")

    try:
        wib_time_str = trigger_time.astimezone(WIB).strftime("%H:%M:%S")
        is_win = exit_reason == "tp_hit"
        is_sl = exit_reason == "sl_hit"

        pnl_pts = move if direction == "bullish" else -move
        pnl_pct = move_pct if direction == "bullish" else -move_pct

        if is_win:
            title = f"🎯 **TARGET REACHED (Take Profit)** · **{symbol}** (Leader: `{leader}`)"
            res_line = "✅ **WIN** (Real-time TP trigger)"
            event_name = "RESOLUTION_TP (Take Profit Win)"
            outcome_text = f"{symbol} sukses hit Take Profit di {format_price(exit_price, symbol)} ({pnl_pct:+.2f}%). Momentum tertransmisi bersih."
        elif is_sl:
            title = f"🛑 **STOP LOSS TRIGGERED (Cut Loss)** · **{symbol}** (Leader: `{leader}`)"
            res_line = "❌ **CUT LOSS** (Real-time SL trigger)"
            event_name = "RESOLUTION_SL (Stop Loss Cut)"
            outcome_text = f"{symbol} hit Stop Loss di {format_price(exit_price, symbol)} ({pnl_pct:+.2f}%). Order flow berbalik arah."
        else:
            is_expired_win = pnl_pts > 0
            res_emoji = "✅" if is_expired_win else "❌"
            res_label = "WIN (Target Profit on Expiry)" if is_expired_win else "LOSS (Negative Move on Expiry)"
            title = f"⏰ **TIME STOP EXPIRED** · **{symbol}** (Leader: `{leader}`)"
            res_line = f"{res_emoji} **{res_label}** ({pnl_pct:+.2f}%)"
            event_name = "RESOLUTION_TIMESTOP (Expired Resolution)"
            outcome_text = f"{symbol} time stop 2 jam habis di {format_price(exit_price, symbol)} ({pnl_pct:+.2f}%). Posisi di-grade sebagai {'WIN' if is_expired_win else 'LOSS'}."

        details = (
            f"Prediction #{pid} ({symbol} {direction.upper()} via {leader}) selesai: {outcome_text} "
            f"Entry: {format_price(ref, symbol)} ➔ Exit: {format_price(exit_price, symbol)} "
            f"({pnl_pts:+.4f} pts · {pnl_pct:+.2f}%). Durasi: {duration_str}."
        )
        metrics = (
            f"{title}\n"
            f"• **Prediction**: `#{pid}` (`{horizon}` {direction.upper()})\n"
            f"• **Entry (Ref)**: `{format_price(ref, symbol)}` → **Exit**: `{format_price(exit_price, symbol)}` "
            f"(`{pnl_pts:+.4f} pts` · `{pnl_pct:+.2f}%`)\n"
            f"• **Trigger Spot**: `{format_price(trigger_price, symbol)}` (TP: `{format_price(tp, symbol)}` · SL: `{format_price(sl, symbol)}`)\n"
            f"• **Result**: {res_line}\n"
            f"• **Duration**: {duration_str} · `{wib_time_str} WIB`"
        )
        post_persona_discord(event_name, details, metrics)
    except Exception as e:
        logger.error(f"Failed to post Discord alert for outcome #{pid}: {e}")

    return True


def sync_record_lead_lag_prediction(
    symbol: str,
    leader: str,
    direction: str,
    ref_price: float,
    leader_move_pct: float,
    leader_vol_ratio: float,
    follower_move_pct: float,
    disparity: float,
    atr_val: float,
    trigger_source: str = "ARMED_VELOCITY (State 3)",
) -> dict | None:
    """Record new Lead-Lag prediction in the ledger and trigger testnet execution."""
    now_utc_dt = datetime.now(UTC)
    now_utc = now_utc_dt.isoformat(timespec="seconds")
    target_bar_utc = (now_utc_dt + timedelta(seconds=MAX_HOLDING_SECS)).isoformat(timespec="seconds")

    if atr_val <= 0:
        atr_val = ref_price * 0.005

    tp_mult = 0.60
    sl_mult = 0.30
    raw_tp_pts = tp_mult * atr_val
    raw_sl_pts = sl_mult * atr_val

    tp_pts = round(max(ref_price * 0.0045, min(ref_price * 0.0075, raw_tp_pts)), 6)
    sl_pts = round(max(ref_price * 0.0025, min(ref_price * 0.0032, raw_sl_pts)), 6)

    if direction == "bullish":
        tp_price = round(ref_price + tp_pts, 6)
        sl_price = round(ref_price - sl_pts, 6)
    else:
        tp_price = round(ref_price - tp_pts, 6)
        sl_price = round(ref_price + sl_pts, 6)

    noise_thr = round(0.5 * atr_val, 6)
    confidence = round(min(0.95, max(0.55, 0.50 + disparity * 0.20)), 4)
    model_version = "v2.0-fsm-lead-lag"

    params = {
        "model": "fsm_lead_lag_disparity",
        "leader": leader,
        "disparity_pct": round(disparity, 4),
        "tp_mult": tp_mult,
        "sl_mult": sl_mult,
    }
    features = {
        "leader": leader,
        "leader_move_pct": round(leader_move_pct, 4),
        "leader_vol_ratio": round(leader_vol_ratio, 2),
        "follower_move_pct": round(follower_move_pct, 4),
        "disparity_pct": round(disparity, 4),
        "atr": round(atr_val, 6),
    }
    inputs = {
        "ref_price": round(ref_price, 6),
        "target_tp_price": tp_price,
        "stop_loss_price": sl_price,
        "tp_atr_mult": tp_mult,
        "sl_atr_mult": sl_mult,
        "leader": leader,
    }
    notes = (
        f"FSM State 4 Disparity {direction.upper()}: {leader} {leader_move_pct:+.2f}% impulse (vol {leader_vol_ratio:.1f}x) "
        f"➔ {symbol} lag ({follower_move_pct:+.2f}%, disparity {disparity:.2f}%)"
    )

    con = ledger.connect()
    try:
        pid = ledger.record_prediction(
            con,
            horizon="lead_lag",
            target_bar_utc=target_bar_utc,
            scheduled_slot_utc=now_utc,
            direction=direction,
            confidence=confidence,
            ref_price=round(ref_price, 6),
            model_version=model_version,
            params=params,
            features=features,
            inputs=inputs,
            signal_source="lead_lag",
            noise_threshold=noise_thr,
            symbol=symbol,
            leader_symbol=leader,
            notes=notes,
        )
        logger.info(f"Recorded Lead-Lag prediction #{pid} for {symbol} (Leader: {leader}, Dir: {direction})")
    except Exception as e:
        logger.error(f"Failed to record prediction in ledger: {e}", exc_info=True)
        return None
    finally:
        con.close()

    exec_info = None
    if FAPI_EXECUTOR:
        try:
            exec_info = FAPI_EXECUTOR.open_lead_lag_position(
                prediction_id=pid,
                symbol=symbol,
                direction=direction,
                ref_price=ref_price,
                tp_price=tp_price,
                sl_price=sl_price,
            )
        except Exception as e:
            logger.error(f"Failed to execute testnet order for #{pid}: {e}")

    try:
        wib_time = now_utc_dt.astimezone(WIB).strftime("%H:%M:%S")
        dir_emoji = "🐂" if direction == "bullish" else "🐻"
        details = (
            f"BTC baru saja breakout {direction.upper()} {leader_move_pct:+.2f}% dengan volume sweep {leader_vol_ratio:.1f}x. "
            f"Follower {symbol} masih tertinggal dan hanya bergerak {follower_move_pct:+.2f}% (lag < 0.05%), disparity {disparity:.2f}%. "
            f"Eksekusi entry {symbol} di {format_price(ref_price, symbol)}, Target TP {format_price(tp_price, symbol)}, SL {format_price(sl_price, symbol)}."
        )
        testnet_line = ""
        if exec_info:
            testnet_line = (
                f"\n• **Testnet Exec**: `#{exec_info['execution_id']}` · Fill `{exec_info['entry_price']}` "
                f"· Margin `${exec_info['margin_used']}` ({exec_info['margin_pct_capital']}% of $100 bankroll · 5x Isolated)"
            )

        route_line = f"\n• **Trigger Route**: 🎯 **Converted from {trigger_source}**" if trigger_source else ""
        metrics = (
            f"⚡ **FSM STATE 4: DISPARITY EXECUTION**\n"
            f"• **Signal**: `{leader}` ➔ **{symbol}**\n"
            f"• **Leader Impulse**: `{leader}` `{leader_move_pct:+.2f}%` (Vol: `{leader_vol_ratio:.1f}x` baseline)\n"
            f"• **Lagging Target**: `{symbol}` (Moved `{follower_move_pct:+.2f}%` · Lag `< 0.05%`)\n"
            f"• **Disparity Score**: `{disparity:.2f}%`\n"
            f"• **Direction**: {dir_emoji} **{direction.upper()}**\n"
            f"• **Entry (Ref)**: `{format_price(ref_price, symbol)}`\n"
            f"• **Target (TP)**: `{format_price(tp_price, symbol)}` (+1.5× ATR) · **Stop Loss (SL)**: `{format_price(sl_price, symbol)}` (-1.0× ATR)\n"
            f"• **Prediction ID**: `#{pid}` · `{wib_time} WIB`"
            f"{route_line}"
            f"{testnet_line}"
        )
        post_persona_discord("DISPARITY_EXECUTION (State 4)", details, metrics)
    except Exception as e:
        logger.error(f"Failed to post Discord trigger alert: {e}")

    return {
        "id": pid,
        "symbol": symbol,
        "leader_symbol": leader,
        "horizon": "lead_lag",
        "direction": direction,
        "ref_price": ref_price,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "created_utc": now_utc,
        "high_watermark": ref_price,
        "low_watermark": ref_price,
        "noise_threshold": noise_thr,
    }
