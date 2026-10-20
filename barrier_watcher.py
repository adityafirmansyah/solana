#!/usr/bin/env python3
"""Solana Multi-Coin Lead-Lag Quantitative Research Engine & 5-State Barrier Daemon.

FSM Architecture (5 States):
- State 0: IDLE (Monitoring combined 1s feed, calculating rolling volatility & baselines)
- State 1: SQUEEZE_DETECTED (BTC 15m range <= 0.22% & volume drying up)
- State 2: ACCUMULATION_BUILDUP (Volume surge >= 2.0x, taker pressure imbalance >= 60%)
- State 3: ARMED_VELOCITY (BTC velocity move 0.08-0.12% with ETH validation)
- State 4: DISPARITY_EXECUTION (Target follower lagged, trigger asymmetric bracket trade)
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

import websockets

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import fetch_market
import predictor
from barrier_notifications import (
    format_price,
    format_duration,
    post_persona_discord,
)
from barrier_db import (
    load_active_trades_from_db,
    sync_record_outcome,
    sync_record_lead_lag_prediction,
    FAPI_EXECUTOR,
    MAX_HOLDING_SECS,
    DB_PATH,
)
from barrier_lead_lag import (
    EngineState,
    evaluate_1s_kline,
    find_lagging_follower_candidate,
)
from barrier_fsm import LeadLagFSM

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("solana_lead_lag")

LEADERS = ["BTCUSDT", "ETHUSDT"]
FOLLOWERS = ["SOLUSDT", "AVAXUSDT", "SUIUSDT", "NEARUSDT", "DOGEUSDT", "LINKUSDT"]
ALL_SYMBOLS = LEADERS + FOLLOWERS
WS_URI = "wss://data-stream.binance.vision:9443/stream?streams=" + "/".join(f"{s.lower()}@kline_1s" for s in ALL_SYMBOLS)
DB_CHECK_INTERVAL_SECS = 2.0


class MultiCoinLeadLagWatcher:
    def __init__(self):
        self.active_trades: dict[int, dict] = {}
        self.last_db_mtime: float = 0.0
        self.last_db_check: float = 0.0
        self.running = True

        self.state: EngineState = EngineState.IDLE
        self.state_entered_ts: float = time.time()
        self.accumulation_bias: str = "bullish"
        self.accumulation_taker_ratio: float = 0.50
        self.accumulation_vol_ratio: float = 1.0
        self.state_3_armed_ts: float = 0.0
        self.armed_direction: str = "bullish"
        self.state_3_reasons: list[str] = []

        self.last_state_1_notify_ts: float = 0.0
        self.last_state_2_notify_ts: float = 0.0
        self.last_state_3_notify_ts: float = 0.0

        self.rolling_1s: dict[str, deque] = {
            "BTCUSDT": deque(maxlen=900),
            "ETHUSDT": deque(maxlen=300),
            **{s: deque(maxlen=120) for s in FOLLOWERS}
        }
        self.last_trigger_ts: dict[str, float] = {s: 0.0 for s in FOLLOWERS}
        self.last_global_trigger_ts: float = 0.0
        self.baselines: dict[str, dict] = {
            s: {"atr": 0.0, "baseline_1s_vol": 10.0} for s in ALL_SYMBOLS
        }
        self.fsm = LeadLagFSM(self)

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
            logger.info(f"Active trades updated: {len(self.active_trades)} in progress "
                        f"({[(t['id'], t['symbol'], t['direction']) for t in self.active_trades.values()]})")

    def evaluate_1s_kline(self, trade: dict, k: dict) -> tuple[str | None, float]:
        return evaluate_1s_kline(trade, k)

    def find_best_lagging_follower(self, direction: str, abs_btc_move: float, btc_move_pct: float, vol_ratio: float) -> dict | None:
        return find_lagging_follower_candidate(
            direction, abs_btc_move, btc_move_pct, vol_ratio,
            self.active_trades, self.last_global_trigger_ts, self.last_trigger_ts,
            self.rolling_1s, self.baselines, FOLLOWERS, FAPI_EXECUTOR,
            self.state_3_reasons,
        )

    def evaluate_fsm(self):
        self.fsm.evaluate()

    async def update_baseline_periodically(self):
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

    async def seed_startup_history(self):
        try:
            logger.info("Seeding initial 15m kline history for BTCUSDT and ETHUSDT FSM warmup...")
            btc_raw = await asyncio.to_thread(fetch_market.fetch_candles, "1m", 60, "BTCUSDT")
            btc_candles = btc_raw[-15:] if btc_raw else []
            if btc_candles:
                for c in btc_candles:
                    t_ms = int(c["open_time"])
                    vol_sample = c["volume"] / 60.0
                    for offset_s in range(0, 60):
                        self.rolling_1s["BTCUSDT"].append({
                            "open": c["open"], "high": c["high"], "low": c["low"],
                            "close": c["close"], "vol": vol_sample,
                            "taker_buy_vol": vol_sample * 0.5, "ts": t_ms + offset_s * 1000,
                        })
                logger.info(f"Seeded {len(self.rolling_1s['BTCUSDT'])} history bars for BTCUSDT.")

            eth_raw = await asyncio.to_thread(fetch_market.fetch_candles, "1m", 60, "ETHUSDT")
            eth_candles = eth_raw[-5:] if eth_raw else []
            if eth_candles:
                for c in eth_candles:
                    t_ms = int(c["open_time"])
                    vol_sample = c["volume"] / 60.0
                    for offset_s in range(0, 60):
                        self.rolling_1s["ETHUSDT"].append({
                            "open": c["open"], "high": c["high"], "low": c["low"],
                            "close": c["close"], "vol": vol_sample,
                            "taker_buy_vol": vol_sample * 0.5, "ts": t_ms + offset_s * 1000,
                        })
                logger.info(f"Seeded {len(self.rolling_1s['ETHUSDT'])} history bars for ETHUSDT.")
        except Exception as e:
            logger.warning(f"Failed to seed history: {e}")

    async def reconcile_startup(self):
        if not self.active_trades:
            return
        logger.info(f"Reconciling {len(self.active_trades)} active trades on startup...")
        now = datetime.now(UTC)
        to_remove = []
        try:
            for pid, trade in list(self.active_trades.items()):
                symbol = trade["symbol"]
                direction = trade["direction"]
                tp = trade["tp_price"]
                sl = trade["sl_price"]

                spot_res = await asyncio.to_thread(fetch_market.fetch_spot, symbol)
                current_spot = spot_res.get("price", trade["ref_price"])

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

                if not exit_reason:
                    try:
                        created_dt = datetime.fromisoformat(trade["created_utc"])
                        if (now - created_dt).total_seconds() >= MAX_HOLDING_SECS:
                            exit_reason = "time_stop"
                    except Exception:
                        pass

                if exit_reason:
                    logger.info(f"Reconciling #{pid} ({symbol} {exit_reason}) at {format_price(current_spot, symbol)}")
                    ok = await asyncio.to_thread(sync_record_outcome, trade, exit_reason, current_spot, now)
                    if ok:
                        to_remove.append(pid)

            for pid in to_remove:
                self.active_trades.pop(pid, None)
        except Exception as e:
            logger.error(f"Error during reconcile startup: {e}")

        if FAPI_EXECUTOR:
            try:
                positions = FAPI_EXECUTOR._request("GET", "/fapi/v2/positionRisk")
                if isinstance(positions, list):
                    for p in positions:
                        amt = float(p.get("positionAmt", 0.0))
                        sym = p.get("symbol", "")
                        if amt != 0 and sym in FOLLOWERS:
                            if not any(t.get("symbol") == sym for t in self.active_trades.values()):
                                close_side = "BUY" if amt < 0 else "SELL"
                                logger.warning(f"Reconcile: Flattening orphaned {sym} ({amt})...")
                                FAPI_EXECUTOR._request("POST", "/fapi/v1/order", {
                                    "symbol": sym, "side": close_side, "type": "MARKET",
                                    "quantity": abs(amt), "positionSide": "BOTH",
                                })
            except Exception as e:
                logger.error(f"Error checking exchange positions in reconcile: {e}")

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
                        data = msg.get("data", msg)
                        k = data.get("k")
                        symbol = data.get("s")
                        if not k or not symbol or symbol not in self.rolling_1s:
                            continue

                        self.rolling_1s[symbol].append({
                            "open": float(k["o"]), "high": float(k["h"]), "low": float(k["l"]),
                            "close": float(k["c"]), "vol": float(k["v"]),
                            "taker_buy_vol": float(k.get("V", 0.0)), "ts": k["t"],
                        })

                        if self.active_trades and symbol in FOLLOWERS:
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

                        if symbol == "BTCUSDT":
                            self.evaluate_fsm()

            except Exception as e:
                logger.error(f"WebSocket error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5.0)

    async def heartbeat_loop(self):
        while self.running:
            try:
                if FAPI_EXECUTOR and self.active_trades:
                    active_symbols: set[str] = {str(t["symbol"]) for t in self.active_trades.values() if t.get("symbol")}
                    for sym in active_symbols:
                        await asyncio.to_thread(FAPI_EXECUTOR.dead_man_heartbeat, sym, 60000)
            except Exception as e:
                logger.debug(f"Heartbeat tick error: {e}")
            await asyncio.sleep(20.0)

    def stop(self):
        self.running = False


async def main():
    watcher = MultiCoinLeadLagWatcher()
    watcher.check_db_update(force=True)
    await watcher.reconcile_startup()
    await watcher.seed_startup_history()

    if FAPI_EXECUTOR:
        logger.info("Preconfiguring margin & leverage for all follower symbols at startup...")
        await asyncio.to_thread(FAPI_EXECUTOR.preconfigure_symbols, FOLLOWERS)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, watcher.stop)
        except NotImplementedError:
            pass

    await asyncio.gather(
        watcher.run_stream(),
        watcher.update_baseline_periodically(),
        watcher.heartbeat_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
