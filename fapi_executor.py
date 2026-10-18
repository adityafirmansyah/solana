#!/usr/bin/env python3
"""Binance Futures Testnet Executor for Solana Multi-Coin Lead-Lag Engine.

Executes simulated paper/testnet orders on Binance Futures Testnet (USDⓈ-M).
Maintains isolated risk parameters:
  - ISOLATED margin type
  - Conservative 5x leverage
  - Pre-set Take-Profit (1.5x ATR) and Stop-Loss (1.0x ATR) bracket orders
  - Dead Man's Switch (countdownCancelAll)
  - Dedicated unchained SQLite log table: testnet_executions in solana.db
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
DB_PATH = BASE_DIR / "solana.db"

logger = logging.getLogger("solana_fapi_executor")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] fapi_executor: %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

# Default Testnet URL
TESTNET_BASE_URL = "https://testnet.binancefuture.com"

# Virtual Capital & Safety Parameters
VIRTUAL_CAPITAL_USD = 100.0          # Strict virtual bankroll: treat equity as $100.00
DEFAULT_NOTIONAL_USD = 6.0           # Minimal size per trade ($5.00 minNotional + small buffer)
DEFAULT_LEVERAGE = 5                 # 5x Isolated leverage
MAX_CONCURRENT_POSITIONS = 2         # Max 2 concurrent positions (keeps total margin <= $5–$10, 5–10% equity)
MAX_DAILY_LOSS_USD = 5.0             # Daily circuit breaker: 5% of $100 ($5.00 realized loss)

# Symbol specifications on Binance Futures USDⓈ-M
SYMBOL_SPECS = {
    "SOLUSDT": {"step_size": 0.01, "price_tick": 0.01, "min_qty": 0.01, "min_notional": 5.0},
    "AVAXUSDT": {"step_size": 1.0, "price_tick": 0.001, "min_qty": 1.0, "min_notional": 5.0},
    "SUIUSDT": {"step_size": 0.1, "price_tick": 0.0001, "min_qty": 0.1, "min_notional": 5.0},
    "NEARUSDT": {"step_size": 1.0, "price_tick": 0.001, "min_qty": 1.0, "min_notional": 5.0},
    "DOGEUSDT": {"step_size": 1.0, "price_tick": 0.00001, "min_qty": 1.0, "min_notional": 5.0},
    "LINKUSDT": {"step_size": 0.01, "price_tick": 0.001, "min_qty": 0.01, "min_notional": 5.0},
}


def load_env() -> dict[str, str]:
    env_vars = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                env_vars[k.strip()] = v.strip().strip('"').strip("'")
    return env_vars


def round_step(val: float, step: float) -> float:
    precision = max(0, int(round(-math.log10(step))))
    factor = 10 ** precision
    return round(math.floor(val * factor + 1e-9) / factor, precision)


def round_tick(val: float, tick: float) -> float:
    precision = max(0, int(round(-math.log10(tick))))
    return round(val, precision)


class BinanceFuturesExecutor:
    def __init__(
        self,
        notional_usd: float = DEFAULT_NOTIONAL_USD,
        leverage: int = DEFAULT_LEVERAGE,
        virtual_capital: float = VIRTUAL_CAPITAL_USD,
        max_concurrent: int = MAX_CONCURRENT_POSITIONS,
        max_daily_loss: float = MAX_DAILY_LOSS_USD,
    ):
        env = load_env()
        self.api_key = env.get("BINANCE_FUTURES_TESTNET_KEY", "")
        self.api_secret = env.get("BINANCE_FUTURES_TESTNET_SECRET", "")
        self.base_url = TESTNET_BASE_URL
        self.notional_usd = notional_usd
        self.leverage = leverage
        self.virtual_capital = virtual_capital
        self.max_concurrent = max_concurrent
        self.max_daily_loss = max_daily_loss
        self._init_db()

    def _init_db(self) -> None:
        """Create unchained execution tracking table if it doesn't exist."""
        with sqlite3.connect(DB_PATH) as con:
            con.execute("""
            CREATE TABLE IF NOT EXISTS testnet_executions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                prediction_id    INTEGER,
                symbol           TEXT NOT NULL,
                direction        TEXT NOT NULL,
                side             TEXT NOT NULL,
                status           TEXT NOT NULL,
                entry_order_id   TEXT,
                entry_price      REAL,
                entry_qty        REAL,
                tp_order_id      TEXT,
                tp_price         REAL,
                sl_order_id      TEXT,
                sl_price         REAL,
                exit_order_id    TEXT,
                exit_price       REAL,
                exit_reason      TEXT,
                pnl_usd          REAL,
                pnl_pct          REAL,
                created_utc      TEXT NOT NULL,
                closed_utc       TEXT,
                raw_response     TEXT
            );
            """)
            con.commit()

    def _request(self, method: str, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        if not self.api_key or not self.api_secret:
            raise ValueError("Binance Futures Testnet API key or secret missing from .env")

        if params is None:
            params = {}
        payload = {k: v for k, v in params.items() if v is not None}
        payload["timestamp"] = int(time.time() * 1000)

        query_string = urllib.parse.urlencode(payload)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        url = f"{self.base_url}{endpoint}?{query_string}&signature={signature}"
        req = urllib.request.Request(
            url,
            headers={"X-MBX-APIKEY": self.api_key, "User-Agent": "Solana-LeadLag-Testnet/1.0"},
            method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            logger.error(f"HTTP {e.code} on {endpoint}: {err_body}")
            try:
                return json.loads(err_body)
            except Exception:
                return {"code": e.code, "msg": err_body}
        except Exception as e:
            logger.error(f"Network error on {endpoint}: {e}")
            return {"code": -9999, "msg": str(e)}

    def get_usdt_balance(self) -> float:
        """Fetch available USDT balance."""
        res = self._request("GET", "/fapi/v2/balance")
        if isinstance(res, list):
            for item in res:
                if item.get("asset") == "USDT":
                    return float(item.get("availableBalance", 0.0))
        return 0.0

    def configure_symbol(self, symbol: str) -> None:
        """Ensure ISOLATED margin type and target leverage."""
        # 1. Margin Type
        m_res = self._request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
        if m_res.get("code") not in (200, -4046):  # -4046: No need to change margin type
            logger.warning(f"Failed to set ISOLATED margin on {symbol}: {m_res}")

        # 2. Leverage
        l_res = self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": self.leverage})
        if "leverage" in l_res:
            logger.info(f"{symbol} leverage configured to {l_res['leverage']}x")
        else:
            logger.warning(f"Failed to set leverage on {symbol}: {l_res}")

    def compute_quantity(self, symbol: str, ref_price: float) -> tuple[float, float, float]:
        """Compute quantized order quantity matching symbol LOT_SIZE & MIN_NOTIONAL filters.
        Returns: (qty, actual_notional, margin_used)
        """
        spec = SYMBOL_SPECS.get(symbol, {"step_size": 0.01, "price_tick": 0.01, "min_qty": 0.01, "min_notional": 5.0})
        step = spec["step_size"]
        min_notional = spec["min_notional"]
        min_qty = spec["min_qty"]

        # Ensure target meets Binance minimum notional $5 with safety buffer
        target = max(self.notional_usd, min_notional + 0.5)
        raw_qty = target / max(ref_price, 1e-6)

        precision = max(0, int(round(-math.log10(step))))
        factor = 10 ** precision
        qty = round(math.ceil(raw_qty * factor - 1e-9) / factor, precision)
        qty = max(qty, min_qty)

        actual_notional = round(qty * ref_price, 2)
        margin_used = round(actual_notional / self.leverage, 2)
        return qty, actual_notional, margin_used

    def open_lead_lag_position(
        self,
        prediction_id: int,
        symbol: str,
        direction: str,
        ref_price: float,
        tp_price: float,
        sl_price: float,
    ) -> dict[str, Any] | None:
        """Open a market position with automated TP & SL bracket triggers on testnet."""
        # 0. Check Bankroll Safeguards ($100 virtual capital limits)
        with sqlite3.connect(DB_PATH) as con:
            open_cnt = con.execute("SELECT count(*) FROM testnet_executions WHERE status = 'OPEN'").fetchone()[0]
            if open_cnt >= self.max_concurrent:
                logger.warning(
                    f"Rejecting {symbol} trade: {open_cnt} active positions already open "
                    f"(capped at {self.max_concurrent} for ${self.virtual_capital:.0f} bankroll limit)"
                )
                return None

            today_prefix = datetime.now(UTC).strftime("%Y-%m-%d")
            loss_sum = con.execute(
                "SELECT coalesce(sum(pnl_usd), 0.0) FROM testnet_executions WHERE closed_utc LIKE ? AND pnl_usd < 0",
                (f"{today_prefix}%",)
            ).fetchone()[0]
            if abs(loss_sum) >= self.max_daily_loss:
                logger.warning(
                    f"Circuit Breaker ACTIVE: Daily realized loss ${abs(loss_sum):.2f} >= max ${self.max_daily_loss:.2f} "
                    f"(5% of ${self.virtual_capital:.0f} capital). Halting new entries."
                )
                return None

        self.configure_symbol(symbol)
        spec = SYMBOL_SPECS.get(symbol, {"price_tick": 0.01, "step_size": 0.01})
        qty, actual_notional, margin_used = self.compute_quantity(symbol, ref_price)

        entry_side = "BUY" if direction == "bullish" else "SELL"
        exit_side = "SELL" if direction == "bullish" else "BUY"

        # Format price ticks
        tp_tick = round_tick(tp_price, spec["price_tick"])
        sl_tick = round_tick(sl_price, spec["price_tick"])

        margin_pct = round((margin_used / self.virtual_capital) * 100, 1)
        logger.info(
            f"Opening Testnet {entry_side} {qty} {symbol} "
            f"(Notional: ${actual_notional:.2f}, Margin: ${margin_used:.2f} [{margin_pct}% of ${self.virtual_capital:.0f}], "
            f"TP: {tp_tick}, SL: {sl_tick})"
        )

        # 1. Send Market Entry Order
        entry_res = self._request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": entry_side,
            "type": "MARKET",
            "quantity": qty,
        })

        if "orderId" not in entry_res:
            logger.error(f"Failed to place entry market order for {symbol}: {entry_res}")
            return None

        entry_order_id = str(entry_res["orderId"])
        avg_price = float(entry_res.get("avgPrice") or 0.0)
        if avg_price <= 0:
            avg_price = ref_price

        # 2. Place Bracket Stop Loss (STOP_MARKET with closePosition=True)
        sl_res = self._request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": exit_side,
            "type": "STOP_MARKET",
            "stopPrice": sl_tick,
            "closePosition": "true",
        })
        sl_order_id = str(sl_res.get("orderId", ""))

        # 3. Place Bracket Take Profit (TAKE_PROFIT_MARKET with closePosition=True)
        tp_res = self._request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": exit_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": tp_tick,
            "closePosition": "true",
        })
        tp_order_id = str(tp_res.get("orderId", ""))

        now_utc = datetime.now(UTC).isoformat(timespec="seconds")

        # 4. Record to SQLite database
        with sqlite3.connect(DB_PATH) as con:
            cur = con.execute("""
            INSERT INTO testnet_executions (
                prediction_id, symbol, direction, side, status,
                entry_order_id, entry_price, entry_qty,
                tp_order_id, tp_price, sl_order_id, sl_price,
                created_utc, raw_response
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                prediction_id, symbol, direction, entry_side, "OPEN",
                entry_order_id, avg_price, qty,
                tp_order_id, tp_tick, sl_order_id, sl_tick,
                now_utc, json.dumps({"entry": entry_res, "tp": tp_res, "sl": sl_res})
            ))
            exec_id = cur.lastrowid
            con.commit()

        logger.info(f"Testnet position #{exec_id} recorded for {symbol} #{prediction_id} (Entry @ {avg_price})")
        return {
            "execution_id": exec_id,
            "entry_order_id": entry_order_id,
            "tp_order_id": tp_order_id,
            "sl_order_id": sl_order_id,
            "entry_price": avg_price,
            "qty": qty,
            "notional_usd": actual_notional,
            "margin_used": margin_used,
            "margin_pct_capital": margin_pct,
        }

    def close_position_on_exit(
        self,
        prediction_id: int,
        symbol: str,
        exit_reason: str,
        realized_price: float,
    ) -> None:
        """Handle position cleanup upon TP, SL, or Time-Stop event."""
        # Cancel any open orders for this symbol (cleans up unused TP or SL order)
        self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

        with sqlite3.connect(DB_PATH) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT * FROM testnet_executions WHERE prediction_id = ? AND status = 'OPEN'",
                (prediction_id,)
            ).fetchone()

            if not row:
                return

            entry_price = float(row["entry_price"])
            entry_qty = float(row["entry_qty"])
            direction = row["direction"]

            # If time-stop or manual, make sure exchange position is closed
            if exit_reason not in ("tp_hit", "sl_hit"):
                close_side = "SELL" if direction == "bullish" else "BUY"
                self._request("POST", "/fapi/v1/order", {
                    "symbol": symbol,
                    "side": close_side,
                    "type": "MARKET",
                    "quantity": entry_qty,
                    "reduceOnly": "true",
                })

            # Calculate realized PnL
            if direction == "bullish":
                pnl_usd = (realized_price - entry_price) * entry_qty
                pnl_pct = (realized_price - entry_price) / entry_price * 100 * self.leverage
            else:
                pnl_usd = (entry_price - realized_price) * entry_qty
                pnl_pct = (entry_price - realized_price) / entry_price * 100 * self.leverage

            now_utc = datetime.now(UTC).isoformat(timespec="seconds")
            con.execute("""
            UPDATE testnet_executions
            SET status = 'CLOSED',
                exit_price = ?,
                exit_reason = ?,
                pnl_usd = ?,
                pnl_pct = ?,
                closed_utc = ?
            WHERE id = ?
            """, (
                round(realized_price, 6),
                exit_reason,
                round(pnl_usd, 4),
                round(pnl_pct, 2),
                now_utc,
                row["id"]
            ))
            con.commit()
            logger.info(f"Testnet position #{row['id']} CLOSED: Reason={exit_reason}, PnL={pnl_pct:+.2f}% (${pnl_usd:+.2f})")

    def dead_man_heartbeat(self, symbol: str, countdown_ms: int = 60000) -> None:
        """Dead man's switch: cancels all orders on symbol if no heartbeat sent within countdown."""
        self._request("POST", "/fapi/v1/countdownCancelAll", {
            "symbol": symbol,
            "countdownTime": countdown_ms
        })


if __name__ == "__main__":
    executor = BinanceFuturesExecutor()
    bal = executor.get_usdt_balance()
    print(f"Binance Futures Testnet USDT Balance: ${bal:.2f}")
