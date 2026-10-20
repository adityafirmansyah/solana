#!/usr/bin/env python3
"""SQLite DDL Schema, Triggers, and Hash-Chain Verification for Satoshi/Solana Ledger.

Contains database tables, triggers enforcing append-only invariants, and
cryptographic SHA-256 chain verification.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS predictions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    created_utc        TEXT    NOT NULL,
    created_wib        TEXT    NOT NULL,
    symbol             TEXT    NOT NULL DEFAULT 'SOLUSDT',
    leader_symbol      TEXT,
    horizon            TEXT    NOT NULL,   -- '1h' | '24h' | 'lead_lag' | 'event'
    target_bar_utc     TEXT    NOT NULL,   -- the bar this call is judged against
    scheduled_slot_utc TEXT    NOT NULL,   -- the cron slot this belongs to
    direction          TEXT    NOT NULL,   -- 'bullish' | 'bearish' | 'no_call'
    confidence         REAL,
    ref_price          REAL    NOT NULL,
    model_version      TEXT    NOT NULL,
    params_json        TEXT    NOT NULL,
    features_json      TEXT    NOT NULL,
    inputs_json        TEXT    NOT NULL,
    signal_source      TEXT    NOT NULL,   -- 'technical' | 'sentiment' | 'lead_lag' | 'ensemble'
    notes              TEXT,
    noise_threshold    REAL    NOT NULL,   -- NOT NULL: ungradable without it
    prev_hash          TEXT    NOT NULL,
    row_hash           TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    prediction_id     INTEGER PRIMARY KEY,
    graded_utc        TEXT    NOT NULL,
    graded_wib        TEXT    NOT NULL,
    realized_price    REAL    NOT NULL,
    move              REAL    NOT NULL,
    move_pct          REAL    NOT NULL,
    realized_dir      TEXT    NOT NULL,
    is_noise          INTEGER NOT NULL,
    correct           INTEGER,            -- NULL when no_call or noise
    base_rate         REAL    NOT NULL,
    prev_hash         TEXT    NOT NULL,
    row_hash          TEXT    NOT NULL,
    FOREIGN KEY (prediction_id) REFERENCES predictions(id)
);

CREATE TABLE IF NOT EXISTS fetch_errors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_utc  TEXT NOT NULL,
    occurred_wib  TEXT NOT NULL,
    source        TEXT NOT NULL,
    error         TEXT NOT NULL
);

-- Market closed: not an error, not a prediction. A recorded hole.
CREATE TABLE IF NOT EXISTS gaps (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_utc     TEXT NOT NULL UNIQUE,
    slot_wib     TEXT NOT NULL,
    reason       TEXT NOT NULL,           -- session_state() value
    recorded_utc TEXT NOT NULL
);

-- Idempotency: a duplicate/retried fire is rejected by the UNIQUE constraint.
CREATE TABLE IF NOT EXISTS cron_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job                TEXT NOT NULL,
    scheduled_slot_utc TEXT NOT NULL,
    started_utc        TEXT NOT NULL,
    completed_utc      TEXT,
    status             TEXT NOT NULL,     -- running|ok|failed|skipped
    detail             TEXT,
    UNIQUE(job, scheduled_slot_utc)
);

-- Model registry. INTENTIONALLY MUTABLE - this is configuration, not the record.
CREATE TABLE IF NOT EXISTS model_versions (
    version       TEXT PRIMARY KEY,
    created_utc   TEXT NOT NULL,
    params_json   TEXT NOT NULL,
    status        TEXT NOT NULL,      -- incumbent | challenger | retired
    promoted_utc  TEXT,
    retired_utc   TEXT,
    evidence_json TEXT,
    note          TEXT
);

-- Append-only enforcement, at the database level.
CREATE TRIGGER IF NOT EXISTS predictions_no_update
BEFORE UPDATE ON predictions
BEGIN SELECT RAISE(ABORT, 'predictions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS predictions_no_delete
BEFORE DELETE ON predictions
BEGIN SELECT RAISE(ABORT, 'predictions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS outcomes_no_update
BEFORE UPDATE ON outcomes
BEGIN SELECT RAISE(ABORT, 'outcomes is append-only'); END;
CREATE TRIGGER IF NOT EXISTS outcomes_no_delete
BEFORE DELETE ON outcomes
BEGIN SELECT RAISE(ABORT, 'outcomes is append-only'); END;

CREATE INDEX IF NOT EXISTS idx_pred_target ON predictions(target_bar_utc);
CREATE INDEX IF NOT EXISTS idx_pred_source ON predictions(signal_source);
CREATE INDEX IF NOT EXISTS idx_pred_model  ON predictions(model_version);
CREATE INDEX IF NOT EXISTS idx_pred_slot   ON predictions(scheduled_slot_utc);
CREATE INDEX IF NOT EXISTS idx_pred_symbol ON predictions(symbol);
"""


def ensure_outcome_details_table(con: sqlite3.Connection) -> None:
    """Ensure outcome_details table and its columns exist."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS outcome_details (
            prediction_id INTEGER PRIMARY KEY,
            symbol TEXT,
            leader_symbol TEXT,
            target_price REAL,
            sl_price REAL,
            bar_high REAL,
            bar_low REAL,
            target_reached INTEGER DEFAULT 0,
            sl_hit INTEGER DEFAULT 0,
            recorded_utc TEXT,
            recorded_wib TEXT,
            FOREIGN KEY (prediction_id) REFERENCES predictions(id)
        );
    """)
    for col, col_type in (("symbol", "TEXT"), ("leader_symbol", "TEXT")):
        try:
            con.execute(f"ALTER TABLE outcome_details ADD COLUMN {col} {col_type}")
        except Exception:
            pass


def _canonical(obj) -> str:
    """Canonical JSON serialization for SHA-256 hash chaining."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _row_hash(prev_hash: str, payload: dict) -> str:
    return hashlib.sha256((prev_hash + _canonical(payload)).encode()).hexdigest()


def _last_hash(con: sqlite3.Connection) -> str:
    r = con.execute("SELECT row_hash FROM predictions ORDER BY id DESC LIMIT 1").fetchone()
    return r["row_hash"] if r else "GENESIS"


def _last_outcome_hash(con: sqlite3.Connection) -> str:
    r = con.execute(
        "SELECT row_hash FROM outcomes "
        "ORDER BY graded_utc DESC, prediction_id DESC LIMIT 1"
    ).fetchone()
    return r["row_hash"] if r else "GENESIS"


def verify_chain(con: sqlite3.Connection) -> dict:
    """Recompute the whole chain. Any edit to any row breaks it."""
    prev = "GENESIS"
    for r in con.execute("SELECT * FROM predictions ORDER BY id ASC"):
        payload = {k: r[k] for k in r.keys() if k not in ("row_hash", "prev_hash", "id")}
        if r["prev_hash"] != prev:
            return {"ok": False, "table": "predictions", "id": r["id"], "reason": "prev_hash mismatch"}
        if _row_hash(prev, payload) != r["row_hash"]:
            return {"ok": False, "table": "predictions", "id": r["id"], "reason": "row_hash mismatch"}
        prev = r["row_hash"]

    o_rows = [dict(r) for r in con.execute("SELECT * FROM outcomes")]
    if o_rows:
        by_hash = {r["row_hash"]: r for r in o_rows}
        roots = [r for r in o_rows if r["prev_hash"] == "GENESIS"]
        if len(roots) != 1:
            return {
                "ok": False, "table": "outcomes",
                "id": (roots[0]["prediction_id"] if roots else None),
                "reason": f"expected exactly 1 GENESIS root, found {len(roots)}"
            }
        for r in o_rows:
            if r["prev_hash"] != "GENESIS" and r["prev_hash"] not in by_hash:
                return {
                    "ok": False, "table": "outcomes", "id": r["prediction_id"],
                    "reason": "prev_hash points to no recorded outcome (orphan)"
                }
        for r in o_rows:
            payload = {k: r[k] for k in r.keys() if k not in ("row_hash", "prev_hash")}
            if _row_hash(r["prev_hash"], payload) != r["row_hash"]:
                return {"ok": False, "table": "outcomes", "id": r["prediction_id"], "reason": "row_hash mismatch"}
        seen, stack = set(), [roots[0]["row_hash"]]
        while stack:
            h = stack.pop()
            for r in o_rows:
                if r["prev_hash"] == h and r["row_hash"] not in seen:
                    seen.add(r["row_hash"])
                    stack.append(r["row_hash"])
        stranded = [r["prediction_id"] for r in o_rows if r["prev_hash"] != "GENESIS" and r["row_hash"] not in seen]
        if stranded:
            return {
                "ok": False, "table": "outcomes", "id": stranded[0],
                "reason": "outcome not reachable from GENESIS root"
            }

    return {"ok": True, "predictions_verified": True, "outcomes_verified": True}
