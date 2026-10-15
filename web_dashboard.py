#!/usr/bin/env python3
"""Local Modular Dashboard Server for Solana SOLUSDT Research Agent.

Ported from Nugget's web_dashboard.py. Only PAXG/XAUUSD-specific labels and
the port/DB path differ — same zero-dependency stdlib server, same API
surface, same /api/improvement readiness panel (generic over improve.py).

Port: 3002 (Nugget owns 3000)
Organized directory structure:
- dashboard/templates/index.html
- dashboard/static/css/style.css
- dashboard/static/js/app.js
"""
from __future__ import annotations

import json
import mimetypes
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
import predictor  # noqa: E402

DB_PATH = BASE_DIR / "solana.db"
DASHBOARD_DIR = BASE_DIR / "dashboard"
TEMPLATES_DIR = DASHBOARD_DIR / "templates"
STATIC_DIR = DASHBOARD_DIR / "static"
PORT = 3002

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))


def _contributions_for_row(r: dict) -> dict:
    """Per-indicator score contributions for one prediction row.

    `notes` only carries contrib=... for the handful of rows written before
    the human-readable rationale prose replaced it (cycle.py always prefers
    `reason` when present, so the JSON fallback stopped firing). Recomputing
    from features_json + params_json is equivalent and works for EVERY row,
    because predictor.score() is a pure function of exactly those two fields
    - nothing here depends on notes, and nothing is written back to the
    ledger (which is append-only and hash-chained; notes must stay as-is).
    """
    try:
        features = json.loads(r.get("features_json") or "{}")
        params = json.loads(r.get("params_json") or "null")
        if not features:
            return {}
        _, contrib = predictor.score(features, params)
        return contrib
    except (ValueError, TypeError, KeyError):
        return {}


def get_db():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def get_stats_data(days: int | None = None, signal_source: str | None = None) -> dict:
    con = get_db()
    where = ["o.prediction_id IS NOT NULL"]
    where_p = []
    args = []
    args_p = []

    if days is not None:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        where.append("p.created_utc >= ?")
        where_p.append("p.created_utc >= ?")
        args.append(cutoff)
        args_p.append(cutoff)

    if signal_source and signal_source != "all":
        where.append("p.signal_source = ?")
        where_p.append("p.signal_source = ?")
        args.append(signal_source)
        args_p.append(signal_source)

    sql = f"""
        SELECT 
            p.id, p.created_wib, p.created_utc, p.horizon, p.direction, p.confidence,
            p.signal_source, p.ref_price, p.noise_threshold,
            o.graded_wib, o.graded_utc, o.realized_price, o.move, o.move_pct,
            o.realized_dir, o.is_noise, o.correct, o.base_rate, o.row_hash
        FROM predictions p
        JOIN outcomes o ON o.prediction_id = p.id
        WHERE {' AND '.join(where)}
        ORDER BY p.id ASC
    """
    rows = [dict(r) for r in con.execute(sql, args).fetchall()]

    pred_where = f"WHERE {' AND '.join(where_p)}" if where_p else ""
    pred_sql = f"SELECT COUNT(*) FROM predictions p {pred_where}"
    total_preds = con.execute(pred_sql, args_p).fetchone()[0]

    pending_where_parts = ["o.prediction_id IS NULL", "p.direction != 'no_call'"] + where_p
    pending_sql = f"""
        SELECT COUNT(*) FROM predictions p
        LEFT JOIN outcomes o ON o.prediction_id = p.id
        WHERE {' AND '.join(pending_where_parts)}
    """
    pending_preds = con.execute(pending_sql, args_p).fetchone()[0]

    # Global signal counts for UI badges
    count_sql = "SELECT signal_source, COUNT(*) c FROM predictions GROUP BY signal_source"
    sig_counts = {r["signal_source"]: r["c"] for r in con.execute(count_sql).fetchall()}

    graded_non_noise = [r for r in rows if r["is_noise"] == 0 and r["correct"] is not None]
    wins = [r for r in graded_non_noise if r["correct"] == 1]
    losses = [r for r in graded_non_noise if r["correct"] == 0]
    noise_rows = [r for r in rows if r["is_noise"] == 1]
    no_calls = [r for r in rows if r["direction"] == "no_call"]

    n_graded = len(graded_non_noise)
    hit_rate = round((len(wins) / n_graded * 100), 2) if n_graded > 0 else None
    
    gross_win_pts = sum(abs(r["move"]) for r in wins)
    gross_loss_pts = sum(abs(r["move"]) for r in losses)
    profit_factor = round(gross_win_pts / gross_loss_pts, 2) if gross_loss_pts > 0 else (99.0 if gross_win_pts > 0 else 1.0)
    net_points = round(sum(r["move"] * (1 if r["direction"] == "bullish" else -1) for r in graded_non_noise), 2)

    avg_base_rate = round(sum(r["base_rate"] for r in rows) / len(rows) * 100, 2) if rows else 48.0
    edge_pp = round(hit_rate - avg_base_rate, 2) if hit_rate is not None else None

    cum_pts = 0.0
    equity_series = []
    for r in graded_non_noise:
        pts = r["move"] if r["direction"] == "bullish" else -r["move"]
        cum_pts += pts
        equity_series.append({
            "id": r["id"],
            "time": r["graded_wib"][:16],
            "points": round(cum_pts, 2),
            "move": round(pts, 2),
            "correct": r["correct"]
        })

    recent_where = f"WHERE {' AND '.join(where_p)}" if where_p else ""
    recent_sql = f"""
        SELECT 
            p.id, p.created_wib, p.created_utc, p.target_bar_utc, p.horizon, p.direction, p.confidence, p.ref_price, 
            p.noise_threshold, p.signal_source, p.notes, p.features_json, p.inputs_json,
            p.params_json,
            o.graded_wib, o.realized_price, o.move, o.is_noise, o.correct, o.base_rate,
            od.target_price, od.sl_price, od.bar_high, od.bar_low, od.target_reached, od.sl_hit
        FROM predictions p
        LEFT JOIN outcomes o ON o.prediction_id = p.id
        LEFT JOIN outcome_details od ON od.prediction_id = p.id
        {recent_where}
        ORDER BY p.id DESC
        LIMIT 50
    """
    recent_rows = [dict(r) for r in con.execute(recent_sql, args_p).fetchall()]
    for r in recent_rows:
        r["contributions"] = _contributions_for_row(r)
        score = sum(r["contributions"].values()) if r.get("contributions") else (r.get("confidence") or 0.0)
        if r.get("direction") == "bearish" and score > 0:
            score = -score
        r["score"] = round(score, 4)
        r["bullish_confidence"] = round(0.5 + 0.5 * score, 4)
        r["bearish_confidence"] = round(0.5 - 0.5 * score, 4)

    con.close()

    return {
        "kpis": {
            "total_predictions": total_preds,
            "pending_predictions": pending_preds,
            "total_outcomes": len(rows),
            "graded_non_noise": n_graded,
            "noise_filtered": len(noise_rows),
            "no_calls": len(no_calls),
            "hit_rate": hit_rate,
            "base_rate": avg_base_rate,
            "edge_pp": edge_pp,
            "profit_factor": profit_factor,
            "net_points": net_points,
            "gross_win_pts": round(gross_win_pts, 2),
            "gross_loss_pts": round(gross_loss_pts, 2),
            "verdict": "SIGNIFICANT" if (edge_pp and edge_pp > 3.0 and n_graded > 30) else ("COLLECTING_DATA" if n_graded < 30 else "NOT_SIGNIFICANT")
        },
        "signal_counts": sig_counts,
        "equity_curve": equity_series,
        "recent_predictions": recent_rows,
        "server_time_wib": datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB"),
        "filter": {"days": days or "all", "signal_source": signal_source or "all"}
    }


def get_candles(interval: str = "1h", limit: int = 300) -> dict:
    """OHLCV bars for the self-hosted candlestick chart.

    Data comes from the SAME feed Solana predicts on (SOLUSDT direct, no
    proxy needed), so every marker drawn on this chart lines up with the
    prices in the ledger.
    """
    sys.path.insert(0, str(BASE_DIR))
    import fetch_market as fm

    allowed = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}
    if interval not in allowed:
        interval = "1h"
    limit = max(60, min(int(limit or 300), 1000))

    try:
        bars = fm.fetch_candles(interval, limit)
    except (fm.FetchError, fm.SchemaError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "bars": []}

    return {
        "ok": True,
        "interval": interval,
        "symbol": "SOLUSDT",
        "bars": [{
            # Lightweight Charts / KLineChart wants UNIX seconds
            "time": b["open_time"] // 1000,
            "open": b["open"], "high": b["high"],
            "low": b["low"], "close": b["close"],
            "volume": b["volume"],
        } for b in bars],
    }


def get_indicators(interval: str = "1h", limit: int = 300) -> dict:
    """Rolling EMA12/EMA26/SMA20/RSI14/ATR14-band series over the SAME bars
    `/api/candles` serves, computed with the exact same pure functions
    predictor.py uses to score every live prediction (ema/sma/rsi/atr) - so
    an overlay line is never a different number than what the ledger
    actually voted on. Read-only: no new table, no new fetch source, no
    change to predictor.py or the live cycle.
    """
    sys.path.insert(0, str(BASE_DIR))
    import fetch_market as fm
    import statistics

    allowed = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}
    if interval not in allowed:
        interval = "1h"
    limit = max(60, min(int(limit or 300), 1000))

    try:
        bars = fm.fetch_candles(interval, limit)
    except (fm.FetchError, fm.SchemaError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    times = [b["open_time"] // 1000 for b in bars]

    def rolling_ema(vals, period):
        out = [None] * len(vals)
        if len(vals) < period:
            return out
        k = 2.0 / (period + 1)
        e = statistics.fmean(vals[:period])
        out[period - 1] = e
        for i in range(period, len(vals)):
            e = vals[i] * k + e * (1 - k)
            out[i] = e
        return out

    def rolling_sma(vals, period):
        return [statistics.fmean(vals[i - period + 1:i + 1]) if i >= period - 1 else None
                for i in range(len(vals))]

    def rolling_atr(highs, lows, closes, period):
        out = [None] * len(closes)
        if len(closes) < period + 1:
            return out
        trs = [None]
        for i in range(1, len(closes)):
            trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                            abs(lows[i] - closes[i - 1])))
        for i in range(period, len(closes)):
            window = trs[i - period + 1:i + 1]
            out[i] = statistics.fmean(window)
        return out

    def rolling_rsi(closes, period):
        out = [None] * len(closes)
        if len(closes) < period + 1:
            return out
        for i in range(period, len(closes)):
            window = closes[i - period:i + 1]
            gains = [max(window[j] - window[j - 1], 0) for j in range(1, len(window))]
            losses = [max(window[j - 1] - window[j], 0) for j in range(1, len(window))]
            ag, al = statistics.fmean(gains), statistics.fmean(losses)
            out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
        return out

    ema12 = rolling_ema(closes, 12)
    ema26 = rolling_ema(closes, 26)
    sma20 = rolling_sma(closes, 20)
    atr14 = rolling_atr(highs, lows, closes, 14)
    rsi14 = rolling_rsi(closes, 14)

    def series(vals):
        return [{"time": t, "value": round(v, 4)} for t, v in zip(times, vals) if v is not None]

    atr_band = [{"time": t, "upper": round(c + a, 4), "lower": round(c - a, 4)}
                for t, c, a in zip(times, closes, atr14) if a is not None]

    return {
        "ok": True,
        "interval": interval,
        "ema12": series(ema12),
        "ema26": series(ema26),
        "sma20": series(sma20),
        "rsi14": series(rsi14),
        "atr_band": atr_band,
    }


def get_markers(signal_source: str | None = "technical") -> dict:
    """Every graded prediction as a chart marker.

    This is the payoff of the self-hosted chart: Solana's own calls, placed
    on the exact bar they were judged against, so accuracy is visible
    instead of inferred from a table.
    """
    con = get_db()
    where = []
    args = []
    if signal_source and signal_source != "all":
        where.append("p.signal_source = ?")
        args.append(signal_source)

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = f"""
        SELECT p.id, p.created_wib, p.created_utc, p.direction, p.ref_price, p.noise_threshold,
               p.signal_source, p.target_bar_utc, p.horizon,
               o.realized_price, o.move, o.is_noise, o.correct, o.graded_wib
        FROM predictions p
        LEFT JOIN outcomes o ON o.prediction_id = p.id
        {where_clause}
        ORDER BY p.id ASC
    """
    rows = con.execute(sql, args).fetchall()
    con.close()

    out = []
    for r in rows:
        graded = r["graded_wib"] is not None
        if not graded:
            if r["direction"] == "no_call":
                kind, label, pos = "no_call_pending", f"#{r['id']} NO-CALL (ungraded)", "inBar"
            else:
                kind, label, pos = "pending", f"#{r['id']} PENDING", "inBar"
        elif r["is_noise"]:
            kind, label, pos = "noise", f"#{r['id']} NOISE", "inBar"
        elif r["correct"] == 1:
            kind, label, pos = "win", f"#{r['id']} WIN", "belowBar"
        elif r["correct"] == 0:
            kind, label, pos = "loss", f"#{r['id']} LOSS", "aboveBar"
        else:
            # Graded, not noise, but correct is still NULL: this is a
            # resolved "no_call" - Solana declined to pick a direction, so
            # win/loss doesn't apply, but the bar DID close and the real
            # price WAS recorded. Must render distinctly from a row that
            # hasn't reached its target bar yet (graded=False above).
            kind, label, pos = "no_call_resolved", f"#{r['id']} NO-CALL", "inBar"

        out.append({
            "id": r["id"],
            "created_wib": r["created_wib"],
            "created_utc": r["created_utc"],
            "target_bar_utc": r["target_bar_utc"],
            "horizon": r["horizon"],
            "direction": r["direction"],
            "ref_price": r["ref_price"],
            "realized_price": r["realized_price"],
            "noise_threshold": r["noise_threshold"],
            "signal_source": r["signal_source"],
            "graded": graded,
            "kind": kind,
            "correct": r["correct"],
            "is_noise": r["is_noise"],
            "move": r["move"],
            "label": label,
            "position": pos,
        })
    return {"ok": True, "markers": out}


def get_live_price() -> dict:
    """Sub-minute spot tick for the live price strip."""
    sys.path.insert(0, str(BASE_DIR))
    import fetch_market as fm
    try:
        spot = fm.fetch_spot()
        cc = fm.fetch_cross_check()
        btc = cc["btc_usd"]
        return {
            "ok": True,
            "spot": spot["price"],
            "updated_at": spot["updated_at"],
            "cross_check": btc,
            "basis": round(btc - spot["price"], 2),
            "basis_pct": round((btc - spot["price"]) / spot["price"] * 100, 3),
        }
    except (fm.FetchError, fm.SchemaError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def get_improvement() -> dict:
    """Improvement-session readiness: exactly what job #4's gate will see.

    Single source of truth: the same improve.load_graded() + improve.split()
    the improvement job itself runs, so this panel cannot drift from the real
    gate. Read-only end to end (ro connection; load_graded/get_incumbent/
    verify_chain are pure SELECTs).
    """
    import improve
    import ledger
    import session_calendar as cal

    now = datetime.now(UTC)
    con = get_db()
    rows = improve.load_graded(con)          # graded, non-noise, valid features
    train, held = improve.split(rows)
    n = len(rows)
    ready = (len(train) >= improve.MIN_TRAIN_ROWS
             and len(held) >= improve.MIN_HELD_OUT_ROWS)
    unlocks = lambda k: (int(k * 0.7) >= improve.MIN_TRAIN_ROWS
                         and k - int(k * 0.7) >= improve.MIN_HELD_OUT_ROWS)
    need_n = next(k for k in range(n, n + 500) if unlocks(k))
    n_train_ok = next(k for k in range(n, n + 500) if int(k * 0.7) >= improve.MIN_TRAIN_ROWS)
    n_held_ok = next(k for k in range(n, n + 500) if k - int(k * 0.7) >= improve.MIN_HELD_OUT_ROWS)
    binding = ("held_out" if n_held_ok > n_train_ok else
               "train" if n_train_ok > n_held_ok else "both")
    rows_needed = max(0, need_n - n)

    # Accrual: qualifying rows per MARKET-OPEN hour since the first qualifying
    # outcome. Open hours via the SAME session calendar the hourly gate uses,
    # so weekend hours and the daily 17:00-18:00 ET break accrue nothing.
    span = con.execute("""SELECT MIN(o.graded_utc) a, MAX(o.graded_utc) b
        FROM outcomes o JOIN predictions p ON p.id = o.prediction_id
        WHERE o.is_noise = 0""").fetchone()
    per_open_hour, open_hours, since_wib = 0.0, 0.0, None
    if span["a"] and span["b"] and n > 1:
        a = datetime.fromisoformat(span["a"])
        if a.tzinfo is None:
            a = a.replace(tzinfo=UTC)
        b = max(now, datetime.fromisoformat(span["b"]).replace(tzinfo=UTC))
        open_hours = len(cal.expected_hourly_slots(a, b))
        if open_hours > 0:
            per_open_hour = (n - 1) / open_hours  # one row existed at span start
        since_wib = a.astimezone(WIB).strftime("%a %d %b %H:%M")
    open_hours_to_gate = (rows_needed / per_open_hour) if (rows_needed and per_open_hour) else 0.0

    # Improve sessions: weekdays 21:00 WIB (cron 0 21 * * 1-5)
    def next_sessions(after, count):
        d = after.astimezone(WIB).replace(minute=0, second=0, microsecond=0)
        out = []
        while len(out) < count:
            d += timedelta(hours=1)
            if d.weekday() < 5 and d.hour == 21:
                out.append(d)
        return out

    first_eligible_wib = None
    if not ready and per_open_hour:
        for s in next_sessions(now, 12):
            open_h = len(cal.expected_hourly_slots(now, s.astimezone(UTC)))
            if n + per_open_hour * open_h >= need_n:
                first_eligible_wib = s.strftime("%a %Y-%m-%d %H:%M WIB")
                break

    next_run = next_sessions(now, 1)[0]
    inc = ledger.get_incumbent(con)
    incumbent = inc["version"] if inc else "v0.1-default (built-in weights)"

    # Not yet graded: anything without an outcome row. Rows past their target
    # bar count toward N the moment the next OPEN-market hourly cycle grades
    # them (grade_due takes everything ungraded whose target has passed - a
    # missed tick is caught up, never skipped).
    now_iso = now.isoformat(timespec="seconds")
    backlog = [{
        "id": r["id"],
        "target_bar_utc": (r["target_bar_utc"] or "")[:16],
        "direction": r["direction"],
        "note": ("past target - grades on the next open-market hourly cycle"
                 if (r["target_bar_utc"] or "") <= now_iso
                 else "target not reached yet"),
    } for r in con.execute("""SELECT p.id, p.target_bar_utc, p.direction
        FROM predictions p WHERE NOT EXISTS
        (SELECT 1 FROM outcomes o WHERE o.prediction_id = p.id) ORDER BY p.id""")]

    recent = []
    for r in con.execute("""SELECT scheduled_slot_utc, status, detail FROM cron_runs
        WHERE job='improve' ORDER BY scheduled_slot_utc DESC LIMIT 3"""):
        try:
            det = json.loads(r["detail"] or "{}")
        except ValueError:
            det = {}
        recent.append({
            "slot_wib": datetime.fromisoformat(r["scheduled_slot_utc"]).astimezone(WIB)
                        .strftime("%a %d %b %H:%M"),
            "status": r["status"], "train": det.get("train"), "held": det.get("held_out")})

    # Live integrity: this endpoint doubles as the health readout of the
    # outcome chain the panel's numbers come from.
    chain = ledger.verify_chain(con)
    # What the gate deliberately EXCLUDES (graded but non-qualifying): noise
    # bars (move under 0.5x ATR) and resolved no-calls (realized, abstained).
    exc = con.execute("""SELECT
        SUM(CASE WHEN o.is_noise=1 THEN 1 ELSE 0 END) noise,
        SUM(CASE WHEN o.is_noise=0 AND o.correct IS NULL THEN 1 ELSE 0 END) no_call
        FROM outcomes o""").fetchone()
    con.close()

    return {
        "ready": ready, "n": n, "need_n": need_n, "rows_needed": rows_needed,
        "excluded": {"noise": exc["noise"] or 0, "no_call": exc["no_call"] or 0},
        "train": {"have": len(train), "need": improve.MIN_TRAIN_ROWS},
        "held": {"have": len(held), "need": improve.MIN_HELD_OUT_ROWS},
        "binding": binding,
        "sources": {"technical": sum(1 for r in rows if r["signal_source"] == "technical"),
                    "sentiment": sum(1 for r in rows if r["signal_source"] == "sentiment")},
        "accrual": {"qualifying_per_open_hour": round(per_open_hour, 4),
                    "open_hours_measured": round(open_hours, 1),
                    "measured_since_wib": since_wib,
                    "open_hours_to_gate": round(open_hours_to_gate, 1) if rows_needed else 0.0},
        "next_run_wib": next_run.strftime("%a %Y-%m-%d %H:%M WIB"),
        "first_eligible_wib": first_eligible_wib,
        "incumbent": incumbent,
        "recent": recent,
        "pending_backlog": backlog,
        "chain": {"ok": chain.get("ok"), "head": (chain.get("head") or "")[:12]},
        "generated_wib": now.astimezone(WIB).strftime("%Y-%m-%d %H:%M:%S WIB"),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Serve index template
        if path == "/" or path == "/index.html":
            index_file = TEMPLATES_DIR / "index.html"
            if index_file.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(index_file.read_bytes())
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
            self._json(get_improvement())
            return

        # API: /api/stats
        if path == "/api/stats":
            days = int(query["days"][0]) if "days" in query and query["days"][0].isdigit() else None
            signal = query.get("signal", [None])[0]
            data = get_stats_data(days=days, signal_source=signal)
            self._json(data)
            return

        # API: /api/candles
        if path == "/api/candles":
            interval = query.get("interval", ["1h"])[0]
            limit = query.get("limit", ["300"])[0]
            self._json(get_candles(interval, limit))
            return

        # API: /api/markers
        if path == "/api/markers":
            sig = query.get("signal", ["technical"])[0]
            self._json(get_markers(signal_source=sig))
            return

        # API: /api/indicators - EMA12/EMA26/SMA20/RSI14/ATR14-band overlays
        if path == "/api/indicators":
            interval = query.get("interval", ["1h"])[0]
            limit = query.get("limit", ["300"])[0]
            self._json(get_indicators(interval, limit))
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
