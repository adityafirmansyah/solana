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

from settings import get_settings, save_settings, validate_settings

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "solana.db"
TEMPLATE_DIR = BASE_DIR / "dashboard_testnet"

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))
PORT = 3003
VIRTUAL_CAPITAL_BASE = 100.0
LEVERAGE = 10

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
    s = get_settings()

    leverage = int(s.get("leverage", 10))
    virtual_base = float(s.get("virtual_capital", 100.0))
    target_notional = float(s.get("target_notional_usd", 40.0))
    margin_per_trade = float(s.get("margin_per_trade_usd", 4.0))
    margin_pct = round((margin_per_trade / virtual_base) * 100, 1)
    max_concurrent = int(s.get("max_concurrent", 2))
    circuit_limit = float(s.get("max_daily_loss", 10.0))

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
            margin = notional / leverage
            margin_locked += margin

            mark_px = tickers.get(sym, entry_px)
            if direction == "bullish":
                pnl = (mark_px - entry_px) * qty
            else:
                pnl = (entry_px - mark_px) * qty
            unrealized_pnl += pnl

        equity = round(virtual_base + realized_pnl + unrealized_pnl, 2)
        avail = max(0.0, round(equity - margin_locked, 2))

        return {
            "virtual_base": virtual_base,
            "equity": equity,
            "available_cash": avail,
            "margin_locked": round(margin_locked, 2),
            "target_margin_pct": f"{margin_pct:.1f}%",
            "target_margin_usd": f"${margin_per_trade:.2f}",
            "target_notional_usd": target_notional,
            "leverage": leverage,
            "max_concurrent": max_concurrent,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": round(unrealized_pnl, 2),
            "today_pnl": today_pnl,
            "daily_loss": daily_loss,
            "circuit_breaker_limit": circuit_limit,
            "total_trades": total_trades,
            "total_wins": total_wins,
            "active_positions_count": len(open_rows),
            "settings": s,
        }
    finally:
        con.close()


def get_open_positions() -> list[dict]:
    tickers = get_live_tickers()
    s = get_settings()
    leverage = int(s.get("leverage", 10))
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

            pos_leverage = int(r["leverage"]) if ("leverage" in r.keys() and r["leverage"]) else leverage
            notional = round(entry_px * qty, 2)
            margin = round(notional / pos_leverage, 2)

            if direction == "bullish":
                pnl_usd = (mark_px - entry_px) * qty
                pnl_pct = (mark_px - entry_px) / entry_px * 100 * pos_leverage
            else:
                pnl_usd = (entry_px - mark_px) * qty
                pnl_pct = (entry_px - mark_px) / entry_px * 100 * pos_leverage

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
                "leverage": pos_leverage,
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
    s = get_settings()
    leverage = int(s.get("leverage", 10))
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

            pos_leverage = int(r["leverage"]) if ("leverage" in r.keys() and r["leverage"]) else leverage
            res.append({
                "id": r["id"],
                "prediction_id": r["prediction_id"],
                "symbol": r["symbol"],
                "direction": r["direction"],
                "side": r["side"],
                "leverage": pos_leverage,
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
_INDEX_HTML_MTIME: float = 0.0

def get_index_html() -> bytes | None:
    global _INDEX_HTML_CACHE, _INDEX_HTML_MTIME
    index_path = TEMPLATE_DIR / "index.html"
    if not index_path.exists():
        return None
    try:
        mtime = index_path.stat().st_mtime
        if _INDEX_HTML_CACHE is not None and mtime == _INDEX_HTML_MTIME:
            return _INDEX_HTML_CACHE
        raw = index_path.read_text(encoding="utf-8")
        modal_path = TEMPLATE_DIR / "partials" / "settings_modal.html"
        if modal_path.exists():
            raw = raw.replace("<!-- {{SETTINGS_MODAL}} -->", modal_path.read_text(encoding="utf-8"))
        _INDEX_HTML_CACHE = raw.encode("utf-8")
        _INDEX_HTML_MTIME = mtime
        return _INDEX_HTML_CACHE
    except Exception as e:
        logger.warning(f"Error reading index.html: {e}")
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

        if path.startswith("/static/"):
            static_file = TEMPLATE_DIR / path.lstrip("/")
            if static_file.exists() and static_file.is_file():
                raw = static_file.read_bytes()
                mime = "application/javascript" if path.endswith(".js") else "text/css"
                self.send_response(200)
                self.send_header("Content-Type", f"{mime}; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            else:
                self.send_error(404, "Static file not found")
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

        if path == "/api/settings":
            self._json(get_settings())
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

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/settings":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                if content_length <= 0:
                    self._json({"status": "error", "message": "Empty payload"}, status=400)
                    return

                raw_body = self.rfile.read(content_length).decode("utf-8")
                payload = json.loads(raw_body)
            except Exception as e:
                self._json({"status": "error", "message": f"Invalid JSON payload: {e}"}, status=400)
                return

            is_valid, validated_or_err = validate_settings(payload)
            if not is_valid or not isinstance(validated_or_err, dict):
                self._json({"status": "error", "message": str(validated_or_err)}, status=400)
                return

            old_settings = get_settings()
            try:
                new_settings = save_settings(validated_or_err)
            except Exception as e:
                self._json({"status": "error", "message": f"Failed to save settings: {e}"}, status=500)
                return

            # Sync leverage to Binance Testnet across follower coins if leverage changed or forced
            binance_sync = {}
            if new_settings.get("leverage") != old_settings.get("leverage") or payload.get("force_sync"):
                try:
                    from fapi_executor import BinanceFuturesExecutor
                    executor = BinanceFuturesExecutor()
                    followers = ["SOLUSDT", "AVAXUSDT", "SUIUSDT", "NEARUSDT", "DOGEUSDT", "LINKUSDT"]
                    binance_sync = executor.apply_leverage_to_symbols(new_settings["leverage"], followers)
                    logger.info(f"Synced {new_settings['leverage']}x leverage to Binance Testnet: {binance_sync}")
                except Exception as e:
                    logger.warning(f"Failed to sync leverage to Binance: {e}")
                    binance_sync = {"error": str(e)}

            self._json({
                "status": "ok",
                "message": "Settings updated successfully",
                "settings": new_settings,
                "binance_sync": binance_sync,
            })
            return

        self.send_error(404, "Not Found")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _json(self, data: Any, status: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
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
