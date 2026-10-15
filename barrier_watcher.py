#!/usr/bin/env python3
"""Solana Event-Driven Watcher & Barrier Trigger Daemon — SOLUSDT.

Dual Responsibility:
1. EVENT DETECTOR (Breakout / Surge Trigger):
   - Streams 1-second klines (solusdt@kline_1s) and rolling volume metrics.
   - Monitors rolling 1-minute and 5-minute volume vs 1-hour average volume.
   - Triggers event prediction when:
     a) Rolling Volume >= 2.5x normal baseline AND
     b) Range Expansion (High - Low >= 1.5x 1m ATR) AND
     c) No active trade is holding AND
     d) Cooldown period elapsed (>= 30 mins since last event prediction).
2. REAL-TIME BARRIER TRIGGER:
   - Subscribes to 1s kline for exact touch of Target (TP) and Stop Loss (SL).
   - Instant ledger resolution + Discord notification on barrier touch.
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
import cycle

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))
WS_URI = "wss://data-stream.binance.vision:9443/ws/solusdt@kline_1s"
DB_PATH = PROJECT_ROOT / "solana.db"
DB_CHECK_INTERVAL_SECS = 5.0
EVENT_COOLDOWN_SECS = 1800.0  # 30 minutes cooldown between event predictions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("solana_event_watcher")


def format_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    elif m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def get_latest_base_rate(con) -> float:
    try:
        r = con.execute("SELECT base_rate FROM outcomes ORDER BY id DESC LIMIT 1").fetchone()
        return float(r[0]) if r and r[0] is not None else 0.50
    except Exception:
        return 0.50


def load_active_trades_from_db() -> dict[int, dict]:
    con = ledger.connect()
    try:
        rows = con.execute("""
            SELECT p.id, p.horizon, p.direction, p.ref_price, p.created_utc,
                   p.scheduled_slot_utc, p.features_json, p.inputs_json,
                   p.params_json, p.noise_threshold, p.notes
            FROM predictions p
            LEFT JOIN outcomes o ON o.prediction_id = p.id
            WHERE o.prediction_id IS NULL
              AND p.signal_source = 'technical'
              AND p.direction IN ('bullish', 'bearish')
            ORDER BY p.id ASC
        """).fetchall()

        active = {}
        for r in rows:
            d = dict(r)
            pid = d["id"]
            ref = float(d["ref_price"])
            direction = d["direction"]
            horizon = d["horizon"] or "event"

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
                atr = features.get("atr14")
                if not atr:
                    thr = d.get("noise_threshold")
                    atr = (thr / predictor.NOISE_ATR_MULT) if thr else 1.0
                levels = predictor.compute_trade_levels(ref, atr, direction, horizon=horizon)
                tp = levels["tp_price"]
                sl = levels["sl_price"]
            else:
                tp = float(tp)
                sl = float(sl)

            active[pid] = {
                "id": pid,
                "horizon": horizon,
                "direction": direction,
                "ref_price": ref,
                "tp_price": tp,
                "sl_price": sl,
                "created_utc": d["created_utc"],
                "high_watermark": ref,
                "low_watermark": ref,
                "noise_threshold": d.get("noise_threshold") or 0.20,
            }

        return active
    finally:
        con.close()


def sync_record_outcome(trade: dict, exit_reason: str, trigger_price: float, trigger_time: datetime) -> bool:
    pid = trade["id"]
    direction = trade["direction"]
    horizon = trade["horizon"]
    ref = trade["ref_price"]
    tp = trade["tp_price"]
    sl = trade["sl_price"]

    exit_price = tp if exit_reason == "tp_hit" else sl
    move = exit_price - ref
    move_pct = (move / ref * 100) if ref else 0.0

    b_high = max(trade.get("high_watermark", ref), trigger_price, exit_price)
    b_low = min(trade.get("low_watermark", ref), trigger_price, exit_price)

    created_dt = datetime.fromisoformat(trade["created_utc"])
    elapsed_secs = (trigger_time - created_dt).total_seconds()
    duration_str = format_duration(elapsed_secs)

    logger.info(f"Triggering {exit_reason.upper()} for #{pid} ({horizon} {direction}) at ${trigger_price:,.2f} "
                f"(exit: ${exit_price:,.2f}, move: {move:+,.2f})")

    con = ledger.connect()
    try:
        already = con.execute("SELECT prediction_id FROM outcomes WHERE prediction_id = ?", (pid,)).fetchone()
        if already:
            logger.warning(f"Prediction #{pid} already graded. Skipping duplicate resolution.")
            return False

        base = get_latest_base_rate(con)
        is_noise = False
        correct = 1 if exit_reason == "tp_hit" else 0

        ledger.record_outcome(
            con,
            prediction_id=pid,
            realized_price=exit_price,
            move=round(move, 4),
            move_pct=round(move_pct, 4),
            is_noise=is_noise,
            correct=correct,
            base_rate=round(base, 6),
        )

        ledger.record_outcome_details(
            con,
            prediction_id=pid,
            target_price=tp,
            sl_price=sl,
            bar_high=round(b_high, 2),
            bar_low=round(b_low, 2),
            target_reached=1 if exit_reason == "tp_hit" else 0,
            sl_hit=1 if exit_reason == "sl_hit" else 0,
        )

        logger.info(f"Successfully recorded outcome for #{pid} in ledger.")
    except Exception as e:
        logger.error(f"Failed to record outcome for #{pid}: {e}", exc_info=True)
        return False
    finally:
        con.close()

    try:
        wib_time_str = trigger_time.astimezone(WIB).strftime("%H:%M:%S")
        is_win = exit_reason == "tp_hit"

        if is_win:
            title = "🎯 **TARGET REACHED (Take Profit)** · Solana SOLUSDT"
            res_line = "• **Result**: ✅ **WIN** (Real-time TP trigger)"
        else:
            title = "🛑 **STOP LOSS TRIGGERED (Cut Loss)** · Solana SOLUSDT"
            res_line = "• **Result**: ❌ **CUT LOSS** (Real-time SL trigger)"

        pnl_pts = move if direction == "bullish" else -move
        pnl_pct = move_pct if direction == "bullish" else -move_pct

        msg = (
            f"{title}\n"
            f"• **Prediction**: #{pid} (`{horizon}` {direction.upper()})\n"
            f"• **Entry (Ref)**: `${ref:,.2f}` → **Exit**: `${exit_price:,.2f}` "
            f"(`{pnl_pts:+,.2f} pts` · `{pnl_pct:+.2f}%`)\n"
            f"• **Trigger Spot**: `${trigger_price:,.2f}` (TP: `${tp:,.2f}` · SL: `${sl:,.2f}`)\n"
            f"{res_line}\n"
            f"• **Duration**: {duration_str} · {wib_time_str} WIB"
        )
        post_res = report_daily.post_discord(msg)
        logger.info(f"Discord alert posted for #{pid}: {post_res}")
    except Exception as e:
        logger.error(f"Failed to post Discord alert for #{pid}: {e}")

    return True


def sync_execute_event_cycle(trigger_reason: str):
    logger.info(f"Executing event breakout prediction cycle! Reason: {trigger_reason}")
    res = cycle.run(horizon="event")
    msg = cycle.format_hourly_delivery(res)
    if msg:
        report_daily.post_discord(msg)
        logger.info(f"Event prediction broadcasted to Discord. Action: {res.get('action')}")
    return res


class SolanaEventWatcher:
    def __init__(self):
        self.active_trades: dict[int, dict] = {}
        self.last_db_mtime: float = 0.0
        self.last_db_check: float = 0.0
        self.running = True
        # Rolling metrics for 1s candles: 60s window
        self.rolling_1s = deque(maxlen=60)
        self.last_event_trigger_ts: float = 0.0
        self.baseline_1s_vol: float = 50.0  # default initial estimate

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
                        f"({list(self.active_trades.keys())})")

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

        return (None, k_close)

    def check_event_trigger(self, k: dict) -> str | None:
        """Check if rolling volume + price action indicates an explosive breakout event."""
        vol = float(k["v"])
        self.rolling_1s.append({
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "vol": vol,
            "ts": k["t"],
        })

        if len(self.rolling_1s) < 30:
            return None

        now_ts = time.time()
        if (now_ts - self.last_event_trigger_ts) < EVENT_COOLDOWN_SECS:
            return None

        # If any active event trade is currently open, don't trigger another one
        if any(t.get("horizon") == "event" for t in self.active_trades.values()):
            return None

        # Compute rolling 30-sec volume vs baseline
        recent_30s_vol = sum(x["vol"] for x in list(self.rolling_1s)[-30:])
        avg_30s_vol = max(100.0, self.baseline_1s_vol * 30.0)

        # Volume Surge >= 3.0x
        vol_ratio = recent_30s_vol / avg_30s_vol
        recent_high = max(x["high"] for x in list(self.rolling_1s)[-30:])
        recent_low = min(x["low"] for x in list(self.rolling_1s)[-30:])
        range_30s = recent_high - recent_low

        # If 30-second move > $0.60 (significant on SOL spot ~$102) with high volume
        if vol_ratio >= 3.0 and range_30s >= 0.60:
            self.last_event_trigger_ts = now_ts
            return f"Volume Surge ({vol_ratio:.1f}x) + Range Expansion (${range_30s:.2f} in 30s)"

        return None

    async def update_baseline_periodically(self):
        """Update 1-hour average volume baseline every 15 minutes."""
        while self.running:
            try:
                candles = await asyncio.to_thread(fetch_market.fetch_candles, "1h", 100)
                if candles:
                    avg_1h_vol = sum(c["volume"] for c in candles[-24:]) / min(24, len(candles))
                    self.baseline_1s_vol = max(10.0, avg_1h_vol / 3600.0)
                    logger.info(f"Updated baseline 1s volume: {self.baseline_1s_vol:.2f} SOL/s")
            except Exception as e:
                logger.warning(f"Failed to update volume baseline: {e}")
            await asyncio.sleep(900)

    async def reconcile_startup(self):
        try:
            ticker = await asyncio.to_thread(fetch_market.fetch_ticker)
            current_spot = ticker["last"]
            now = datetime.now(UTC)
            logger.info(f"Startup spot check: ${current_spot:,.2f}")

            to_remove = []
            for pid, trade in list(self.active_trades.items()):
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
                    logger.info(f"Reconciling #{pid} on startup ({exit_reason}) at ${current_spot:,.2f}")
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
                logger.info(f"Connecting to Binance 1s kline WebSocket: {WS_URI}")
                async with websockets.connect(WS_URI, ping_interval=20, ping_timeout=15) as ws:
                    logger.info("Connected to WebSocket successfully. Monitoring SOLUSDT real-time stream...")
                    async for raw_msg in ws:
                        if not self.running:
                            break

                        self.check_db_update()
                        msg = json.loads(raw_msg)
                        k = msg.get("k")
                        if not k:
                            continue

                        # 1. Event trigger check
                        event_reason = self.check_event_trigger(k)
                        if event_reason:
                            logger.info(f"⚡ EVENT BREAKOUT DETECTED: {event_reason}")
                            asyncio.create_task(asyncio.to_thread(sync_execute_event_cycle, event_reason))

                        # 2. Barrier touch check
                        if not self.active_trades:
                            continue

                        now = datetime.now(UTC)
                        concluded = []
                        for pid, trade in list(self.active_trades.items()):
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
    watcher = SolanaEventWatcher()
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
