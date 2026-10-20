#!/usr/bin/env python3
"""Binance Futures Testnet API Client and Formatting Utilities.

Provides HMAC-SHA256 authenticated request handling for Binance Futures USDⓈ-M
Testnet endpoints, symbol specifications, and quantization helpers.
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
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
DB_PATH = BASE_DIR / "solana.db"

logger = logging.getLogger("solana_fapi_client")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] fapi_client: %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

# Default Testnet URL
TESTNET_BASE_URL = "https://testnet.binancefuture.com"

# Symbol specifications on Binance Futures USDⓈ-M
SYMBOL_SPECS: dict[str, dict[str, float]] = {
    "SOLUSDT": {"step_size": 0.01, "price_tick": 0.01, "min_qty": 0.01, "min_notional": 5.0},
    "AVAXUSDT": {"step_size": 1.0, "price_tick": 0.001, "min_qty": 1.0, "min_notional": 5.0},
    "SUIUSDT": {"step_size": 0.1, "price_tick": 0.0001, "min_qty": 0.1, "min_notional": 5.0},
    "NEARUSDT": {"step_size": 1.0, "price_tick": 0.001, "min_qty": 1.0, "min_notional": 5.0},
    "DOGEUSDT": {"step_size": 1.0, "price_tick": 0.00001, "min_qty": 1.0, "min_notional": 5.0},
    "LINKUSDT": {"step_size": 0.01, "price_tick": 0.001, "min_qty": 0.01, "min_notional": 5.0},
}


def load_env() -> dict[str, str]:
    """Parse key-value pairs from .env file."""
    env_vars: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                env_vars[k.strip()] = v.strip().strip('"').strip("'")
    return env_vars


def round_step(val: float, step: float) -> float:
    """Round value down to nearest step size (lot size filter)."""
    precision = max(0, int(round(-math.log10(step))))
    factor = 10 ** precision
    return round(math.floor(val * factor + 1e-9) / factor, precision)


def round_tick(val: float, tick: float) -> float:
    """Round value to nearest tick size (price filter)."""
    precision = max(0, int(round(-math.log10(tick))))
    return round(val, precision)


def init_executions_db() -> None:
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
            leverage         INTEGER DEFAULT 10,
            created_utc      TEXT NOT NULL,
            closed_utc       TEXT,
            raw_response     TEXT
        );
        """)
        try:
            con.execute("ALTER TABLE testnet_executions ADD COLUMN leverage INTEGER DEFAULT 10")
        except Exception:
            pass
        con.commit()


class BinanceFuturesApiClient:
    """Low-level authenticated HTTP client for Binance Futures USDⓈ-M Testnet."""

    def __init__(self, api_key: str | None = None, api_secret: str | None = None, base_url: str = TESTNET_BASE_URL):
        env = load_env()
        self.api_key = api_key or env.get("BINANCE_FUTURES_TESTNET_KEY", "")
        self.api_secret = api_secret or env.get("BINANCE_FUTURES_TESTNET_SECRET", "")
        self.base_url = base_url

    def request(self, method: str, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        """Execute HMAC-SHA256 signed request against Binance Futures REST API."""
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
