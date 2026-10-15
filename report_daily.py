#!/usr/bin/env python3
"""Solana's Job 5 — deliver the daily report to Discord, grounded and
drift-resistant. Ported from Nugget's report_daily.py verbatim structure;
only the token path, channel ID, and message copy (SOL framing, cross-check
instead of PAXG basis) differ.

The failure this is built against is NARRATIVE DRIFT: over 30 days, "hit-rate
51%, direction unclear" starts reading like "the model called 3 of the last 5"
and the natural pull is toward treating it as a signal. So the report is built
so that day-25 reads the same "not significant" framing as day-1:

  * The numbers are assembled in CODE from the ledger (never by prose).
  * Prose is validated against the evidence bundle; any ungrounded figure
    rejects the whole report instead of posting it.
  * The uncertainty line is PREPENDED, not optional - n, CI and the base rate
    appear before any narrative.
  * The disclaimer is appended by code, not by the writer.

Delivery is a direct Discord REST POST, which works independently of the
gateway WebSocket.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import daily                       # noqa: E402
import ledger                      # noqa: E402

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))
JOB = "daily_report_deliver"
ENV_PATH = Path.home() / ".hermes" / "profiles" / "solana" / ".env"
CHANNEL_ID = "1560267383896145981"


def bot_token() -> str:
    for line in ENV_PATH.read_text(errors="replace").splitlines():
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("no DISCORD_BOT_TOKEN in solana .env")


def post_discord(content: str, channel_id: str = CHANNEL_ID) -> dict:
    """Direct REST post. Returns the created message id on success."""
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    body = json.dumps({"content": content[:1990]}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": "Bot " + bot_token(),
                 "Content-Type": "application/json",
                 "User-Agent": "DiscordBot (hermes, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return {"ok": True, "message_id": json.loads(r.read().decode())["id"]}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": e.read().decode()[:300]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def uncertainty_header(ev: dict) -> str:
    """The line that must appear before any narrative. Assembled in code."""
    tr = ev["track_record"]
    if tr.get("n"):
        return (f"**Track record** — hit-rate {tr['hit_rate']}% "
                f"(n={tr['n']}, 95% CI {tr['ci95']}, baseline {tr['baseline']}%) "
                f"→ **{tr['verdict']}**")
    return (f"**Track record** — no graded calls yet (baseline {tr['baseline']}%). "
            f"→ **{tr['verdict']}** — nothing to report as an edge.")


def build_message(ev: dict, prose: str | None = None) -> str:
    """Assemble the final post. Numbers from code; prose optional and checked."""
    lines = [
        f"₿ **SOLUSDT daily report** — {ev['now_wib'][:16]} WIB",
        "",
        uncertainty_header(ev),
        "",
        f"**Market** — spot {ev['spot']:.2f}, cross-check (CoinGecko) "
        f"{ev['cross_check_sol']} (delta {ev['cross_check_delta_pct']:+.3f}%)",
        f"**Call** — {ev['direction']} (score {ev['score']:+.3f}, "
        f"conf {ev['confidence']:.2f})",
        f"**Noise threshold** — {ev['noise_threshold']:.2f} (moves below are "
        f"excluded from the hit-rate)",
        f"**Ledger** — {ev['ledger_counts']['predictions']} predictions, "
        f"{ev['ledger_counts']['outcomes']} graded, "
        f"{ev['ledger_counts']['gaps']} market-closed gaps, "
        f"chain {'OK' if ev['chain']['ok'] else 'BROKEN'}",
    ]


    if ev.get("yesterday"):
        lines.append("")
        lines.append("**Last graded calls**")
        for y in ev["yesterday"][:5]:
            outcome = ("noise" if y["is_noise"] else
                       ("hit" if y["correct"] == 1 else
                        ("miss" if y["correct"] == 0 else "no-call")))
            lines.append(f"• #{y['prediction_id']} {y['realized_dir']} "
                         f"{y['move']:+.2f} ({y['move_pct']:+.3f}%) → {outcome}")

    if prose:
        lines += ["", "---", "", prose.strip()]

    lines += ["", f"_source: {', '.join(k for k, v in ev['source_status'].items() if v == 'ok')}_"]
    lines += ["", f"_{daily.DISCLAIMER}_"]
    return "\n".join(lines)


def run(now: datetime | None = None, *, prose: str | None = None,
        dry_run: bool = False, post: bool = True,
        evidence: dict | None = None) -> dict:
    """Build evidence, ground any prose against it, and deliver.

    `evidence` lets the caller validate prose against the exact bundle it was
    written from. Without it, a live price tick between writing the prose and
    validating it would spuriously reject an accurate report.
    """
    now = now or datetime.now(UTC)
    slot = now.replace(minute=0, second=0, microsecond=0)
    slot_iso = slot.isoformat(timespec="seconds")
    con = ledger.connect()

    if not dry_run and not ledger.claim_slot(con, JOB, slot_iso):
        return {"action": "duplicate_slot_rejected", "slot_utc": slot_iso}

    try:
        ev = evidence if evidence is not None else daily.build_report_evidence(con, now)
    except Exception as e:
        if not dry_run:
            ledger.record_fetch_error(con, "report_deliver",
                                      f"{type(e).__name__}: {e}")
            ledger.finish_slot(con, JOB, slot_iso, "failed", str(e)[:300])
        return {"action": "evidence_failed", "error": f"{type(e).__name__}: {e}"}

    out = {"slot_utc": slot_iso, "evidence": ev}

    # Ground the prose against the SAME bundle it was written from.
    if prose:
        g = daily.validate_grounding(prose, ev)
        out["grounding"] = g
        if not g["ok"]:
            out["action"] = "report_rejected"
            out["reason"] = "ungrounded numbers: " + ", ".join(g["ungrounded"])
            if not dry_run:
                ledger.finish_slot(con, JOB, slot_iso, "failed", out["reason"][:300])
            return out

    msg = build_message(ev, prose)
    out["message"] = msg
    out["chars"] = len(msg)

    if dry_run or not post:
        out["action"] = "preview"
        return out

    res = post_discord(msg)
    out["delivery"] = res
    out["action"] = "posted" if res.get("ok") else "post_failed"
    ledger.finish_slot(con, JOB, slot_iso,
                       "ok" if res.get("ok") else "failed",
                       str(res)[:300])
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-post", action="store_true")
    ap.add_argument("--prose")
    a = ap.parse_args()
    print(json.dumps(run(prose=a.prose, dry_run=a.dry_run, post=not a.no_post),
                     indent=2, default=str))