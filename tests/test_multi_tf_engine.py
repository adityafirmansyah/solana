#!/usr/bin/env python3
"""Comprehensive Unit and Integration Test Suite for Top-Down Multi-Timeframe Trading Engine.

Verifies:
a) HTF to LTF filter logic (Daily -> 4H -> 1H -> 15m -> 5m)
b) Dynamic position sizing ($5 max loss arithmetic across different SL distances & bounds)
c) 1:3 minimum Risk:Reward bracket calculation
d) End-to-end pipeline execution with synthesized candle cascades (Bullish & Bearish)
e) Real-world candle ingestion robustness across the 5 universe coins
"""
import math
import sys
import unittest
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from multi_tf_strategy import (
    analyze_1d_bias,
    analyze_4h_alignment,
    analyze_1h_setup,
    analyze_15m_mss,
    analyze_5m_trigger,
    calculate_risk_position_size,
    calculate_bracket_orders,
    evaluate_mtf_pipeline,
    compute_ema,
    compute_atr,
    check_sr_barrier_guard,
    check_market_saturation,
    FIXED_LOSS_USD,
    MIN_RISK_REWARD,
)
import fetch_market as fm


def make_candle(open_p: float, high_p: float, low_p: float, close_p: float, open_time: int = 1000) -> dict:
    return {
        "open_time": open_time,
        "open": float(open_p),
        "high": float(high_p),
        "low": float(low_p),
        "close": float(close_p),
        "volume": 100.0,
        "trades": 50,
    }


def make_series(base_price: float, step: float, count: int, noise: float = 0.5) -> list[dict]:
    """Generate simple synthetic candle sequence."""
    candles = []
    current = base_price
    for i in range(count):
        o = current
        c = current + step
        h = max(o, c) + noise
        l = min(o, c) - noise
        candles.append(make_candle(o, h, l, c, open_time=1000 + i * 300000))
        current = c
    return candles


class TestPositionSizingAndRiskContract(unittest.TestCase):
    """Test Suite for Section 3: Risk Management & Position Sizing Contract."""

    def test_fixed_loss_arithmetic_3_usd_default(self):
        """Verify default FIXED_LOSS_USD=3.00 produces ~$3.00 loss at Stop Loss."""
        self.assertEqual(FIXED_LOSS_USD, 3.00)
        entry = 100.0
        sl = 98.0  # $2.00 distance
        res = calculate_risk_position_size("SOLUSDT", entry, sl, leverage=10)
        # raw_qty = 3.00 / 2.0 = 1.5 SOL
        self.assertEqual(res["qty"], 1.5)
        self.assertEqual(res["notional_usd"], 150.0)
        self.assertEqual(res["margin_usd"], 15.0)
        self.assertAlmostEqual(res["estimated_loss_usd"], 3.00, places=2)

    def test_fixed_loss_arithmetic_basic_long(self):
        """Verify qty = 5.00 / abs(entry - sl) produces ~$5.00 loss for LONG when max_loss_usd=5.00."""
        entry = 100.0
        sl = 98.0  # $2.00 distance
        res = calculate_risk_position_size("SOLUSDT", entry, sl, leverage=10, max_loss_usd=5.00)

        # raw_qty = 5.0 / 2.0 = 2.5 SOL
        self.assertEqual(res["qty"], 2.5)
        self.assertEqual(res["notional_usd"], 250.0)
        self.assertEqual(res["margin_usd"], 25.0)
        self.assertAlmostEqual(res["estimated_loss_usd"], 5.00, places=2)

    def test_fixed_loss_arithmetic_basic_short(self):
        """Verify qty = 5.00 / abs(entry - sl) produces ~$5.00 loss for SHORT."""
        entry = 10.0
        sl = 10.50  # $0.50 distance
        res = calculate_risk_position_size("LINKUSDT", entry, sl, leverage=10, max_loss_usd=5.00)

        # raw_qty = 5.0 / 0.50 = 10.0 LINK
        self.assertEqual(res["qty"], 10.0)
        self.assertEqual(res["notional_usd"], 100.0)
        self.assertEqual(res["margin_usd"], 10.0)
        self.assertAlmostEqual(res["estimated_loss_usd"], 5.00, places=2)

    def test_different_sl_distances(self):
        """Verify sizing across narrow and wide SL distances."""
        # 1. Very tight SL ($0.50 distance on $100 entry = 0.5%)
        res_tight = calculate_risk_position_size("SOLUSDT", 100.0, 99.50, leverage=10, max_loss_usd=5.00)
        self.assertEqual(res_tight["margin_usd"], 50.0)
        self.assertEqual(res_tight["qty"], 5.0)
        self.assertLessEqual(res_tight["estimated_loss_usd"], 5.00)

        # 2. Medium SL ($2.50 distance on $100 entry = 2.5%)
        res_med = calculate_risk_position_size("SOLUSDT", 100.0, 97.50, leverage=10, max_loss_usd=5.00)
        self.assertEqual(res_med["qty"], 2.0)
        self.assertEqual(res_med["notional_usd"], 200.0)
        self.assertEqual(res_med["margin_usd"], 20.0)
        self.assertAlmostEqual(res_med["estimated_loss_usd"], 5.00, places=2)

        # 3. Wide SL ($10.00 distance on $100 entry = 10%)
        res_wide = calculate_risk_position_size("SOLUSDT", 100.0, 90.0, leverage=10, max_loss_usd=5.00)
        self.assertEqual(res_wide["qty"], 0.5)
        self.assertEqual(res_wide["notional_usd"], 50.0)
        self.assertAlmostEqual(res_wide["estimated_loss_usd"], 5.00, places=2)

    def test_binance_min_notional_bound(self):
        """Ensure order notional never drops below Binance minNotional ($5.00)."""
        entry = 10.0
        sl = 1.0  # distance = 9.0
        res = calculate_risk_position_size("NEARUSDT", entry, sl, leverage=10)
        self.assertGreaterEqual(res["notional_usd"], 5.00)

    def test_margin_cap_at_50_percent_bankroll(self):
        """Ensure margin used never exceeds 50% of bankroll ($50 margin on $100 bankroll)."""
        entry = 50.0
        sl = 49.99
        res = calculate_risk_position_size("SOLUSDT", entry, sl, leverage=10, bankroll=100.0, max_margin_pct=0.50)
        self.assertLessEqual(res["margin_usd"], 50.00)
        self.assertLessEqual(res["notional_usd"], 500.00)

    def test_multi_symbol_quantization(self):
        """Test step_size and min_qty compliance across universe."""
        coins = ["SOLUSDT", "SUIUSDT", "NEARUSDT", "DOGEUSDT", "LINKUSDT"]
        for sym in coins:
            res = calculate_risk_position_size(sym, 10.0, 9.5, leverage=10)
            self.assertGreater(res["qty"], 0.0)
            self.assertGreaterEqual(res["notional_usd"], 5.0)


class TestBracketOrdersAndRiskReward(unittest.TestCase):
    """Test Suite for Section 3: Minimum 1:3 Risk:Reward Ratio Bracket Contract."""

    def test_long_bracket_minimum_1_to_3_rr(self):
        """Verify for LONG: TP >= Entry + 3.0 * (Entry - SL)."""
        entry = 100.0
        sl = 98.0
        bracket = calculate_bracket_orders("SOLUSDT", "bullish", entry, sl, min_rr=3.0)

        self.assertGreaterEqual(bracket["tp_price"], entry + 3.0 * (entry - sl))
        self.assertGreaterEqual(bracket["actual_rr"], 3.0)
        self.assertTrue(bracket["min_rr_met"])
        self.assertEqual(bracket["sl_price"], 98.0)
        self.assertEqual(bracket["tp_price"], 106.0)

    def test_short_bracket_minimum_1_to_3_rr(self):
        """Verify for SHORT: TP <= Entry - 3.0 * (SL - Entry)."""
        entry = 100.0
        sl = 102.0
        bracket = calculate_bracket_orders("SOLUSDT", "bearish", entry, sl, min_rr=3.0)

        self.assertLessEqual(bracket["tp_price"], entry - 3.0 * (sl - entry))
        self.assertGreaterEqual(bracket["actual_rr"], 3.0)
        self.assertTrue(bracket["min_rr_met"])
        self.assertEqual(bracket["sl_price"], 102.0)
        self.assertEqual(bracket["tp_price"], 94.0)

    def test_tick_rounding_preserves_min_rr(self):
        """Ensure tick quantization never violates 1:3 R:R (e.g. 2.99x)."""
        entry = 11.653
        sl = 11.789
        bracket = calculate_bracket_orders("LINKUSDT", "bearish", entry, sl, min_rr=3.0)
        self.assertGreaterEqual(bracket["actual_rr"], 3.0)
        self.assertTrue(bracket["min_rr_met"])


class TestTopDownMultiTimeframeFilters(unittest.TestCase):
    """Test Suite for Section 2: Top-Down Multi-Timeframe Alignment (1D -> 4H -> 1H -> 15m -> 5m)."""

    def test_1d_daily_bias_detection(self):
        """Test Daily trend filter: bullish, bearish, and neutral chop."""
        c_bull = make_series(base_price=50.0, step=1.5, count=30, noise=0.2)
        res_bull = analyze_1d_bias(c_bull)
        self.assertEqual(res_bull.bias, "bullish")
        self.assertGreater(res_bull.last_close, res_bull.ema20_1d)

        c_bear = make_series(base_price=100.0, step=-1.5, count=30, noise=0.2)
        res_bear = analyze_1d_bias(c_bear)
        self.assertEqual(res_bear.bias, "bearish")
        self.assertLess(res_bear.last_close, res_bear.ema20_1d)

        c_flat = [make_candle(50.0, 50.05, 49.95, 50.0) for _ in range(30)]
        res_flat = analyze_1d_bias(c_flat)
        self.assertEqual(res_flat.bias, "neutral")

    def test_4h_intermediate_alignment_and_overextension(self):
        """Test 4H alignment matching 1D bias, and rejection of overextended moves."""
        c_4h_bull = make_series(base_price=80.0, step=0.4, count=30, noise=0.2)
        res_aligned = analyze_4h_alignment(c_4h_bull, "bullish")
        self.assertTrue(res_aligned.aligned)

        res_mismatch = analyze_4h_alignment(c_4h_bull, "bearish")
        self.assertFalse(res_mismatch.aligned)

        # Overextended 4H candle (> 2.5 * ATR away from EMA and > 5% price)
        c_4h_extended = make_series(base_price=80.0, step=0.1, count=28, noise=0.1)
        c_4h_extended.append(make_candle(85.0, 130.0, 85.0, 125.0))
        res_ext = analyze_4h_alignment(c_4h_extended, "bullish")
        self.assertFalse(res_ext.aligned)
        self.assertIn("overextended", res_ext.reason)

    def test_1h_pullback_setup_vs_breakout_chasing(self):
        """Test 1H setup waits for pullback into value area and rejects breakout chasing."""
        c_1h_chase = make_series(base_price=100.0, step=0.2, count=25, noise=0.1)
        c_1h_chase.append(make_candle(106.0, 112.0, 105.0, 110.0))
        res_chase = analyze_1h_setup(c_1h_chase, "bullish")
        self.assertFalse(res_chase.setup_ready)
        self.assertTrue(res_chase.extended)

        c_1h_pullback = make_series(base_price=95.0, step=0.3, count=24, noise=0.1)
        ema = compute_ema([b["close"] for b in c_1h_pullback], 20)[-1]
        c_1h_pullback.append(make_candle(ema + 0.5, ema + 0.6, ema - 0.2, ema + 0.1))
        res_pullback = analyze_1h_setup(c_1h_pullback, "bullish")
        self.assertTrue(res_pullback.setup_ready)
        self.assertTrue(res_pullback.pullback_tested)

    def test_15m_market_structure_shift(self):
        """Test 15m MSS confirming pullback exhaustion."""
        candles_15m = [
            make_candle(102.0, 102.5, 101.5, 101.8),
            make_candle(101.8, 102.0, 101.0, 101.2),
            make_candle(101.2, 101.5, 100.5, 100.8),
            make_candle(100.8, 101.0, 100.2, 100.5),
            make_candle(100.5, 100.8, 99.8, 100.0),
            make_candle(100.0, 100.4, 99.9, 100.2),
            make_candle(100.2, 100.5, 100.1, 100.4),
            make_candle(100.4, 101.1, 100.3, 101.0),
            make_candle(101.0, 101.4, 100.9, 101.2),
            make_candle(101.2, 102.8, 101.1, 102.6),
        ]
        res_mss = analyze_15m_mss(candles_15m, "bullish")
        self.assertTrue(res_mss.mss_confirmed)
        self.assertIn("MSS confirmed", res_mss.reason)

    def test_5m_trigger_and_tight_sl_definition(self):
        """Test 5m micro trigger and SL anchoring at local swing low."""
        candles_5m = [
            make_candle(100.0, 100.3, 99.7, 99.8),
            make_candle(99.8, 100.0, 99.5, 99.6),
            make_candle(99.6, 99.9, 99.4, 99.5),
            make_candle(99.5, 100.1, 99.5, 100.0),
            make_candle(100.0, 100.5, 99.9, 100.4),
            make_candle(100.4, 101.0, 100.3, 100.9),
        ]
        res_trig = analyze_5m_trigger(candles_5m, "bullish")
        self.assertTrue(res_trig.triggered)
        self.assertEqual(res_trig.entry_price, 100.9)
        self.assertLess(res_trig.sl_price, 99.4)
        self.assertGreater(res_trig.sl_dist, 0.0)

    def test_end_to_end_pipeline_cascade_bullish(self):
        """Verify full bullish pipeline cascade."""
        c_1d = make_series(base_price=60.0, step=1.0, count=30, noise=0.2)
        c_4h = make_series(base_price=80.0, step=0.3, count=30, noise=0.1)
        # Establish higher resistance ceiling in 4H/1H to allow safe runway
        c_4h[0] = make_candle(80.0, 95.0, 79.0, 80.0)

        c_1h = make_series(base_price=85.0, step=0.1, count=24, noise=0.1)
        c_1h[0] = make_candle(85.0, 94.0, 84.0, 85.0)
        ema_1h = compute_ema([b["close"] for b in c_1h], 20)[-1]
        c_1h.append(make_candle(ema_1h + 0.3, ema_1h + 0.4, ema_1h - 0.1, ema_1h + 0.1))

        c_15m = [
            make_candle(88.0, 88.3, 87.5, 87.8),
            make_candle(87.8, 88.0, 87.2, 87.4),
            make_candle(87.4, 87.7, 87.0, 87.2),
            make_candle(87.2, 87.5, 86.8, 87.0),
            make_candle(87.0, 87.2, 86.9, 87.1),
            make_candle(87.1, 87.4, 87.0, 87.3),
            make_candle(87.3, 87.5, 87.1, 87.4),
            make_candle(87.4, 87.6, 87.2, 87.5),
            make_candle(87.5, 87.8, 87.3, 87.6),
            make_candle(87.6, 88.5, 87.5, 88.4),
        ]

        c_5m = [
            make_candle(88.0, 88.2, 87.8, 87.9),
            make_candle(87.9, 88.1, 87.7, 87.8),
            make_candle(87.8, 88.0, 87.6, 87.7),
            make_candle(87.7, 88.2, 87.6, 88.1),
            make_candle(88.1, 88.6, 88.0, 88.5),
        ]

        tf_data = {
            "1d": c_1d,
            "4h": c_4h,
            "1h": c_1h,
            "15m": c_15m,
            "5m": c_5m,
        }

        res = evaluate_mtf_pipeline("SOLUSDT", tf_data, min_rr=3.0)
        self.assertTrue(res.valid)
        self.assertEqual(res.direction, "bullish")
        self.assertGreater(res.entry_price, res.sl_price)
        self.assertGreater(res.tp_price, res.entry_price)
        self.assertGreaterEqual(res.risk_reward, 3.0)

    def test_end_to_end_pipeline_cascade_bearish(self):
        """Verify full bearish pipeline cascade."""
        c_1d = make_series(base_price=100.0, step=-1.0, count=30, noise=0.2)
        c_4h = make_series(base_price=80.0, step=-0.3, count=30, noise=0.1)
        # Establish lower support floor in 4H/1H to allow safe runway
        c_4h[0] = make_candle(80.0, 81.0, 60.0, 80.0)

        c_1h = make_series(base_price=75.0, step=-0.1, count=24, noise=0.1)
        c_1h[0] = make_candle(75.0, 76.0, 62.0, 75.0)
        ema_1h = compute_ema([b["close"] for b in c_1h], 20)[-1]
        # Bounce into EMA20 resistance
        c_1h.append(make_candle(ema_1h - 0.3, ema_1h + 0.1, ema_1h - 0.4, ema_1h - 0.1))

        # 15m bounce exhaustion / MSS
        c_15m = [
            make_candle(72.0, 72.5, 71.8, 72.3),
            make_candle(72.3, 72.8, 72.1, 72.6),
            make_candle(72.6, 73.0, 72.4, 72.8),
            make_candle(72.8, 73.1, 72.6, 72.9),
            make_candle(72.9, 73.0, 72.5, 72.7),
            make_candle(72.7, 72.8, 72.3, 72.5),
            make_candle(72.5, 72.7, 72.2, 72.4),
            make_candle(72.4, 72.5, 72.1, 72.3),
            make_candle(72.3, 72.4, 72.0, 72.1),
            make_candle(72.1, 72.2, 71.2, 71.4),  # MSS break down
        ]

        # 5m trigger: bearish breakdown bar
        c_5m = [
            make_candle(71.8, 72.0, 71.6, 71.9),
            make_candle(71.9, 72.1, 71.7, 71.8),
            make_candle(71.8, 71.9, 71.6, 71.7),
            make_candle(71.7, 71.8, 71.4, 71.5),
            make_candle(71.5, 71.6, 70.9, 71.0),
        ]

        tf_data = {
            "1d": c_1d,
            "4h": c_4h,
            "1h": c_1h,
            "15m": c_15m,
            "5m": c_5m,
        }

        res = evaluate_mtf_pipeline("SOLUSDT", tf_data, min_rr=3.0)
        self.assertTrue(res.valid)
        self.assertEqual(res.direction, "bearish")
        self.assertLess(res.entry_price, res.sl_price)
        self.assertLess(res.tp_price, res.entry_price)
        self.assertGreaterEqual(res.risk_reward, 3.0)

    def test_pipeline_neutral_stand_down(self):
        """Pipeline immediately stands down if 1D bias is neutral."""
        c_flat = [make_candle(50.0, 50.05, 49.95, 50.0) for _ in range(30)]
        tf_data = {"1d": c_flat}
        res = evaluate_mtf_pipeline("SOLUSDT", tf_data)
        self.assertFalse(res.valid)
        self.assertEqual(res.stage_failed, "1d_bias_neutral")

    def test_reject_1tick_noise_false_mss(self):
        """Negative control: 1-tick (0.0010) displacement in consolidation must be REJECTED as false MSS."""
        # Swing low at 4.2930. Next bar closes at 4.2920 (0.0010 dip, only 0.02% buffer)
        candles_15m = [
            make_candle(4.35, 4.36, 4.34, 4.35),
            make_candle(4.35, 4.35, 4.30, 4.31),
            make_candle(4.31, 4.33, 4.30, 4.32),
            make_candle(4.32, 4.34, 4.31, 4.33),
            make_candle(4.33, 4.34, 4.2930, 4.30),  # swing low is 4.2930
            make_candle(4.30, 4.32, 4.2950, 4.31),
            make_candle(4.31, 4.32, 4.30, 4.305),
            make_candle(4.305, 4.31, 4.30, 4.302),
            make_candle(4.302, 4.305, 4.2915, 4.2920),  # closes at 4.2920 (only 0.0010 below 4.2930)
        ]
        res_mss = analyze_15m_mss(candles_15m, "bearish")
        self.assertFalse(res_mss.mss_confirmed)
        self.assertIn("waiting for bounce exhaustion", res_mss.reason)

    def test_reject_1h_counter_penetration(self):
        """1H setup must reject bearish trade if price closed above EMA20 by > 0.3%."""
        c_1h = make_series(base_price=50.0, step=-0.1, count=25, noise=0.05)
        ema_1h = compute_ema([b["close"] for b in c_1h], 20)[-1]
        # Append candle closing 1.0% ABOVE EMA20 (counter-penetration)
        c_1h.append(make_candle(ema_1h + 0.3, ema_1h + 0.6, ema_1h, ema_1h * 1.010))
        res_setup = analyze_1h_setup(c_1h, "bearish")
        self.assertFalse(res_setup.setup_ready)
        self.assertIn("waiting for retracement", res_setup.reason)


    def test_reject_1d_oversold_exhaustion(self):
        """1D filter must return neutral when price is crash-extended > 25% below EMA20."""
        c_1d = make_series(base_price=100.0, step=-0.1, count=25, noise=0.1)
        # Add extreme flash-crash candle
        c_1d.append(make_candle(97.0, 97.5, 65.0, 68.0))
        res = analyze_1d_bias(c_1d)
        self.assertEqual(res.bias, "neutral")
        self.assertIn("oversold shorting rejected", res.reason)

    def test_reject_4h_relief_rally_counter_trend(self):
        """4H alignment must reject bearish bias when 4H prints consecutive higher highs/lows."""
        base_candles = [make_candle(95.0, 95.5, 94.5, 95.0) for _ in range(18)]
        c_4h = base_candles + [
            make_candle(90.0, 91.0, 89.0, 89.5),
            make_candle(89.5, 90.0, 88.0, 88.5),
            make_candle(88.5, 89.0, 87.0, 87.5),
            # 3 consecutive green recovery candles with higher lows
            make_candle(87.5, 89.0, 87.2, 88.8),
            make_candle(88.8, 90.2, 88.5, 90.0),
            make_candle(90.0, 91.5, 89.8, 91.2),
        ]
        res = analyze_4h_alignment(c_4h, "bearish")
        self.assertFalse(res.aligned)
        self.assertIn("actively rallying counter-trend", res.reason)

    def test_5m_trigger_anchors_to_15m_structural_level(self):
        """5m SL must anchor to 15m structural invalidation level with minimum buffer."""
        candles_5m = [
            make_candle(10.0, 10.05, 9.95, 9.98),
            make_candle(9.98, 10.02, 9.92, 9.95),
            make_candle(9.95, 9.98, 9.90, 9.92),
            make_candle(9.92, 9.95, 9.88, 9.90),
            make_candle(9.90, 9.92, 9.85, 9.88),
            make_candle(9.88, 9.90, 9.82, 9.84),  # Bearish micro impulse
        ]
        # 15m pullback swing high was at 10.15
        res = analyze_5m_trigger(candles_5m, "bearish", structural_sl_level=10.15, atr_15m=0.10)
        self.assertTrue(res.triggered)
        # SL should be anchored beyond 10.15, not just 5m high of 9.92
        self.assertGreater(res.sl_price, 10.15)
        self.assertGreaterEqual(res.sl_dist / res.entry_price, 0.008)

    def test_sr_barrier_rejects_short_into_support_floor(self):
        """Verify check_sr_barrier_guard rejects shorts within buffer of major support floor."""
        # 4H candles with a strong support floor at 93.00
        c_4h = [make_candle(95.0, 96.0, 94.0, 94.5) for _ in range(15)]
        c_4h[2] = make_candle(94.0, 94.5, 93.0, 93.5)  # Major support at 93.00
        c_1h = [make_candle(94.0, 94.5, 93.8, 94.1) for _ in range(25)]

        # Entry at 93.10 is within 0.10 of support 93.00 -> should be rejected!
        passed, reason, _ = check_sr_barrier_guard(c_4h, c_1h, "bearish", entry_price=93.10, sl_price=94.50)
        self.assertFalse(passed)
        self.assertIn("bounce risk", reason)

        # Entry at 94.20 has plenty of room above 93.00 -> should pass!
        passed_ok, reason_ok, _ = check_sr_barrier_guard(c_4h, c_1h, "bearish", entry_price=94.20, sl_price=94.50)
        self.assertTrue(passed_ok)
        self.assertIn("passed", reason_ok)

    def test_sr_barrier_rejects_long_into_resistance_ceiling(self):
        """Verify check_sr_barrier_guard rejects longs within buffer of major resistance ceiling."""
        # 4H candles with a ceiling at 100.00
        c_4h = [make_candle(95.0, 96.0, 94.0, 94.5) for _ in range(15)]
        c_4h[2] = make_candle(96.0, 100.0, 95.0, 99.0)  # Major ceiling at 100.00
        c_1h = [make_candle(97.0, 98.0, 96.5, 97.5) for _ in range(25)]

        # Entry at 99.85 is within 0.15 of ceiling 100.00 -> should be rejected!
        passed, reason, _ = check_sr_barrier_guard(c_4h, c_1h, "bullish", entry_price=99.85, sl_price=98.50)
        self.assertFalse(passed)
        self.assertIn("rejection risk", reason)

        # Entry at 97.50 has plenty of room below 100.00 -> should pass!
        passed_ok, reason_ok, _ = check_sr_barrier_guard(c_4h, c_1h, "bullish", entry_price=97.50, sl_price=97.00)
        self.assertTrue(passed_ok)
        self.assertIn("passed", reason_ok)

    def test_market_saturation_detection(self):
        """Verify check_market_saturation detects overextended climax and near-TP conditions."""
        # 1H candles in steep rally (RSI high, price way above EMA20)
        c_1h_bull = [make_candle(100.0 + i*1.0, 101.0 + i*1.0, 99.8 + i*1.0, 100.9 + i*1.0) for i in range(25)]
        
        # Bullish overextended at 130.0 with entry at 100.0, TP at 135.0
        is_sat, reason, diag = check_market_saturation(
            symbol="SOLUSDT",
            direction="bullish",
            entry_price=100.0,
            current_price=130.0,
            tp_price=135.0,
            candles_1h=c_1h_bull,
        )
        self.assertTrue(is_sat)
        self.assertIn("Exhaustion", reason)

        # Normal condition (RSI healthy, moderate distance)
        c_1h_normal = [make_candle(100.0, 100.5, 99.5, 100.0) for _ in range(25)]
        is_sat_norm, reason_norm, _ = check_market_saturation(
            symbol="SOLUSDT",
            direction="bullish",
            entry_price=100.0,
            current_price=100.8,
            tp_price=110.0,
            candles_1h=c_1h_normal,
        )
        self.assertFalse(is_sat_norm)

    def test_live_binance_market_data_ingestion(self):
        """Verify pipeline evaluates live Binance candles cleanly for all 5 universe symbols."""
        coins = ["SOLUSDT", "SUIUSDT", "NEARUSDT", "DOGEUSDT", "LINKUSDT"]
        for sym in coins:
            try:
                tf_data = {
                    "1d": fm.fetch_candles("1d", 60, sym),
                    "4h": fm.fetch_candles("4h", 60, sym),
                    "1h": fm.fetch_candles("1h", 60, sym),
                    "15m": fm.fetch_candles("15m", 60, sym),
                    "5m": fm.fetch_candles("5m", 60, sym),
                }
                res = evaluate_mtf_pipeline(sym, tf_data)
                self.assertIsInstance(res.valid, bool)
                self.assertIn(res.direction, ("bullish", "bearish", "none"))
            except Exception as e:
                self.fail(f"Live evaluation failed on {sym}: {e}")


if __name__ == "__main__":
    unittest.main()
