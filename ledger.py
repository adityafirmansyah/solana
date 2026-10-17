#!/usr/bin/env python3
"""Satoshi's append-only prediction ledger (hardened) — ported from Nugget's
ledger.py verbatim (same schema, same hash-chain contract, same idempotency
guarantees). Only DB_PATH/backup filenames differ; see fetch_market.py and
predictor.py docstrings for what's genuinely SOL-specific in this engine.

Design contract (SOUL.md, non-negotiable):
  * Every prediction is written BEFORE the bar it is judged against exists.
  * Append-only. No UPDATE, no DELETE - enforced by SQLite triggers so it holds
    even against a bug in calling code.
  * A failed fetch is recorded as a failure. Nothing is carried forward.
  * Raw inputs are stored with every prediction so outcomes stay attributable.

Hardening added after the reliability review:
  * busy_timeout + retry-with-backoff (lock contention is a demonstrated failure
    mode in this codebase, not speculation)
  * cron_runs with UNIQUE(job, scheduled_slot_utc) so a duplicate or retried
    fire is REJECTED at insert, not merely idempotent by luck
  * gaps table - market-closed is neither an error nor a prediction
  * hash chain over predictions+outcomes, so "unmodified" is verifiable
    externally instead of trusting triggers that live in the same file
  * backup() for the nightly copy (single SSD = single point of failure)

Tables: predictions, outcomes, fetch_errors, gaps, cron_runs
"""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

WIB = timezone(timedelta(hours=7))
UTC = timezone.utc
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "solana.db"

BUSY_TIMEOUT_MS = 15000
RETRY_ATTEMPTS = 6
RETRY_BASE_S = 0.25

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
-- The append-only guarantee covers predictions/outcomes, which is what makes the
-- track record trustworthy; the registry only says which model is live.
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


# --------------------------------------------------------------- connection

def connect(path: Path | str = None) -> sqlite3.Connection:
    """Open the ledger.

    `path=None` resolves DB_PATH at CALL time, not at import time. That matters
    for tests and for anything that needs to point the ledger at a sandbox:
    binding the default as a parameter default would freeze it at import.
    """
    if path is None:
        path = DB_PATH
    con = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    ensure_outcome_details_table(con)
    return con


def with_retry(fn, attempts: int = RETRY_ATTEMPTS, base: float = RETRY_BASE_S,
               con: sqlite3.Connection | None = None):
    """Retry only on lock contention, with exponential backoff + jitter.

    `con` is optional but important: a failed write can leave an implicit
    transaction open, which HOLDS a lock and makes the next attempt fail too.
    Rolling back before each retry is what makes the retry actually able to
    succeed rather than spinning against our own stale lock.
    """
    last = None
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if con is not None:
                try:
                    con.rollback()
                except Exception:
                    pass
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last = e
            if i == attempts - 1:
                break
            time.sleep(base * (2 ** i) + random.uniform(0, base))
    raise last


# ------------------------------------------------------------------- chain

def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _row_hash(prev_hash: str, payload: dict) -> str:
    return hashlib.sha256((prev_hash + _canonical(payload)).encode()).hexdigest()


def _last_hash(con) -> str:
    r = con.execute(
        "SELECT row_hash FROM predictions ORDER BY id DESC LIMIT 1").fetchone()
    return r["row_hash"] if r else "GENESIS"


def _last_outcome_hash(con) -> str:
    """Head of the outcomes chain = the row graded LAST, not the highest pid.

    prediction_id is the *forecast's* id, so a late-graded prediction (its
    target bar already passed, graded hours later) inserts MID-chain, and when
    one grading pass writes both a backlog row and the current row, both point
    at the SAME parent (fork) - the chain is a tree, not a line. This sets the
    next row's parent to the newest-graded outcome so future writes stay
    linear in (graded_utc, prediction_id); verify_chain checks the historical
    fork as a tree. graded_utc increases monotonically across writes and the
    pid tiebreak keeps same-second grading deterministic.
    """
    r = con.execute(
        "SELECT row_hash FROM outcomes "
        "ORDER BY graded_utc DESC, prediction_id DESC LIMIT 1").fetchone()
    return r["row_hash"] if r else "GENESIS"


def verify_chain(con) -> dict:
    """Recompute the whole chain. Any edit to any row breaks it.

    NOTE: the autoincrement `id` is excluded from the hashed payload - it does
    not exist until after the insert, so it was never part of the original hash.
    Including it here would make every chain fail verification by construction.
    """
    prev = "GENESIS"
    for r in con.execute("SELECT * FROM predictions ORDER BY id ASC"):
        payload = {k: r[k] for k in r.keys()
                   if k not in ("row_hash", "prev_hash", "id")}
        if r["prev_hash"] != prev:
            return {"ok": False, "table": "predictions", "id": r["id"],
                    "reason": "prev_hash mismatch"}
        if _row_hash(prev, payload) != r["row_hash"]:
            return {"ok": False, "table": "predictions", "id": r["id"],
                    "reason": "row_hash mismatch"}
        prev = r["row_hash"]

    # Outcomes are a TREE, not a line: record_outcome picks its parent as the
    # outcome with MAX(prediction_id) at grading time, so a single grading pass
    # that catches up a backlogged row (low pid) alongside the current row
    # (high pid) makes BOTH reference the same parent. No total order - by pid
    # or by graded_utc - can linearize that fork (historically: pid13, graded
    # a day late, and pid40 of the same pass BOTH reference pid39). So verify
    # the structure the writer actually wrote:
    #   1. exactly one GENESIS root,
    #   2. every prev_hash resolves (GENESIS or another recorded outcome),
    #   3. every row_hash re-derives from its own recorded prev,
    #   4. every row is reachable from the root.
    # A forged/edited payload still breaks (3); a lost or foreign row still
    # breaks (2)/(4). (Append-only triggers make mid-chain loss a disk-level
    # event; (2)/(4) catch it, and the tail regressions the nightly backup
    # check covers.)
    o_rows = [dict(r) for r in con.execute("SELECT * FROM outcomes")]
    if o_rows:
        by_hash = {r["row_hash"]: r for r in o_rows}
        roots = [r for r in o_rows if r["prev_hash"] == "GENESIS"]
        if len(roots) != 1:
            return {"ok": False, "table": "outcomes",
                    "id": (roots[0]["prediction_id"] if roots else None),
                    "reason": f"expected exactly 1 GENESIS root, found {len(roots)}"}
        for r in o_rows:
            if r["prev_hash"] != "GENESIS" and r["prev_hash"] not in by_hash:
                return {"ok": False, "table": "outcomes", "id": r["prediction_id"],
                        "reason": "prev_hash points to no recorded outcome (orphan)"}
        # prediction_id IS part of the payload (known at write time), so it
        # stays in the re-hash.
        for r in o_rows:
            payload = {k: r[k] for k in r.keys() if k not in ("row_hash", "prev_hash")}
            if _row_hash(r["prev_hash"], payload) != r["row_hash"]:
                return {"ok": False, "table": "outcomes", "id": r["prediction_id"],
                        "reason": "row_hash mismatch"}
        seen, stack = set(), [roots[0]["row_hash"]]
        while stack:
            h = stack.pop()
            for r in o_rows:
                if r["prev_hash"] == h and r["row_hash"] not in seen:
                    seen.add(r["row_hash"])
                    stack.append(r["row_hash"])
        stranded = [r["prediction_id"] for r in o_rows
                    if r["prev_hash"] != "GENESIS" and r["row_hash"] not in seen]
        if stranded:
            return {"ok": False, "table": "outcomes", "id": stranded[0],
                    "reason": "outcome not reachable from GENESIS root"}
        # The head is the row graded LAST - what the next write builds on
        # (record_outcome's _last_outcome_hash). Total-graded ordering, then
        # pid, breaks same-second ties deterministically.
        head_row = max(o_rows, key=lambda r: (r["graded_utc"], r["prediction_id"]))
        return {"ok": True, "head": head_row["row_hash"]}
    return {"ok": True, "head": prev}


# ---------------------------------------------------------------- writes

def now_both() -> tuple[str, str]:
    n = datetime.now(UTC)
    return (n.isoformat(timespec="seconds"),
            n.astimezone(WIB).isoformat(timespec="seconds"))


def claim_slot(con, job: str, scheduled_slot_utc: str) -> bool:
    """Idempotency gate. True = this slot is OURS to run; False = already ran.

    UNIQUE(job, scheduled_slot_utc) is what makes a duplicate or retried fire
    impossible to double-execute, rather than merely unlikely.
    """
    utc, _ = now_both()

    def _do():
        con.execute(
            "INSERT INTO cron_runs (job, scheduled_slot_utc, started_utc, status)"
            " VALUES (?,?,?,'running')", (job, scheduled_slot_utc, utc))
        con.commit()
        return True

    try:
        return with_retry(_do, con=con)
    except sqlite3.IntegrityError:
        # A rejected duplicate leaves an OPEN implicit transaction, which holds a
        # write lock. Without this rollback the guard itself would break the very
        # next write from any connection ("database is locked"). Reproduced.
        try:
            con.rollback()
        except Exception:
            pass
        return False


def finish_slot(con, job: str, scheduled_slot_utc: str, status: str,
                detail: str | None = None) -> None:
    utc, _ = now_both()

    def _do():
        con.execute(
            "UPDATE cron_runs SET completed_utc=?, status=?, detail=?"
            " WHERE job=? AND scheduled_slot_utc=?",
            (utc, status, detail, job, scheduled_slot_utc))
        con.commit()
    with_retry(_do, con=con)


def record_prediction(con, *, horizon, target_bar_utc, scheduled_slot_utc,
                      direction, confidence, ref_price, model_version, params,
                      features, inputs, signal_source, noise_threshold,
                      symbol: str = "SOLUSDT", leader_symbol: str | None = None,
                      notes=None) -> int:
    """Append one prediction, chained. Returns its id. Never updates anything."""
    utc, wib = now_both()

    def _do():
        prev = _last_hash(con)
        payload = {
            "created_utc": utc, "created_wib": wib,
            "symbol": symbol, "leader_symbol": leader_symbol,
            "horizon": horizon,
            "target_bar_utc": target_bar_utc,
            "scheduled_slot_utc": scheduled_slot_utc, "direction": direction,
            "confidence": confidence, "ref_price": ref_price,
            "model_version": model_version, "params_json": _canonical(params),
            "features_json": _canonical(features), "inputs_json": _canonical(inputs),
            "signal_source": signal_source, "notes": notes,
            "noise_threshold": noise_threshold,
        }
        h = _row_hash(prev, payload)
        cur = con.execute(
            """INSERT INTO predictions
               (created_utc, created_wib, symbol, leader_symbol, horizon, target_bar_utc,
                scheduled_slot_utc, direction, confidence, ref_price,
                model_version, params_json, features_json, inputs_json,
                signal_source, notes, noise_threshold, prev_hash, row_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (utc, wib, symbol, leader_symbol, horizon, target_bar_utc, scheduled_slot_utc, direction,
             confidence, ref_price, model_version, _canonical(params),
             _canonical(features), _canonical(inputs), signal_source, notes,
             noise_threshold, prev, h))
        con.commit()
        return int(cur.lastrowid)

    return with_retry(_do, con=con)


def record_outcome(con, *, prediction_id, realized_price, move, move_pct,
                   is_noise, correct, base_rate) -> None:
    utc, wib = now_both()
    realized_dir = "bullish" if move > 0 else ("bearish" if move < 0 else "flat")

    def _do():
        # (graded_utc, prediction_id), NOT MAX(prediction_id): a late-graded
        # prediction inserts mid-sequence, so the newest GRADED row is the
        # true chain head. See _last_outcome_hash.
        prev = _last_outcome_hash(con)
        payload = {
            "prediction_id": prediction_id, "graded_utc": utc, "graded_wib": wib,
            "realized_price": realized_price, "move": move, "move_pct": move_pct,
            "realized_dir": realized_dir, "is_noise": 1 if is_noise else 0,
            "correct": correct, "base_rate": base_rate,
        }
        h = _row_hash(prev, payload)
        con.execute(
            """INSERT INTO outcomes
               (prediction_id, graded_utc, graded_wib, realized_price, move,
                move_pct, realized_dir, is_noise, correct, base_rate,
                prev_hash, row_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (prediction_id, utc, wib, realized_price, move, move_pct,
             realized_dir, 1 if is_noise else 0, correct, base_rate, prev, h))
        con.commit()
    with_retry(_do, con=con)


def ensure_outcome_details_table(con) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS outcome_details (
            prediction_id INTEGER PRIMARY KEY,
            symbol TEXT,
            leader_symbol TEXT,
            target_price REAL NOT NULL,
            sl_price REAL NOT NULL,
            bar_high REAL NOT NULL,
            bar_low REAL NOT NULL,
            target_reached INTEGER NOT NULL,
            sl_hit INTEGER NOT NULL,
            recorded_utc TEXT NOT NULL,
            recorded_wib TEXT NOT NULL,
            FOREIGN KEY(prediction_id) REFERENCES predictions(id)
        )
    """)
    cols = [r[1] for r in con.execute("PRAGMA table_info(outcome_details)").fetchall()]
    if "symbol" not in cols:
        con.execute("ALTER TABLE outcome_details ADD COLUMN symbol TEXT")
    if "leader_symbol" not in cols:
        con.execute("ALTER TABLE outcome_details ADD COLUMN leader_symbol TEXT")
    con.commit()


def record_outcome_details(con, *, prediction_id: int, target_price: float,
                           sl_price: float, bar_high: float, bar_low: float,
                           target_reached: int, sl_hit: int,
                           symbol: str | None = None, leader_symbol: str | None = None) -> None:
    ensure_outcome_details_table(con)
    utc, wib = now_both()

    def _do():
        con.execute(
            """INSERT OR REPLACE INTO outcome_details
               (prediction_id, symbol, leader_symbol, target_price, sl_price, bar_high, bar_low,
                target_reached, sl_hit, recorded_utc, recorded_wib)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (prediction_id, symbol, leader_symbol, target_price, sl_price, bar_high, bar_low,
             target_reached, sl_hit, utc, wib))
        con.commit()

    with_retry(_do, con=con)


def record_fetch_error(con, source: str, error: str) -> None:
    utc, wib = now_both()

    def _do():
        con.execute(
            "INSERT INTO fetch_errors (occurred_utc, occurred_wib, source, error)"
            " VALUES (?,?,?,?)", (utc, wib, source, error))
        con.commit()
    with_retry(_do, con=con)


def record_gap(con, slot_utc: datetime, reason: str) -> None:
    """Market closed: record the hole. Not an error, not a prediction."""
    utc, _ = now_both()
    wib = slot_utc.astimezone(WIB).isoformat(timespec="seconds")

    def _do():
        con.execute(
            "INSERT OR IGNORE INTO gaps (slot_utc, slot_wib, reason, recorded_utc)"
            " VALUES (?,?,?,?)",
            (slot_utc.isoformat(timespec="seconds"), wib, reason, utc))
        con.commit()
    with_retry(_do, con=con)


def ungraded(con, before_utc: str | None = None, horizon: str | None = None):
    """Oldest ungraded predictions whose target bar has passed.

    Deliberately does NOT assume the previous hour ran - a missed tick simply
    means nothing is graded this cycle, which is logged, not silently skipped.
    """
    q = """SELECT p.* FROM predictions p
           LEFT JOIN outcomes o ON o.prediction_id = p.id
           WHERE o.prediction_id IS NULL"""
    args = []
    if before_utc:
        q += " AND p.target_bar_utc <= ?"
        args.append(before_utc)
    if horizon:
        q += " AND p.horizon = ?"
        args.append(horizon)
    q += " ORDER BY p.target_bar_utc ASC"
    return con.execute(q, args).fetchall()


def get_incumbent(con, horizon: str | None = None) -> sqlite3.Row | None:
    if horizon:
        return con.execute(
            "SELECT * FROM model_versions WHERE status='incumbent' AND (version LIKE ? OR note LIKE ?)"
            " ORDER BY promoted_utc DESC LIMIT 1", (f"%{horizon}%", f"%horizon={horizon}%")).fetchone()
    return con.execute(
        "SELECT * FROM model_versions WHERE status='incumbent'"
        " ORDER BY promoted_utc DESC LIMIT 1").fetchone()


def register_model(con, version: str, params: dict, status: str,
                   evidence: dict | None = None, note: str | None = None) -> None:
    """Add or update a model version. The registry is deliberately mutable."""
    utc, _ = now_both()

    def _do():
        con.execute(
            """INSERT INTO model_versions
               (version, created_utc, params_json, status, evidence_json, note)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(version) DO UPDATE SET
                 params_json=excluded.params_json,
                 status=excluded.status,
                 evidence_json=excluded.evidence_json,
                 note=excluded.note""",
            (version, utc, _canonical(params), status,
             _canonical(evidence) if evidence else None, note))
        con.commit()
    with_retry(_do, con=con)


def promote_model(con, version: str, evidence: dict, note: str | None = None, horizon: str | None = None) -> None:
    """Make `version` the incumbent, retiring whatever was live."""
    utc, _ = now_both()
    filter_clause = f" AND (version LIKE '%{horizon}%' OR note LIKE '%horizon={horizon}%')" if horizon else ""

    def _do():
        con.execute(
            f"UPDATE model_versions SET status='retired', retired_utc=?"
            f" WHERE status='incumbent' AND version<>?{filter_clause}", (utc, version))
        con.execute(
            "UPDATE model_versions SET status='incumbent', promoted_utc=?,"
            " evidence_json=?, note=? WHERE version=?",
            (utc, _canonical(evidence), note, version))
        con.commit()
    with_retry(_do, con=con)


def backup(dest: Path | None = None) -> Path:
    """Consistent online backup - safe while other processes are writing."""
    dest = dest or (BASE_DIR / "backups" /
                    f"satoshi-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.db")
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(dest))
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    return dest


if __name__ == "__main__":
    con = connect()
    print(f"ledger ready at {DB_PATH}")
    print(f"busy_timeout={con.execute('PRAGMA busy_timeout').fetchone()[0]}ms")
    for t in ("predictions", "outcomes", "fetch_errors", "gaps", "cron_runs"):
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<14} rows={n}")
    print(f"chain verify: {verify_chain(con)}")