#!/usr/bin/env python3
"""Lead-Lag Disparity Opportunity Search & 1s Kline Barrier Evaluation.

Implements micro-lag filtering, cluster concurrency limits, exhaustion guards,
spread filters, and real-time tick barrier grading.
"""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from enum import IntEnum
from typing import Any

logger = logging.getLogger("solana_lead_lag_finder")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] lead_lag: %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

# Risk & Cooldown Constants
GLOBAL_CLUSTER_COOLDOWN_SECS = 180.0  # 3m between ANY follower trade triggers
FOLLOWER_COOLDOWN_SECS = 900.0        # 15m cooldown per symbol
MAX_CONCURRENT_SAME_DIR_TRADES = 1    # Max 1 active trade in same direction
EXHAUSTION_BTC_60S_MOVE_PCT = 0.60   # Exhaustion guard threshold
FOLLOWER_LAG_MAX_PCT = 0.04          # Strict lag ceiling
MAX_SPREAD_PCT = 0.08                # Max allowable book spread (0.08%)
MAX_HOLDING_SECS = 360.0             # 6 minutes max holding time-stop for micro lead-lag


class EngineState(IntEnum):
    IDLE = 0
    SQUEEZE_DETECTED = 1
    ACCUMULATION_BUILDUP = 2
    ARMED_VELOCITY = 3
    DISPARITY_EXECUTION = 4


def evaluate_1s_kline(trade: dict, k: dict) -> tuple[str | None, float]:
    """Evaluate a single 1s kline tick against open trade TP, SL, or Time-Stop.
    Returns: (exit_reason | None, trigger_price)
    """
    direction = trade["direction"]
    tp = trade["tp_price"]
    sl = trade["sl_price"]
    ref = trade["ref_price"]

    high = float(k["h"])
    low = float(k["l"])
    close = float(k["c"])

    trade["high_watermark"] = max(trade.get("high_watermark", ref), high)
    trade["low_watermark"] = min(trade.get("low_watermark", ref), low)

    # 1. Take Profit Check
    if direction == "bullish" and high >= tp:
        return "tp_hit", tp
    elif direction == "bearish" and low <= tp:
        return "tp_hit", tp

    # 2. Stop Loss Check
    if direction == "bullish" and low <= sl:
        return "sl_hit", sl
    elif direction == "bearish" and high >= sl:
        return "sl_hit", sl

    # 3. Time-Stop Check (6 minutes)
    try:
        created_dt = datetime.fromisoformat(trade["created_utc"])
        if (datetime.now(UTC) - created_dt).total_seconds() >= MAX_HOLDING_SECS:
            return "time_stop", close
    except Exception:
        pass

    return None, close


def find_lagging_follower_candidate(
    direction: str,
    abs_btc_move: float,
    btc_move_pct: float,
    vol_ratio: float,
    active_trades: dict[int, dict],
    last_global_trigger_ts: float,
    last_trigger_ts: dict[str, float],
    rolling_1s: dict[str, Any],
    baselines: dict[str, dict],
    followers: list[str],
    fapi_executor: Any = None,
    blocker_reasons: list[str] | None = None,
) -> dict | None:
    now_ts = time.time()

    def record_blocker(msg: str):
        if blocker_reasons is not None and msg not in blocker_reasons:
            if len(blocker_reasons) < 5:
                blocker_reasons.append(msg)

    # 1. Global Cluster Cooldown Guard
    if (now_ts - last_global_trigger_ts) < GLOBAL_CLUSTER_COOLDOWN_SECS:
        elapsed = int(now_ts - last_global_trigger_ts)
        logger.info(f"Cluster cooldown active ({elapsed}s < {int(GLOBAL_CLUSTER_COOLDOWN_SECS)}s). Skipping candidate search.")
        record_blocker(f"Cluster cooldown active ({elapsed}s/180s)")
        return None

    # 2. Directional Cluster Concurrency Guard
    active_same_dir = [t for t in active_trades.values() if t.get("direction") == direction]
    if len(active_same_dir) >= MAX_CONCURRENT_SAME_DIR_TRADES:
        symbols = [t.get("symbol") for t in active_same_dir]
        logger.info(f"Max concurrent {direction.upper()} trades active ({len(active_same_dir)}/{MAX_CONCURRENT_SAME_DIR_TRADES}): {symbols}. Skipping.")
        record_blocker(f"Max concurrent {direction.upper()} ({symbols})")
        return None

    # 3. Climax / Exhaustion Guard on Leader (BTC)
    btc_hist = list(rolling_1s.get("BTCUSDT", []))
    if len(btc_hist) >= 30:
        window_len = min(len(btc_hist), 60)
        w_ex = btc_hist[-window_len:]
        if w_ex[0]["open"] > 0:
            btc_ex_move_pct = (w_ex[-1]["close"] - w_ex[0]["open"]) / w_ex[0]["open"] * 100.0
            if direction == "bullish" and btc_ex_move_pct >= EXHAUSTION_BTC_60S_MOVE_PCT:
                logger.warning(
                    f"⚠️ Exhaustion Guard BLOCKED BULLISH entry: BTC already pumped {btc_ex_move_pct:+.2f}% in {window_len}s "
                    f"(Threshold >= +{EXHAUSTION_BTC_60S_MOVE_PCT:.2f}%). Climax risk."
                )
                record_blocker(f"BTC pump exhaustion ({btc_ex_move_pct:+.2f}% in {window_len}s)")
                return None
            elif direction == "bearish" and btc_ex_move_pct <= -EXHAUSTION_BTC_60S_MOVE_PCT:
                logger.warning(
                    f"⚠️ Exhaustion Guard BLOCKED BEARISH entry: BTC already dumped {btc_ex_move_pct:+.2f}% in {window_len}s "
                    f"(Threshold <= -{EXHAUSTION_BTC_60S_MOVE_PCT:.2f}%). Climax/absorption risk."
                )
                record_blocker(f"BTC dump exhaustion ({btc_ex_move_pct:+.2f}% in {window_len}s)")
                return None

    candidates = []
    for follower in followers:
        if any(t.get("symbol") == follower for t in active_trades.values()):
            continue

        if (now_ts - last_trigger_ts.get(follower, 0.0)) < FOLLOWER_COOLDOWN_SECS:
            continue

        f_hist = list(rolling_1s.get(follower, []))
        if len(f_hist) < 30:
            continue

        # Spread Guard
        if fapi_executor:
            depth = fapi_executor._request("GET", "/fapi/v1/depth", {"symbol": follower, "limit": 5})
            bids = depth.get("bids", [])
            asks = depth.get("asks", [])
            if bids and asks:
                best_bid = float(bids[0][0])
                best_ask = float(asks[0][0])
                spread_pct = ((best_ask - best_bid) / best_bid) * 100.0 if best_bid > 0 else 0.0
                if spread_pct > MAX_SPREAD_PCT:
                    logger.warning(
                        f"⚠️ Spread Guard BLOCKED {follower} entry: spread={spread_pct:.3f}% > {MAX_SPREAD_PCT}%. Skipping illiquid book."
                    )
                    record_blocker(f"Spread {follower} lebar ({spread_pct:.3f}% > {MAX_SPREAD_PCT}%)")
                    continue

        f_now = f_hist[-1]["close"]
        if f_now <= 0:
            continue

        # Follower Pre-Expansion Guard (60s)
        if len(f_hist) >= 60:
            f_60_start = f_hist[-60]["open"]
            if f_60_start > 0:
                f_60_move = (f_now - f_60_start) / f_60_start * 100.0
                f_60_dir = f_60_move if direction == "bullish" else -f_60_move
                if f_60_dir >= 0.15:
                    logger.info(f"Follower {follower} already expanded {f_60_dir:+.2f}% in 60s. Skipping.")
                    record_blocker(f"{follower} sudah expand {f_60_dir:+.2f}%")
                    continue

        # Follower Micro-Lag Guard (12s)
        if len(f_hist) >= 12:
            f_12_start = f_hist[-12]["open"]
            if f_12_start > 0:
                f_12_move = (f_now - f_12_start) / f_12_start * 100.0
                f_12_dir = f_12_move if direction == "bullish" else -f_12_move
                if f_12_dir >= 0.035:
                    record_blocker(f"{follower} micro-move {f_12_dir:+.2f}%")
                    continue

        f_30 = f_hist[-30:]
        f_start = f_30[0]["open"]
        if f_start <= 0:
            continue

        follower_move_pct = (f_now - f_start) / f_start * 100.0
        realized_dir_move = follower_move_pct if direction == "bullish" else -follower_move_pct

        if realized_dir_move >= FOLLOWER_LAG_MAX_PCT:
            record_blocker(f"{follower} lag habis ({realized_dir_move:+.2f}%)")
            continue

        disparity = abs_btc_move - max(0.0, realized_dir_move)
        if disparity < 0.03:
            record_blocker(f"Disparity {follower} tipis ({disparity:.2f}% < 0.03%)")
            continue

        atr = baselines.get(follower, {}).get("atr") or (f_now * 0.005)
        candidates.append({
            "follower": follower,
            "leader": "BTCUSDT",
            "direction": direction,
            "ref_price": f_now,
            "leader_move_pct": btc_move_pct,
            "leader_vol_ratio": vol_ratio,
            "follower_move_pct": follower_move_pct,
            "disparity": disparity,
            "atr": atr,
        })

    if not candidates:
        return None

    candidates.sort(key=lambda x: x["disparity"], reverse=True)
    return candidates[0]
