#!/usr/bin/env python3
"""Nugget's grading contract: statistics that make a hit-rate mean something.

A hit-rate alone is not a result. Every accuracy this module emits carries:
  * n            - graded sample size
  * CI           - bootstrap 95% confidence interval
  * z            - test against the stored base rate (NOT against a naked 50%)
  * p_value      - two-sided binomial
  * significance - does the CI exclude the baseline?

It also implements the multiple-comparisons correction that matters most here:
when you test N rules and report the best, the best beats chance by
construction. `best_of_n_significance` answers "is this better than what
picking the winner out of N tries produces under pure noise?" - which is the
question my own backtest initially got wrong.
"""
from __future__ import annotations

import math
import random

__all__ = [
    "binom_pmf", "binom_cdf", "p_value_two_sided", "z_score",
    "bootstrap_ci", "grading_verdict", "best_of_n_significance",
    "required_n_for_edge", "summarize",
]


# ---------------------------------------------------------------- binomial

def binom_pmf(k: int, n: int, p: float = 0.5) -> float:
    if k < 0 or k > n:
        return 0.0
    return math.comb(n, k) * (p ** k) * ((1 - p) ** (n - k))


def binom_cdf(k: int, n: int, p: float = 0.5) -> float:
    """P(X <= k). Exact, no scipy."""
    return sum(binom_pmf(i, n, p) for i in range(0, k + 1))


def p_value_two_sided(wins: int, n: int, p0: float) -> float:
    """Exact two-sided binomial p-value for H0: true rate == p0."""
    if n == 0:
        return 1.0
    obs = binom_pmf(wins, n, p0)
    return min(1.0, sum(binom_pmf(i, n, p0) for i in range(n + 1)
                        if binom_pmf(i, n, p0) <= obs + 1e-15))


# ---------------------------------------------------------------- normal / z

def z_score(p: float, n: int, p0: float) -> float:
    """Normal approximation z for observed rate p vs baseline p0."""
    if n == 0:
        return 0.0
    se = math.sqrt(p0 * (1 - p0) / n)
    if se == 0:
        return 0.0
    return (p - p0) / se


def bootstrap_ci(wins: int, n: int, iters: int = 20000, alpha: float = 0.05,
                 seed: int = 7) -> tuple[float, float]:
    """Percentile bootstrap CI for a hit-rate."""
    if n == 0:
        return (0.0, 0.0)
    rnd = random.Random(seed)
    p = wins / n
    sims = []
    for _ in range(iters):
        sims.append(sum(1 for _ in range(n) if rnd.random() < p) / n)
    sims.sort()
    lo = sims[int((alpha / 2) * iters)]
    hi = sims[min(iters - 1, int((1 - alpha / 2) * iters))]
    return (lo, hi)


# ---------------------------------------------------------------- contract

def grading_verdict(wins: int, n: int, base_rate: float, *, label: str = ""):
    """The grading contract. Every reported accuracy goes through this.

    base_rate is the always-up rate in the same window - never pass 0.5 unless
    the window genuinely was 50/50.
    """
    if n == 0:
        # Same SHAPE as the populated case. A report whose keys appear and
        # disappear with sample size is a trap for every consumer downstream.
        return {"label": label, "n": 0, "wins": 0, "hit_rate": None,
                "ci95": None, "baseline": round(base_rate * 100, 2),
                "edge_pp": None, "z": None, "p_value": None,
                "significant": False, "verdict": "NO_DATA"}

    p = wins / n
    lo, hi = bootstrap_ci(wins, n)
    z = z_score(p, n, base_rate)
    pv = p_value_two_sided(wins, n, base_rate)
    excludes = (lo > base_rate) or (hi < base_rate)
    return {
        "label": label,
        "n": n,
        "wins": wins,
        "hit_rate": round(p * 100, 2),
        "ci95": [round(lo * 100, 2), round(hi * 100, 2)],
        "baseline": round(base_rate * 100, 2),
        "edge_pp": round((p - base_rate) * 100, 2),
        "z": round(z, 2),
        "p_value": round(pv, 4),
        "significant": bool(excludes and pv < 0.05),
        "verdict": ("SIGNIFICANT" if (excludes and pv < 0.05)
                    else "NOT_SIGNIFICANT"),
    }


def best_of_n_significance(wins: int, n: int, trials: int, base_rate: float = 0.5,
                           sims: int = 20000, seed: int = 11) -> dict:
    """Significance corrected for having picked the best of `trials` rules.

    Under pure noise, max-of-N beats the per-rule rate by construction. This
    returns the p-value against THAT null, which is the honest test when a
    rule was selected by search over many candidates.
    """
    rnd = random.Random(seed)
    p = wins / n
    better = 0
    for _ in range(sims):
        best = 0.0
        for _t in range(trials):
            rate = sum(1 for _ in range(n) if rnd.random() < base_rate) / n
            if rate > best:
                best = rate
        if best >= p:
            better += 1
    return {
        "observed_rate": round(p * 100, 2),
        "rules_tested": trials,
        "n": n,
        "p_value_corrected": round(better / sims, 4),
        "significant": (better / sims) < 0.05,
        "note": ("p-value vs the null of picking the best of "
                 f"{trials} rules under pure noise"),
    }


def required_n_for_edge(edge: float, power: float = 0.8, alpha: float = 0.05) -> int:
    """How many graded bars to detect a real edge of `edge` (e.g. 0.52)."""
    d = abs(edge - 0.5)
    if d == 0:
        return 10 ** 9
    z_a = 1.959964 if alpha == 0.05 else 1.644854
    z_b = 0.841621 if power == 0.8 else 1.281552
    return math.ceil(((z_a + z_b) ** 2) * 0.25 / (d ** 2))


def summarize(rows) -> dict:
    """Summarize graded outcomes into the honest report shape.

    rows: iterable of dicts with 'correct' (1/0/None), 'is_noise' (0/1),
          'base_rate', 'signal_source'.
    """
    rows = list(rows)
    graded = [r for r in rows if r["correct"] is not None and not r["is_noise"]]
    noise = [r for r in rows if r["is_noise"]]
    nocall = [r for r in rows if r["correct"] is None and not r["is_noise"]]
    wins = sum(1 for r in graded if r["correct"] == 1)

    # The base rate is a property of the MARKET WINDOW, not of whether we chose
    # to make a call. Take it from every stored outcome that has one, so a run
    # with only no-calls still reports the real always-up rate instead of
    # silently falling back to a hardcoded 0.5.
    base_rows = [r for r in rows if r.get("base_rate") is not None]
    base = (sum(r["base_rate"] for r in base_rows) / len(base_rows)
            if base_rows else 0.5)

    out = grading_verdict(wins, len(graded), base, label="overall")
    out["noise_bars"] = len(noise)
    out["no_call_bars"] = len(nocall)
    out["total_outcomes"] = len(rows)
    return out


if __name__ == "__main__":
    print("=== self-test: reproduce the challenger's verified numbers ===")
    # The challenger reported rates to 2dp; 51.35% of 229 is not an integer win
    # count, so check the analytic z for the reported rate, then check the
    # contract runs on the nearest integer win count.
    print(f"analytic z(51.35%, n=229, vs 50%) = {z_score(0.5135, 229, 0.5):+.2f}")
    assert round(z_score(0.5135, 229, 0.5), 2) == 0.41

    v = grading_verdict(round(0.5135 * 229), 229, 0.5,
                        label="RSI mean-revert (best of 8)")
    print(f"{v['label']}: {v['hit_rate']}% n={v['n']} "
          f"CI={v['ci95']} z={v['z']} p={v['p_value']} -> {v['verdict']}")
    assert v["verdict"] == "NOT_SIGNIFICANT"

    print(f"analytic z(50.69%, n=1000, vs 50%) = {z_score(0.5069, 1000, 0.5):+.2f}")
    assert round(z_score(0.5069, 1000, 0.5), 2) == 0.44

    v2 = grading_verdict(round(0.5069 * 1000), 1000, 0.5, label="EMA+RSI confluence")
    print(f"{v2['label']}: {v2['hit_rate']}% n={v2['n']} "
          f"CI={v2['ci95']} z={v2['z']} p={v2['p_value']} -> {v2['verdict']}")
    assert v2["verdict"] == "NOT_SIGNIFICANT"

    b = best_of_n_significance(round(0.5069 * 1000), 1000, trials=8, sims=5000)
    print(f"best-of-8 corrected p = {b['p_value_corrected']} -> "
          f"significant={b['significant']}")
    assert b["p_value_corrected"] > 0.5, "expected non-significant"

    print("\nrequired n for a real edge to be detectable:")
    for e in (0.51, 0.52, 0.55):
        n = required_n_for_edge(e)
        print(f"  true {e*100:.0f}% -> n={n} (~{n/24:.0f} days @24 bars/day)")
    print("\nALL SELF-TESTS PASSED")
