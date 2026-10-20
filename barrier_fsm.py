#!/usr/bin/env python3
"""5-State Finite State Machine Engine for Solana Lead-Lag Watcher.

States:
- State 0: IDLE (Monitoring combined 1s feed)
- State 1: SQUEEZE_DETECTED (BTC 15m range <= 0.22% & dry volume)
- State 2: ACCUMULATION_BUILDUP (Volume surge >= 2.0x, taker pressure imbalance)
- State 3: ARMED_VELOCITY (BTC velocity move 0.08-0.12% with ETH validation)
- State 4: DISPARITY_EXECUTION (Target follower lagged, trigger asymmetric bracket trade)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
import logging
import time
from typing import Any

from barrier_lead_lag import EngineState
from barrier_notifications import format_price, post_persona_discord
from barrier_db import sync_record_lead_lag_prediction

logger = logging.getLogger("solana_barrier_fsm")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] fsm: %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))

# FSM Threshold Constants
SQUEEZE_RANGE_MAX_PCT = 0.22         # <= 0.22% range across 15 minutes
SQUEEZE_VOL_MAX_RATIO = 0.80         # Volume drying up (< 80% baseline)
ACCUMULATION_MIN_VOL_RATIO = 2.0     # Volume surge >= 2.0x baseline
TAKER_RATIO_BULLISH = 0.60           # >= 60% buyer volume
TAKER_RATIO_BEARISH = 0.40           # <= 40% buyer volume (heavy selling)
ARMED_VELOCITY_MIN_PCT = 0.08        # >= 0.08% move in 20s
ARMED_VELOCITY_MAX_PCT = 0.14        # <= 0.14% move in 20s
DISPARITY_IMPULSE_MIN_PCT = 0.10     # Breakthrough impulse >= 0.10%
STATE_3_TTL_SECS = 45.0              # State 3 expires after 45s
STATE_1_COOLDOWN_SECS = 1800.0       # 30m debounce for State 1 alerts
STATE_2_COOLDOWN_SECS = 900.0        # 15m debounce for State 2 alerts
STATE_3_DEBOUNCE_SECS = 180.0        # 3m debounce for State 3 alerts


class LeadLagFSM:
    """Manages state transitions and trigger evaluation for the lead-lag watcher."""

    def __init__(self, watcher: Any):
        self.w = watcher

    def transition_to_squeeze(self, range_pct: float, vol_ratio: float, btc_spot: float):
        self.w.state = EngineState.SQUEEZE_DETECTED
        self.w.state_entered_ts = time.time()
        logger.info(f"FSM State 1 (SQUEEZE_DETECTED): BTC 15m range={range_pct:.3f}%, vol={vol_ratio:.2f}x")

        now = time.time()
        if (now - self.w.last_state_1_notify_ts) >= STATE_1_COOLDOWN_SECS:
            self.w.last_state_1_notify_ts = now
            wib_str = datetime.now(UTC).astimezone(WIB).strftime("%H:%M:%S")
            details = (
                f"BTC 15m range kejepit di {range_pct:.3f}% (threshold <= {SQUEEZE_RANGE_MAX_PCT}%), "
                f"volume mengering ke {vol_ratio:.2f}x baseline. Spot: {format_price(btc_spot, 'BTCUSDT')}."
            )
            metrics = (
                f"📦 **FSM STATE 1: SQUEEZE DETECTED**\n"
                f"• **BTC 15m Range**: `{range_pct:.3f}%` (Threshold: `≤ {SQUEEZE_RANGE_MAX_PCT}%`)\n"
                f"• **Volume Ratio**: `{vol_ratio:.2f}x` baseline (Volume drying up)\n"
                f"• **BTC Spot**: `{format_price(btc_spot, 'BTCUSDT')}`\n"
                f"• **Timestamp**: `{wib_str} WIB`"
            )
            asyncio.create_task(asyncio.to_thread(post_persona_discord, "SQUEEZE_DETECTED (State 1)", details, metrics))

    def transition_to_accumulation(self, bias: str, taker_ratio: float, vol_ratio: float, btc_spot: float):
        self.w.state = EngineState.ACCUMULATION_BUILDUP
        self.w.state_entered_ts = time.time()
        self.w.accumulation_bias = bias
        self.w.accumulation_taker_ratio = taker_ratio
        self.w.accumulation_vol_ratio = vol_ratio
        logger.info(f"FSM State 2 (ACCUMULATION_BUILDUP): Bias={bias.upper()}, Taker={taker_ratio*100:.1f}%, Vol={vol_ratio:.2f}x")

        now = time.time()
        if (now - self.w.last_state_2_notify_ts) >= STATE_2_COOLDOWN_SECS:
            self.w.last_state_2_notify_ts = now
            wib_str = datetime.now(UTC).astimezone(WIB).strftime("%H:%M:%S")
            dir_emoji = "🐂" if bias == "bullish" else "🐻"
            details = (
                f"BTC akumulasi {bias.upper()}: volume lonjak {vol_ratio:.2f}x baseline (threshold >= {ACCUMULATION_MIN_VOL_RATIO}x), "
                f"Taker Buy/Sell ratio {taker_ratio*100:.1f}%. Spot: {format_price(btc_spot, 'BTCUSDT')}."
            )
            metrics = (
                f"🔋 **FSM STATE 2: ACCUMULATION BUILDUP**\n"
                f"• **Bias**: **{bias.upper()}** {dir_emoji}\n"
                f"• **Taker Ratio**: `{taker_ratio * 100:.1f}%` (Threshold: `≥ 60%` buy or `≤ 40%` sell)\n"
                f"• **Volume Surge**: `{vol_ratio:.2f}x` baseline (Threshold: `≥ 2.0x`)\n"
                f"• **BTC Spot**: `{format_price(btc_spot, 'BTCUSDT')}`\n"
                f"• **Timestamp**: `{wib_str} WIB`"
            )
            asyncio.create_task(asyncio.to_thread(post_persona_discord, "ACCUMULATION_BUILDUP (State 2)", details, metrics))

    def transition_to_armed_velocity(self, direction: str, btc_move: float, eth_move: float):
        self.w.state = EngineState.ARMED_VELOCITY
        self.w.state_entered_ts = time.time()
        self.w.state_3_armed_ts = time.time()
        self.w.armed_direction = direction
        self.w.state_3_reasons = []
        logger.info(f"FSM State 3 (ARMED_VELOCITY): Dir={direction.upper()}, BTC move={btc_move:+.3f}%, ETH confirm={eth_move:+.3f}%, TTL=45s")

        now = time.time()
        if (now - self.w.last_state_3_notify_ts) >= STATE_3_DEBOUNCE_SECS:
            self.w.last_state_3_notify_ts = now
            wib_str = datetime.now(UTC).astimezone(WIB).strftime("%H:%M:%S")
            dir_emoji = "🐂" if direction == "bullish" else "🐻"
            details = (
                f"BTC velocity impulse {btc_move:+.3f}% dalam 20s (threshold {ARMED_VELOCITY_MIN_PCT}% - {ARMED_VELOCITY_MAX_PCT}%), "
                f"ETH konfirmasi {eth_move:+.3f}%. Direction: {direction.upper()}. Armed TTL: 45 detik."
            )
            metrics = (
                f"🚨 **FSM STATE 3: ARMED VELOCITY**\n"
                f"• **BTC Velocity**: `{btc_move:+.3f}%` in 20s (Threshold: `0.08% - 0.12%`)\n"
                f"• **ETH Direction Confirmation**: `{eth_move:+.3f}%` in 20s\n"
                f"• **Armed Bias**: **{direction.upper()}** {dir_emoji}\n"
                f"• **Transmission Window**: `45s active TTL`\n"
                f"• **Timestamp**: `{wib_str} WIB`"
            )
            asyncio.create_task(asyncio.to_thread(post_persona_discord, "ARMED_VELOCITY (State 3)", details, metrics))

    def execute_state_4(self, opp: dict):
        trigger_source = "ARMED_VELOCITY (State 3)" if self.w.state == EngineState.ARMED_VELOCITY else "FAST_PATH_BURST"
        self.w.state = EngineState.DISPARITY_EXECUTION
        self.w.state_entered_ts = time.time()
        follower = opp["follower"]
        now_ts = time.time()
        self.w.last_trigger_ts[follower] = now_ts
        self.w.last_global_trigger_ts = now_ts
        logger.info(
            f"⚡ FSM State 4 (DISPARITY_EXECUTION): {opp['leader']} {opp['direction']} {opp['leader_move_pct']:+.2f}% "
            f"➔ {follower} lag (disparity: {opp['disparity']:.2f}%) via {trigger_source}"
        )
        trade = sync_record_lead_lag_prediction(
            opp["follower"],
            opp["leader"],
            opp["direction"],
            opp["ref_price"],
            opp["leader_move_pct"],
            opp["leader_vol_ratio"],
            opp["follower_move_pct"],
            opp["disparity"],
            opp["atr"],
            trigger_source=trigger_source,
        )
        if trade:
            self.w.active_trades[trade["id"]] = trade

        self.w.state = EngineState.IDLE
        self.w.state_entered_ts = time.time()
        self.w.state_3_reasons = []

    def evaluate(self):
        now_ts = time.time()

        if self.w.state == EngineState.ARMED_VELOCITY:
            if (now_ts - self.w.state_3_armed_ts) > STATE_3_TTL_SECS:
                direction = getattr(self.w, "armed_direction", "bullish")
                reasons = getattr(self.w, "state_3_reasons", [])
                reason_summary = " · ".join(reasons[:2]) if reasons else "Altcoin ikut gerak sinkron / tidak ada disparity ≥ 0.03%"
                logger.info(f"ARMED_VELOCITY TTL expired ({STATE_3_TTL_SECS}s). Reverting to IDLE. Reason: {reason_summary}")

                wib_str = datetime.now(UTC).astimezone(WIB).strftime("%H:%M:%S")
                dir_emoji = "🐂" if direction == "bullish" else "🐻"
                details = (
                    f"Window transmisi ARMED VELOCITY ({direction.upper()}) selama {STATE_3_TTL_SECS}s telah habis tanpa eksekusi State 4. "
                    f"Penyebab: {reason_summary}. FSM stand down dan kembali ke status IDLE."
                )
                metrics = (
                    f"🛑 **FSM STATE 3 STAND DOWN: WINDOW EXPIRED**\n"
                    f"• **Status**: Stand Down ➔ Reverting to `IDLE`\n"
                    f"• **Transmission Window**: `{STATE_3_TTL_SECS}s TTL Expired`\n"
                    f"• **Armed Bias**: **{direction.upper()}** {dir_emoji}\n"
                    f"• **State 4 Outcome**: ❌ **No Execution**\n"
                    f"• **Diagnosis**: `{reason_summary}`\n"
                    f"• **Timestamp**: `{wib_str} WIB`"
                )
                asyncio.create_task(asyncio.to_thread(post_persona_discord, "ARMED_VELOCITY_EXPIRED (State 3 Stand Down)", details, metrics))

                self.w.state = EngineState.IDLE
                self.w.state_entered_ts = now_ts
                self.w.state_3_reasons = []

        if self.w.state == EngineState.ACCUMULATION_BUILDUP:
            if (now_ts - self.w.state_entered_ts) > 300.0:
                logger.info("ACCUMULATION_BUILDUP window expired without breakout. Reverting to IDLE.")
                self.w.state = EngineState.IDLE
                self.w.state_entered_ts = now_ts

        btc_hist = list(self.w.rolling_1s["BTCUSDT"])
        if len(btc_hist) < 30:
            return

        w30 = btc_hist[-30:]
        btc_start_30 = w30[0]["open"]
        btc_now = w30[-1]["close"]
        if btc_start_30 <= 0:
            return

        btc_30s_move_pct = (btc_now - btc_start_30) / btc_start_30 * 100.0
        abs_btc_30s = abs(btc_30s_move_pct)
        vol_30 = sum(x["vol"] for x in w30)
        taker_buy_30 = sum(x.get("taker_buy_vol", 0.0) for x in w30)
        baseline_1s_btc = self.w.baselines["BTCUSDT"].get("baseline_1s_vol", 0.15)
        baseline_30_btc = max(1e-6, baseline_1s_btc * 30.0)
        vol_ratio_30 = vol_30 / baseline_30_btc
        taker_ratio_30 = (taker_buy_30 / vol_30) if vol_30 > 0 else 0.50

        w20_btc = btc_hist[-20:] if len(btc_hist) >= 20 else w30[-20:]
        btc_20s_move_pct = (w20_btc[-1]["close"] - w20_btc[0]["open"]) / w20_btc[0]["open"] * 100.0

        w15_btc = btc_hist[-15:] if len(btc_hist) >= 15 else w30[-15:]
        btc_15s_move_pct = (w15_btc[-1]["close"] - w15_btc[0]["open"]) / w15_btc[0]["open"] * 100.0 if w15_btc[0]["open"] > 0 else 0.0
        abs_btc_15s = abs(btc_15s_move_pct)
        vol_15s = sum(x["vol"] for x in w15_btc)
        baseline_15s_btc = max(1e-6, baseline_1s_btc * len(w15_btc))
        vol_ratio_15s = vol_15s / baseline_15s_btc

        eth_hist = list(self.w.rolling_1s["ETHUSDT"])
        eth_20s_move_pct = 0.0
        if len(eth_hist) >= 20:
            w20_eth = eth_hist[-20:]
            if w20_eth[0]["open"] > 0:
                eth_20s_move_pct = (w20_eth[-1]["close"] - w20_eth[0]["open"]) / w20_eth[0]["open"] * 100.0

        highs = [x["high"] for x in btc_hist]
        lows = [x["low"] for x in btc_hist]
        min_l = min(lows)
        max_h = max(highs)
        range_15m_pct = (max_h - min_l) / min_l * 100.0 if min_l > 0 else 1.0
        vol_15m = sum(x["vol"] for x in btc_hist)
        baseline_15m = max(1e-6, baseline_1s_btc * len(btc_hist))
        vol_ratio_15m = vol_15m / baseline_15m
        is_squeeze = (range_15m_pct <= SQUEEZE_RANGE_MAX_PCT) and (vol_ratio_15m <= SQUEEZE_VOL_MAX_RATIO)

        # FAST-PATH INTERRUPT: Sub-Second Order Flow Burst Trigger
        if len(btc_hist) >= 5:
            w5_btc = btc_hist[-5:]
            vol_5s = sum(x["vol"] for x in w5_btc)
            baseline_5s_btc = max(1e-6, baseline_1s_btc * 5.0)
            vol_ratio_5s = vol_5s / baseline_5s_btc
            taker_buy_5s = sum(x.get("taker_buy_vol", 0.0) for x in w5_btc)
            taker_ratio_5s = (taker_buy_5s / vol_5s) if vol_5s > 0 else 0.50

            btc_5s_move_pct = (w5_btc[-1]["close"] - w5_btc[0]["open"]) / w5_btc[0]["open"] * 100.0
            abs_btc_5s = abs(btc_5s_move_pct)

            is_burst_bull = (vol_ratio_5s >= 4.0 and taker_ratio_5s >= 0.70 and btc_5s_move_pct >= 0.06)
            is_burst_bear = (vol_ratio_5s >= 4.0 and taker_ratio_5s <= 0.30 and btc_5s_move_pct <= -0.06)

            if is_burst_bull or is_burst_bear:
                burst_dir = "bullish" if is_burst_bull else "bearish"
                logger.info(f"⚡ FAST-PATH BURST: Dir={burst_dir.upper()}, Vol={vol_ratio_5s:.1f}x (5s), Move={btc_5s_move_pct:+.3f}%")
                candidate = self.w.find_best_lagging_follower(burst_dir, abs_btc_5s, btc_5s_move_pct, vol_ratio_5s)
                if candidate:
                    logger.info(f"🚀 FAST-PATH BURST SNIPING: {candidate['follower']} (Disparity: {candidate['disparity']:.3f}%)")
                    self.execute_state_4(candidate)
                    return

        # Path to State 4: DISPARITY_EXECUTION
        if self.w.state in (EngineState.ARMED_VELOCITY, EngineState.ACCUMULATION_BUILDUP):
            direction = "bullish" if btc_15s_move_pct > 0 else "bearish"
            is_impulse_15s = (abs_btc_15s >= DISPARITY_IMPULSE_MIN_PCT and vol_ratio_15s >= ACCUMULATION_MIN_VOL_RATIO)
            is_impulse_30s = (abs_btc_30s >= DISPARITY_IMPULSE_MIN_PCT and vol_ratio_30 >= ACCUMULATION_MIN_VOL_RATIO)
            if is_impulse_15s or is_impulse_30s:
                use_15s = is_impulse_15s and (abs_btc_15s >= abs_btc_30s * 0.8)
                cand_abs = abs_btc_15s if use_15s else abs_btc_30s
                cand_move = btc_15s_move_pct if use_15s else btc_30s_move_pct
                cand_vol = vol_ratio_15s if use_15s else vol_ratio_30
                candidate = self.w.find_best_lagging_follower(direction, cand_abs, cand_move, cand_vol)
                if candidate:
                    self.execute_state_4(candidate)
                    return

        # Path to State 3: ARMED_VELOCITY
        if self.w.state == EngineState.ACCUMULATION_BUILDUP:
            bias = self.w.accumulation_bias
            if bias == "bullish":
                btc_armed = (ARMED_VELOCITY_MIN_PCT <= btc_20s_move_pct <= 0.16) or (ARMED_VELOCITY_MIN_PCT <= btc_30s_move_pct <= 0.16)
                eth_confirmed = eth_20s_move_pct > 0.0
            else:
                btc_armed = (-0.16 <= btc_20s_move_pct <= -ARMED_VELOCITY_MIN_PCT) or (-0.16 <= btc_30s_move_pct <= -ARMED_VELOCITY_MIN_PCT)
                eth_confirmed = eth_20s_move_pct < 0.0

            if btc_armed and eth_confirmed:
                self.transition_to_armed_velocity(bias, btc_20s_move_pct, eth_20s_move_pct)
                return

        # Path to State 2: ACCUMULATION_BUILDUP
        if self.w.state in (EngineState.IDLE, EngineState.SQUEEZE_DETECTED):
            if vol_ratio_30 >= ACCUMULATION_MIN_VOL_RATIO:
                if taker_ratio_30 >= TAKER_RATIO_BULLISH:
                    self.transition_to_accumulation("bullish", taker_ratio_30, vol_ratio_30, btc_now)
                    return
                elif taker_ratio_30 <= TAKER_RATIO_BEARISH:
                    self.transition_to_accumulation("bearish", taker_ratio_30, vol_ratio_30, btc_now)
                    return

        # Path to State 1: SQUEEZE_DETECTED
        if self.w.state == EngineState.IDLE:
            if is_squeeze and len(btc_hist) >= 300:
                self.transition_to_squeeze(range_15m_pct, vol_ratio_15m, btc_now)
                return

        if self.w.state == EngineState.SQUEEZE_DETECTED:
            if range_15m_pct > 0.35:
                logger.info(f"Squeeze range expanded ({range_15m_pct:.2f}% > 0.35%). Reverting to IDLE.")
                self.w.state = EngineState.IDLE
                self.w.state_entered_ts = now_ts
