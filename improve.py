#!/usr/bin/env python3
"""Job 4 - algorithm improvement for Solana (SOLUSDT) research engine.

Guardrails against overfitting and self-destruction:
  1. Ledger split: TRAIN slice for search, HELD-OUT slice for promotion gate.
  2. Promotion requires held-out CI to exclude baseline with best-of-N correction.
  3. Live rollback trigger: auto-revert if performance degrades post-promotion.
"""
from __future__ import annotations

import copy
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ledger                      # noqa: E402
import predictor                   # noqa: E402
import stats                       # noqa: E402

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))
JOB = "improve"

HELD_OUT_FRACTION = 0.30     # never seen by the search
MIN_TRAIN_ROWS = 40
MIN_HELD_OUT_ROWS = 20
ROLLBACK_TOLERANCE_PP = 8.0  # underperform the prior incumbent by this much...
ROLLBACK_WINDOW = 30         # ...over this many graded bars -> auto-revert


# ---------------------------------------------------------------- data access

def load_graded(con, horizon: str | None = None) -> list[dict]:
    """Graded, non-noise predictions with the features they were made from."""
    sql = """SELECT p.id, p.features_json, p.signal_source, p.direction AS made_call,
                  o.realized_dir, o.correct, o.is_noise, o.base_rate, o.move
           FROM predictions p JOIN outcomes o ON o.prediction_id = p.id
           WHERE o.is_noise = 0 AND p.signal_source = 'technical'"""
    args = []
    if horizon:
        sql += " AND p.horizon = ?"
        args.append(horizon)
    sql += " ORDER BY p.id ASC"

    rows = con.execute(sql, args).fetchall()
    out = []
    for r in rows:
        try:
            feats = json.loads(r["features_json"])
        except Exception:
            continue
        if not isinstance(feats, dict) or not feats:
            continue
        out.append({
            "id": r["id"],
            "features": feats,
            "realized_dir": r["realized_dir"],
            "base_rate": r["base_rate"] or 0.5,
            "signal_source": r["signal_source"],
        })
    return out


def split(rows: list[dict], held_out_fraction: float = HELD_OUT_FRACTION):
    """Chronological split. NEVER shuffled - a shuffled split leaks the future."""
    n = len(rows)
    cut = int(n * (1 - held_out_fraction))
    return rows[:cut], rows[cut:]


# --------------------------------------------------------------- evaluation

def evaluate(rows: list[dict], params: dict, abstain_bar: float, horizon: str = "event") -> dict:
    """Re-score each stored prediction with `params` and score the calls."""
    wins = calls = 0
    base_sum = 0.0
    for r in rows:
        try:
            s, _ = predictor.score(r["features"], params)
        except Exception:
            continue
        regime = predictor.check_regime_abstain(r["features"], s, horizon=horizon)
        if regime or abs(s) < abstain_bar:
            continue
        direction = "bullish" if s > 0 else "bearish"
        calls += 1
        base_sum += r["base_rate"]
        if direction == r["realized_dir"]:
            wins += 1
    base = (base_sum / calls) if calls else 0.5
    verdict = stats.grading_verdict(wins, calls, base, label="candidate")
    verdict["coverage"] = round(calls / len(rows) * 100, 1) if rows else 0.0
    return verdict


def candidate_grid(horizon: str = "event") -> list[dict]:
    """A small, interpretable grid with wide parameter dispersion tailored for SOL."""
    base = predictor.build_params()
    out = [{"name": "incumbent", "params": copy.deepcopy(base),
            "abstain_bar": predictor.ABSTAIN_BAR}]

    # 1. Momentum-Breakout led (ROC24 + Momentum heavy)
    p = copy.deepcopy(base)
    p["momentum"]["weight"] = 0.35
    p["trend"]["weight"] = 0.25
    p["position"]["weight"] = 0.25
    p["rsi"]["weight"] = 0.15
    out.append({"name": "momentum-breakout", "params": p, "abstain_bar": 0.28})

    # 2. Mean-Reversion / Exhaustion Fader (RSI dominant)
    p = copy.deepcopy(base)
    p["rsi"]["weight"] = 0.45
    p["momentum"]["weight"] = 0.10
    p["position"]["weight"] = 0.25
    p["trend"]["weight"] = 0.20
    out.append({"name": "rsi-reversal", "params": p, "abstain_bar": 0.28})

    # 3. Trend-Following (EMA12/EMA26 + SMA20 led)
    p = copy.deepcopy(base)
    p["trend"]["weight"] = 0.35
    p["position"]["weight"] = 0.35
    p["rsi"]["weight"] = 0.15
    p["momentum"]["weight"] = 0.15
    out.append({"name": "trend-following", "params": p, "abstain_bar": 0.28})

    # 4. Selective High-Conviction Gate
    p = copy.deepcopy(base)
    p["trend"]["weight"] = 0.30
    p["position"]["weight"] = 0.30
    p["rsi"]["weight"] = 0.25
    p["momentum"]["weight"] = 0.15
    out.append({"name": "high-conviction", "params": p, "abstain_bar": 0.38})

    # 5. Position-Heavy (20-SMA expansion)
    p = copy.deepcopy(base)
    p["position"]["weight"] = 0.45
    p["trend"]["weight"] = 0.25
    p["rsi"]["weight"] = 0.20
    p["momentum"]["weight"] = 0.10
    out.append({"name": "position-led", "params": p, "abstain_bar": 0.28})

    return out


# -------------------------------------------------------------------- search

def search(con, now: datetime | None = None, *, dry_run: bool = False, horizon: str = "event") -> dict:
    now = now or datetime.now(UTC)
    slot = now.replace(minute=0, second=0, microsecond=0)
    slot_iso = slot.isoformat(timespec="seconds")
    job_name = f"improve_{horizon}" if horizon != "event" else "improve"

    if not dry_run and not ledger.claim_slot(con, job_name, slot_iso):
        return {"action": "duplicate_slot_rejected", "slot_utc": slot_iso}

    rows = load_graded(con, horizon=horizon)
    train, held = split(rows)

    if len(train) < MIN_TRAIN_ROWS or len(held) < MIN_HELD_OUT_ROWS:
        res = {
            "action": "insufficient_data",
            "horizon": horizon,
            "train": len(train),
            "held_out": len(held),
            "need_train": MIN_TRAIN_ROWS,
            "need_held_out": MIN_HELD_OUT_ROWS,
            "note": f"no promotion attempted; held-out slice for {horizon} must be earned",
        }
        if not dry_run:
            ledger.finish_slot(con, job_name, slot_iso, "skipped", json.dumps(res)[:300])
        return res

    inc = ledger.get_incumbent(con, horizon=horizon)
    inc_params = (json.loads(inc["params_json"]) if inc
                  else predictor.build_params())
    inc_bar = (json.loads(inc["params_json"]).get("_abstain_bar",
               predictor.ABSTAIN_BAR) if inc else predictor.ABSTAIN_BAR)

    grid = candidate_grid(horizon=horizon)
    scored = []
    for cand in grid:
        v = evaluate(train, cand["params"], cand["abstain_bar"], horizon=horizon)
        scored.append({"name": cand["name"], "params": cand["params"],
                       "abstain_bar": cand["abstain_bar"], "train": v})
    scored.sort(key=lambda c: (c["train"]["hit_rate"] or 0), reverse=True)
    best = scored[0]

    incumbent_held = evaluate(held, inc_params, inc_bar, horizon=horizon)
    challenger_held = evaluate(held, best["params"], best["abstain_bar"], horizon=horizon)
    corrected = stats.best_of_n_significance(
        round((challenger_held["hit_rate"] or 0) / 100 * (challenger_held["n"] or 1)),
        challenger_held["n"] or 1, trials=len(grid), base_rate=challenger_held["baseline"] / 100)

    beats_incumbent = ((challenger_held["hit_rate"] or 0)
                       > (incumbent_held["hit_rate"] or 0))
    promote = (challenger_held["significant"] and beats_incumbent
               and corrected["significant"])

    evidence = {
        "horizon": horizon,
        "train_n": len(train), "held_out_n": len(held),
        "candidates_tried": len(grid),
        "search_ranking": [{"name": c["name"], "train_hit": c["train"]["hit_rate"],
                            "train_n": c["train"]["n"]} for c in scored],
        "best_on_train": best["name"],
        "incumbent_held_out": incumbent_held,
        "challenger_held_out": challenger_held,
        "corrected_p": corrected["p_value_corrected"],
        "decision": "promote" if promote else "keep",
        "reason": ("held-out CI excludes baseline, beats incumbent, and survives "
                   "the best-of-N correction" if promote else
                   "does not clear the held-out bar"),
    }

    if promote and not dry_run:
        version = f"v0.2-{horizon}-{best['name']}"
        params = copy.deepcopy(best["params"])
        params["_abstain_bar"] = best["abstain_bar"]
        note = f"horizon={horizon}; {evidence['reason']}"
        ledger.register_model(con, version, params, "challenger", evidence, note=note)
        ledger.promote_model(con, version, evidence, note=note, horizon=horizon)
        evidence["promoted_version"] = version

    if not dry_run:
        ledger.finish_slot(con, job_name, slot_iso, "ok", evidence["decision"])

    return {"action": "promoted" if promote else "kept_incumbent",
            "slot_utc": slot_iso, "evidence": evidence}


# ----------------------------------------------------------------- rollback

def check_rollback(con, now: datetime | None = None, *, dry_run: bool = False, horizon: str = "event") -> dict:
    """Auto-revert a promoted model that is underperforming the one it replaced."""
    inc = ledger.get_incumbent(con, horizon=horizon)
    if not inc:
        return {"action": "no_incumbent", "horizon": horizon}
    ev = json.loads(inc["evidence_json"]) if inc["evidence_json"] else {}
    prior = ev.get("incumbent_held_out", {})
    if not prior:
        return {"action": "no_prior_baseline", "version": inc["version"], "horizon": horizon}

    promoted_at = inc["promoted_utc"]
    rows = con.execute(
        """SELECT o.correct, o.is_noise, o.base_rate
           FROM outcomes o JOIN predictions p ON p.id = o.prediction_id
           WHERE o.graded_utc >= ? AND o.is_noise = 0 AND o.correct IS NOT NULL AND p.horizon = ?
           ORDER BY o.prediction_id ASC LIMIT ?""",
        (promoted_at, horizon, ROLLBACK_WINDOW)).fetchall()

    if len(rows) < ROLLBACK_WINDOW:
        return {"action": "insufficient_post_promotion_data",
                "have": len(rows), "need": ROLLBACK_WINDOW, "version": inc["version"], "horizon": horizon}

    cur = stats.summarize([dict(r) for r in rows])
    prior_rate = prior.get("hit_rate")
    gap = (prior_rate or 0) - (cur["hit_rate"] or 0)
    should_revert = gap > ROLLBACK_TOLERANCE_PP

    if should_revert and not dry_run:
        con.execute("UPDATE model_versions SET status='retired', retired_utc=?"
                    " WHERE version=?",
                    (now.isoformat(timespec="seconds") if now else
                     datetime.now(UTC).isoformat(timespec="seconds"), inc["version"]))
        con.commit()

    return {"action": "reverted" if should_revert else "held",
            "version": inc["version"], "horizon": horizon, "current_hit_rate": cur["hit_rate"],
            "prior_hit_rate": prior_rate, "gap_pp": round(gap, 2),
            "tolerance_pp": ROLLBACK_TOLERANCE_PP}


def run(now: datetime | None = None, *, dry_run: bool = False, horizon: str = "event") -> dict:
    con = ledger.connect()
    try:
        rb = check_rollback(con, now, dry_run=dry_run, horizon=horizon)
        s = search(con, now, dry_run=dry_run, horizon=horizon)
        return {"horizon": horizon, "rollback": rb, "search": s}
    finally:
        con.close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--horizon", default="event")
    a = ap.parse_args()
    print(json.dumps(run(dry_run=a.dry_run, horizon=a.horizon), indent=2, default=str))
