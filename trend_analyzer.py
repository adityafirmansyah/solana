#!/usr/bin/env python3
"""Market Trend Phase & Lifecycle Analyzer for Solana Multi-Coin Fleet.

Determines whether a coin or open position is:
1. 🌱 START OF TREND: Early Donchian breakout / expansion phase (< 1.0x ATR, early momentum).
2. 🏄 RIDING THE WAVE: Mid-trend momentum continuation (1.0x to 2.5x ATR, healthy RSI 50-75).
3. ⚠️ END OF TREND / EXHAUSTION: Overextended (> 2.5x ATR, RSI > 75 / < 25 climax, mean-reversion risk).
4. ⚖️ CONSOLIDATION: Compression around EMA20.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import fetch_market as fm
from multi_tf_strategy import compute_atr, compute_ema

logger = logging.getLogger("trend_analyzer")

_PHASE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
CACHE_TTL_SECS = 20.0  # Cache for 20s to avoid hitting Binance API rate limits


def compute_rsi(closes: list[float], period: int = 14) -> float:
    """Compute Relative Strength Index (RSI)."""
    if len(closes) < period + 1:
        return 50.0
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))


def get_trend_phase(symbol: str, position_direction: str | None = None) -> dict[str, Any]:
    """Analyze the trend lifecycle for a given coin or active position."""
    global _PHASE_CACHE
    now = time.time()
    cache_key = f"{symbol}_{position_direction or 'auto'}"
    if cache_key in _PHASE_CACHE:
        cached_ts, data = _PHASE_CACHE[cache_key]
        if now - cached_ts < CACHE_TTL_SECS:
            return data

    try:
        candles_1h = fm.fetch_candles("1h", 60, symbol)
        if not candles_1h or len(candles_1h) < 25:
            return _fallback_phase(symbol)

        closes = [x["close"] for x in candles_1h]
        last_c = closes[-1]
        ema_series = compute_ema(closes, 20)
        ema20 = ema_series[-1]
        prev_ema = ema_series[-2]
        ema_slope = (ema20 - prev_ema) / max(prev_ema, 1e-6)
        atr = compute_atr(candles_1h, 14)
        dist_atr = (last_c - ema20) / atr if atr > 0 else 0.0
        rsi = compute_rsi(closes, 14)

        # Determine trend direction
        if position_direction:
            direction = position_direction.lower()
        else:
            direction = "bullish" if last_c >= ema20 else "bearish"

        is_long = direction == "bullish"

        # Check recency of 20h Donchian breakout
        breakout_bars_ago = None
        for i in range(1, 10):
            bar = candles_1h[-i]
            if is_long and bar["close"] > max(x["high"] for x in candles_1h[-20 - i : -i]):
                breakout_bars_ago = i
                break
            elif not is_long and bar["close"] < min(x["low"] for x in candles_1h[-20 - i : -i]):
                breakout_bars_ago = i
                break

        # Determine Phase & Recommendations
        if is_long:
            if dist_atr > 2.2 or rsi >= 75:
                phase = "END"
                label = "⚠️ End of Trend (Exhaustion)"
                badge = "bg-rose-500/20 text-rose-400 border-rose-500/30"
                desc = f"Price overextended (+{dist_atr:.1f}x ATR above EMA, RSI {rsi:.0f}). High mean-reversion risk."
                hint = "Overextended rally. Recommendation: Tighten SL to Breakeven or lock partial profit."
            elif breakout_bars_ago and breakout_bars_ago <= 3 and dist_atr <= 1.2:
                phase = "START"
                label = "🌱 Start of Trend (Breakout)"
                badge = "bg-emerald-500/20 text-emerald-400 border-emerald-500/30"
                desc = f"Fresh Donchian 20h breakout {breakout_bars_ago}h ago (+{dist_atr:.1f}x ATR). Expansion starting."
                hint = "Early trend expansion. Recommendation: Give trade breathing room, keep wide TP."
            elif dist_atr >= 0.35 and rsi >= 48:
                phase = "RIDING"
                label = "🏄 Riding the Wave"
                badge = "bg-cyan-500/20 text-cyan-400 border-cyan-500/30"
                desc = f"Healthy bullish continuation (+{dist_atr:.1f}x ATR, RSI {rsi:.0f}). Orderly momentum."
                hint = "Healthy momentum runner. Recommendation: Ride the wave with standard trailing SL."
            else:
                phase = "CONSOLIDATION"
                label = "⚖️ Consolidation / Pullback"
                badge = "bg-slate-800 text-slate-300 border-slate-700"
                desc = f"Price compressing near EMA20 ({dist_atr:+.1f}x ATR, RSI {rsi:.0f}). Indecision band."
                hint = "Compression zone. Hold / wait for directional impulse confirmation."
        else:
            if dist_atr < -2.2 or rsi <= 25:
                phase = "END"
                label = "⚠️ End of Trend (Exhaustion)"
                badge = "bg-rose-500/20 text-rose-400 border-rose-500/30"
                desc = f"Price overextended on downside ({dist_atr:.1f}x ATR below EMA, RSI {rsi:.0f}). Bounce risk high."
                hint = "Overextended selloff. Recommendation: Tighten SL to Breakeven or lock partial profit."
            elif breakout_bars_ago and breakout_bars_ago <= 3 and dist_atr >= -1.2:
                phase = "START"
                label = "🌱 Start of Trend (Breakout)"
                badge = "bg-emerald-500/20 text-emerald-400 border-emerald-500/30"
                desc = f"Fresh Donchian 20h breakdown {breakout_bars_ago}h ago ({dist_atr:.1f}x ATR). Expansion starting."
                hint = "Early trend expansion. Recommendation: Give trade breathing room, keep wide TP."
            elif dist_atr <= -0.35 and rsi <= 52:
                phase = "RIDING"
                label = "🏄 Riding the Wave"
                badge = "bg-cyan-500/20 text-cyan-400 border-cyan-500/30"
                desc = f"Healthy bearish continuation ({dist_atr:.1f}x ATR, RSI {rsi:.0f}). Orderly downward momentum."
                hint = "Healthy downward trend runner. Recommendation: Ride the wave with standard trailing SL."
            else:
                phase = "CONSOLIDATION"
                label = "⚖️ Consolidation / Pullback"
                badge = "bg-slate-800 text-slate-300 border-slate-700"
                desc = f"Price compressing near EMA20 ({dist_atr:+.1f}x ATR, RSI {rsi:.0f}). Indecision band."
                hint = "Compression zone. Hold / wait for directional impulse confirmation."

        res = {
            "symbol": symbol,
            "direction": direction,
            "phase": phase,
            "label": label,
            "badge": badge,
            "desc": desc,
            "hint": hint,
            "dist_atr": round(dist_atr, 2),
            "rsi": round(rsi, 1),
            "ema20": round(ema20, 4),
            "atr": round(atr, 4),
            "breakout_bars": breakout_bars_ago,
            "updated_ts": int(now),
        }
        _PHASE_CACHE[cache_key] = (now, res)
        return res

    except Exception as e:
        logger.warning(f"Error computing trend phase for {symbol}: {e}")
        return _fallback_phase(symbol)


def _fallback_phase(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "direction": "neutral",
        "phase": "RIDING",
        "label": "🏄 Riding the Wave",
        "badge": "bg-cyan-500/20 text-cyan-400 border-cyan-500/30",
        "desc": "Active market momentum tracking.",
        "hint": "Monitor price action against bracket levels.",
        "dist_atr": 0.0,
        "rsi": 50.0,
        "ema20": 0.0,
        "atr": 0.0,
        "breakout_bars": None,
        "updated_ts": int(time.time()),
    }


def get_all_universe_trends() -> list[dict[str, Any]]:
    """Scan all active universe follower coins and return their trend phases."""
    universe = ["SOLUSDT", "SUIUSDT", "NEARUSDT", "DOGEUSDT", "LINKUSDT", "AVAXUSDT"]
    results = []
    for sym in universe:
        results.append(get_trend_phase(sym))
    return results


if __name__ == "__main__":
    trends = get_all_universe_trends()
    for t in trends:
        print(f"{t['symbol']:8} | {t['label']} | Dist: {t['dist_atr']:+.2f}x | RSI: {t['rsi']} | {t['desc']}")
