#!/usr/bin/env python3
"""Statistical Query & Data Aggregation Engine for Solana Web Dashboard.

Provides read-only database metrics, indicator series computation,
candlestick formatting, and improvement gate readiness evaluation.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "solana.db"

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))

import predictor  # noqa: E402


def _contributions_for_row(r: dict) -> dict:
    """Per-indicator score contributions for one prediction row."""
    try:
        features = json.loads(r.get("features_json") or "{}")
        params = json.loads(r.get("params_json") or "null")
        if not features:
            return {}
        _, contrib = predictor.score(features, params)
        return contrib
    except (ValueError, TypeError, KeyError):
        return {}


def get_db() -> sqlite3.Connection:
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
        if signal_source == "technical":
            where.append("p.signal_source IN ('technical', 'lead_lag')")
            where_p.append("p.signal_source IN ('technical', 'lead_lag')")
        else:
            where.append("p.signal_source = ?")
            where_p.append("p.signal_source = ?")
            args.append(signal_source)
            args_p.append(signal_source)

    sql = f"""
        SELECT 
            p.id, p.symbol, p.leader_symbol, p.created_wib, p.created_utc, p.horizon, p.direction, p.confidence,
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

    count_sql = "SELECT signal_source, COUNT(*) c FROM predictions GROUP BY signal_source"
    sig_counts = {r["signal_source"]: r["c"] for r in con.execute(count_sql).fetchall()}

    graded_non_noise = [r for r in rows if r["is_noise"] == 0 and r["correct"] is not None]
    n_graded = len(graded_non_noise)
    wins = sum(1 for r in graded_non_noise if r["correct"] == 1)
    losses = n_graded - wins

    hit_rate = round((wins / n_graded) * 100, 1) if n_graded > 0 else 0.0
    avg_base_rate = round(statistics.fmean([r["base_rate"] for r in rows]) * 100, 1) if rows else 50.0
    edge_pp = round(hit_rate - avg_base_rate, 1) if n_graded > 0 else 0.0

    noise_count = sum(1 for r in rows if r["is_noise"] == 1)
    noise_ratio = round((noise_count / len(rows)) * 100, 1) if rows else 0.0

    net_points = round(sum(r["move"] for r in graded_non_noise), 2)
    win_moves = [r["move"] for r in graded_non_noise if r["correct"] == 1]
    loss_moves = [abs(r["move"]) for r in graded_non_noise if r["correct"] == 0]
    profit_factor = round(sum(win_moves) / sum(loss_moves), 2) if sum(loss_moves) > 0 else (99.0 if win_moves else 0.0)

    equity = 0.0
    equity_series = []
    for r in graded_non_noise:
        equity += r["move"]
        equity_series.append({
            "id": r["id"],
            "time": r["graded_wib"],
            "equity": round(equity, 2),
            "move": round(r["move"], 2),
            "correct": r["correct"]
        })

    recent_sql = f"""
        SELECT p.id, p.symbol, p.leader_symbol, p.created_wib, p.horizon, p.direction, p.confidence,
               p.signal_source, p.ref_price, p.noise_threshold, p.features_json, p.params_json, p.inputs_json, p.notes,
               o.graded_wib, o.realized_price, o.move, o.move_pct, o.realized_dir, o.is_noise, o.correct, o.base_rate,
               od.target_price, od.sl_price, od.target_reached, od.sl_hit
        FROM predictions p
        LEFT JOIN outcomes o ON o.prediction_id = p.id
        LEFT JOIN outcome_details od ON od.prediction_id = p.id
        {pred_where}
        ORDER BY p.id DESC
        LIMIT 100
    """
    recent_rows = []
    for r in con.execute(recent_sql, args_p).fetchall():
        d = dict(r)
        d["contributions"] = _contributions_for_row(d)
        recent_rows.append(d)

    active_trades = []
    lead_lag_active_sql = """
        SELECT p.id, p.symbol, p.leader_symbol, p.created_wib, p.created_utc, p.direction,
               p.ref_price, p.noise_threshold, p.inputs_json, p.features_json, p.notes
        FROM predictions p
        LEFT JOIN outcomes o ON o.prediction_id = p.id
        WHERE o.prediction_id IS NULL AND p.signal_source = 'lead_lag' AND p.direction IN ('bullish', 'bearish')
        ORDER BY p.id ASC
    """
    for r in con.execute(lead_lag_active_sql).fetchall():
        d = dict(r)
        d["contributions"] = _contributions_for_row(d)
        active_trades.append(d)

    by_symbol: dict[str, dict] = {}
    multi_symbols = ["SOLUSDT", "AVAXUSDT", "SUIUSDT", "NEARUSDT", "DOGEUSDT", "LINKUSDT"]
    for sym in multi_symbols:
        sym_graded = [r for r in graded_non_noise if (r.get("symbol") or "SOLUSDT") == sym]
        sym_n = len(sym_graded)
        sym_wins = sum(1 for r in sym_graded if r["correct"] == 1)
        sym_wr = round((sym_wins / sym_n) * 100, 1) if sym_n > 0 else 0.0
        sym_pts = round(sum(r["move"] for r in sym_graded), 2)
        by_symbol[sym] = {"total": sym_n, "wins": sym_wins, "win_rate": sym_wr, "net_points": sym_pts}

    con.close()

    return {
        "kpi": {
            "hit_rate": hit_rate, "n_graded": n_graded, "wins": wins, "losses": losses,
            "avg_base_rate": avg_base_rate, "edge_pp": edge_pp, "net_points": net_points,
            "profit_factor": profit_factor, "noise_ratio": noise_ratio, "noise_count": noise_count,
            "total_calls": total_preds, "pending_count": pending_preds,
            "verdict": "SIGNIFICANT" if (edge_pp and edge_pp > 3.0 and n_graded > 30) else ("COLLECTING_DATA" if n_graded < 30 else "NOT_SIGNIFICANT")
        },
        "by_symbol": by_symbol,
        "active_trades": active_trades,
        "signal_counts": sig_counts,
        "equity_curve": equity_series,
        "recent_predictions": recent_rows,
        "server_time_wib": datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB"),
        "filter": {"days": days or "all", "signal_source": signal_source or "all"}
    }


def get_candles(interval: str = "1h", limit: int = 300, symbol: str = "SOLUSDT") -> dict:
    import fetch_market as fm

    allowed = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}
    if interval not in allowed:
        interval = "1h"
    limit = max(60, min(int(limit or 300), 1000))
    sym = (symbol or "SOLUSDT").upper()

    try:
        bars = fm.fetch_candles(interval, limit, symbol=sym)
    except (fm.FetchError, fm.SchemaError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "bars": []}

    return {
        "ok": True,
        "interval": interval,
        "symbol": sym,
        "bars": [{
            "time": b["open_time"] // 1000,
            "open": b["open"], "high": b["high"],
            "low": b["low"], "close": b["close"],
            "volume": b["volume"],
        } for b in bars],
    }


def get_indicators(interval: str = "1h", limit: int = 300, symbol: str = "SOLUSDT") -> dict:
    import fetch_market as fm

    allowed = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}
    if interval not in allowed:
        interval = "1h"
    limit = max(60, min(int(limit or 300), 1000))
    sym = (symbol or "SOLUSDT").upper()

    try:
        bars = fm.fetch_candles(interval, limit, symbol=sym)
    except (fm.FetchError, fm.SchemaError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    times = [b["open_time"] // 1000 for b in bars]

    def rolling_ema(vals: list[float], period: int) -> list[float | None]:
        out: list[float | None] = [None] * len(vals)
        if len(vals) < period:
            return out
        k = 2.0 / (period + 1)
        e = float(statistics.fmean(vals[:period]))
        out[period - 1] = e
        for i in range(period, len(vals)):
            e = vals[i] * k + e * (1 - k)
            out[i] = e
        return out

    def rolling_sma(vals: list[float], period: int) -> list[float | None]:
        return [float(statistics.fmean(vals[i - period + 1:i + 1])) if i >= period - 1 else None for i in range(len(vals))]

    def rolling_atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float | None]:
        out: list[float | None] = [None] * len(closes)
        if len(closes) < period + 1:
            return out
        trs: list[float] = []
        for i in range(1, len(closes)):
            trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
        for i in range(period, len(closes)):
            window = trs[i - period:i]
            out[i] = float(statistics.fmean(window))
        return out

    def rolling_rsi(closes: list[float], period: int) -> list[float | None]:
        out: list[float | None] = [None] * len(closes)
        if len(closes) < period + 1:
            return out
        for i in range(period, len(closes)):
            window = closes[i - period:i + 1]
            gains = [max(window[j] - window[j - 1], 0.0) for j in range(1, len(window))]
            losses = [max(window[j - 1] - window[j], 0.0) for j in range(1, len(window))]
            ag, al = float(statistics.fmean(gains)), float(statistics.fmean(losses))
            out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
        return out

    ema12 = rolling_ema(closes, 12)
    ema26 = rolling_ema(closes, 26)
    sma20 = rolling_sma(closes, 20)
    atr14 = rolling_atr(highs, lows, closes, 14)
    rsi14 = rolling_rsi(closes, 14)

    def series(vals):
        return [{"time": t, "value": round(v, 4)} for t, v in zip(times, vals) if v is not None]

    atr_band = [{"time": t, "upper": round(c + a, 4), "lower": round(c - a, 4), "atr": round(a, 4)}
                for t, c, a in zip(times, closes, atr14) if a is not None]

    return {
        "ok": True,
        "interval": interval,
        "ema12": series(ema12),
        "ema26": series(ema26),
        "sma20": series(sma20),
        "rsi14": series(rsi14),
        "atr14": series(atr14),
        "atr_band": atr_band,
    }


def get_markers(signal_source: str | None = "technical", symbol: str | None = None) -> dict:
    con = get_db()
    where = []
    args = []
    if signal_source and signal_source != "all":
        if signal_source == "technical":
            where.append("p.signal_source IN ('technical', 'lead_lag')")
        else:
            where.append("p.signal_source = ?")
            args.append(signal_source)
    if symbol and symbol != "all":
        where.append("p.symbol = ?")
        args.append(symbol)

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = f"""
        SELECT p.id, p.symbol, p.leader_symbol, p.created_wib, p.created_utc, p.direction, p.ref_price, p.noise_threshold,
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
        sym = r["symbol"] or "SOLUSDT"
        if not graded:
            if r["direction"] == "no_call":
                kind, label, pos = "no_call_pending", f"#{r['id']} {sym} NO-CALL (ungraded)", "inBar"
            else:
                kind, label, pos = "pending", f"#{r['id']} {sym} PENDING", "inBar"
        elif r["is_noise"]:
            kind, label, pos = "noise", f"#{r['id']} {sym} NOISE", "inBar"
        elif r["correct"] == 1:
            kind, label, pos = "win", f"#{r['id']} {sym} WIN", "belowBar"
        elif r["correct"] == 0:
            kind, label, pos = "loss", f"#{r['id']} {sym} LOSS", "aboveBar"
        else:
            kind, label, pos = "no_call_resolved", f"#{r['id']} {sym} NO-CALL", "inBar"

        out.append({
            "id": r["id"],
            "symbol": sym,
            "leader_symbol": r["leader_symbol"],
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
    import fetch_market as fm
    try:
        spot = fm.fetch_spot("SOLUSDT")
        cross = spot["price"]
        try:
            cc = fm.fetch_cross_check()
            if cc and cc.get("sol_usd"):
                cross = cc["sol_usd"]
        except Exception:
            pass

        spots = {}
        for s in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", "SUIUSDT", "NEARUSDT", "DOGEUSDT", "LINKUSDT"]:
            try:
                sp = fm.fetch_spot(s)
                spots[s] = sp["price"]
            except Exception:
                pass

        return {
            "ok": True,
            "spot": spot["price"],
            "updated_at": spot["updated_at"],
            "cross_check": cross,
            "basis": round(cross - spot["price"], 2),
            "basis_pct": round((cross - spot["price"]) / spot["price"] * 100, 3) if spot["price"] else 0.0,
            "spots": spots,
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


from dashboard_improvement import get_improvement
