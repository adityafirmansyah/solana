#!/usr/bin/env python3
"""Satoshi's predictor: a multi-parameter directional model with abstention.

Ported from Nugget's predictor.py (XAUUSD). v1 is deliberately the SAME
interpretable technical parameter set — the point is not to invent a
SOL-specific model on day one, it is to start measurable and let cronjob #4
(improve.py) earn any change out-of-sample, same discipline as gold.

Parameters (all observable at decision time, no look-ahead):
  trend     : EMA12 vs EMA26
  position  : close vs SMA20, normalized by ATR
  momentum  : 24-bar rate of change
  rsi       : RSI14 distance from 50
  breadth   : higher-high / lower-low structure over 6 bars
  volume    : current volume vs 24-bar average (confirmation, not direction)
  session_sweep : Asian-session liquidity sweep (ported as-is, works on any
                  OHLC — not gold-specific)

What's DELIBERATELY DROPPED from Nugget's v1, and why:
  - dxy_proxy (EURUSDT rate-of-change as a dollar-direction proxy): that
    signal's premise is "gold is dollar-priced, so dollar strength is a
    headwind" — a story specific to a dollar-denominated commodity. SOL's
    relationship to dollar strength is not the same mechanism (and is itself
    an unsettled empirical question), so porting the weight-0.0-until-earned
    pattern here would be inventing a parameter on priors, not measuring one.
    Add it back ONLY if/when a specific macro hypothesis is being tested.

The model emits a score in [-1, +1] and abstains when |score| is below the
confidence bar — a forced call on a flat market is noise, not a prediction.
Abstention is recorded as a legitimate outcome, same as Nugget.
"""
from __future__ import annotations

import statistics

MODEL_VERSION = "v0.1-event-driven-sol"
NOISE_ATR_MULT = 0.5      # moves below this fraction of ATR14 are noise
ABSTAIN_BAR = 0.28        # |score| below this -> no_call (calibrated from 0.18)
TP_ATR_MULT_1H = 1.5         # Target / Take-Profit multiplier for 1h (1.5x ATR14, R:R 1:1.5)
TP_ATR_MULT_SWING = 2.0      # Target / Take-Profit multiplier for 8h/12h swing (2.0x ATR14, R:R 1:2.0)
SL_ATR_MULT = 1.0            # Stop-Loss multiplier (1.0x ATR14)
MAX_HOLDING_HOURS_1H = 6     # Maximum holding time for 1h (calibrated from 24h to prevent lockout)
MAX_HOLDING_HOURS_15M = 3    # Maximum holding time for 15m (12 bars / 3 hours)
MAX_HOLDING_HOURS_EVENT = 2  # Maximum holding time for event-driven breakout (2 hours)
MAX_HOLDING_HOURS = MAX_HOLDING_HOURS_1H

# Backwards compatibility alias
TP_ATR_MULT = TP_ATR_MULT_1H


def get_max_holding_hours(horizon: str = "1h") -> float:
    """Return maximum holding hours before time-stop conclusion."""
    if horizon == "event":
        return MAX_HOLDING_HOURS_EVENT
    elif horizon == "15m":
        return MAX_HOLDING_HOURS_15M
    elif horizon == "1h":
        return MAX_HOLDING_HOURS_1H
    return 24.0


def get_tp_mult_for_horizon(horizon: str = "1h") -> float:
    """Return Take-Profit ATR multiplier calibrated for horizon."""
    if horizon in ("8h", "12h", "24h", "1d"):
        return TP_ATR_MULT_SWING
    return TP_ATR_MULT_1H


def compute_trade_levels(
    ref_price: float,
    atr: float,
    direction: str,
    p_bull: float = 0.5,
    p_bear: float = 0.5,
    horizon: str = "1h",
    tp_mult: float | None = None,
    sl_mult: float | None = None,
) -> dict:
    """Compute target and stop loss points & prices based on ATR14.

    For bullish (or bullish lean): TP above ref (+), SL below ref (-).
    For bearish (or bearish lean): TP below ref (-), SL above ref (+).
    """
    atr = max(0.0, float(atr or 0.0))
    ref = float(ref_price or 0.0)
    if tp_mult is None:
        tp_mult = get_tp_mult_for_horizon(horizon)
    if sl_mult is None:
        sl_mult = SL_ATR_MULT

    tp_pts = round(tp_mult * atr, 2)
    sl_pts = round(sl_mult * atr, 2)

    is_bull = direction == "bullish" or (direction == "no_call" and p_bull >= p_bear)

    if is_bull:
        tp_price = round(ref + tp_pts, 2)
        sl_price = round(ref - sl_pts, 2)
        tp_sign = "+"
        sl_sign = "-"
    else:
        tp_price = round(ref - tp_pts, 2)
        sl_price = round(ref + sl_pts, 2)
        tp_sign = "-"
        sl_sign = "+"

    rr_ratio = round(tp_mult / sl_mult, 2) if sl_mult > 0 else 0.0

    return {
        "ref_price": round(ref, 2),
        "tp_price": tp_price,
        "sl_price": sl_price,
        "tp_points": tp_pts,
        "sl_points": sl_pts,
        "tp_mult": tp_mult,
        "sl_mult": sl_mult,
        "rr_ratio": rr_ratio,
        "tp_sign": tp_sign,
        "sl_sign": sl_sign,
        "is_bull_side": is_bull,
    }


def ema(vals, period):
    if len(vals) < period:
        return None
    k = 2.0 / (period + 1)
    e = statistics.fmean(vals[:period])
    for v in vals[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))][-period:]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))][-period:]
    ag, al = statistics.fmean(gains), statistics.fmean(losses)
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
               abs(lows[i] - closes[i - 1])) for i in range(1, len(closes))][-period:]
    return statistics.fmean(trs)


def build_params():
    """The parameter set. Re-weighted based on empirical validation on SOLUSDT:
    - RSI and Position are primary high-win-rate drivers.
    - Breadth zeroed out (buying 6h highs bought exhaustion wicks).
    - Cross-venue drift added to capture Binance vs CoinGecko basis mean-reversion.
    """
    return {
        "trend": {"weight": 0.20, "lookback": [12, 26]},
        "position": {"weight": 0.30, "lookback": 20},
        "momentum": {"weight": 0.10, "lookback": 24},
        "rsi": {"weight": 0.35, "lookback": 14},
        "breadth": {"weight": 0.0, "lookback": 6},
        "volume_confirm": {"weight": 0.0, "lookback": 24},
        "cross_venue_drift": {"weight": 0.05, "lookback": 1},
        "session_sweep": {"weight": 0.0, "lookback": 8},
    }


def session_sweep_signal(bars: list[dict]) -> int:
    """Liquidity-sweep microstructure: did price take out the Asian session's
    high/low and then reject back inside it?

    Ported verbatim from Nugget — this is pure OHLC structure, not gold-
    specific. Crypto desks read the same stop-run pattern around the same
    session boundary (Tokyo/Singapore/HK morning). Needs only OHLC already
    fetched every hour; no new API call. Starts at weight 0.0 (INERT) —
    same promotion discipline as gold: the gate must earn it a non-zero
    weight, not a hand-raise.

    Returns +1 (swept the low, closed back above -> bullish), -1 (swept the
    high, closed back below -> bearish), or 0 (no sweep / not enough bars).
    """
    if len(bars) < 32 or "open_time" not in bars[0]:
        return 0

    from datetime import datetime, timezone
    asian_bars = []
    for b in bars[-32:-1]:  # the window BEFORE the current/last bar
        hour = datetime.fromtimestamp(b["open_time"] / 1000, timezone.utc).hour
        if 0 <= hour < 8:
            asian_bars.append(b)
    if len(asian_bars) < 4:
        return 0

    asian_high = max(b["high"] for b in asian_bars)
    asian_low = min(b["low"] for b in asian_bars)
    last = bars[-1]

    if last["low"] < asian_low and last["close"] > asian_low:
        return 1
    if last["high"] > asian_high and last["close"] < asian_high:
        return -1
    return 0


def compute_features(bars: list[dict], cross_check_sol: float | None = None) -> dict:
    """Technical feature set from OHLCV bars.

    Uses completed closed bars to avoid unclosed partial-hour volume and candle distortion.
    """
    closed_bars = bars[:-1] if len(bars) > 60 else bars
    closes = [b["close"] for b in closed_bars]
    highs = [b["high"] for b in closed_bars]
    lows = [b["low"] for b in closed_bars]
    vols = [b["volume"] for b in closed_bars]

    a = atr(highs, lows, closes, 14)
    e12, e26 = ema(closes, 12), ema(closes, 26)
    sma20 = statistics.fmean(closes[-20:])
    r = rsi(closes, 14)
    avg_vol = statistics.fmean(vols[-24:]) if len(vols) >= 24 else statistics.fmean(vols)

    cross_drift = 0.0
    if cross_check_sol and a:
        delta = closes[-1] - cross_check_sol
        # Binance premium -> fade downwards (-); Binance discount -> fade upwards (+)
        cross_drift = -1.0 * max(-1.0, min(1.0, delta / 35.0))

    return {
        "close": closes[-1],
        "ema12": e12,
        "ema26": e26,
        "sma20": sma20,
        "rsi14": r,
        "atr14": a,
        "roc24": (closes[-1] - closes[-25]) / closes[-25] * 100 if len(closes) > 25 else None,
        "mom1": (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) > 2 else None,
        "higher_high_6": closes[-1] > max(highs[-7:-1]) if len(highs) > 7 else None,
        "lower_low_6": closes[-1] < min(lows[-7:-1]) if len(lows) > 7 else None,
        "vol_vs_avg": (vols[-1] / avg_vol) if avg_vol else None,
        "atr_pct": (a / closes[-1] * 100) if a else None,
        "bars": len(bars),
        "session_sweep": session_sweep_signal(closed_bars),
        "cross_venue_drift": round(cross_drift, 4),
    }


def score(f: dict, params: dict | None = None) -> tuple[float, dict]:
    """Return (score in [-1,1], per-parameter contributions)."""
    params = params or build_params()
    contrib = {}

    p = params["trend"]
    if f["ema12"] is not None and f["ema26"] is not None and f["atr14"]:
        d = (f["ema12"] - f["ema26"]) / f["atr14"]
        contrib["trend"] = p["weight"] * max(-1.0, min(1.0, d))

    p = params["position"]
    if f["atr14"]:
        d = (f["close"] - f["sma20"]) / f["atr14"]
        contrib["position"] = p["weight"] * max(-1.0, min(1.0, d))

    p = params["momentum"]
    if f["roc24"] is not None:
        contrib["momentum"] = p["weight"] * max(-1.0, min(1.0, f["roc24"] / 0.5))

    p = params["rsi"]
    if f["rsi14"] is not None:
        contrib["rsi"] = p["weight"] * max(-1.0, min(1.0, (f["rsi14"] - 50) / 20))

    p = params["breadth"]
    if f["higher_high_6"] is not None:
        b = (1 if f["higher_high_6"] else 0) + (-1 if f["lower_low_6"] else 0)
        contrib["breadth"] = p["weight"] * b

    p = params["volume_confirm"]
    if f["vol_vs_avg"] is not None:
        direction = 1 if contrib.get("trend", 0) >= 0 else -1
        strength = max(-1.0, min(1.0, (f["vol_vs_avg"] - 1.0) / 1.0))
        contrib["volume_confirm"] = p["weight"] * direction * strength

    p = params.get("cross_venue_drift", {"weight": 0.0})
    drift = f.get("cross_venue_drift")
    if drift is not None:
        contrib["cross_venue_drift"] = p["weight"] * max(-1.0, min(1.0, float(drift)))

    # session_sweep: inert until the improvement gate earns it a weight
    p = params.get("session_sweep", {"weight": 0.0})
    sweep = f.get("session_sweep")
    if sweep:
        contrib["session_sweep"] = p["weight"] * max(-1.0, min(1.0, float(sweep)))

    total = sum(contrib.values())
    total = max(-1.0, min(1.0, total))
    return total, {k: round(v, 4) for k, v in contrib.items()}


def explain_prediction(features: dict, contributions: dict, direction: str, score: float, noise_thr: float, p_bull: float | None = None, p_bear: float | None = None) -> str:
    """Generate a clean, human-readable rationale for the prediction."""
    if direction == "no_call":
        regime = features.get("regime_abstain")
        if regime:
            return f"Abstained: {regime}. Volatility filter: ${noise_thr:.2f}."
        return f"Market in low-conviction chop (|score|={abs(score):.2f} < {ABSTAIN_BAR:.2f} abstain threshold). Volatility filter: ${noise_thr:.2f}."

    # Sort strongest contributing parameters
    drivers = sorted(contributions.items(), key=lambda kv: abs(kv[1]), reverse=True)
    top_pos = [f"{k} (+{v:.2f})" for k, v in drivers if v > 0.03]
    top_neg = [f"{k} ({v:.2f})" for k, v in drivers if v < -0.03]

    parts = []
    close = features.get("close", 0)
    sma20 = features.get("sma20", 0)
    rsi14 = features.get("rsi14", 50)
    ema12 = features.get("ema12", 0)
    ema26 = features.get("ema26", 0)

    if direction == "bullish":
        reasons = []
        if close > sma20:
            reasons.append(f"Price (${close:.0f}) holding above 20-SMA (${sma20:.0f})")
        if ema12 > ema26:
            reasons.append("Fast EMA12 above EMA26")
        if rsi14 > 50:
            reasons.append(f"RSI bullish momentum ({rsi14:.1f})")

        main_story = ". ".join(reasons) if reasons else "Confluence of technical momentum indicators"
        parts.append(f"Bullish bias: {main_story}.")
    else:
        reasons = []
        if close < sma20:
            reasons.append(f"Price (${close:.0f}) rejected below 20-SMA (${sma20:.0f})")
        if ema12 < ema26:
            reasons.append("Fast EMA12 under EMA26")
        if rsi14 < 50:
            reasons.append(f"RSI bearish momentum ({rsi14:.1f})")

        main_story = ". ".join(reasons) if reasons else "Confluence of technical momentum indicators"
        parts.append(f"Bearish bias: {main_story}.")

    if top_pos:
        parts.append(f"Key drivers: {', '.join(top_pos)}.")
    if top_neg:
        parts.append(f"Drag/headwinds: {', '.join(top_neg)}.")

    return " ".join(parts)


def check_regime_abstain(features: dict, score: float, current_utc=None, horizon: str = "1h") -> str | None:
    """Regime filter: abstains on high volatility, crossover turnover, or RSI exhaustion wicks."""
    atr = features.get("atr14") or 0
    max_atr = {
        "event": 2.50,
        "5m": 1.20,
        "15m": 1.80,
        "1h": 3.50,
        "8h": 8.00,
        "12h": 12.00,
        "24h": 18.00,
    }.get(horizon, 3.50)

    if atr > max_atr:
        return f"High volatility regime (ATR14=${atr:.2f} > ${max_atr:.2f})"

    # Crossover turnover window is specific to 1h intraday session turnover
    if horizon == "1h" and current_utc:
        hr = current_utc.hour if hasattr(current_utc, "hour") else int(str(current_utc)[11:13])
        if hr in [12, 18]:
            return f"Structural crossover turnover window (UTC {hr:02d}:00)"

    rsi = features.get("rsi14", 50)
    if score > 0 and rsi > 75:
        return f"Overbought exhaustion risk (RSI14={rsi:.1f} > 75)"
    if score < 0 and rsi < 25:
        return f"Oversold exhaustion risk (RSI14={rsi:.1f} < 25)"

    return None


def predict(bars: list[dict], params: dict | None = None, cross_check_sol: float | None = None, current_utc=None, horizon: str = "1h") -> dict:
    """Full decision: direction, confidence, features, contributions."""
    features = compute_features(bars, cross_check_sol=cross_check_sol)
    s, contrib = score(features, params)

    # Calculate probabilistic confidence for BOTH directions (sums to 100%)
    p_bull = round(0.5 + 0.5 * s, 4)
    p_bear = round(0.5 - 0.5 * s, 4)

    # Volatility, Session & Exhaustion Regime Filters
    regime_abstain = check_regime_abstain(features, s, current_utc=current_utc, horizon=horizon)

    if regime_abstain:
        direction = "no_call"
        features["regime_abstain"] = regime_abstain
    elif abs(s) < ABSTAIN_BAR:
        direction = "no_call"
    else:
        direction = "bullish" if s > 0 else "bearish"

    noise_thr = round(NOISE_ATR_MULT * (features["atr14"] or 0), 4)
    reason = explain_prediction(features, contrib, direction, s, noise_thr, p_bull, p_bear)

    return {
        "direction": direction,
        "score": round(s, 4),
        "confidence": round(min(1.0, abs(s)), 4),
        "bullish_confidence": p_bull,
        "bearish_confidence": p_bear,
        "features": features,
        "contributions": contrib,
        "model_version": MODEL_VERSION,
        "params": params or build_params(),
        "noise_threshold": noise_thr,
        "reason": reason,
        "max_holding_hours": get_max_holding_hours(horizon),
    }


if __name__ == "__main__":
    import json
    import urllib.request

    UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/125 Safari/537.36"
    url = ("https://data-api.binance.vision/api/v3/klines"
           "?symbol=SOLUSDT&interval=1h&limit=200")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    raw = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
    bars = [{"open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
             "close": float(k[4]), "volume": float(k[5]),
             "open_time": int(k[0])} for k in raw]

    out = predict(bars)
    print(f"MODEL {out['model_version']}")
    print(f"  direction   : {out['direction']}")
    print(f"  score       : {out['score']:+.4f}")
    print(f"  confidence  : {out['confidence']:.4f}")
    print(f"  noise_thr   : {out['noise_threshold']}")
    print(f"  contributions:")
    for k, v in sorted(out["contributions"].items(), key=lambda x: -abs(x[1])):
        print(f"    {k:<16} {v:+.4f}")
    print(f"  key features: RSI={out['features']['rsi14']:.2f} "
          f"ATR={out['features']['atr14']:.2f} "
          f"close_vs_sma20={((out['features']['close']-out['features']['sma20'])/out['features']['sma20']*100):+.3f}%")
