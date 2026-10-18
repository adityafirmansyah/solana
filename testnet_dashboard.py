#!/usr/bin/env python3
"""Standalone Binance Futures Testnet Trading Dashboard ($100 Virtual Bankroll).

Runs on Port 3003. Zero external Python dependencies (pure stdlib).
Serves real-time execution statistics, open positions, unrealized PnL,
and historical closed trades from solana.db (testnet_executions).
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "solana.db"
TEMPLATE_DIR = BASE_DIR / "dashboard_testnet"

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))
PORT = 3003
VIRTUAL_CAPITAL_BASE = 100.0
LEVERAGE = 5

logger = logging.getLogger("testnet_dashboard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

_TICKER_CACHE: dict[str, float] = {}
_LAST_TICKER_TS: float = 0.0


def get_live_tickers() -> dict[str, float]:
    """Fetch all ticker prices from Binance Futures testnet with 1s caching."""
    global _TICKER_CACHE, _LAST_TICKER_TS
    now = time.time()
    if now - _LAST_TICKER_TS < 1.0 and _TICKER_CACHE:
        return _TICKER_CACHE

    url = "https://testnet.binancefuture.com/fapi/v1/ticker/price"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Solana-Dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            res = {}
            for item in data:
                res[item["symbol"]] = float(item["price"])
            _TICKER_CACHE = res
            _LAST_TICKER_TS = now
            return res
    except Exception as e:
        logger.debug(f"Failed to fetch live tickers from Binance: {e}")
        return _TICKER_CACHE


def get_db():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def get_stats() -> dict:
    today_prefix = datetime.now(UTC).strftime("%Y-%m-%d")
    tickers = get_live_tickers()

    con = get_db()
    try:
        # 1. Closed Trades Stats
        stats_row = con.execute("""
            SELECT 
                COUNT(*) as total_trades,
                COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END), 0) as total_wins,
                COALESCE(SUM(pnl_usd), 0.0) as realized_pnl,
                COALESCE(SUM(CASE WHEN closed_utc LIKE ? THEN pnl_usd ELSE 0.0 END), 0.0) as today_pnl,
                COALESCE(SUM(CASE WHEN closed_utc LIKE ? AND pnl_usd < 0 THEN pnl_usd ELSE 0.0 END), 0.0) as daily_loss
            FROM testnet_executions 
            WHERE status = 'CLOSED'
        """, (f"{today_prefix}%", f"{today_prefix}%")).fetchone()

        total_trades = stats_row["total_trades"] if stats_row else 0
        total_wins = stats_row["total_wins"] if stats_row else 0
        realized_pnl = round(stats_row["realized_pnl"] if stats_row else 0.0, 4)
        today_pnl = round(stats_row["today_pnl"] if stats_row else 0.0, 4)
        daily_loss = round(stats_row["daily_loss"] if stats_row else 0.0, 4)

        # 2. Open Positions
        open_rows = con.execute("SELECT * FROM testnet_executions WHERE status = 'OPEN'").fetchall()
        margin_locked = 0.0
        unrealized_pnl = 0.0

        for r in open_rows:
            sym = r["symbol"]
            entry_px = float(r["entry_price"])
            qty = float(r["entry_qty"])
            direction = r["direction"]
            notional = entry_px * qty
            margin = notional / LEVERAGE
            margin_locked += margin

            mark_px = tickers.get(sym, entry_px)
            if direction == "bullish":
                pnl = (mark_px - entry_px) * qty
            else:
                pnl = (entry_px - mark_px) * qty
            unrealized_pnl += pnl

        equity = round(VIRTUAL_CAPITAL_BASE + realized_pnl + unrealized_pnl, 2)
        avail = max(0.0, round(equity - margin_locked, 2))

        return {
            "virtual_base": VIRTUAL_CAPITAL_BASE,
            "equity": equity,
            "available_cash": avail,
            "margin_locked": round(margin_locked, 2),
            "realized_pnl": realized_pnl,
            "unrealized_pnl": round(unrealized_pnl, 2),
            "today_pnl": today_pnl,
            "daily_loss": daily_loss,
            "circuit_breaker_limit": 10.0,
            "total_trades": total_trades,
            "total_wins": total_wins,
            "active_positions_count": len(open_rows),
        }
    finally:
        con.close()


def get_open_positions() -> list[dict]:
    tickers = get_live_tickers()
    con = get_db()
    try:
        rows = con.execute("SELECT * FROM testnet_executions WHERE status = 'OPEN' ORDER BY id ASC").fetchall()
        res = []
        for r in rows:
            sym = r["symbol"]
            entry_px = float(r["entry_price"])
            qty = float(r["entry_qty"])
            direction = r["direction"]
            tp_px = float(r["tp_price"]) if r["tp_price"] else 0.0
            sl_px = float(r["sl_price"]) if r["sl_price"] else 0.0
            mark_px = tickers.get(sym, entry_px)

            notional = round(entry_px * qty, 2)
            margin = round(notional / LEVERAGE, 2)

            if direction == "bullish":
                pnl_usd = (mark_px - entry_px) * qty
                pnl_pct = (mark_px - entry_px) / entry_px * 100 * LEVERAGE
            else:
                pnl_usd = (entry_px - mark_px) * qty
                pnl_pct = (entry_px - mark_px) / entry_px * 100 * LEVERAGE

            res.append({
                "id": r["id"],
                "prediction_id": r["prediction_id"],
                "symbol": sym,
                "direction": direction,
                "entry_price": entry_px,
                "mark_price": mark_px,
                "entry_qty": qty,
                "notional_usd": notional,
                "margin_used": margin,
                "tp_price": tp_px,
                "sl_price": sl_px,
                "unrealized_pnl_usd": round(pnl_usd, 4),
                "unrealized_pnl_pct": round(pnl_pct, 2),
                "created_utc": r["created_utc"],
            })
        return res
    finally:
        con.close()


def get_history() -> list[dict]:
    con = get_db()
    try:
        rows = con.execute("""
            SELECT * FROM testnet_executions 
            WHERE status = 'CLOSED' 
            ORDER BY id DESC 
            LIMIT 50
        """).fetchall()

        res = []
        for r in rows:
            created_dt = datetime.fromisoformat(r["created_utc"]) if r["created_utc"] else None
            closed_dt = datetime.fromisoformat(r["closed_utc"]) if r["closed_utc"] else None

            duration_str = "--"
            closed_wib = "--"
            if closed_dt:
                closed_wib = closed_dt.astimezone(WIB).strftime("%H:%M:%S WIB")
                if created_dt:
                    secs = int((closed_dt - created_dt).total_seconds())
                    if secs < 60:
                        duration_str = f"{secs}s"
                    elif secs < 3600:
                        duration_str = f"{secs // 60}m {secs % 60}s"
                    else:
                        duration_str = f"{secs // 3600}h {(secs % 3600) // 60}m"

            res.append({
                "id": r["id"],
                "prediction_id": r["prediction_id"],
                "symbol": r["symbol"],
                "direction": r["direction"],
                "side": r["side"],
                "entry_price": float(r["entry_price"]),
                "exit_price": float(r["exit_price"]) if r["exit_price"] else 0.0,
                "exit_reason": r["exit_reason"],
                "pnl_usd": float(r["pnl_usd"]) if r["pnl_usd"] is not None else 0.0,
                "pnl_pct": float(r["pnl_pct"]) if r["pnl_pct"] is not None else 0.0,
                "closed_wib": closed_wib,
                "duration": duration_str,
            })
        return res
    finally:
        con.close()


_INDEX_HTML_CACHE: bytes | None = None

def get_index_html() -> bytes | None:
    global _INDEX_HTML_CACHE
    if _INDEX_HTML_CACHE is None:
        index_path = TEMPLATE_DIR / "index.html"
        if index_path.exists():
            _INDEX_HTML_CACHE = index_path.read_bytes()
    return _INDEX_HTML_CACHE


class TestnetDashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            content = get_index_html()
            if content:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            else:
                self.send_error(404, "Template index.html not found")
                return

        if path == "/api/stats":
            self._json(get_stats())
            return

        if path == "/api/positions":
            self._json(get_open_positions())
            return

        if path == "/api/history":
            self._json(get_history())
            return

        if path == "/api/health":
            self._json({
                "status": "ok",
                "time_utc": datetime.now(UTC).isoformat(),
                "time_wib": datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB"),
                "port": PORT,
            })
            return

        self.send_error(404, "Not Found")

    def _json(self, data: Any):
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Mute standard HTTP access logging to avoid spamming terminal/systemd
        pass


def main():
    server_address = ("0.0.0.0", PORT)
    httpd = ThreadingHTTPServer(server_address, TestnetDashboardHandler)
    logger.info(f"⚡ Solana Testnet Trading Dashboard ($100 Bankroll) running on http://0.0.0.0:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Dashboard server shutting down.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
