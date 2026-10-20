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

from ledger_schema import (
    SCHEMA,
    ensure_outcome_details_table,
    verify_chain,
    _canonical,
    _row_hash,
    _last_hash,
    _last_outcome_hash,
)

BUSY_TIMEOUT_MS = 15000
RETRY_ATTEMPTS = 6
RETRY_BASE_S = 0.25


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