#!/usr/bin/env python3
"""Solana's daily pre-open prediction (Job 1) and grounded daily report (Job 5).

Ported from Nugget's daily.py. What's genuinely different for SOL:
  - No `next_open()` / session-boundary walk: SOL never closes, so the daily
    target is simply now+24h (still walked forward defensively in case a
    future calendar nuance is ever modeled, but the walk is a single no-op
    iteration today).
  - No `real_yield_10y` or `high_impact_24h` (ForexFactory calendar) fields —
    those are gold-specific macro context (opportunity cost of holding a
    non-yielding asset; FX/rates calendar) with no equivalent fetch wired up
    yet for SOL. Adding them back requires a genuine SOL-relevant macro
    source, not a copy-paste of gold's.
  - `basis` becomes `cross_check_delta`: Binance vs CoinGecko cross-exchange
    sanity check, not a persistent token-vs-spot basis (see fetch_market.py).

Both jobs keep the same structural rule from Nugget's reliability review:
the LLM is a PROPOSER; a non-LLM validator is the COMMITTER.

  job 1: the daily call is produced by the SAME deterministic predictor as the
         hourly cycle (not by prose). The LLM's only role is to narrate the
         ledger row that already exists - it cannot invent the direction.

  job 5: every number in the report must resolve to a ledger row or the day's
         fetch. build_report() assembles those numbers from the ledger in code;
         the prose is generated around them and then checked with
         validate_grounding() - any figure not present in the evidence bundle
         REJECTS the report instead of posting it.

No trade advice, ever. Every reported rate carries n, CI and the base rate.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_market as fm          # noqa: E402
import ledger                      # noqa: E402
import predictor                   # noqa: E402
import session_calendar as cal     # noqa: E402
import stats                       # noqa: E402

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))
JOB_DAILY = "daily_predict"
JOB_REPORT = "daily_report"

DISCLAIMER = ("Learning instrument and reference only - NOT financial advice. "
              "No trade execution. Numbers carry their uncertainty.")


# ------------------------------------------------------------------- helpers

def next_open(now: datetime) -> datetime:
    """SOL has no session boundary — always `now` (the market is always open).
    Kept for interface parity with Nugget's daily.py; any caller that still
    calls this gets a sane, immediate answer rather than a KeyError."""
    return now


def daily_target(now: datetime) -> datetime:
    """The bar a daily call is judged against: now + 24h, walked forward only
    if a future calendar nuance is ever modeled (today `cal.is_open()` is
    always True, so this loop runs its first iteration and returns)."""
    t = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=24)
    for _ in range(24 * 5):
        if cal.is_open(t):
            return t
        t += timedelta(hours=1)
    return t


def day_ahead_evidence(con, now: datetime) -> dict:
    """The evidence bundle for a daily call. Everything here is measured."""
    snap = fm.collect()
    candles = snap["candles"]
    feats = predictor.compute_features(candles)
    p = predictor.predict(candles)
    base = stats.summarize([dict(r) for r in con.execute(
        """SELECT o.correct, o.is_noise, o.base_rate FROM outcomes o""")])

    return {
        "now_utc": now.isoformat(timespec="seconds"),
        "now_wib": now.astimezone(WIB).isoformat(timespec="seconds"),
        "next_open_utc": next_open(now).isoformat(timespec="seconds"),
        "spot": snap["spot"]["price"],
        "cross_check_sol": (snap.get("cross_check") or {}).get("sol_usd"),
        "cross_check_delta_pct": (round(
            ((snap.get("cross_check") or {}).get("sol_usd", snap["spot"]["price"])
             - snap["spot"]["price"]) / snap["spot"]["price"] * 100, 3)),
        "features": feats,
        "direction": p["direction"],
        "score": p["score"],
        "confidence": p["confidence"],
        "contributions": p["contributions"],
        "noise_threshold": p["noise_threshold"],
        "model_version": p["model_version"],
        "track_record": base,
        "ledger_counts": {
            t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("predictions", "outcomes", "gaps", "fetch_errors")},
        "source_status": snap["source_status"],
    }


# --------------------------------------------------------------- job 1

def run_daily(now: datetime | None = None, *, dry_run: bool = False) -> dict:
    """Daily pre-open prediction. Deterministic call, recorded before the day."""
    now = now or datetime.now(UTC)
    slot = now.replace(minute=0, second=0, microsecond=0)
    slot_iso = slot.isoformat(timespec="seconds")
    con = ledger.connect()

    if not dry_run and not ledger.claim_slot(con, JOB_DAILY, slot_iso):
        return {"action": "duplicate_slot_rejected", "slot_utc": slot_iso}

    try:
        ev = day_ahead_evidence(con, now)
    except (fm.FetchError, fm.SchemaError) as e:
        if not dry_run:
            ledger.record_fetch_error(con, "daily", f"{type(e).__name__}: {e}")
            ledger.finish_slot(con, JOB_DAILY, slot_iso, "failed", str(e)[:300])
        return {"action": "fetch_failed", "error": f"{type(e).__name__}: {e}"}

    target = daily_target(now)
    pid = None
    if not dry_run:
        notes = ev.get("reason") or f"daily; contrib={json.dumps(ev['contributions'])}"
        pid = ledger.record_prediction(
            con, horizon="24h",
            target_bar_utc=target.isoformat(timespec="seconds"),
            scheduled_slot_utc=slot_iso,
            direction=ev["direction"], confidence=ev["confidence"],
            ref_price=ev["spot"], model_version=ev["model_version"],
            params=predictor.build_params(), features=ev["features"],
            inputs={"spot": ev["spot"], "cross_check_sol": ev["cross_check_sol"],
                    "cross_check_delta_pct": ev["cross_check_delta_pct"],
                    "source_status": ev["source_status"]},
            signal_source="technical", noise_threshold=ev["noise_threshold"],
            notes=notes)
        ledger.finish_slot(con, JOB_DAILY, slot_iso, "ok", ev["direction"])

    return {"action": "predicted", "slot_utc": slot_iso, "prediction_id": pid,
            "evidence": ev}


# --------------------------------------------------------------- job 5

def build_report_evidence(con, now: datetime) -> dict:
    """The ONLY source of numbers allowed in job 5's report."""
    ev = day_ahead_evidence(con, now)
    rows = [dict(r) for r in con.execute(
        """SELECT o.correct, o.is_noise, o.base_rate, p.direction, p.signal_source
           FROM outcomes o JOIN predictions p ON p.id = o.prediction_id""")]
    ev["track_record"] = stats.summarize(rows)
    ev["chain"] = ledger.verify_chain(con)
    ev["yesterday"] = [
        {k: r[k] for k in ("prediction_id", "realized_dir", "move", "move_pct",
                           "is_noise", "correct")}
        for r in con.execute(
            """SELECT * FROM outcomes ORDER BY prediction_id DESC LIMIT 5""")]
    ev["open_today"] = [s.isoformat(timespec="seconds") for s in
                        cal.expected_hourly_slots(now, now + timedelta(hours=24))][:3]
    return ev

def _numbers(text: str) -> list[tuple[float, int]]:
    """Every number in `text` with its stated decimal precision.

    Precision matters: a report that rounds 0.2335 to "0.23" is making the SAME
    claim, so the comparison has to be numeric, not textual.
    """
    out = []
    for m in re.finditer(r"-?\d+\.?\d*", text):
        s = m.group(0)
        try:
            v = float(s)
        except ValueError:
            continue
        dec = len(s.split(".")[1]) if "." in s else 0
        out.append((v, dec))
    return out


def validate_grounding(prose: str, evidence: dict) -> dict:
    """Reject a report containing a figure that is not in the evidence.

    Comparison is NUMERIC with the prose's stated precision, so "4177.00" is
    accepted against an evidence value of 4177.0 and "0.23" against 0.2335 -
    rounding is not fabrication. A value with no near-match in the evidence
    bundle is fabrication, and rejects the whole report.
    """
    ev_nums = [v for v, _ in _numbers(json.dumps(evidence, default=str))]
    ungrounded = []
    for v, dec in _numbers(prose):
        tol = 0.5 * (10 ** -dec) if dec else 0.0
        hit = any(abs(e - v) <= tol or round(e, dec) == round(v, dec)
                  for e in ev_nums)
        if not hit and dec == 0:
            # bare small integers (counts) and years are structural, not claims
            hit = (0 <= v <= 12) or (2024 <= v <= 2035)
        if not hit:
            ungrounded.append(f"{v:g}")
    return {"ok": not ungrounded, "ungrounded": ungrounded[:20],
            "count": len(ungrounded)}


def run_report(now: datetime | None = None, *, dry_run: bool = False,
               prose: str | None = None, evidence: dict | None = None) -> dict:
    """Gather evidence, optionally validate supplied prose, and record the run.

    `evidence` lets a caller validate prose against the exact bundle it was
    written from, instead of a fresh fetch that may have moved.
    """
    now = now or datetime.now(UTC)
    slot = now.replace(minute=0, second=0, microsecond=0)
    slot_iso = slot.isoformat(timespec="seconds")
    con = ledger.connect()

    if not dry_run and not ledger.claim_slot(con, JOB_REPORT, slot_iso):
        return {"action": "duplicate_slot_rejected", "slot_utc": slot_iso}

    try:
        ev = build_report_evidence(con, now)
    except (fm.FetchError, fm.SchemaError) as e:
        if not dry_run:
            ledger.record_fetch_error(con, "report", f"{type(e).__name__}: {e}")
            ledger.finish_slot(con, JOB_REPORT, slot_iso, "failed", str(e)[:300])
        return {"action": "fetch_failed", "error": f"{type(e).__name__}: {e}"}

    out = {"action": "evidence_ready", "slot_utc": slot_iso, "evidence": ev,
           "disclaimer": DISCLAIMER}

    if prose is not None:
        # Validate against the SAME bundle the prose was written from. Callers
        # that already hold an evidence bundle (the normal path - the writer
        # reads the evidence, writes prose, hands both back) pass it in, so a
        # live price tick between write and validate cannot spuriously reject
        # an accurate report.
        check_ev = evidence if evidence is not None else ev
        g = validate_grounding(prose, check_ev)
        out["grounding"] = g
        if not g["ok"]:
            out["action"] = "report_rejected"
            out["reason"] = "ungrounded numbers: " + ", ".join(g["ungrounded"])
            if not dry_run:
                ledger.finish_slot(con, JOB_REPORT, slot_iso, "failed",
                                   out["reason"][:300])
            return out
        out["action"] = "report_ok"
        out["prose"] = prose

    if not dry_run:
        ledger.finish_slot(con, JOB_REPORT, slot_iso, "ok", out["action"])
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", choices=["daily", "report"], default="daily")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    fn = run_daily if a.job == "daily" else run_report
    print(json.dumps(fn(dry_run=a.dry_run), indent=2, default=str))