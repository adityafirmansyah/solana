#!/usr/bin/env python3
"""Cycle Reporting & Discord Delivery Formatting for Solana Engine.

Provides message rendering for hourly/multi-horizon cycle outcomes, position holdings,
and ledger track-record statistical reports.
"""
from __future__ import annotations

import json
from typing import Any

import ledger
import predictor
import stats


def format_hourly_delivery(r: dict) -> str:
    """Render the hourly cycle result as the Discord message.

    Returns an EMPTY STRING for the silent cases (market closed, duplicate slot).
    The cron runner suppresses delivery on empty stdout.
    """
    a = r.get("action")

    if a in ("gap", "duplicate_slot_rejected"):
        return ""

    horizon = r.get("horizon", "1h")
    if a in ("fetch_failed", "error"):
        return (f"⚠️ **Solana {horizon} cycle FAILED** ({r.get('slot_wib')})\n"
                f"`{a}`: {r.get('error')}")

    if a == "holding":
        h = r.get("holding", {}) or {}
        dir_str = str(h.get("direction", ""))
        dir_badge = "🟢 **Bullish**" if dir_str == "bullish" else "🔴 **Bearish**"
        pnl = float(h.get("pnl") or 0.0)
        pnl_pct = float(h.get("pnl_pct") or 0.0)
        pnl_sign = "+" if pnl > 0 else ""

        entry = float(h.get("entry_price") or 0.0)
        curr = float(h.get("current_price") or 0.0)
        tp = float(h.get("tp_price") or 0.0)
        sl = float(h.get("sl_price") or 0.0)

        cycle_title = "⚡ **Event Position In Progress**" if horizon == "event" else ("**15-Minute cycle (15M)**" if horizon == "15m" else ("**Hourly cycle**" if horizon == "1h" else f"**{horizon.upper()} cycle**"))
        unit_str = "bars" if horizon in ("15m", "5m", "event") else "h"
        time_unit = "breakout window" if horizon == "event" else ("15 minutes" if horizon == "15m" else ("hour" if horizon == "1h" else f"{horizon} bar"))

        lines = [
            f"{cycle_title} — {str(r.get('slot_wib'))[:16]} WIB",
            "",
            f"⏳ **Position In Progress** — Holding #{h.get('id')} ({h.get('bars_held')}{unit_str} / {h.get('max_bars')}{unit_str})",
            f"• **Direction** — {dir_badge}",
            f"• **Entry** — ${entry:,.2f}  ·  **Current** — ${curr:,.2f} ({pnl_sign}${pnl:,.2f} pts / {pnl_sign}{pnl_pct:.2f}%)",
            f"• **Take Profit** — ${tp:,.2f}  ·  **Cut Loss** — ${sl:,.2f}",
            "",
            f"_Target barrier not yet reached. No new prediction this {time_unit}._",
        ]
        return "\n".join(lines)

    p = r.get("prediction", {}) or {}
    graded = [g for g in r.get("graded", []) if g.get("status") == "graded"]

    score = p.get("score", 0.0)
    p_bull = p.get("bullish_confidence")
    p_bear = p.get("bearish_confidence")
    if p_bull is None or p_bear is None:
        p_bull = round(0.5 + 0.5 * score, 4)
        p_bear = round(0.5 - 0.5 * score, 4)

    dir_str = p.get("direction", "no_call")
    if dir_str == "bullish":
        verdict_badge = "🟢 **Bullish**"
    elif dir_str == "bearish":
        verdict_badge = "🔴 **Bearish**"
    else:
        verdict_badge = "⚪ **No Call** (Abstained / Neutral)"

    reason = p.get("reason") or ""
    if "Abstained:" in reason:
        reason_display = "Abstained: " + reason.split("Abstained:", 1)[1].strip()
    elif "Market in low-conviction chop" in reason:
        reason_display = "Market in low-conviction chop " + reason.split("Market in low-conviction chop", 1)[1].strip()
    elif "Bullish bias:" in reason:
        reason_display = "Bullish bias: " + reason.split("Bullish bias:", 1)[1].strip()
    elif "Bearish bias:" in reason:
        reason_display = "Bearish bias: " + reason.split("Bearish bias:", 1)[1].strip()
    else:
        reason_display = reason.strip()

    ref_price = p.get("ref_price")
    atr_val = (p.get("features") or {}).get("atr14") or ((p.get("noise_threshold") or 0) * 2.0)
    levels = p.get("levels")
    if not levels and ref_price:
        levels = predictor.compute_trade_levels(
            ref_price=ref_price,
            atr=atr_val,
            direction=dir_str,
            p_bull=p_bull,
            p_bear=p_bear,
            horizon=horizon,
        )

    if horizon == "1h":
        cycle_title = f"**Hourly cycle** — {str(r.get('slot_wib'))[:16]} WIB"
    elif horizon == "event":
        cycle_title = f"⚡ **Event Breakout Trigger** — {str(r.get('slot_wib'))[:19]} WIB"
    elif horizon == "5m":
        cycle_title = f"**5-Minute cycle (5M)** — {str(r.get('slot_wib'))[:16]} WIB"
    elif horizon == "15m":
        cycle_title = f"**15-Minute cycle (15M)** — {str(r.get('slot_wib'))[:16]} WIB"
    elif horizon == "8h":
        cycle_title = f"**8-Hour cycle (8H)** — {str(r.get('slot_wib'))[:16]} WIB"
    elif horizon == "12h":
        cycle_title = f"**12-Hour cycle (12H)** — {str(r.get('slot_wib'))[:16]} WIB"
    else:
        cycle_title = f"**{horizon.upper()} cycle** — {str(r.get('slot_wib'))[:16]} WIB"

    lines = [
        cycle_title,
        "",
        f"**Verdict** — {verdict_badge}",
    ]
    if reason_display:
        lines.append(f"**Reason** — {reason_display}")

    lines.extend([
        f"**Confidence** — 🐂 **{p_bull*100:.1f}% Bullish** · 🐻 **{p_bear*100:.1f}% Bearish** (Conviction: {p.get('confidence', 0):.2f})",
        f"**Ref price** — ${p.get('ref_price')}  ·  "
        f"noise threshold ${p.get('noise_threshold')}",
    ])

    if levels:
        tp_sign = levels["tp_sign"]
        sl_sign = levels["sl_sign"]
        lean_suffix = ""
        if dir_str == "no_call":
            lean_side = "Bullish" if levels["is_bull_side"] else "Bearish"
            lean_suffix = f"  ·  _{lean_side} lean_"

        tp_mult_val = levels.get("tp_mult", 1.5)
        sl_mult_val = levels.get("sl_mult", 1.0)
        rr_val = levels.get("rr_ratio", round(tp_mult_val / sl_mult_val, 1) if sl_mult_val else 1.5)

        lines.extend([
            f"**Target (TP)** — ${levels['tp_price']:.2f} ({tp_sign}${levels['tp_points']:.2f} pts  ·  {tp_mult_val:.1f}× ATR · R:R 1:{rr_val:.1f}){lean_suffix}",
            f"**Stop Loss (SL)** — ${levels['sl_price']:.2f} ({sl_sign}${levels['sl_points']:.2f} pts  ·  {sl_mult_val:.1f}× ATR){lean_suffix}",
        ])

    if graded:
        lines += ["", "**Graded**"]
        for g in graded:
            exit_reason = g.get("exit_reason", "")
            if exit_reason == "tp_hit":
                mark = "✅ **WIN (Take Profit Hit)**"
            elif exit_reason == "sl_hit":
                mark = "❌ **LOSS (Cut Loss Hit)**"
            elif exit_reason == "time_stop":
                sub = "WIN" if g.get("correct") == 1 else ("LOSS" if g.get("correct") == 0 else "NOISE")
                mark = f"⏰ **TIME-STOP (24h Close)** ({sub})"
            elif g.get("noise"):
                mark = "NOISE (excluded)"
            elif g.get("correct") == 1:
                mark = "WIN"
            elif g.get("correct") == 0:
                mark = "LOSS"
            else:
                mark = "no-call"

            exit_p = g.get("exit_price")
            exit_str = f"Exit ${exit_p:,.2f} · " if exit_p is not None else ""

            target_status = ""
            if not exit_reason:
                if g.get("target_reached"):
                    extreme_label = "High" if g.get("is_bull_side") else "Low"
                    target_status = f"  ·  🎯 **Target Reached** (TP: ${g['tp_price']:.2f} · {extreme_label}: ${g['extreme_val']:.2f})"
                elif g.get("sl_hit"):
                    extreme_label = "Low" if g.get("is_bull_side") else "High"
                    target_status = f"  ·  🛑 **Stop Hit** (SL: ${g['sl_price']:.2f} · {extreme_label}: ${g['extreme_val']:.2f})"
                elif g.get("tp_price"):
                    extreme_label = "High" if g.get("is_bull_side") else "Low"
                    target_status = f"  ·  ⏳ **Target Missed** (TP: ${g['tp_price']:.2f} · {extreme_label}: ${g['extreme_val']:.2f})"

            lines.append(f"• #{g['id']} {str(g['direction']).upper()} → "
                         f"{exit_str}move {g['move']:+.2f} pts · **{mark}**{target_status}")
    else:
        time_unit = "15 minutes" if horizon == "15m" else ("hour" if horizon == "1h" else f"{horizon} bar")
        lines += ["", f"_No predictions due for grading this {time_unit}._"]

    return "\n".join(lines)


format_cycle_delivery = format_hourly_delivery


def report(con) -> dict:
    """Honest summary: hit-rate with n, CI, and the base rate it must beat."""
    rows = [dict(r) for r in con.execute(
        """SELECT o.correct, o.is_noise, o.base_rate, p.signal_source
           FROM outcomes o JOIN predictions p ON p.id = o.prediction_id""")]
    summary = stats.summarize(rows)
    summary["chain"] = ledger.verify_chain(con)
    summary["counts"] = {
        t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("predictions", "outcomes", "gaps", "fetch_errors", "cron_runs")
    }
    return summary
