#!/usr/bin/env python3
"""Local Modular Dashboard Server for Solana SOLUSDT Research Agent.

Port: 3002
Organized directory structure:
- dashboard/templates/index.html
- dashboard/static/css/style.css
- dashboard/static/js/app.js
"""
from __future__ import annotations

import json
import mimetypes
import sys
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from dashboard_stats import (
    _contributions_for_row,
    get_db,
    get_stats_data,
    get_candles,
    get_indicators,
    get_markers,
    get_live_price,
    get_improvement,
)

DB_PATH = BASE_DIR / "solana.db"
DASHBOARD_DIR = BASE_DIR / "dashboard"
TEMPLATES_DIR = DASHBOARD_DIR / "templates"
STATIC_DIR = DASHBOARD_DIR / "static"
PORT = 3002

UTC = timezone.utc

def render_index_html() -> bytes | None:
    index_file = TEMPLATES_DIR / "index.html"
    if not index_file.exists():
        return None
    raw = index_file.read_text(encoding="utf-8")
    partials = {
        "<!-- {{CHARTS}} -->": "charts.html",
        "<!-- {{TECHNICAL_KPIS}} -->": "technical_kpis.html",
        "<!-- {{SENTIMENT_VIEW}} -->": "sentiment_view.html",
        "<!-- {{DETAILS_MODAL}} -->": "details_modal.html",
    }
    for tag, fname in partials.items():
        p_path = TEMPLATES_DIR / "partials" / fname
        if p_path.exists():
            raw = raw.replace(tag, p_path.read_text(encoding="utf-8"))
    return raw.encode("utf-8")


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Serve index template
        if path == "/" or path == "/index.html":
            content = render_index_html()
            if content:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            else:
                self.send_response(404)
                self.end_headers()
                return

        # Serve static assets (CSS, JS)
        if path.startswith("/static/"):
            rel_path = path[len("/static/"):]
            file_path = STATIC_DIR / rel_path
            try:
                file_path.resolve().relative_to(STATIC_DIR.resolve())
            except ValueError:
                self.send_response(403)
                self.end_headers()
                return

            if file_path.exists() and file_path.is_file():
                mime, _ = mimetypes.guess_type(str(file_path))
                self.send_response(200)
                self.send_header("Content-Type", mime or "application/octet-stream")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(file_path.read_bytes())
                return
            else:
                self.send_response(404)
                self.end_headers()
                return

        # API: /api/improvement - job #4 readiness (single source of truth:
        # the same load_graded/split the improvement job itself runs)
        if path == "/api/improvement":
            try:
                self._json(get_improvement())
            except Exception as e:
                self._json({
                    "ready": False,
                    "n": 0,
                    "need_n": 60,
                    "rows_needed": 60,
                    "held": {"have": 0, "need": 20},
                    "train": {"have": 0, "need": 40},
                    "next_run_wib": "N/A",
                    "incumbent": "v0.1-event-driven-sol",
                    "error": str(e),
                })
            return

        # API: /api/stats
        if path == "/api/stats":
            try:
                days = int(query["days"][0]) if "days" in query and query["days"][0].isdigit() else None
                signal = query.get("signal", [None])[0]
                data = get_stats_data(days=days, signal_source=signal)
                self._json(data)
            except Exception as e:
                self._json({"error": str(e), "kpis": {}, "recent_predictions": []})
            return

        # API: /api/candles
        if path == "/api/candles":
            interval = query.get("interval", ["1h"])[0]
            limit_str = query.get("limit", ["300"])[0]
            limit = int(limit_str) if limit_str.isdigit() else 300
            symbol = query.get("symbol", ["SOLUSDT"])[0]
            self._json(get_candles(interval, limit, symbol=symbol))
            return

        # API: /api/markers
        if path == "/api/markers":
            sig = query.get("signal", ["technical"])[0]
            symbol = query.get("symbol", [None])[0]
            self._json(get_markers(signal_source=sig, symbol=symbol))
            return

        # API: /api/indicators - EMA12/EMA26/SMA20/RSI14/ATR14-band overlays
        if path == "/api/indicators":
            interval = query.get("interval", ["1h"])[0]
            limit_str = query.get("limit", ["300"])[0]
            limit = int(limit_str) if limit_str.isdigit() else 300
            symbol = query.get("symbol", ["SOLUSDT"])[0]
            self._json(get_indicators(interval, limit, symbol=symbol))
            return

        # API: /api/live
        if path == "/api/live":
            self._json(get_live_price())
            return

        self.send_response(404)
        self.end_headers()

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def log_message(self, format, *args):
        pass


def run_server():
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"₿ Solana Modular Dashboard running on http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard...")
        server.server_close()


if __name__ == "__main__":
    run_server()
