"""Unit tests guarding Top-Down MTF Cluster Safety Rules in multi_tf_engine.py.

Rules guarded:
1. Directional Concurrency Guard (MAX_CONCURRENT_SAME_DIR_TRADES = 1)
2. Per-Symbol Exit Cooldown (SYMBOL_EXIT_COOLDOWN_SECS = 900.0 / 15m)
3. Global Cluster Cooldown (GLOBAL_CLUSTER_COOLDOWN_SECS = 180.0 / 3m)
4. Dynamic Consecutive Loss Protection (2 losses -> 15m cooldown, 3+ losses -> 60m freeze)
5. Forensic Incident Replay & Regression Guard
"""
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from multi_tf_engine import (
    MultiTimeframeEngine,
    get_consecutive_losses,
    parse_utc_timestamp,
    MAX_CONCURRENT_POSITIONS,
    MAX_CONCURRENT_SAME_DIR_TRADES,
    SYMBOL_EXIT_COOLDOWN_SECS,
    GLOBAL_CLUSTER_COOLDOWN_SECS,
    CONSEC_LOSS_2_COOLDOWN_SECS,
    CONSEC_LOSS_3_FREEZE_SECS,
)
from multi_tf_strategy import MTFPipelineResult


def create_in_memory_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY,
            symbol TEXT,
            horizon TEXT,
            direction TEXT,
            features_json TEXT,
            inputs_json TEXT,
            params_json TEXT,
            created_utc TEXT DEFAULT '2026-11-05T00:00:00+00:00'
        )
    """)
    con.execute("""
        CREATE TABLE testnet_executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_id INTEGER,
            symbol TEXT,
            direction TEXT,
            side TEXT,
            status TEXT,
            entry_price REAL,
            entry_qty REAL,
            tp_price REAL,
            tp_order_id TEXT,
            sl_price REAL,
            sl_order_id TEXT,
            exit_price REAL,
            exit_reason TEXT,
            pnl_usd REAL,
            pnl_pct REAL,
            created_utc TEXT,
            closed_utc TEXT
        )
    """)
    con.execute("""
        CREATE TABLE live_executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_id INTEGER,
            symbol TEXT,
            direction TEXT,
            side TEXT,
            status TEXT,
            entry_price REAL,
            entry_qty REAL,
            tp_price REAL,
            tp_order_id TEXT,
            sl_price REAL,
            sl_order_id TEXT,
            exit_price REAL,
            exit_reason TEXT,
            pnl_usd REAL,
            pnl_pct REAL,
            created_utc TEXT,
            closed_utc TEXT
        )
    """)
    return con


def make_qualifying_pipeline_result(symbol: str, direction: str = "bearish") -> MTFPipelineResult:
    entry = 100.0 if direction == "bullish" else 100.0
    sl = 98.0 if direction == "bullish" else 102.0
    tp = 106.0 if direction == "bullish" else 94.0
    return MTFPipelineResult(
        valid=True,
        symbol=symbol,
        direction=direction,
        entry_price=entry,
        sl_price=sl,
        tp_price=tp,
        risk_reward=3.0,
        stage_failed=None,
        diagnostics={"1d": {"bias": direction}},
    )


class TestDirectionalConcurrencyGuard(unittest.TestCase):
    def setUp(self):
        self.con = create_in_memory_db()
        self.engine = MultiTimeframeEngine(env_mode="TESTNET", db_path=self.con)
        # Prevent load_active_trades from clearing active_trades in tests
        self.engine.load_active_trades = MagicMock(return_value=self.engine.active_trades)

    def tearDown(self):
        self.con.close()

    def test_blocks_second_trade_with_same_direction_in_scan(self):
        """When an active short (bearish) exists, a new bearish setup must be blocked."""
        self.engine.active_trades = {
            1: {"symbol": "SOLUSDT", "direction": "bearish", "side": "SELL"}
        }

        # Mock fetch_symbol_candles to return mock candle dict
        self.engine.fetch_symbol_candles = AsyncMock(return_value={"1d": [1]})
        self.engine.execute_entry = AsyncMock()

        with patch("multi_tf_engine.evaluate_mtf_pipeline") as mock_eval:
            mock_eval.return_value = make_qualifying_pipeline_result("DOGEUSDT", direction="bearish")
            asyncio.run(self.engine.scan_market())

        # execute_entry must NOT have been called because bearish is already active
        self.engine.execute_entry.assert_not_called()

    def test_allows_opposite_direction_in_scan(self):
        """When an active short (bearish) exists, a new bullish setup must be allowed."""
        self.engine.active_trades = {
            1: {"symbol": "SOLUSDT", "direction": "bearish", "side": "SELL"}
        }

        self.engine.fetch_symbol_candles = AsyncMock(return_value={"1d": [1]})
        self.engine.execute_entry = AsyncMock()

        with patch("multi_tf_engine.FOLLOWERS", ["DOGEUSDT"]), \
             patch("multi_tf_engine.evaluate_mtf_pipeline") as mock_eval:
            mock_eval.return_value = make_qualifying_pipeline_result("DOGEUSDT", direction="bullish")
            asyncio.run(self.engine.scan_market())

        # Bullish setup is allowed because active trade is bearish
        self.engine.execute_entry.assert_called_once()
        called_opp = self.engine.execute_entry.call_args[0][0]
        self.assertEqual(called_opp.direction, "bullish")

    def test_total_concurrency_ceiling_blocks_when_two_positions_active(self):
        """When 2 positions are active (1 long + 1 short), scanning is blocked entirely."""
        self.engine.active_trades = {
            1: {"symbol": "SOLUSDT", "direction": "bearish", "side": "SELL"},
            2: {"symbol": "NEARUSDT", "direction": "bullish", "side": "BUY"},
        }
        self.engine.fetch_symbol_candles = AsyncMock()
        self.engine.execute_entry = AsyncMock()

        asyncio.run(self.engine.scan_market())

        self.engine.fetch_symbol_candles.assert_not_called()
        self.engine.execute_entry.assert_not_called()

    def test_execute_entry_defense_in_depth_blocks_same_direction(self):
        """execute_entry itself must guard against same-direction duplicate entries."""
        self.engine.active_trades = {
            1: {"symbol": "SOLUSDT", "direction": "bearish", "side": "SELL"}
        }
        opp = make_qualifying_pipeline_result("DOGEUSDT", direction="bearish")

        res = asyncio.run(self.engine.execute_entry(opp))
        self.assertIsNone(res)
        # Active trades must not contain DOGEUSDT
        self.assertEqual(len(self.engine.active_trades), 1)


class TestSymbolExitCooldown(unittest.TestCase):
    def setUp(self):
        self.con = create_in_memory_db()
        self.engine = MultiTimeframeEngine(env_mode="TESTNET", db_path=self.con)
        self.engine.load_active_trades = MagicMock(return_value={})

    def tearDown(self):
        self.con.close()

    def test_blocks_symbol_reentry_within_900s(self):
        """Symbol that closed 30s ago must be skipped during market scan."""
        now = time.time()
        self.engine.symbol_cooldowns["DOGEUSDT"] = now - 30.0  # 30s ago (< 900s)

        self.engine.fetch_symbol_candles = AsyncMock()
        self.engine.execute_entry = AsyncMock()

        with patch("multi_tf_engine.FOLLOWERS", ["DOGEUSDT"]):
            asyncio.run(self.engine.scan_market())

        # DOGEUSDT was skipped in cooldown: no candles fetched, no entry
        self.engine.fetch_symbol_candles.assert_not_called()
        self.engine.execute_entry.assert_not_called()

    def test_allows_symbol_reentry_after_900s(self):
        """Symbol that closed 905s ago must be eligible for new scan & entry."""
        now = time.time()
        self.engine.symbol_cooldowns["DOGEUSDT"] = now - 905.0  # > 900s ago

        self.engine.fetch_symbol_candles = AsyncMock(return_value={"1d": [1]})
        self.engine.execute_entry = AsyncMock()

        with patch("multi_tf_engine.FOLLOWERS", ["DOGEUSDT"]), \
             patch("multi_tf_engine.evaluate_mtf_pipeline") as mock_eval:
            mock_eval.return_value = make_qualifying_pipeline_result("DOGEUSDT", direction="bearish")
            asyncio.run(self.engine.scan_market())

        self.engine.fetch_symbol_candles.assert_called_once_with("DOGEUSDT")
        self.engine.execute_entry.assert_called_once()

    def test_different_symbol_not_blocked_by_exit_cooldown(self):
        """Exit cooldown for DOGEUSDT must not block scanning for SOLUSDT."""
        now = time.time()
        self.engine.symbol_cooldowns["DOGEUSDT"] = now - 30.0

        self.engine.fetch_symbol_candles = AsyncMock(return_value={"1d": [1]})
        self.engine.execute_entry = AsyncMock()

        with patch("multi_tf_engine.FOLLOWERS", ["DOGEUSDT", "SOLUSDT"]), \
             patch("multi_tf_engine.evaluate_mtf_pipeline") as mock_eval:
            mock_eval.return_value = make_qualifying_pipeline_result("SOLUSDT", direction="bullish")
            asyncio.run(self.engine.scan_market())

        # DOGEUSDT was skipped, SOLUSDT was processed and entered
        self.engine.fetch_symbol_candles.assert_called_once_with("SOLUSDT")
        self.engine.execute_entry.assert_called_once()

    def test_load_active_trades_populates_cooldown_from_db(self):
        """Booting engine must populate symbol_cooldowns from recent closed DB records."""
        now_dt = datetime.now(timezone.utc)
        closed_recent = (now_dt - timedelta(seconds=200)).isoformat(timespec="seconds")

        self.con.execute("""
            INSERT INTO testnet_executions (symbol, status, closed_utc)
            VALUES ('DOGEUSDT', 'CLOSED', ?)
        """, (closed_recent,))

        engine = MultiTimeframeEngine(env_mode="TESTNET", db_path=self.con)
        engine.load_active_trades()

        self.assertIn("DOGEUSDT", engine.symbol_cooldowns)
        self.assertAlmostEqual(
            engine.symbol_cooldowns["DOGEUSDT"],
            datetime.fromisoformat(closed_recent).timestamp(),
            delta=1.0,
        )

    def test_reconcile_and_record_symbol_exit_methods(self):
        """Helper methods record_symbol_exit and reconcile_closed_trade set cooldown."""
        t_exit = 1735689600.0
        self.engine.record_symbol_exit("NEARUSDT", t_exit)
        self.assertEqual(self.engine.symbol_cooldowns["NEARUSDT"], t_exit)

        self.engine.reconcile_closed_trade("SUIUSDT", t_exit + 50, is_loss=True)
        self.assertEqual(self.engine.symbol_cooldowns["SUIUSDT"], t_exit + 50)
        self.assertEqual(self.engine.last_loss_time, t_exit + 50)


class TestGlobalClusterCooldown(unittest.TestCase):
    def setUp(self):
        self.con = create_in_memory_db()
        self.engine = MultiTimeframeEngine(env_mode="TESTNET", db_path=self.con)
        self.engine.load_active_trades = MagicMock(return_value={})

    def tearDown(self):
        self.con.close()

    def test_blocks_entry_within_180s(self):
        """Global cluster cooldown skips scanning when last entry was 60s ago (< 180s)."""
        now = time.time()
        self.engine.last_global_entry_time = now - 60.0

        self.engine.fetch_symbol_candles = AsyncMock()
        self.engine.execute_entry = AsyncMock()

        asyncio.run(self.engine.scan_market())

        self.engine.fetch_symbol_candles.assert_not_called()
        self.engine.execute_entry.assert_not_called()

    def test_allows_entry_after_180s(self):
        """Global cluster cooldown permits scanning when last entry was 190s ago (>= 180s)."""
        now = time.time()
        self.engine.last_global_entry_time = now - 190.0

        self.engine.fetch_symbol_candles = AsyncMock(return_value={"1d": [1]})
        self.engine.execute_entry = AsyncMock()

        with patch("multi_tf_engine.FOLLOWERS", ["SOLUSDT"]), \
             patch("multi_tf_engine.evaluate_mtf_pipeline") as mock_eval:
            mock_eval.return_value = make_qualifying_pipeline_result("SOLUSDT", direction="bullish")
            asyncio.run(self.engine.scan_market())

        self.engine.fetch_symbol_candles.assert_called_once_with("SOLUSDT")
        self.engine.execute_entry.assert_called_once()

    def test_execute_entry_updates_last_global_entry_time(self):
        """Successful execute_entry updates self.last_global_entry_time to current epoch."""
        opp = make_qualifying_pipeline_result("NEARUSDT", direction="bullish")
        self.engine.last_global_entry_time = 0.0

        mock_exec_info = {
            "execution_id": 99,
            "entry_price": 100.0,
            "qty": 1.5,
            "tp_order_id": "tp123",
            "sl_order_id": "sl123",
        }

        with patch.object(self.engine.executor, "open_lead_lag_position", return_value=mock_exec_info), \
             patch.object(MultiTimeframeEngine, "setup_is_eligible", return_value=True), \
             patch("multi_tf_engine.ledger.record_prediction", return_value=123), \
             patch("multi_tf_engine.post_persona_discord"):
            now_before = time.time()
            asyncio.run(self.engine.execute_entry(opp))
            now_after = time.time()

        self.assertTrue(now_before <= self.engine.last_global_entry_time <= now_after)
        self.assertIn(123, self.engine.active_trades)

    def test_close_trade_updates_symbol_cooldown_and_loss_time(self):
        """close_trade on SL sets symbol cooldown and last_loss_time."""
        self.engine.active_trades[123] = {
            "symbol": "SOLUSDT",
            "direction": "bearish",
            "entry_price": 100.0,
        }
        self.engine.symbol_cooldowns.clear()
        self.engine.last_loss_time = 0.0

        with patch.object(self.engine.executor, "close_position_on_exit"), \
             patch("multi_tf_engine.ledger.record_outcome"), \
             patch("multi_tf_engine.post_persona_discord"):
            now_before = time.time()
            asyncio.run(self.engine.close_trade(123, "SOLUSDT", "sl_hit", 102.0))
            now_after = time.time()

        self.assertIn("SOLUSDT", self.engine.symbol_cooldowns)
        self.assertTrue(now_before <= self.engine.symbol_cooldowns["SOLUSDT"] <= now_after)
        self.assertTrue(now_before <= self.engine.last_loss_time <= now_after)

    def test_load_active_trades_populates_last_global_entry_from_db(self):
        """Booting engine must populate last_global_entry_time from the latest execution."""
        now_dt = datetime.now(timezone.utc)
        entry_time_str = (now_dt - timedelta(seconds=120)).isoformat(timespec="seconds")

        self.con.execute("""
            INSERT INTO testnet_executions (symbol, status, created_utc)
            VALUES ('SOLUSDT', 'OPEN', ?)
        """, (entry_time_str,))

        engine = MultiTimeframeEngine(env_mode="TESTNET", db_path=self.con)
        engine.load_active_trades()

        self.assertAlmostEqual(
            engine.last_global_entry_time,
            datetime.fromisoformat(entry_time_str).timestamp(),
            delta=1.0,
        )


class TestDynamicConsecutiveLossProtection(unittest.TestCase):
    def setUp(self):
        self.con = create_in_memory_db()
        self.engine = MultiTimeframeEngine(env_mode="TESTNET", db_path=self.con)
        self.engine.load_active_trades = MagicMock(return_value={})

    def tearDown(self):
        self.con.close()

    def test_get_consecutive_losses_zero_when_profitable(self):
        """Latest closed trade with positive PnL resets consecutive loss count to 0."""
        self.con.execute("""
            INSERT INTO testnet_executions (symbol, status, pnl_usd, exit_reason)
            VALUES ('SOLUSDT', 'CLOSED', -3.1, 'sl_hit')
        """)
        self.con.execute("""
            INSERT INTO testnet_executions (symbol, status, pnl_usd, exit_reason)
            VALUES ('DOGEUSDT', 'CLOSED', 1.5, 'tp_hit')
        """)
        losses = get_consecutive_losses(self.con, "testnet_executions")
        self.assertEqual(losses, 0)

    def test_get_consecutive_losses_counting_multiple(self):
        """Correctly counts 3 consecutive losses and stops at preceding win."""
        records = [
            ("NEARUSDT", 2.0, "tp_hit"),
            ("SOLUSDT", -3.2, "sl_hit"),
            ("DOGEUSDT", -3.18, "sl_hit"),
            ("SUIUSDT", -3.05, "sl_hit"),
        ]
        for sym, pnl, reason in records:
            self.con.execute("""
                INSERT INTO testnet_executions (symbol, status, pnl_usd, exit_reason)
                VALUES (?, 'CLOSED', ?, ?)
            """, (sym, pnl, reason))

        losses = get_consecutive_losses(self.con, "testnet_executions")
        self.assertEqual(losses, 3)

    def test_get_consecutive_losses_stops_at_breakeven(self):
        """pnl_usd == 0.0 (breakeven) is not a loss and terminates streak."""
        self.con.execute("""
            INSERT INTO testnet_executions (symbol, status, pnl_usd, exit_reason)
            VALUES ('SOLUSDT', 'CLOSED', -3.0, 'sl_hit')
        """)
        self.con.execute("""
            INSERT INTO testnet_executions (symbol, status, pnl_usd, exit_reason)
            VALUES ('DOGEUSDT', 'CLOSED', 0.0, 'time_stop')
        """)
        self.con.execute("""
            INSERT INTO testnet_executions (symbol, status, pnl_usd, exit_reason)
            VALUES ('SUIUSDT', 'CLOSED', -3.0, 'sl_hit')
        """)
        losses = get_consecutive_losses(self.con, "testnet_executions")
        self.assertEqual(losses, 1)

    def test_consecutive_losses_2_scales_cooldown_to_15m_blocks_within_900s(self):
        """2 consecutive losses scale global cluster cooldown to 15m (900s). Within 900s must block."""
        now = time.time()
        self.engine.get_consecutive_losses = MagicMock(return_value=2)
        self.engine.last_global_entry_time = now - 300.0  # 300s > 180s, but < 900s

        self.engine.fetch_symbol_candles = AsyncMock()
        self.engine.execute_entry = AsyncMock()

        asyncio.run(self.engine.scan_market())

        self.engine.fetch_symbol_candles.assert_not_called()
        self.engine.execute_entry.assert_not_called()

    def test_consecutive_losses_2_allows_after_900s(self):
        """2 consecutive losses allow entry after 900s (15m) have elapsed."""
        now = time.time()
        self.engine.get_consecutive_losses = MagicMock(return_value=2)
        self.engine.last_global_entry_time = now - 910.0  # > 900s

        self.engine.fetch_symbol_candles = AsyncMock(return_value={"1d": [1]})
        self.engine.execute_entry = AsyncMock()

        with patch("multi_tf_engine.FOLLOWERS", ["SOLUSDT"]), \
             patch("multi_tf_engine.evaluate_mtf_pipeline") as mock_eval:
            mock_eval.return_value = make_qualifying_pipeline_result("SOLUSDT", direction="bullish")
            asyncio.run(self.engine.scan_market())

        self.engine.fetch_symbol_candles.assert_called_once_with("SOLUSDT")
        self.engine.execute_entry.assert_called_once()

    def test_consecutive_losses_3_triggers_60m_freeze_blocks_within_3600s(self):
        """3+ consecutive losses trigger 60m (3600s) freeze circuit breaker."""
        now = time.time()
        self.engine.get_consecutive_losses = MagicMock(return_value=3)
        self.engine.last_loss_time = now - 1200.0  # 20m elapsed, still inside 60m freeze

        self.engine.fetch_symbol_candles = AsyncMock()
        self.engine.execute_entry = AsyncMock()

        asyncio.run(self.engine.scan_market())

        self.engine.fetch_symbol_candles.assert_not_called()
        self.engine.execute_entry.assert_not_called()

    def test_consecutive_losses_3_allows_after_3600s(self):
        """3+ consecutive losses freeze unfreezes after 3600s (60m)."""
        now = time.time()
        self.engine.get_consecutive_losses = MagicMock(return_value=3)
        self.engine.last_loss_time = now - 3610.0
        self.engine.last_global_entry_time = now - 3610.0

        self.engine.fetch_symbol_candles = AsyncMock(return_value={"1d": [1]})
        self.engine.execute_entry = AsyncMock()

        with patch("multi_tf_engine.FOLLOWERS", ["SOLUSDT"]), \
             patch("multi_tf_engine.evaluate_mtf_pipeline") as mock_eval:
            mock_eval.return_value = make_qualifying_pipeline_result("SOLUSDT", direction="bullish")
            asyncio.run(self.engine.scan_market())

        self.engine.fetch_symbol_candles.assert_called_once_with("SOLUSDT")
        self.engine.execute_entry.assert_called_once()


class TestForensicIncidentPreventionAndIdempotency(unittest.TestCase):
    def setUp(self):
        self.con = create_in_memory_db()
        self.engine = MultiTimeframeEngine(env_mode="TESTNET", db_path=self.con)
        self.engine.load_active_trades = MagicMock(return_value=self.engine.active_trades)

    def tearDown(self):
        self.con.close()

    def test_replay_nov4_incident_is_blocked_by_directional_guard_and_cooldown(self):
        """Forensic replay: On 4 Nov 12:14:06 DOGEUSDT exited (+$0.75).
        At 12:14:35 (29s later), DOGE tried to enter BEARISH while SOLUSDT was already in BEARISH.
        Both guards (Directional Concurrency + Exit Cooldown) must independently reject this.
        """
        now = time.time()
        # 1. SOLUSDT is active in a BEARISH trade
        self.engine.active_trades = {
            1: {"symbol": "SOLUSDT", "direction": "bearish", "side": "SELL"}
        }

        # 2. DOGEUSDT exited 29 seconds ago
        self.engine.symbol_cooldowns["DOGEUSDT"] = now - 29.0

        self.engine.fetch_symbol_candles = AsyncMock(return_value={"1d": [1]})
        self.engine.execute_entry = AsyncMock()

        with patch("multi_tf_engine.FOLLOWERS", ["DOGEUSDT"]), \
             patch("multi_tf_engine.evaluate_mtf_pipeline") as mock_eval:
            mock_eval.return_value = make_qualifying_pipeline_result("DOGEUSDT", direction="bearish")
            asyncio.run(self.engine.scan_market())

        # DOGEUSDT is rejected before even evaluating pipeline due to exit cooldown!
        self.engine.fetch_symbol_candles.assert_not_called()
        self.engine.execute_entry.assert_not_called()

        # If cooldown had elapsed but SOL is still bearish, directional concurrency must STILL reject it:
        self.engine.symbol_cooldowns["DOGEUSDT"] = now - 1000.0  # elapsed cooldown
        with patch("multi_tf_engine.FOLLOWERS", ["DOGEUSDT"]), \
             patch("multi_tf_engine.evaluate_mtf_pipeline") as mock_eval:
            mock_eval.return_value = make_qualifying_pipeline_result("DOGEUSDT", direction="bearish")
            asyncio.run(self.engine.scan_market())

        self.engine.fetch_symbol_candles.assert_called_once_with("DOGEUSDT")
        # But execute_entry must NOT be called due to directional concurrency ceiling
        self.engine.execute_entry.assert_not_called()

    def test_setup_is_eligible_idempotency_remains_unregressed(self):
        """Verify setup_is_eligible still functions identically (no regression from previous fix)."""
        self.con.execute(
            "INSERT INTO predictions (id, symbol, horizon, direction, features_json, inputs_json) "
            "VALUES (1, 'SOLUSDT', 'multi_tf', 'bearish', '{\"sl_price\":101,\"tp_price\":97}', '{\"ref_price\":99}')"
        )
        self.assertFalse(
            MultiTimeframeEngine.setup_is_eligible(
                self.con, "testnet_executions", "SOLUSDT", "bearish", 99, 101, 97, now_ts=100, cooldown_secs=900
            )
        )
        self.assertTrue(
            MultiTimeframeEngine.setup_is_eligible(
                self.con, "testnet_executions", "NEARUSDT", "bearish", 99, 101, 97, now_ts=100, cooldown_secs=900
            )
        )


if __name__ == "__main__":
    unittest.main()
