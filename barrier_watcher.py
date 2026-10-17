#!/usr/bin/env python3
"""Solana Multi-Coin Lead-Lag Quantitative Research Engine & Barrier Daemon.

Architecture:
1. Leaders: BTCUSDT, ETHUSDT (liquidity drivers & market direction anchors).
2. Target Followers: SOLUSDT, AVAXUSDT, SUIUSDT, NEARUSDT, DOGEUSDT, LINKUSDT.
3. Stream: Binance Combined WebSocket Stream (1s klines for all 8 coins).
4. Lead-Lag Impulse & Disparity Detector:
   - Rolling 1s price & volume window (60s) for all 8 coins.
   - Periodic 1h ATR14 & 1h average volume baseline per coin.
   - Trigger: Leader impulse (>= 0.35% in 30s with vol >= 2.0x baseline).
   - Follower Lag Check: Follower move < 0.15% in that direction.
   - Disparity scoring: disparity = abs(leader_move_pct) - abs(realized_follower_move).
   - Enter top lagging follower with dynamic TP (1.5x ATR) and SL (1.0x ATR).
   - Guard rails: max 1 active trade per follower symbol, 15m cooldown per symbol.
   - Instant Discord alert on entry and on barrier touch (WIN/LOSS).
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

import websockets
import ledger
import predictor
import report_daily
import fetch_market

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))

LEADERS = ["BTCUSDT", "ETHUSDT"]
FOLLOWERS = ["SOLUSDT", "AVAXUSDT", "SUIUSDT", "NEARUSDT", "DOGEUSDT", "LINKUSDT"]
ALL_SYMBOLS = LEADERS + FOLLOWERS

# Binance Combined WebSocket Stream URI
WS_STREAMS = "/".join(s.lower() + "@kline_1s" for s in ALL_SYMBOLS)
WS_URI = f"wss://data-stream.binance.vision:9443/stream?streams={WS_STREAMS}"

DB_PATH = PROJECT_ROOT / "solana.db"
DB_CHECK_INTERVAL_SECS = 5.0

# Detection thresholds
LEADER_IMPULSE_MIN_PCT = 0.35      # 0.35% move in 30s
LEADER_IMPULSE_MIN_VOL_RATIO = 2.0 # 2.0x 30s baseline volume
FOLLOWER_LAG_MAX_PCT = 0.15        # Follower moved < 0.15% in impulse direction
COOLDOWN_SECS = 900.0              # 15 minutes cooldown per follower symbol
MAX_HOLDING_SECS = 7200.0          # 2 hours max holding time-stop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("solana_lead_lag")


def format_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    elif m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def format_price(px: float, symbol: str = "") -> str:
    if px < 0.1:
        return f"${px:.5f}"
    elif px < 2.0:
        return f"${px:.4f}"
    elif px < 100.0:
        return f"${px:.3f}"
    return f"${px:,.2f}"


def get_latest_base_rate(con) -> float:
    try:
        r = con.execute("SELECT base_rate FROM outcomes ORDER BY prediction_id DESC LIMIT 1").fetchone()
        return float(r[0]) if r and r[0] is not None else 0.50
    except Exception:
        return 0.50


def load_active_trades_from_db() -> dict[int, dict]:
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


def sync_record_outcome(trade: dict, exit_reason: str, trigger_price: float, trigger_time: datetime) -> bool:
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
        correct = 1 if exit_reason == "tp_hit" else (0 if exit_reason == "sl_hit" else None)

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

    try:
        wib_time_str = trigger_time.astimezone(WIB).strftime("%H:%M:%S")
        is_win = exit_reason == "tp_hit"

        if is_win:
            title = f"🎯 **TARGET REACHED (Take Profit)** · {symbol} (Leader: {leader})"
            res_line = "• **Result**: ✅ **WIN** (Real-time TP trigger)"
        elif exit_reason == "sl_hit":
            title = f"🛑 **STOP LOSS TRIGGERED (Cut Loss)** · {symbol} (Leader: {leader})"
            res_line = "• **Result**: ❌ **CUT LOSS** (Real-time SL trigger)"
        else:
            title = f"⏰ **TIME STOP EXPIRED** · {symbol} (Leader: {leader})"
            res_line = "• **Result**: ⏱️ **FLAT CUT** (Max holding elapsed)"

        pnl_pts = move if direction == "bullish" else -move
        pnl_pct = move_pct if direction == "bullish" else -move_pct

        msg = (
            f"{title}\n"
            f"• **Prediction**: #{pid} (`{horizon}` {direction.upper()})\n"
            f"• **Entry (Ref)**: `{format_price(ref, symbol)}` → **Exit**: `{format_price(exit_price, symbol)}` "
            f"(`{pnl_pts:+.4f} pts` · `{pnl_pct:+.2f}%`)\n"
            f"• **Trigger Spot**: `{format_price(trigger_price, symbol)}` (TP: `{format_price(tp, symbol)}` · SL: `{format_price(sl, symbol)}`)\n"
            f"{res_line}\n"
            f"• **Duration**: {duration_str} · {wib_time_str} WIB"
        )
        post_res = report_daily.post_discord(msg)
        logger.info(f"Discord alert posted for #{pid}: {post_res}")
    except Exception as e:
        logger.error(f"Failed to post Discord alert for #{pid}: {e}")

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
) -> dict | None:
    now_utc_dt = datetime.now(UTC)
    now_utc = now_utc_dt.isoformat(timespec="seconds")
    now_wib = now_utc_dt.astimezone(WIB).isoformat(timespec="seconds")
    target_bar_utc = (now_utc_dt + timedelta(seconds=MAX_HOLDING_SECS)).isoformat(timespec="seconds")

    if atr_val <= 0:
        atr_val = ref_price * 0.005

    tp_mult = 1.5
    sl_mult = 1.0
    tp_pts = round(tp_mult * atr_val, 6)
    sl_pts = round(sl_mult * atr_val, 6)

    if direction == "bullish":
        tp_price = round(ref_price + tp_pts, 6)
        sl_price = round(ref_price - sl_pts, 6)
    else:
        tp_price = round(ref_price - tp_pts, 6)
        sl_price = round(ref_price + sl_pts, 6)

    noise_thr = round(0.5 * atr_val, 6)
    confidence = round(min(0.95, max(0.55, 0.50 + disparity * 0.20)), 4)
    model_version = "v1.0-lead-lag-multi"

    params = {
        "model": "lead_lag_disparity",
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
        f"Lead-Lag {direction.upper()}: {leader} {leader_move_pct:+.2f}% impulse (vol {leader_vol_ratio:.1f}x) "
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

    try:
        wib_time = now_utc_dt.astimezone(WIB).strftime("%H:%M:%S")
        dir_emoji = "🐂" if direction == "bullish" else "🐻"
        msg = (
            f"⚡ **LEAD-LAG SIGNAL TRIGGERED** · `{leader}` ➔ **{symbol}**\n"
            f"• **Leader Impulse**: `{leader}` **{direction.upper()}** `{leader_move_pct:+.2f}%` in 30s "
            f"(Vol: `{leader_vol_ratio:.1f}x` baseline)\n"
            f"• **Lagging Target**: `{symbol}` (Moved only `{follower_move_pct:+.2f}%` · Disparity: `{disparity:.2f}%`)\n"
            f"• **Direction**: {dir_emoji} **{direction.upper()}**\n"
            f"• **Entry (Ref)**: `{format_price(ref_price, symbol)}`\n"
            f"• **Target (TP)**: `{format_price(tp_price, symbol)}` (+1.5× ATR) · **Stop Loss (SL)**: `{format_price(sl_price, symbol)}` (-1.0× ATR)\n"
            f"• **Slot**: #{pid} · {wib_time} WIB"
        )
        post_res = report_daily.post_discord(msg)
        logger.info(f"Lead-lag signal alert posted to Discord for #{pid}: {post_res}")
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


class MultiCoinLeadLagWatcher:
    def __init__(self):
        self.active_trades: dict[int, dict] = {}
        self.last_db_mtime: float = 0.0
        self.last_db_check: float = 0.0
        self.running = True

        # Rolling 1s candles: 60s window per coin
        self.rolling_1s: dict[str, deque] = {s: deque(maxlen=60) for s in ALL_SYMBOLS}
        self.last_trigger_ts: dict[str, float] = {s: 0.0 for s in FOLLOWERS}
        self.baselines: dict[str, dict] = {
            s: {"atr": 0.0, "baseline_1s_vol": 10.0} for s in ALL_SYMBOLS
        }

    def check_db_update(self, force: bool = False):
        now = time.monotonic()
        if not force and (now - self.last_db_check) < DB_CHECK_INTERVAL_SECS:
            return

        self.last_db_check = now
        try:
            mtime = DB_PATH.stat().st_mtime
        except FileNotFoundError:
            return

        if force or mtime > self.last_db_mtime:
            self.last_db_mtime = mtime
            fresh = load_active_trades_from_db()
            for pid, trade in fresh.items():
                if pid in self.active_trades:
                    trade["high_watermark"] = self.active_trades[pid].get("high_watermark", trade["ref_price"])
                    trade["low_watermark"] = self.active_trades[pid].get("low_watermark", trade["ref_price"])
            self.active_trades = fresh
            logger.info(f"Active trades updated from DB: {len(self.active_trades)} in progress "
                        f"({[(t['id'], t['symbol'], t['direction']) for t in self.active_trades.values()]})")

    def evaluate_1s_kline(self, trade: dict, k: dict) -> tuple[str | None, float]:
        direction = trade["direction"]
        tp = trade["tp_price"]
        sl = trade["sl_price"]

        k_high = float(k["h"])
        k_low = float(k["l"])
        k_open = float(k["o"])
        k_close = float(k["c"])

        trade["high_watermark"] = max(trade.get("high_watermark", k_high), k_high)
        trade["low_watermark"] = min(trade.get("low_watermark", k_low), k_low)

        if direction == "bullish":
            tp_hit = k_high >= tp
            sl_hit = k_low <= sl
            if tp_hit and sl_hit:
                if k_open <= k_close:
                    return ("tp_hit", tp)
                return ("sl_hit", sl)
            elif tp_hit:
                return ("tp_hit", tp)
            elif sl_hit:
                return ("sl_hit", sl)
        else:
            tp_hit = k_low <= tp
            sl_hit = k_high >= sl
            if tp_hit and sl_hit:
                if k_open >= k_close:
                    return ("tp_hit", tp)
                return ("sl_hit", sl)
            elif tp_hit:
                return ("tp_hit", tp)
            elif sl_hit:
                return ("sl_hit", sl)

        # Time-stop check (2 hours)
        try:
            created_dt = datetime.fromisoformat(trade["created_utc"])
            if (datetime.now(UTC) - created_dt).total_seconds() >= MAX_HOLDING_SECS:
                return ("time_stop", k_close)
        except Exception:
            pass

        return (None, k_close)

    def evaluate_lead_lag_opportunity(self, leader_symbol: str) -> dict | None:
        """Evaluate if leader impulse creates a disparity with any lagging follower."""
        leader_hist = list(self.rolling_1s[leader_symbol])
        if len(leader_hist) < 30:
            return None

        # 30-second window on leader
        l_30 = leader_hist[-30:]
        l_start = l_30[0]["open"]
        l_now = l_30[-1]["close"]
        if l_start <= 0:
            return None

        leader_move_pct = (l_now - l_start) / l_start * 100.0
        abs_l_move = abs(leader_move_pct)

        # Check impulse threshold
        if abs_l_move < LEADER_IMPULSE_MIN_PCT:
            return None

        l_vol_30s = sum(x["vol"] for x in l_30)
        baseline_1s = self.baselines[leader_symbol].get("baseline_1s_vol") or 10.0
        l_baseline_30s = max(1e-6, baseline_1s * 30.0)
        vol_ratio = l_vol_30s / l_baseline_30s

        if vol_ratio < LEADER_IMPULSE_MIN_VOL_RATIO:
            return None

        direction = "bullish" if leader_move_pct > 0 else "bearish"
        now_ts = time.time()

        candidates = []
        for follower in FOLLOWERS:
            # Slot guard: max 1 active trade per follower symbol
            if any(t.get("symbol") == follower for t in self.active_trades.values()):
                continue

            # Cooldown guard: 15 minutes per follower symbol
            if (now_ts - self.last_trigger_ts.get(follower, 0.0)) < COOLDOWN_SECS:
                continue

            f_hist = list(self.rolling_1s[follower])
            if len(f_hist) < 30:
                continue

            f_30 = f_hist[-30:]
            f_start = f_30[0]["open"]
            f_now = f_30[-1]["close"]
            if f_start <= 0:
                continue

            follower_move_pct = (f_now - f_start) / f_start * 100.0

            # Follower move in direction of impulse
            realized_dir_move = follower_move_pct if direction == "bullish" else -follower_move_pct

            # Lag check: follower has moved < 0.15% in that direction
            if realized_dir_move >= FOLLOWER_LAG_MAX_PCT:
                continue

            # Disparity: expected impulse minus realized move
            disparity = abs_l_move - max(0.0, realized_dir_move)
            if disparity <= 0.10:
                continue

            atr = self.baselines[follower].get("atr") or (f_now * 0.005)
            candidates.append({
                "follower": follower,
                "leader": leader_symbol,
                "direction": direction,
                "ref_price": f_now,
                "leader_move_pct": leader_move_pct,
                "leader_vol_ratio": vol_ratio,
                "follower_move_pct": follower_move_pct,
                "disparity": disparity,
                "atr": atr,
            })

        if not candidates:
            return None

        # Pick candidate with greatest disparity
        candidates.sort(key=lambda c: c["disparity"], reverse=True)
        return candidates[0]

    async def update_baseline_periodically(self):
        """Fetch 1h ATR14 & 1h average volume baseline for all 8 coins every 15m."""
        while self.running:
            for symbol in ALL_SYMBOLS:
                try:
                    candles = await asyncio.to_thread(fetch_market.fetch_candles, "1h", 100, symbol)
                    if candles and len(candles) >= 20:
                        highs = [c["high"] for c in candles]
                        lows = [c["low"] for c in candles]
                        closes = [c["close"] for c in candles]
                        atr_val = predictor.atr(highs, lows, closes, period=14)
                        if atr_val is None or atr_val <= 0:
                            atr_val = closes[-1] * 0.005

                        avg_1h_vol = sum(c["volume"] for c in candles[-24:]) / min(24, len(candles))
                        baseline_1s = max(1e-6, avg_1h_vol / 3600.0)

                        self.baselines[symbol] = {
                            "atr": atr_val,
                            "baseline_1s_vol": baseline_1s,
                        }
                        logger.info(f"Baseline updated for {symbol:<8}: ATR14={format_price(atr_val, symbol)} | Vol={baseline_1s:.2f}/s")
                except Exception as e:
                    logger.warning(f"Failed to update baseline for {symbol}: {e}")
                await asyncio.sleep(1.0)
            await asyncio.sleep(900)

    async def reconcile_startup(self):
        try:
            now = datetime.now(UTC)
            to_remove = []
            for pid, trade in list(self.active_trades.items()):
                symbol = trade.get("symbol", "SOLUSDT")
                spot_data = await asyncio.to_thread(fetch_market.fetch_spot, symbol)
                current_spot = spot_data["price"]
                direction = trade["direction"]
                tp = trade["tp_price"]
                sl = trade["sl_price"]
                exit_reason = None

                if direction == "bullish":
                    if current_spot >= tp:
                        exit_reason = "tp_hit"
                    elif current_spot <= sl:
                        exit_reason = "sl_hit"
                else:
                    if current_spot <= tp:
                        exit_reason = "tp_hit"
                    elif current_spot >= sl:
                        exit_reason = "sl_hit"

                if exit_reason:
                    logger.info(f"Reconciling #{pid} ({symbol} {exit_reason}) at {format_price(current_spot, symbol)}")
                    ok = await asyncio.to_thread(sync_record_outcome, trade, exit_reason, current_spot, now)
                    if ok:
                        to_remove.append(pid)

            for pid in to_remove:
                self.active_trades.pop(pid, None)
        except Exception as e:
            logger.error(f"Error during reconcile startup: {e}")

    async def run_stream(self):
        while self.running:
            try:
                logger.info(f"Connecting to Binance Combined WebSocket Stream: {WS_URI}")
                async with websockets.connect(WS_URI, ping_interval=20, ping_timeout=15) as ws:
                    logger.info(f"Connected to stream. Monitoring 8 coins: Leaders={LEADERS}, Followers={FOLLOWERS}")
                    async for raw_msg in ws:
                        if not self.running:
                            break

                        self.check_db_update()
                        msg = json.loads(raw_msg)
                        data = msg.get("data")
                        if not data:
                            continue

                        k = data.get("k")
                        symbol = data.get("s")
                        if not k or not symbol or symbol not in self.rolling_1s:
                            continue

                        # 1. Store in rolling window
                        self.rolling_1s[symbol].append({
                            "open": float(k["o"]),
                            "high": float(k["h"]),
                            "low": float(k["l"]),
                            "close": float(k["c"]),
                            "vol": float(k["v"]),
                            "ts": k["t"],
                        })

                        # 2. If Leader kline -> evaluate Lead-Lag impulse & disparity
                        if symbol in LEADERS:
                            opp = self.evaluate_lead_lag_opportunity(symbol)
                            if opp:
                                follower = opp["follower"]
                                self.last_trigger_ts[follower] = time.time()
                                logger.info(
                                    f"⚡ LEAD-LAG TRIGGER: {opp['leader']} {opp['direction']} {opp['leader_move_pct']:+.2f}% "
                                    f"➔ {follower} lag (disparity: {opp['disparity']:.2f}%)"
                                )
                                trade = await asyncio.to_thread(
                                    sync_record_lead_lag_prediction,
                                    opp["follower"],
                                    opp["leader"],
                                    opp["direction"],
                                    opp["ref_price"],
                                    opp["leader_move_pct"],
                                    opp["leader_vol_ratio"],
                                    opp["follower_move_pct"],
                                    opp["disparity"],
                                    opp["atr"],
                                )
                                if trade:
                                    self.active_trades[trade["id"]] = trade

                        # 3. If Follower kline -> check active trades for barrier touch (TP/SL)
                        if self.active_trades:
                            concluded = []
                            now = datetime.now(UTC)
                            for pid, trade in list(self.active_trades.items()):
                                if trade.get("symbol") == symbol:
                                    exit_reason, trigger_px = self.evaluate_1s_kline(trade, k)
                                    if exit_reason:
                                        concluded.append((trade, exit_reason, trigger_px))

                            for trade, exit_reason, trigger_px in concluded:
                                pid = trade["id"]
                                self.active_trades.pop(pid, None)
                                asyncio.create_task(
                                    asyncio.to_thread(sync_record_outcome, trade, exit_reason, trigger_px, now)
                                )

            except Exception as e:
                logger.error(f"WebSocket error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5.0)

    def stop(self):
        self.running = False


async def main():
    watcher = MultiCoinLeadLagWatcher()
    watcher.check_db_update(force=True)
    await watcher.reconcile_startup()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, watcher.stop)
        except NotImplementedError:
            pass

    await asyncio.gather(
        watcher.run_stream(),
        watcher.update_baseline_periodically(),
    )


if __name__ == "__main__":
    asyncio.run(main())
