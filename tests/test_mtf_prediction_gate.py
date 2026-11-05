import sqlite3
import unittest

from multi_tf_engine import MultiTimeframeEngine


class TestMtfPredictionGate(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY, symbol TEXT, horizon TEXT, direction TEXT, features_json TEXT, inputs_json TEXT, created_utc TEXT DEFAULT '2099-01-01T00:00:00+00:00')")
        self.con.execute("CREATE TABLE testnet_executions (prediction_id INTEGER, symbol TEXT, status TEXT, closed_utc TEXT)")
        self.con.execute("CREATE TABLE live_executions (prediction_id INTEGER, symbol TEXT, status TEXT, closed_utc TEXT)")

    def tearDown(self):
        self.con.close()

    def test_unresolved_same_setup_is_suppressed_but_changed_setup_and_other_symbol_pass(self):
        self.con.execute("INSERT INTO predictions (id, symbol, horizon, direction, features_json, inputs_json) VALUES (1, 'SOLUSDT', 'multi_tf', 'bearish', '{\"sl_price\":101,\"tp_price\":97}', '{\"ref_price\":99}')")
        self.assertFalse(MultiTimeframeEngine.setup_is_eligible(self.con, "testnet_executions", "SOLUSDT", "bearish", 99, 101, 97, now_ts=100, cooldown_secs=900))
        self.assertTrue(MultiTimeframeEngine.setup_is_eligible(self.con, "testnet_executions", "NEARUSDT", "bearish", 99, 101, 97, now_ts=100, cooldown_secs=900))

    def test_price_drift_during_scans_cannot_bypass_claim(self):
        self.con.execute("INSERT INTO predictions (id, symbol, horizon, direction, features_json, inputs_json) VALUES (1, 'SOLUSDT', 'multi_tf', 'bearish', '{}', '{}')")
        for prices in [(99, 101, 97), (99.5, 102, 96), (100, 103, 95)]:
            self.assertFalse(MultiTimeframeEngine.setup_is_eligible(
                self.con, "testnet_executions", "SOLUSDT", "bearish", *prices,
                now_ts=1800, cooldown_secs=900))

    def test_distinct_symbol_has_independent_claim(self):
        self.con.execute("INSERT INTO predictions (id, symbol, horizon, direction, features_json, inputs_json) VALUES (1, 'SOLUSDT', 'multi_tf', 'bearish', '{}', '{}')")
        self.assertTrue(MultiTimeframeEngine.setup_is_eligible(
            self.con, "testnet_executions", "NEARUSDT", "bearish", 99, 101, 97,
            now_ts=100, cooldown_secs=900))

    def test_elapsed_cooldown_allows_new_claim(self):
        self.con.execute("INSERT INTO predictions (id, symbol, horizon, direction, features_json, inputs_json) VALUES (1, 'SOLUSDT', 'multi_tf', 'bearish', '{}', '{}')")
        self.assertTrue(MultiTimeframeEngine.setup_is_eligible(
            self.con, "testnet_executions", "SOLUSDT", "bearish", 110, 112, 104,
            now_ts=4070909700, cooldown_secs=900))

    def test_direction_change_does_not_bypass_active_claim(self):
        self.con.execute("INSERT INTO predictions (id, symbol, horizon, direction, features_json, inputs_json) VALUES (1, 'SOLUSDT', 'multi_tf', 'bearish', '{}', '{}')")
        self.assertFalse(MultiTimeframeEngine.setup_is_eligible(
            self.con, "testnet_executions", "SOLUSDT", "bullish", 99, 97, 103,
            now_ts=100, cooldown_secs=900))

    def test_closed_recent_is_blocked_until_closed_cooldown_expires(self):
        self.con.execute("INSERT INTO predictions (id, symbol, horizon, direction, features_json, inputs_json, created_utc) VALUES (1, 'SOLUSDT', 'multi_tf', 'bearish', '{}', '{}', '2025-01-01T00:00:00+00:00')")
        self.con.execute("INSERT INTO testnet_executions VALUES (1, 'SOLUSDT', 'CLOSED', '2025-01-01T00:10:00+00:00')")
        self.assertFalse(MultiTimeframeEngine.setup_is_eligible(self.con, "testnet_executions", "SOLUSDT", "bearish", 99, 101, 97, now_ts=1735690800, cooldown_secs=900))
        self.assertTrue(MultiTimeframeEngine.setup_is_eligible(self.con, "testnet_executions", "SOLUSDT", "bearish", 99, 101, 97, now_ts=1735691400, cooldown_secs=900))

    def test_open_execution_always_blocks(self):
        self.con.execute("INSERT INTO predictions (id, symbol, horizon, direction, features_json, inputs_json) VALUES (1, 'SOLUSDT', 'multi_tf', 'bearish', '{}', '{}')")
        self.con.execute("INSERT INTO testnet_executions VALUES (1, 'SOLUSDT', 'OPEN', NULL)")
        self.assertFalse(MultiTimeframeEngine.setup_is_eligible(self.con, "testnet_executions", "SOLUSDT", "bearish", 99, 101, 97, now_ts=4102444800))

    def test_terminal_execution_missing_closed_time_fails_closed(self):
        self.con.execute("INSERT INTO predictions (id, symbol, horizon, direction, features_json, inputs_json, created_utc) VALUES (1, 'SOLUSDT', 'multi_tf', 'bearish', '{}', '{}', 'invalid')")
        self.con.execute("INSERT INTO testnet_executions VALUES (1, 'SOLUSDT', 'CLOSED', NULL)")
        self.assertFalse(MultiTimeframeEngine.setup_is_eligible(self.con, "testnet_executions", "SOLUSDT", "bearish", 99, 101, 97, now_ts=4102444800))

    def test_missing_prediction_timestamp_fails_closed(self):
        self.con.execute("INSERT INTO predictions (id, symbol, horizon, direction, features_json, inputs_json, created_utc) VALUES (1, 'SOLUSDT', 'multi_tf', 'bearish', '{}', '{}', NULL)")
        self.assertFalse(MultiTimeframeEngine.setup_is_eligible(self.con, "testnet_executions", "SOLUSDT", "bearish", 99, 101, 97, now_ts=4102444800))

    def test_terminal_execution_resolves_dedup_claim(self):
        self.con.execute("INSERT INTO predictions (id, symbol, horizon, direction, features_json, inputs_json, created_utc) VALUES (1, 'SOLUSDT', 'multi_tf', 'bearish', '{}', '{}', '2025-01-01T00:00:00+00:00')")
        self.con.execute("INSERT INTO testnet_executions VALUES (1, 'SOLUSDT', 'CLOSED', '2025-01-01T00:00:00+00:00')")
        self.assertTrue(MultiTimeframeEngine.setup_is_eligible(self.con, "testnet_executions", "SOLUSDT", "bearish", 99, 101, 97, now_ts=1735690500, cooldown_secs=900))
      
    def test_failed_or_missing_execution_stays_suppressed(self):
        self.con.execute("INSERT INTO predictions (id, symbol, horizon, direction, features_json, inputs_json) VALUES (1, 'SOLUSDT', 'multi_tf', 'bearish', '{\"sl_price\":101,\"tp_price\":97}', '{\"ref_price\":99}')")
        self.con.execute("INSERT INTO testnet_executions VALUES (1, 'SOLUSDT', 'FAILED', NULL)")
        self.assertFalse(MultiTimeframeEngine.setup_is_eligible(self.con, "testnet_executions", "SOLUSDT", "bearish", 99, 101, 97))


if __name__ == "__main__":
    unittest.main()
