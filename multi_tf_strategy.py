#!/usr/bin/env python3
"""Top-Down Multi-Timeframe Strategy Engine (1D -> 4H -> 1H -> 15m -> 5m).

Core Rules & Mathematical Formulations:
1. Multi-Timeframe Alignment:
   - 1D: Macro Directional Bias Filter (EMA20_1d + price structure).
   - 4H: Intermediate Trend Alignment (EMA20_4h slope + overextension ATR guard).
   - 1H: Setup Identification (Pullback into value area, never chase extended candles).
   - 15m: Structural Shift / Exhaustion of pullback (MSS / engulfing counter-trend break).
   - 5m: Precision Trigger & Risk Definition (Micro break + tight structural swing SL).
2. Risk Management & Position Sizing Contract:
   - Fixed loss per trade: Exactly $3.00 USD max loss at Stop Loss.
   - Formula: qty = 3.00 / abs(entry_price - sl_price).
   - Bounds: notional >= $5.00 (Binance minNotional), margin <= 50% of bankroll ($50 max margin on $100 bankroll).
3. 1:3 Minimum Risk:Reward Bracket:
   - Distance to TP >= 3.0 * Distance to SL.
   - Long: TP >= Entry + 3.0 * (Entry - SL).
   - Short: TP <= Entry - 3.0 * (SL - Entry).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from fapi_client import SYMBOL_SPECS, round_step, round_tick

DEFAULT_BANKROLL = 100.0
DEFAULT_MAX_MARGIN_PCT = 0.50  # Max 50% margin locked ($50.00 on $100 bankroll)
FIXED_LOSS_USD = 3.00
MIN_RISK_REWARD = 3.0


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
        return 50.0 if avg_gain == 0 else 100.0
    return 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))


def check_market_saturation(
    symbol: str,
    direction: str,
    entry_price: float,
    current_price: float,
    tp_price: float,
    candles_1h: list[dict],
    candles_15m: list[dict] | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Detect whether market momentum has reached saturation/exhaustion
    and unlikely to yield further favorable expansion.
    
    Signals evaluated:
    1. Overextended ATR Stretch: Price > 2.2x ATR from EMA20 (or < -2.2x for shorts).
    2. Momentum Climax: RSI >= 75 on 1H (overbought) or RSI <= 25 (oversold).
    3. Target Milestone Exhaustion: Reached >= 75% of target TP distance with momentum flattening.
    4. 15m Momentum Disconnect: Counter RSI reversal (> 75 for long, < 25 for short).
    """
    if not candles_1h or len(candles_1h) < 20:
        return False, "Insufficient 1H candles for saturation check", {}

    closes_1h = [b["close"] for b in candles_1h]
    ema_series = compute_ema(closes_1h, 20)
    ema20 = ema_series[-1]
    atr_1h = compute_atr(candles_1h, 14)
    rsi_1h = compute_rsi(closes_1h, 14)

    dist_atr = (current_price - ema20) / atr_1h if atr_1h > 0 else 0.0

    # Calculate progression toward Take Profit
    total_tp_dist = abs(tp_price - entry_price)
    current_progress_dist = (current_price - entry_price) if direction == "bullish" else (entry_price - current_price)
    progress_ratio = (current_progress_dist / total_tp_dist) if total_tp_dist > 0 else 0.0

    rsi_15m = 50.0
    if candles_15m and len(candles_15m) >= 15:
        rsi_15m = compute_rsi([b["close"] for b in candles_15m], 14)

    diag = {
        "symbol": symbol,
        "direction": direction,
        "dist_atr": round(dist_atr, 2),
        "rsi_1h": round(rsi_1h, 1),
        "rsi_15m": round(rsi_15m, 1),
        "tp_progress_pct": round(progress_ratio * 100, 1),
    }

    if direction == "bullish":
        # 1. Extreme mean-reversion risk
        if dist_atr >= 2.2 and rsi_1h >= 70:
            return True, f"Overextended bullish climax (+{dist_atr:.1f}x ATR above EMA20, RSI {rsi_1h:.0f}). Exhaustion peak reached.", diag
        # 2. RSI extreme overbought
        if rsi_1h >= 76 or rsi_15m >= 80:
            return True, f"Extreme overbought saturation (RSI_1h: {rsi_1h:.0f}, RSI_15m: {rsi_15m:.0f}). High mean-reversion risk.", diag
        # 3. Near-TP stagnation
        if progress_ratio >= 0.75 and (rsi_15m >= 72 or dist_atr >= 1.8):
            return True, f"Reached {progress_ratio*100:.0f}% of TP target with overextended momentum. Securing full profit at saturation.", diag

    elif direction == "bearish":
        # 1. Extreme mean-reversion risk
        if dist_atr <= -2.2 and rsi_1h <= 30:
            return True, f"Overextended bearish selloff ({dist_atr:.1f}x ATR below EMA20, RSI {rsi_1h:.0f}). Climax bounce risk.", diag
        # 2. RSI extreme oversold
        if rsi_1h <= 24 or rsi_15m <= 20:
            return True, f"Extreme oversold saturation (RSI_1h: {rsi_1h:.0f}, RSI_15m: {rsi_15m:.0f}). High bounce risk.", diag
        # 3. Near-TP stagnation
        if progress_ratio >= 0.75 and (rsi_15m <= 28 or dist_atr <= -1.8):
            return True, f"Reached {progress_ratio*100:.0f}% of TP target with oversold climax. Securing full profit at saturation.", diag

    return False, "Market momentum healthy / within normal trend parameters", diag


# -----------------------------------------------------------------------------
# Technical Indicator Primitives
# -----------------------------------------------------------------------------

def compute_ema(closes: list[float], period: int = 20) -> list[float]:
    """Compute Exponential Moving Average (EMA) series."""
    if not closes:
        return []
    if len(closes) < period:
        # Fallback for short series: simple average baseline
        sma = sum(closes) / len(closes)
        return [sma] * len(closes)

    multiplier = 2.0 / (period + 1.0)
    # Seed with SMA of first 'period' bars
    ema = [sum(closes[:period]) / period]
    for price in closes[period:]:
        new_val = (price - ema[-1]) * multiplier + ema[-1]
        ema.append(new_val)

    # Pad prefix so output length matches input closes length
    padding = [ema[0]] * (period - 1)
    return padding + ema


def compute_atr(candles: list[dict], period: int = 14) -> float:
    """Compute Average True Range (ATR) over closed candles."""
    if len(candles) < 2:
        return 0.0

    tr_list = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        prev_c = candles[i - 1]["close"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        tr_list.append(tr)

    if not tr_list:
        return 0.0

    lookback = min(period, len(tr_list))
    return sum(tr_list[-lookback:]) / lookback


# -----------------------------------------------------------------------------
# Multi-Timeframe Analysis Primitives
# -----------------------------------------------------------------------------

@dataclass
class BiasResult:
    bias: str  # "bullish", "bearish", "neutral"
    ema20_1d: float
    last_close: float
    reason: str


@dataclass
class AlignmentResult:
    aligned: bool
    direction: str  # "bullish", "bearish", "none"
    ema20_4h: float
    atr_4h: float
    distance_to_ema: float
    reason: str


@dataclass
class SetupResult:
    setup_ready: bool
    direction: str
    ema20_1h: float
    pullback_tested: bool
    extended: bool
    reason: str


@dataclass
class MssResult:
    mss_confirmed: bool
    direction: str
    shift_level: float
    reason: str
    invalidation_level: float = 0.0



@dataclass
class TriggerResult:
    triggered: bool
    direction: str
    entry_price: float
    sl_price: float
    sl_dist: float
    reason: str


@dataclass
class MTFPipelineResult:
    valid: bool
    symbol: str
    direction: str  # "bullish", "bearish", or "none"
    entry_price: float
    sl_price: float
    tp_price: float
    risk_reward: float
    stage_failed: str | None
    diagnostics: dict[str, Any]


def analyze_1d_bias(candles_1d: list[dict]) -> BiasResult:
    """Daily (1D): Trend & directional bias filter.
    - Evaluates closed daily bars against EMA20_1d and structure.
    - Bullish: Close > EMA20_1d and EMA slope >= 0 / higher lows -> LONG ONLY.
    - Bearish: Close < EMA20_1d and EMA slope <= 0 / lower highs -> SHORT ONLY.
    - Flat/Chop: Close hugging EMA20 within 0.15% or conflicting structure -> NEUTRAL (STAND DOWN).
    """
    if len(candles_1d) < 20:
        return BiasResult("neutral", 0.0, 0.0, "Insufficient 1D candles (< 20)")

    closes = [b["close"] for b in candles_1d]
    ema_series = compute_ema(closes, 20)
    last_close = closes[-1]
    last_ema = ema_series[-1]
    prev_ema = ema_series[-2]
    ema_slope = (last_ema - prev_ema) / max(prev_ema, 1e-6)

    # Indecision band: within 0.15% of EMA20 is treated as chop
    diff_pct = (last_close - last_ema) / max(last_ema, 1e-6)
    if abs(diff_pct) < 0.0015:
        return BiasResult("neutral", last_ema, last_close, f"1D price hugging EMA20 within 0.15% ({diff_pct*100:+.2f}%)")

    # Structure check over last 6 daily bars
    recent_lows = [b["low"] for b in candles_1d[-6:]]
    recent_highs = [b["high"] for b in candles_1d[-6:]]
    higher_lows = min(recent_lows[-3:]) >= min(recent_lows[:3])
    lower_highs = max(recent_highs[-3:]) <= max(recent_highs[:3])

    atr_1d = compute_atr(candles_1d, 14)
    # Anti-exhaustion / Anti-oversold filter: If price is > 5.0 ATR or > 25% away from EMA20, stand down
    max_extension_pct = 0.25
    max_extension_atr = max(5.0 * atr_1d, last_ema * max_extension_pct) if atr_1d > 0 else (last_ema * max_extension_pct)

    if last_close > last_ema:
        if (last_close - last_ema) > max_extension_atr:
            return BiasResult("neutral", last_ema, last_close, f"1D overextended bullish (+{((last_close-last_ema)/last_ema)*100:.1f}% above EMA20 > 3*ATR) - chasing rejected")
        if ema_slope >= -0.0002 or higher_lows:
            return BiasResult("bullish", last_ema, last_close, f"1D Close > EMA20 ({last_close:.4f} > {last_ema:.4f}) with bullish structure")

    if last_close < last_ema:
        if (last_ema - last_close) > max_extension_atr:
            return BiasResult("neutral", last_ema, last_close, f"1D overextended bearish (-{((last_ema-last_close)/last_ema)*100:.1f}% below EMA20 > 3*ATR) - oversold shorting rejected")
        if ema_slope <= 0.0002 or lower_highs:
            return BiasResult("bearish", last_ema, last_close, f"1D Close < EMA20 ({last_close:.4f} < {last_ema:.4f}) with bearish structure")

    return BiasResult("neutral", last_ema, last_close, f"1D mixed structure: close={last_close:.4f}, ema={last_ema:.4f}")


def analyze_4h_alignment(candles_4h: list[dict], daily_bias: str) -> AlignmentResult:
    """4-Hour (4H): Intermediate trend alignment.
    - Confirms intermediate momentum matches Daily bias.
    - Rejects entries if 4H is overextended from mean (> 2.5x 4H ATR).
    """
    if daily_bias not in ("bullish", "bearish"):
        return AlignmentResult(False, "none", 0.0, 0.0, 0.0, f"Cannot align on {daily_bias} daily bias")

    if len(candles_4h) < 20:
        return AlignmentResult(False, "none", 0.0, 0.0, 0.0, "Insufficient 4H candles (< 20)")

    closes = [b["close"] for b in candles_4h]
    ema_series = compute_ema(closes, 20)
    last_close = closes[-1]
    last_ema = ema_series[-1]
    prev_ema = ema_series[-2]
    ema_slope = (last_ema - prev_ema) / max(prev_ema, 1e-6)

    atr_4h = compute_atr(candles_4h, 14)
    distance_to_ema = abs(last_close - last_ema)
    max_extension = max(2.5 * atr_4h, last_ema * 0.05) if atr_4h > 0 else (last_ema * 0.05)

    if distance_to_ema > max_extension:
        return AlignmentResult(
            False,
            "none",
            last_ema,
            atr_4h,
            distance_to_ema,
            f"4H overextended: distance {distance_to_ema:.4f} > 2.5*ATR ({max_extension:.4f})"
        )

    if daily_bias == "bullish":
        if last_close >= last_ema * 0.995 and ema_slope >= -0.001:
            return AlignmentResult(True, "bullish", last_ema, atr_4h, distance_to_ema, "4H aligned bullish with rising/holding EMA20")
        return AlignmentResult(False, "none", last_ema, atr_4h, distance_to_ema, f"4H not aligned bullish (close={last_close:.4f}, ema={last_ema:.4f})")

    if daily_bias == "bearish":
        # Check if 4H is printing consecutive higher lows / recovery momentum
        recent_4h = candles_4h[-4:]
        is_4h_rallying = (
            len(recent_4h) >= 3
            and recent_4h[-1]["close"] > recent_4h[-2]["close"] > recent_4h[-3]["close"]
            and recent_4h[-1]["low"] > recent_4h[-2]["low"] > recent_4h[-3]["low"]
        )
        if is_4h_rallying:
            return AlignmentResult(False, "none", last_ema, atr_4h, distance_to_ema, "4H actively rallying counter-trend (consecutive higher highs/lows) - shorting rejected")

        if last_close <= last_ema * 1.005 and ema_slope <= 0.001:
            return AlignmentResult(True, "bearish", last_ema, atr_4h, distance_to_ema, "4H aligned bearish with falling/holding EMA20")
        return AlignmentResult(False, "none", last_ema, atr_4h, distance_to_ema, f"4H not aligned bearish (close={last_close:.4f}, ema={last_ema:.4f})")

    return AlignmentResult(False, "none", last_ema, atr_4h, distance_to_ema, "4H neutral")


def analyze_1h_setup(candles_1h: list[dict], direction: str) -> SetupResult:
    """1-Hour (1H): Setup identification.
    - Waits for controlled retracement / pullback into value area (dynamic EMA20_1h).
    - Strictly rejects chasing extended candles (e.g. breakout bar far from EMA20).
    - Rejects counter-trend penetration (e.g. closing cleanly above EMA20 on a short setup).
    """
    if len(candles_1h) < 20:
        return SetupResult(False, direction, 0.0, False, False, "Insufficient 1H candles (< 20)")

    closes = [b["close"] for b in candles_1h]
    ema_series = compute_ema(closes, 20)
    last_bar = candles_1h[-1]
    last_close = last_bar["close"]
    last_ema = ema_series[-1]

    # Check extension away from EMA: > 2.0% distance is chasing
    dist_pct = (last_close - last_ema) / last_ema
    if direction == "bullish":
        if dist_pct > 0.020:
            return SetupResult(False, direction, last_ema, False, True, f"1H extended bullish (+{dist_pct*100:.2f}% above EMA20, chasing rejected)")

        # Pullback test: within last 4 bars, price dipped into value zone (within 1.0% of EMA20)
        recent_bars = candles_1h[-4:]
        touched_value = any(b["low"] <= last_ema * 1.008 for b in recent_bars)
        # Price must hold support at/above EMA20 (not broken below by > 0.3%)
        held_support = last_close >= last_ema * 0.997

        if touched_value and held_support:
            return SetupResult(True, direction, last_ema, True, False, "1H pullback into EMA20 value area confirmed")
        return SetupResult(False, direction, last_ema, False, False, f"1H waiting for pullback into value area (dist={dist_pct*100:+.2f}%)")

    elif direction == "bearish":
        if dist_pct < -0.020:
            return SetupResult(False, direction, last_ema, False, True, f"1H extended bearish ({dist_pct*100:.2f}% below EMA20, chasing rejected)")

        recent_bars = candles_1h[-4:]
        touched_value = any(b["high"] >= last_ema * 0.992 for b in recent_bars)
        # Price must hold resistance at/below EMA20 (not broken above by > 0.3%)
        held_resistance = last_close <= last_ema * 1.003

        if touched_value and held_resistance:
            return SetupResult(True, direction, last_ema, True, False, "1H bounce into EMA20 resistance value area confirmed")
        return SetupResult(False, direction, last_ema, False, False, f"1H waiting for retracement into value area (dist={dist_pct*100:+.2f}%)")

    return SetupResult(False, "none", last_ema, False, False, "Unknown direction")


def analyze_15m_mss(candles_15m: list[dict], direction: str) -> MssResult:
    """15-Minute (15m): Structural shift / exhaustion of pullback (Market Structure Shift MSS).
    Requires:
    1. Real displacement beyond recent swing level (>= 0.15% or 0.20 ATR buffer, not 1-tick noise).
    2. Solid candle body (>= 40% of range, closing in direction of displacement).
    3. Volume expansion gate (if volume present, cannot be anemic low-volume drift).
    """
    if len(candles_15m) < 8:
        return MssResult(False, direction, 0.0, "Insufficient 15m candles (< 8)")

    last_bar = candles_15m[-1]
    prev_bar = candles_15m[-2]

    # Swing high / low of the pullback over the last 8 bars (excluding current bar)
    pullback_window = candles_15m[-9:-1]
    swing_high = max(b["high"] for b in pullback_window)
    swing_low = min(b["low"] for b in pullback_window)

    atr_15m = compute_atr(candles_15m, 14)
    c_body = abs(last_bar["close"] - last_bar["open"])
    c_range = max(last_bar["high"] - last_bar["low"], 1e-6)
    body_ratio = c_body / c_range

    # Volume gate
    vols = [b.get("volume", 0.0) for b in pullback_window if b.get("volume")]
    avg_vol = sum(vols) / len(vols) if vols else 0.0
    last_vol = last_bar.get("volume", 0.0)
    vol_pass = (avg_vol == 0.0) or (last_vol >= avg_vol * 0.75)

    if direction == "bullish":
        min_disp = max(swing_high * 0.0015, atr_15m * 0.20) if atr_15m > 0 else swing_high * 0.0015
        broke_swing_clean = (last_bar["close"] >= swing_high + min_disp) and (last_bar["close"] > last_bar["open"])
        bullish_displacement = (
            last_bar["close"] >= swing_high + min_disp
            and last_bar["close"] > prev_bar["high"]
            and last_bar["close"] > last_bar["open"]
            and body_ratio >= 0.40
            and (last_bar["close"] - last_bar["low"]) >= c_range * 0.55
        )

        if (broke_swing_clean or bullish_displacement) and vol_pass:
            return MssResult(True, direction, swing_high, f"15m MSS confirmed: close {last_bar['close']:.4f} displaced above level {swing_high:.4f}", invalidation_level=swing_low)
        return MssResult(False, direction, swing_high, f"15m waiting for pullback exhaustion (swing high={swing_high:.4f}, close={last_bar['close']:.4f})", invalidation_level=swing_low)

    elif direction == "bearish":
        min_disp = max(swing_low * 0.0015, atr_15m * 0.20) if atr_15m > 0 else swing_low * 0.0015
        broke_swing_clean = (last_bar["close"] <= swing_low - min_disp) and (last_bar["close"] < last_bar["open"])
        bearish_displacement = (
            last_bar["close"] <= swing_low - min_disp
            and last_bar["close"] < prev_bar["low"]
            and last_bar["close"] < last_bar["open"]
            and body_ratio >= 0.40
            and (last_bar["high"] - last_bar["close"]) >= c_range * 0.55
        )

        if (broke_swing_clean or bearish_displacement) and vol_pass:
            return MssResult(True, direction, swing_low, f"15m MSS confirmed: close {last_bar['close']:.4f} displaced below level {swing_low:.4f}", invalidation_level=swing_high)
        return MssResult(False, direction, swing_low, f"15m waiting for bounce exhaustion (swing low={swing_low:.4f}, close={last_bar['close']:.4f})", invalidation_level=swing_high)

    return MssResult(False, "none", 0.0, "Unknown direction")


def analyze_5m_trigger(
    candles_5m: list[dict],
    direction: str,
    structural_sl_level: float = 0.0,
    atr_15m: float = 0.0,
) -> TriggerResult:
    """5-Minute (5m): Precision trigger & risk definition.
    - Long: Micro trigger (bullish 5m bar) with SL anchored to structural swing low / minimum ATR pad.
    - Short: Micro trigger (bearish 5m bar) with SL anchored to structural swing high / minimum ATR pad.
    """
    if len(candles_5m) < 5:
        return TriggerResult(False, direction, 0.0, 0.0, 0.0, "Insufficient 5m candles (< 5)")

    last_bar = candles_5m[-1]
    prev_bar = candles_5m[-2]
    entry_price = last_bar["close"]

    # Micro structure over last 5 bars
    micro_window = candles_5m[-6:-1]

    if direction == "bullish":
        is_bullish_candle = last_bar["close"] > last_bar["open"]
        rng = max(last_bar["high"] - last_bar["low"], 1e-6)
        closes_upper_half = (last_bar["close"] - last_bar["low"]) >= rng * 0.50
        micro_break = (last_bar["close"] >= prev_bar["high"]) or (last_bar["close"] > max(b["high"] for b in micro_window[-3:]))

        if is_bullish_candle and closes_upper_half and micro_break:
            lowest_low = min(b["low"] for b in candles_5m[-6:])
            base_pivot = min(lowest_low, structural_sl_level) if structural_sl_level > 0 else lowest_low
            pad = max(entry_price * 0.0015, (atr_15m * 0.20) if atr_15m > 0 else 0.0)
            sl_price = base_pivot - pad
            sl_dist = entry_price - sl_price
            if sl_dist <= 0:
                return TriggerResult(False, direction, entry_price, 0.0, 0.0, "Invalid SL calculation (SL >= entry)")
            # Minimum SL buffer: require at least 0.8% distance to prevent noise stop-outs
            min_sl_dist = max(entry_price * 0.008, (1.0 * atr_15m) if atr_15m > 0 else (entry_price * 0.008))
            if sl_dist < min_sl_dist:
                sl_price = entry_price - min_sl_dist
                sl_dist = min_sl_dist
            return TriggerResult(True, direction, entry_price, sl_price, sl_dist, f"5m Long trigger active @ {entry_price:.4f}, SL @ {sl_price:.4f}")
        return TriggerResult(False, direction, entry_price, 0.0, 0.0, "5m waiting for bullish micro impulse trigger")

    elif direction == "bearish":
        is_bearish_candle = last_bar["close"] < last_bar["open"]
        rng = max(last_bar["high"] - last_bar["low"], 1e-6)
        closes_lower_half = (last_bar["high"] - last_bar["close"]) >= rng * 0.50
        micro_break = (last_bar["close"] <= prev_bar["low"]) or (last_bar["close"] < min(b["low"] for b in micro_window[-3:]))

        if is_bearish_candle and closes_lower_half and micro_break:
            highest_high = max(b["high"] for b in candles_5m[-6:])
            base_pivot = max(highest_high, structural_sl_level) if structural_sl_level > 0 else highest_high
            pad = max(entry_price * 0.0015, (atr_15m * 0.20) if atr_15m > 0 else 0.0)
            sl_price = base_pivot + pad
            sl_dist = sl_price - entry_price
            if sl_dist <= 0:
                return TriggerResult(False, direction, entry_price, 0.0, 0.0, "Invalid SL calculation (SL <= entry)")
            # Minimum SL buffer: require at least 0.8% distance to prevent noise stop-outs
            min_sl_dist = max(entry_price * 0.008, (1.0 * atr_15m) if atr_15m > 0 else (entry_price * 0.008))
            if sl_dist < min_sl_dist:
                sl_price = entry_price + min_sl_dist
                sl_dist = min_sl_dist
            return TriggerResult(True, direction, entry_price, sl_price, sl_dist, f"5m Short trigger active @ {entry_price:.4f}, SL @ {sl_price:.4f}")
        return TriggerResult(False, direction, entry_price, 0.0, 0.0, "5m waiting for bearish micro impulse trigger")

    return TriggerResult(False, "none", 0.0, 0.0, 0.0, "Unknown direction")


# -----------------------------------------------------------------------------
# Risk Management & Bracket Arithmetic
# -----------------------------------------------------------------------------

def calculate_risk_position_size(
    symbol: str,
    entry_price: float,
    sl_price: float,
    leverage: int = 10,
    max_loss_usd: float = FIXED_LOSS_USD,
    bankroll: float = DEFAULT_BANKROLL,
    max_margin_pct: float = DEFAULT_MAX_MARGIN_PCT,
) -> dict[str, Any]:
    """Calculate dynamic position size strictly honoring:
    1. Fixed loss: Exactly $3.00 USD max loss at Stop Loss.
       formula: qty = 3.00 / abs(entry_price - sl_price)
    2. Binance minNotional >= $5.00 constraint.
    3. Maximum margin constraint: margin <= 50% of bankroll ($50 margin at $100 bankroll).
    4. Symbol quantization to stepSize and minQty.
    """
    sl_dist = abs(entry_price - sl_price)
    if sl_dist <= 0 or entry_price <= 0:
        raise ValueError(f"Invalid entry ({entry_price}) or SL ({sl_price}) price")

    spec = SYMBOL_SPECS.get(symbol, {"step_size": 0.01, "price_tick": 0.01, "min_qty": 0.01, "min_notional": 5.0})
    step = spec["step_size"]
    min_qty = spec["min_qty"]
    min_notional = spec.get("min_notional", 5.0)

    # 1. Base unconstrained sizing for exact $3.00 loss
    raw_qty = max_loss_usd / sl_dist

    # 2. Maximum margin cap ($50.00 max margin at 50% of $100 bankroll)
    max_allowed_margin = bankroll * max_margin_pct
    max_allowed_notional = max_allowed_margin * leverage
    max_qty_by_margin = max_allowed_notional / entry_price
    if raw_qty > max_qty_by_margin:
        raw_qty = max_qty_by_margin

    # 3. Minimum notional bound (Binance >= $5.00 USDT)
    min_qty_by_notional = min_notional / entry_price
    if raw_qty < min_qty_by_notional:
        raw_qty = min_qty_by_notional

    # 4. Symbol min_qty bound
    if raw_qty < min_qty:
        raw_qty = min_qty

    # 5. Quantize to symbol step_size
    qty = round_step(raw_qty, step)
    if qty < min_qty:
        # Bump up to min_qty if rounding down brought it under
        precision = max(0, int(round(-math.log10(step))))
        factor = 10 ** precision
        qty = round(math.ceil(raw_qty * factor - 1e-9) / factor, precision)
        qty = max(qty, min_qty)

    actual_notional = round(qty * entry_price, 2)
    margin_used = round(actual_notional / leverage, 2)
    estimated_loss = round(qty * sl_dist, 4)

    return {
        "qty": qty,
        "notional_usd": actual_notional,
        "margin_usd": margin_used,
        "estimated_loss_usd": estimated_loss,
        "sl_dist": sl_dist,
        "sl_dist_pct": round((sl_dist / entry_price) * 100, 3),
        "leverage": leverage,
    }


def calculate_bracket_orders(
    symbol: str,
    direction: str,
    entry_price: float,
    sl_price: float,
    min_rr: float = MIN_RISK_REWARD,
) -> dict[str, Any]:
    """Calculate bracket Take-Profit and Stop-Loss orders with minimum 1:3 R:R ratio.
    - Long: TP >= Entry + 3.0 * (Entry - SL)
    - Short: TP <= Entry - 3.0 * (SL - Entry)
    """
    sl_dist = abs(entry_price - sl_price)
    if sl_dist <= 0 or entry_price <= 0:
        raise ValueError(f"Invalid entry {entry_price} or sl {sl_price}")

    spec = SYMBOL_SPECS.get(symbol, {"price_tick": 0.01, "step_size": 0.01})
    tick = spec["price_tick"]

    min_tp_dist = min_rr * sl_dist

    if direction == "bullish":
        raw_tp = entry_price + min_tp_dist
        tp_tick = round_tick(raw_tp, tick)
        # Guarantee R:R >= min_rr strictly even after rounding
        if (tp_tick - entry_price) < min_tp_dist - 1e-7:
            tp_tick = round_tick(tp_tick + tick, tick)

        sl_tick = round_tick(sl_price, tick)
        if sl_tick >= entry_price:
            sl_tick = round_tick(entry_price - tick, tick)

        actual_rr = round((tp_tick - entry_price) / max(entry_price - sl_tick, 1e-6), 3)

    elif direction == "bearish":
        raw_tp = entry_price - min_tp_dist
        tp_tick = round_tick(raw_tp, tick)
        # Guarantee R:R >= min_rr strictly even after rounding
        if (entry_price - tp_tick) < min_tp_dist - 1e-7:
            tp_tick = round_tick(tp_tick - tick, tick)

        sl_tick = round_tick(sl_price, tick)
        if sl_tick <= entry_price:
            sl_tick = round_tick(entry_price + tick, tick)

        actual_rr = round((entry_price - tp_tick) / max(sl_tick - entry_price, 1e-6), 3)
    else:
        raise ValueError(f"Unknown direction: {direction}")

    return {
        "symbol": symbol,
        "direction": direction,
        "entry_price": entry_price,
        "sl_price": sl_tick,
        "tp_price": tp_tick,
        "sl_dist": sl_dist,
        "tp_dist": abs(tp_tick - entry_price),
        "actual_rr": actual_rr,
        "min_rr_met": actual_rr >= (min_rr - 1e-4),
    }


def check_sr_barrier_guard(
    candles_4h: list[dict],
    candles_1h: list[dict],
    direction: str,
    entry_price: float,
    sl_price: float,
) -> tuple[bool, str, dict[str, Any]]:
    """Guard against shorting into major support floors or longing into major resistance ceilings.
    Rules:
    1. Lookback: Prior 24h-48h established swing levels across closed 4H and 1H bars (excluding incomplete bar).
    2. Minimum Buffer: Distance to barrier must be >= max(0.35 * atr_1h, entry_price * 0.0035).
    3. Minimum Runway: Distance to barrier must be >= 0.40 * sl_dist (at least 0.4R runway before barrier).
    """
    if len(candles_4h) < 12 or len(candles_1h) < 20:
        return True, "Insufficient 4H/1H candles for S/R barrier audit", {}

    atr_1h = compute_atr(candles_1h, 14)
    sl_dist = abs(entry_price - sl_price)

    # Use closed bars excluding current active/trigger bar
    closed_4h = candles_4h[-13:-1]
    closed_1h = candles_1h[-25:-1]

    major_support = min(min(b["low"] for b in closed_4h), min(b["low"] for b in closed_1h))
    major_resistance = max(max(b["high"] for b in closed_4h), max(b["high"] for b in closed_1h))

    min_buffer = max(0.35 * atr_1h, entry_price * 0.0035)
    min_runway = sl_dist * 0.40

    if direction == "bearish":
        dist_to_floor = entry_price - major_support
        if dist_to_floor < min_buffer:
            return False, f"Short rejected: entry {entry_price:.4f} too close to major support floor {major_support:.4f} (dist={dist_to_floor:.4f} < {min_buffer:.4f}, bounce risk)", {
                "major_support": major_support, "dist_to_floor": dist_to_floor, "min_buffer": min_buffer
            }
        if dist_to_floor < min_runway:
            return False, f"Short rejected: insufficient room to major support floor {major_support:.4f} (dist={dist_to_floor:.4f} < {min_runway:.4f}, negative runway)", {
                "major_support": major_support, "dist_to_floor": dist_to_floor, "min_runway": min_runway
            }
        return True, "Bearish S/R barrier audit passed (safe distance from support floor)", {
            "major_support": major_support, "dist_to_floor": dist_to_floor, "min_buffer": min_buffer
        }

    elif direction == "bullish":
        dist_to_ceiling = major_resistance - entry_price
        if dist_to_ceiling < min_buffer:
            return False, f"Long rejected: entry {entry_price:.4f} too close to major resistance ceiling {major_resistance:.4f} (dist={dist_to_ceiling:.4f} < {min_buffer:.4f}, rejection risk)", {
                "major_resistance": major_resistance, "dist_to_ceiling": dist_to_ceiling, "min_buffer": min_buffer
            }
        if dist_to_ceiling < min_runway:
            return False, f"Long rejected: insufficient room to major resistance ceiling {major_resistance:.4f} (dist={dist_to_ceiling:.4f} < {min_runway:.4f}, negative runway)", {
                "major_resistance": major_resistance, "dist_to_ceiling": dist_to_ceiling, "min_runway": min_runway
            }
        return True, "Bullish S/R barrier audit passed (safe distance from resistance ceiling)", {
            "major_resistance": major_resistance, "dist_to_ceiling": dist_to_ceiling, "min_buffer": min_buffer
        }

    return True, "Unknown direction, bypassing barrier audit", {}


# -----------------------------------------------------------------------------
# End-to-End MTF Pipeline Evaluator
# -----------------------------------------------------------------------------

def evaluate_mtf_pipeline(
    symbol: str,
    candles_by_tf: dict[str, list[dict]],
    min_rr: float = MIN_RISK_REWARD,
) -> MTFPipelineResult:
    """Evaluate Top-Down Multi-Timeframe hierarchy:
    Daily (1D) ➔ 4-Hour (4H) ➔ 1-Hour (1H) ➔ 15-Minute (15m) ➔ 5-Minute (5m).
    """
    diagnostics: dict[str, Any] = {}

    # Stage 1: Daily Bias
    c_1d = candles_by_tf.get("1d", [])
    res_1d = analyze_1d_bias(c_1d)
    diagnostics["1d"] = {"bias": res_1d.bias, "reason": res_1d.reason}
    if res_1d.bias == "neutral":
        return MTFPipelineResult(
            False, symbol, "none", 0.0, 0.0, 0.0, 0.0, "1d_bias_neutral", diagnostics
        )

    direction = res_1d.bias

    # Stage 2: 4H Alignment
    c_4h = candles_by_tf.get("4h", [])
    res_4h = analyze_4h_alignment(c_4h, direction)
    diagnostics["4h"] = {"aligned": res_4h.aligned, "reason": res_4h.reason}
    if not res_4h.aligned:
        return MTFPipelineResult(
            False, symbol, direction, 0.0, 0.0, 0.0, 0.0, "4h_alignment_rejected", diagnostics
        )

    # Stage 3: 1H Setup Identification (Pullback into value)
    c_1h = candles_by_tf.get("1h", [])
    res_1h = analyze_1h_setup(c_1h, direction)
    diagnostics["1h"] = {"setup_ready": res_1h.setup_ready, "reason": res_1h.reason}
    if not res_1h.setup_ready:
        return MTFPipelineResult(
            False, symbol, direction, 0.0, 0.0, 0.0, 0.0, "1h_setup_waiting", diagnostics
        )

    # Stage 4: 15m MSS (Market Structure Shift)
    c_15m = candles_by_tf.get("15m", [])
    res_15m = analyze_15m_mss(c_15m, direction)
    diagnostics["15m"] = {"mss_confirmed": res_15m.mss_confirmed, "reason": res_15m.reason}
    if not res_15m.mss_confirmed:
        return MTFPipelineResult(
            False, symbol, direction, 0.0, 0.0, 0.0, 0.0, "15m_mss_waiting", diagnostics
        )

    atr_15m = compute_atr(c_15m, 14)

    # Stage 5: 5m Precision Trigger
    c_5m = candles_by_tf.get("5m", [])
    res_5m = analyze_5m_trigger(
        c_5m,
        direction,
        structural_sl_level=res_15m.invalidation_level,
        atr_15m=atr_15m,
    )
    diagnostics["5m"] = {"triggered": res_5m.triggered, "reason": res_5m.reason}
    if not res_5m.triggered:
        return MTFPipelineResult(
            False, symbol, direction, 0.0, 0.0, 0.0, 0.0, "5m_trigger_waiting", diagnostics
        )

    entry_px = res_5m.entry_price
    sl_px = res_5m.sl_price

    # Stage 6: Major Support & Resistance Barrier Guard (Anti-Bounce / Anti-Rejection)
    sr_ok, sr_reason, sr_data = check_sr_barrier_guard(
        candles_4h=c_4h,
        candles_1h=c_1h,
        direction=direction,
        entry_price=entry_px,
        sl_price=sl_px,
    )
    diagnostics["sr_barrier"] = {"passed": sr_ok, "reason": sr_reason, **sr_data}
    if not sr_ok:
        stage_name = "support_floor_guard" if direction == "bearish" else "resistance_ceiling_guard"
        return MTFPipelineResult(
            False, symbol, direction, 0.0, 0.0, 0.0, 0.0, stage_name, diagnostics
        )

    # Bracket Calculation (1:3 min R:R)
    bracket = calculate_bracket_orders(symbol, direction, entry_px, sl_px, min_rr)
    diagnostics["bracket"] = bracket

    return MTFPipelineResult(
        valid=True,
        symbol=symbol,
        direction=direction,
        entry_price=entry_px,
        sl_price=bracket["sl_price"],
        tp_price=bracket["tp_price"],
        risk_reward=bracket["actual_rr"],
        stage_failed=None,
        diagnostics=diagnostics,
    )
