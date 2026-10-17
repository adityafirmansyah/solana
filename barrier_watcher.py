#!/usr/bin/env python3
"""Solana Multi-Coin Lead-Lag Quantitative Research Engine & 5-State Barrier Daemon.

FSM Architecture (5 States):
- State 0: IDLE (Monitoring combined 1s feed, calculating rolling volatility & baselines)
- State 1: SQUEEZE_DETECTED (BTC 10-15m range <= 0.22%, volume drying up <= 1.0x baseline)
- State 2: ACCUMULATION_BUILDUP (BTC volume >= 2.0x baseline, Taker Buy/Sell ratio >= 60% or <= 40%)
- State 3: ARMED_VELOCITY (ETH confirmation + BTC moving 0.08% - 0.12%, 45s TTL)
- State 4: DISPARITY_EXECUTION (BTC >= 0.12% - 0.15% with volume sweep, follower moved < 0.05%,
                                dynamic TP 1.5x ATR, SL 1.0x ATR)
- Resolution / Barrier Exit (Sub-second TP hit, SL hit, or 2-hour TimeStop flat cut)

Natural Persona Reporting:
- Transitions into State 1, 2, 3, 4 and trade resolutions invoke the Solana quant persona
  via 9Router (http://127.0.0.1:20128/v1/chat/completions, model qwen3.8-max).
- Voice style: Casual Bahasa Indonesia (gue/lo), sharp, candid, explaining order book compression,
  flow absorption, taker pressure, lead-lag divergence, and data figures.
- Direct REST delivery to Discord channel 1560267383896145981 via report_daily.post_discord.
- Cooldown & debounce guards prevent notification spam (at least 15m for Squeeze & Accumulation).
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone, timedelta
from enum import IntEnum
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

import websockets
import ledger
import predictor
import report_daily
import fetch_market

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))

LEADERS = ["BTCUSDT", "ETHUSDT"]
FOLLOWERS = ["SOLUSDT", "AVAXUSDT", "SUIUSDT", "NEARUSDT", "DOGEUSDT", "LINKUSDT"]
ALL_SYMBOLS = LEADERS + FOLLOWERS

# Binance Combined WebSocket Stream URI (1s klines for all 8 coins)
WS_STREAMS = "/".join(s.lower() + "@kline_1s" for s in ALL_SYMBOLS)
WS_URI = f"wss://data-stream.binance.vision:9443/stream?streams={WS_STREAMS}"

DB_PATH = PROJECT_ROOT / "solana.db"
DB_CHECK_INTERVAL_SECS = 5.0

# 9Router configuration
HERMES_ENV_PATH = Path.home() / ".hermes" / ".env"
ROUTER_URL = "http://127.0.0.1:20128/v1/chat/completions"
ROUTER_MODEL = "qwen3.8-max"

# FSM Thresholds
SQUEEZE_RANGE_MAX_PCT = 0.22          # BTC 10-15m range <= 0.22%
SQUEEZE_VOL_MAX_RATIO = 1.00          # BTC volume drying up <= 1.0x baseline
ACCUMULATION_MIN_VOL_RATIO = 2.0      # BTC volume surge >= 2.0x baseline
TAKER_RATIO_BULLISH = 0.60            # Taker Buy ratio >= 60%
TAKER_RATIO_BEARISH = 0.40            # Taker Buy ratio <= 40% (Sell >= 60%)
ARMED_VELOCITY_MIN_PCT = 0.08         # BTC move >= 0.08%
ARMED_VELOCITY_MAX_PCT = 0.12         # BTC move <= 0.12%
DISPARITY_IMPULSE_MIN_PCT = 0.12      # Leader breakout impulse >= 0.12% - 0.15%
FOLLOWER_LAG_MAX_PCT = 0.05           # Lagging follower moved < 0.05%
STATE_3_TTL_SECS = 45.0               # ARMED_VELOCITY timeout (45s TTL)
FOLLOWER_COOLDOWN_SECS = 900.0        # 15 minutes cooldown per follower symbol
MAX_HOLDING_SECS = 7200.0             # 2 hours max holding time-stop

# Notification Cooldown / Debounce guards
STATE_1_COOLDOWN_SECS = 900.0         # 15 minutes cooldown between SQUEEZE reports
STATE_2_COOLDOWN_SECS = 900.0         # 15 minutes cooldown between ACCUMULATION reports
STATE_3_DEBOUNCE_SECS = 180.0         # 3 minutes debounce for ARMED reports

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("solana_lead_lag")


class EngineState(IntEnum):
    IDLE = 0
    SQUEEZE_DETECTED = 1
    ACCUMULATION_BUILDUP = 2
    ARMED_VELOCITY = 3
    DISPARITY_EXECUTION = 4


def format_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    elif m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def format_price(px: float, symbol: str = "") -> str:
    if px < 0.1:
        return f"${px:.5f}"
    elif px < 2.0:
        return f"${px:.4f}"
    elif px < 100.0:
        return f"${px:.3f}"
    return f"${px:,.2f}"


def get_latest_base_rate(con) -> float:
    try:
        r = con.execute("SELECT base_rate FROM outcomes ORDER BY prediction_id DESC LIMIT 1").fetchone()
        return float(r[0]) if r and r[0] is not None else 0.50
    except Exception:
        return 0.50


def get_openai_api_key() -> str:
    if not HERMES_ENV_PATH.exists():
        return ""
    try:
        for line in HERMES_ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except Exception:
        pass
    return ""


def get_fallback_commentary(event_name: str) -> str:
    ev = event_name.upper()
    if "SQUEEZE" in ev or "STATE 1" in ev:
        return (
            "Gue ngeliat order book BTC lagi mampet parah, volatilitas 15m drop di bawah 0.22% dan volume kering kerontang. "
            "Ini textbook volatility squeeze. Tahan dulu posisi, jangan nebak arah sebelum ada imbalance flow yang jelas."
        )
    elif "ACCUMULATION" in ev or "STATE 2" in ev:
        return (
            "Order flow BTC mulai nunjukin absorpsi likuiditas yang agresif. Volume melonjak di atas 2x baseline dan "
            "taker pressure berat sebelah nyapu resting limit walls. Aliran order lagi dikumpulkan."
        )
    elif "ARMED" in ev or "STATE 3" in ev:
        return (
            "BTC mulai dapet dorongan akselerasi dan ETH udah konfirmasi validasi arah yang sama. "
            "FSM gue arming sekarang, window transmisi cuma 45 detik sebelum momentum decay."
        )
    elif "DISPARITY" in ev or "STATE 4" in ev:
        return (
            "BTC baru aja sweep order book dengan volume sweep tinggi, tapi follower masih tidur lelap di belakang. "
            "Inefisiensi lead-lag terdeteksi secara kuantitatif, gue eksekusi lagging target-nya sekarang."
        )
    elif "TP" in ev or "TARGET" in ev or "WIN" in ev:
        return (
            "Take profit hit bersih. Transmisi momentum dari BTC ke target follower jalan sesuai kalkulasi statistik. "
            "Edge bekerja dengan presisi, sekarang FSM balik reset buat nangkep transmisi likuiditas berikutnya."
        )
    elif "SL" in ev or "STOP LOSS" in ev or "CUT LOSS" in ev:
        return (
            "Stop loss kena. Likuiditas pembalikan nyerap aliran order lebih cepat dari prediksi. "
            "Cut loss disiplin tanpa tawar-menawar, data tetap tercatat jujur di ledger."
        )
    elif "TIME" in ev:
        return (
            "Time stop habis 2 jam. Volatilitas mereda dan momentum impulsif terhenti. "
            "Flat cut buat bersihin slot sesuai manajemen risiko."
        )
    return "Flow order book terpantau di stream 1s. Metrik pasar tercatat di ledger."


def generate_solana_commentary(event_name: str, context_details: str) -> str:
    key = get_openai_api_key()
    if not key:
        return get_fallback_commentary(event_name)

    system_prompt = (
        "Lo adalah Solana, persona riset kuantitatif crypto di balik Solana Lead-Lag Engine "
        "(menganalisis transmisi likuiditas BTC/ETH ke altcoin high-beta: SOL, AVAX, SUI, NEAR, DOGE, LINK).\n"
        "Karakter lo:\n"
        "- Bahasa Indonesia santai (gue/lo), tajam, blak-blakan, realistis, analitis ala quant researcher.\n"
        "- Sangat paham mikrostruktur pasar: order book compression, flow absorption, taker pressure / imbalance, "
        "limit order walls, lead-lag delay, dan volatility squeeze.\n"
        "- Bukan dukun sinyal atau influencer pomp-pomp crypto. Selalu objektif berbasis fakta angka kuantitatif.\n"
        "- Tulis 1-2 paragraf pendek (maksimal 3-4 kalimat total) yang mengomentari situasi pasar ini secara natural dan autentik."
    )
    user_prompt = f"Event / State FSM: {event_name}\nData pasar & metrik:\n{context_details}\n\nKomentar lo:"

    payload = {
        "model": ROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": 250,
        "temperature": 0.7,
        "stream": False
    }

    try:
        req = urllib.request.Request(
            ROUTER_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json"
            }
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            commentary = data["choices"][0]["message"]["content"].strip()
            if commentary:
                return commentary
    except Exception as e:
        logger.warning(f"9Router LLM commentary generation failed ({e}), using fallback commentary.")

    return get_fallback_commentary(event_name)


def post_persona_discord(event_name: str, details_text: str, structured_metrics: str) -> dict:
    commentary = generate_solana_commentary(event_name, details_text)
    full_message = f"{commentary}\n\n{structured_metrics}".strip()
    res = report_daily.post_discord(full_message)
    logger.info(f"Discord persona alert posted for {event_name}: {res}")
    return res


def load_active_trades_from_db() -> dict[int, dict]:
    con = ledger.connect()
    try:
        rows = con.execute("""
            SELECT p.id, p.symbol, p.leader_symbol, p.horizon, p.direction, p.ref_price, p.created_utc,
                   p.scheduled_slot_utc, p.features_json, p.inputs_json,
                   p.params_json, p.noise_threshold, p.notes
            FROM predictions p
            LEFT JOIN outcomes o ON o.prediction_id = p.id
            WHERE o.prediction_id IS NULL
              AND p.direction IN ('bullish', 'bearish')
            ORDER BY p.id ASC
        """).fetchall()

        active = {}
        for r in rows:
            d = dict(r)
            pid = d["id"]
            ref = float(d["ref_price"])
            direction = d["direction"]
            horizon = d["horizon"] or "lead_lag"
            symbol = d.get("symbol") or "SOLUSDT"
            leader_symbol = d.get("leader_symbol") or "BTCUSDT"

            try:
                inputs = json.loads(d["inputs_json"] or "{}")
            except Exception:
                inputs = {}
            try:
                features = json.loads(d["features_json"] or "{}")
            except Exception:
                features = {}

            tp = inputs.get("target_tp_price") or inputs.get("tp_price")
            sl = inputs.get("stop_loss_price") or inputs.get("sl_price")

            if tp is None or sl is None:
                atr = features.get("atr") or features.get("atr14")
                if not atr:
                    thr = d.get("noise_threshold")
                    atr = (thr / 0.5) if thr else (ref * 0.005)
                levels = predictor.compute_trade_levels(ref, atr, direction, horizon=horizon, tp_mult=1.5, sl_mult=1.0)
                tp = levels["tp_price"]
                sl = levels["sl_price"]
            else:
                tp = float(tp)
                sl = float(sl)

            active[pid] = {
                "id": pid,
                "symbol": symbol,
                "leader_symbol": leader_symbol,
                "horizon": horizon,
                "direction": direction,
                "ref_price": ref,
                "tp_price": tp,
                "sl_price": sl,
                "created_utc": d["created_utc"],
                "high_watermark": ref,
                "low_watermark": ref,
                "noise_threshold": d.get("noise_threshold") or (ref * 0.0025),
            }

        return active
    finally:
        con.close()


def sync_record_outcome(trade: dict, exit_reason: str, trigger_price: float, trigger_time: datetime) -> bool:
    pid = trade["id"]
    symbol = trade["symbol"]
    leader = trade["leader_symbol"]
    direction = trade["direction"]
    horizon = trade["horizon"]
    ref = trade["ref_price"]
    tp = trade["tp_price"]
    sl = trade["sl_price"]

    if exit_reason == "tp_hit":
        exit_price = tp
    elif exit_reason == "sl_hit":
        exit_price = sl
    else:
        exit_price = trigger_price

    move = exit_price - ref
    move_pct = (move / ref * 100) if ref else 0.0

    b_high = max(trade.get("high_watermark", ref), trigger_price, exit_price)
    b_low = min(trade.get("low_watermark", ref), trigger_price, exit_price)

    created_dt = datetime.fromisoformat(trade["created_utc"])
    elapsed_secs = (trigger_time - created_dt).total_seconds()
    duration_str = format_duration(elapsed_secs)

    logger.info(f"Triggering {exit_reason.upper()} for #{pid} ({symbol} {direction}) at {format_price(trigger_price, symbol)} "
                f"(exit: {format_price(exit_price, symbol)}, move: {move_pct:+.2f}%)")

    con = ledger.connect()
    try:
        already = con.execute("SELECT prediction_id FROM outcomes WHERE prediction_id = ?", (pid,)).fetchone()
        if already:
            logger.warning(f"Prediction #{pid} already graded. Skipping duplicate resolution.")
            return False

        base = get_latest_base_rate(con)
        is_noise = False
        correct = 1 if exit_reason == "tp_hit" else (0 if exit_reason == "sl_hit" else None)

        ledger.record_outcome(
            con,
            prediction_id=pid,
            realized_price=round(exit_price, 6),
            move=round(move, 6),
            move_pct=round(move_pct, 4),
            is_noise=is_noise,
            correct=correct,
            base_rate=round(base, 6),
        )

        ledger.record_outcome_details(
            con,
            prediction_id=pid,
            symbol=symbol,
            leader_symbol=leader,
            target_price=round(tp, 6),
            sl_price=round(sl, 6),
            bar_high=round(b_high, 6),
            bar_low=round(b_low, 6),
            target_reached=1 if exit_reason == "tp_hit" else 0,
            sl_hit=1 if exit_reason == "sl_hit" else 0,
        )

        logger.info(f"Successfully recorded outcome for #{pid} ({symbol}) in ledger.")
    except Exception as e:
        logger.error(f"Failed to record outcome for #{pid}: {e}", exc_info=True)
        return False
    finally:
        con.close()

    try:
        wib_time_str = trigger_time.astimezone(WIB).strftime("%H:%M:%S")
        is_win = exit_reason == "tp_hit"
        is_sl = exit_reason == "sl_hit"

        pnl_pts = move if direction == "bullish" else -move
        pnl_pct = move_pct if direction == "bullish" else -move_pct

        if is_win:
            title = f"🎯 **TARGET REACHED (Take Profit)** · **{symbol}** (Leader: `{leader}`)"
            res_line = "✅ **WIN** (Real-time TP trigger)"
            event_name = "RESOLUTION_TP (Take Profit Win)"
            outcome_text = f"{symbol} sukses hit Take Profit di {format_price(exit_price, symbol)} ({pnl_pct:+.2f}%). Momentum tertransmisi bersih."
        elif is_sl:
            title = f"🛑 **STOP LOSS TRIGGERED (Cut Loss)** · **{symbol}** (Leader: `{leader}`)"
            res_line = "❌ **CUT LOSS** (Real-time SL trigger)"
            event_name = "RESOLUTION_SL (Stop Loss Cut)"
            outcome_text = f"{symbol} hit Stop Loss di {format_price(exit_price, symbol)} ({pnl_pct:+.2f}%). Order flow berbalik arah."
        else:
            title = f"⏰ **TIME STOP EXPIRED (Flat Cut)** · **{symbol}** (Leader: `{leader}`)"
            res_line = "⏱️ **FLAT CUT** (Max 2h holding elapsed)"
            event_name = "RESOLUTION_TIMESTOP (Flat Cut)"
            outcome_text = f"{symbol} time stop 2 jam habis di {format_price(exit_price, symbol)} ({pnl_pct:+.2f}%). Volatilitas mereda."

        details = (
            f"Prediction #{pid} ({symbol} {direction.upper()} via {leader}) selesai: {outcome_text} "
            f"Entry: {format_price(ref, symbol)} ➔ Exit: {format_price(exit_price, symbol)} "
            f"({pnl_pts:+.4f} pts · {pnl_pct:+.2f}%). Durasi: {duration_str}."
        )
        metrics = (
            f"{title}\n"
            f"• **Prediction**: `#{pid}` (`{horizon}` {direction.upper()})\n"
            f"• **Entry (Ref)**: `{format_price(ref, symbol)}` → **Exit**: `{format_price(exit_price, symbol)}` "
            f"(`{pnl_pts:+.4f} pts` · `{pnl_pct:+.2f}%`)\n"
            f"• **Trigger Spot**: `{format_price(trigger_price, symbol)}` (TP: `{format_price(tp, symbol)}` · SL: `{format_price(sl, symbol)}`)\n"
            f"• **Result**: {res_line}\n"
            f"• **Duration**: {duration_str} · `{wib_time_str} WIB`"
        )
        post_persona_discord(event_name, details, metrics)
    except Exception as e:
        logger.error(f"Failed to post Discord alert for outcome #{pid}: {e}")

    return True


def sync_record_lead_lag_prediction(
    symbol: str,
    leader: str,
    direction: str,
    ref_price: float,
    leader_move_pct: float,
    leader_vol_ratio: float,
    follower_move_pct: float,
    disparity: float,
    atr_val: float,
) -> dict | None:
    now_utc_dt = datetime.now(UTC)
    now_utc = now_utc_dt.isoformat(timespec="seconds")
    target_bar_utc = (now_utc_dt + timedelta(seconds=MAX_HOLDING_SECS)).isoformat(timespec="seconds")

    if atr_val <= 0:
        atr_val = ref_price * 0.005

    tp_mult = 1.5
    sl_mult = 1.0
    tp_pts = round(tp_mult * atr_val, 6)
    sl_pts = round(sl_mult * atr_val, 6)

    if direction == "bullish":
        tp_price = round(ref_price + tp_pts, 6)
        sl_price = round(ref_price - sl_pts, 6)
    else:
        tp_price = round(ref_price - tp_pts, 6)
        sl_price = round(ref_price + sl_pts, 6)

    noise_thr = round(0.5 * atr_val, 6)
    confidence = round(min(0.95, max(0.55, 0.50 + disparity * 0.20)), 4)
    model_version = "v2.0-fsm-lead-lag"

    params = {
        "model": "fsm_lead_lag_disparity",
        "leader": leader,
        "disparity_pct": round(disparity, 4),
        "tp_mult": tp_mult,
        "sl_mult": sl_mult,
    }
    features = {
        "leader": leader,
        "leader_move_pct": round(leader_move_pct, 4),
        "leader_vol_ratio": round(leader_vol_ratio, 2),
        "follower_move_pct": round(follower_move_pct, 4),
        "disparity_pct": round(disparity, 4),
        "atr": round(atr_val, 6),
    }
    inputs = {
        "ref_price": round(ref_price, 6),
        "target_tp_price": tp_price,
        "stop_loss_price": sl_price,
        "tp_atr_mult": tp_mult,
        "sl_atr_mult": sl_mult,
        "leader": leader,
    }
    notes = (
        f"FSM State 4 Disparity {direction.upper()}: {leader} {leader_move_pct:+.2f}% impulse (vol {leader_vol_ratio:.1f}x) "
        f"➔ {symbol} lag ({follower_move_pct:+.2f}%, disparity {disparity:.2f}%)"
    )

    con = ledger.connect()
    try:
        pid = ledger.record_prediction(
            con,
            horizon="lead_lag",
            target_bar_utc=target_bar_utc,
            scheduled_slot_utc=now_utc,
            direction=direction,
            confidence=confidence,
            ref_price=round(ref_price, 6),
            model_version=model_version,
            params=params,
            features=features,
            inputs=inputs,
            signal_source="lead_lag",
            noise_threshold=noise_thr,
            symbol=symbol,
            leader_symbol=leader,
            notes=notes,
        )
        logger.info(f"Recorded Lead-Lag prediction #{pid} for {symbol} (Leader: {leader}, Dir: {direction})")
    except Exception as e:
        logger.error(f"Failed to record prediction in ledger: {e}", exc_info=True)
        return None
    finally:
        con.close()

    try:
        wib_time = now_utc_dt.astimezone(WIB).strftime("%H:%M:%S")
        dir_emoji = "🐂" if direction == "bullish" else "🐻"
        details = (
            f"BTC baru saja breakout {direction.upper()} {leader_move_pct:+.2f}% dengan volume sweep {leader_vol_ratio:.1f}x. "
            f"Follower {symbol} masih tertinggal dan hanya bergerak {follower_move_pct:+.2f}% (lag < 0.05%), disparity {disparity:.2f}%. "
            f"Eksekusi entry {symbol} di {format_price(ref_price, symbol)}, Target TP {format_price(tp_price, symbol)}, SL {format_price(sl_price, symbol)}."
        )
        metrics = (
            f"⚡ **FSM STATE 4: DISPARITY EXECUTION**\n"
            f"• **Signal**: `{leader}` ➔ **{symbol}**\n"
            f"• **Leader Impulse**: `{leader}` `{leader_move_pct:+.2f}%` (Vol: `{leader_vol_ratio:.1f}x` baseline)\n"
            f"• **Lagging Target**: `{symbol}` (Moved `{follower_move_pct:+.2f}%` · Lag `< 0.05%`)\n"
            f"• **Disparity Score**: `{disparity:.2f}%`\n"
            f"• **Direction**: {dir_emoji} **{direction.upper()}**\n"
            f"• **Entry (Ref)**: `{format_price(ref_price, symbol)}`\n"
            f"• **Target (TP)**: `{format_price(tp_price, symbol)}` (+1.5× ATR) · **Stop Loss (SL)**: `{format_price(sl_price, symbol)}` (-1.0× ATR)\n"
            f"• **Prediction ID**: `#{pid}` · `{wib_time} WIB`"
        )
        post_persona_discord("DISPARITY_EXECUTION (State 4)", details, metrics)
    except Exception as e:
        logger.error(f"Failed to post Discord trigger alert: {e}")

    return {
        "id": pid,
        "symbol": symbol,
        "leader_symbol": leader,
        "horizon": "lead_lag",
        "direction": direction,
        "ref_price": ref_price,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "created_utc": now_utc,
        "high_watermark": ref_price,
        "low_watermark": ref_price,
        "noise_threshold": noise_thr,
    }


class MultiCoinLeadLagWatcher:
    def __init__(self):
        self.active_trades: dict[int, dict] = {}
        self.last_db_mtime: float = 0.0
        self.last_db_check: float = 0.0
        self.running = True

        # FSM State & Memory
        self.state: EngineState = EngineState.IDLE
        self.state_entered_ts: float = time.time()
        self.accumulation_bias: str = "bullish"
        self.accumulation_taker_ratio: float = 0.50
        self.accumulation_vol_ratio: float = 1.0
        self.state_3_armed_ts: float = 0.0
        self.armed_direction: str = "bullish"

        # Debounce / Cooldown notification trackers
        self.last_state_1_notify_ts: float = 0.0
        self.last_state_2_notify_ts: float = 0.0
        self.last_state_3_notify_ts: float = 0.0

        # Rolling 1s candles: 900s for BTC (15m), 300s for ETH (5m), 120s for followers
        self.rolling_1s: dict[str, deque] = {
            "BTCUSDT": deque(maxlen=900),
            "ETHUSDT": deque(maxlen=300),
            **{s: deque(maxlen=120) for s in FOLLOWERS}
        }
        self.last_trigger_ts: dict[str, float] = {s: 0.0 for s in FOLLOWERS}
        self.baselines: dict[str, dict] = {
            s: {"atr": 0.0, "baseline_1s_vol": 10.0} for s in ALL_SYMBOLS
        }

    def check_db_update(self, force: bool = False):
        now = time.monotonic()
        if not force and (now - self.last_db_check) < DB_CHECK_INTERVAL_SECS:
            return

        self.last_db_check = now
        try:
            mtime = DB_PATH.stat().st_mtime
        except FileNotFoundError:
            return

        if force or mtime > self.last_db_mtime:
            self.last_db_mtime = mtime
            fresh = load_active_trades_from_db()
            for pid, trade in fresh.items():
                if pid in self.active_trades:
                    trade["high_watermark"] = self.active_trades[pid].get("high_watermark", trade["ref_price"])
                    trade["low_watermark"] = self.active_trades[pid].get("low_watermark", trade["ref_price"])
            self.active_trades = fresh
            logger.info(f"Active trades updated from DB: {len(self.active_trades)} in progress "
                        f"({[(t['id'], t['symbol'], t['direction']) for t in self.active_trades.values()]})")

    def evaluate_1s_kline(self, trade: dict, k: dict) -> tuple[str | None, float]:
        direction = trade["direction"]
        tp = trade["tp_price"]
        sl = trade["sl_price"]

        k_high = float(k["h"])
        k_low = float(k["l"])
        k_open = float(k["o"])
        k_close = float(k["c"])

        trade["high_watermark"] = max(trade.get("high_watermark", k_high), k_high)
        trade["low_watermark"] = min(trade.get("low_watermark", k_low), k_low)

        if direction == "bullish":
            tp_hit = k_high >= tp
            sl_hit = k_low <= sl
            if tp_hit and sl_hit:
                if k_open <= k_close:
                    return ("tp_hit", tp)
                return ("sl_hit", sl)
            elif tp_hit:
                return ("tp_hit", tp)
            elif sl_hit:
                return ("sl_hit", sl)
        else:
            tp_hit = k_low <= tp
            sl_hit = k_high >= sl
            if tp_hit and sl_hit:
                if k_open >= k_close:
                    return ("tp_hit", tp)
                return ("sl_hit", sl)
            elif tp_hit:
                return ("tp_hit", tp)
            elif sl_hit:
                return ("sl_hit", sl)

        # Time-stop check (2 hours)
        try:
            created_dt = datetime.fromisoformat(trade["created_utc"])
            if (datetime.now(UTC) - created_dt).total_seconds() >= MAX_HOLDING_SECS:
                return ("time_stop", k_close)
        except Exception:
            pass

        return (None, k_close)

    def transition_to_squeeze(self, range_pct: float, vol_ratio: float, btc_spot: float):
        self.state = EngineState.SQUEEZE_DETECTED
        self.state_entered_ts = time.time()
        logger.info(f"FSM State 1 (SQUEEZE_DETECTED): BTC 15m range={range_pct:.3f}%, vol={vol_ratio:.2f}x")

        now = time.time()
        if (now - self.last_state_1_notify_ts) >= STATE_1_COOLDOWN_SECS:
            self.last_state_1_notify_ts = now
            wib_str = datetime.now(UTC).astimezone(WIB).strftime("%H:%M:%S")
            details = (
                f"BTC 15m range kejepit di {range_pct:.3f}% (threshold <= {SQUEEZE_RANGE_MAX_PCT}%), "
                f"volume mengering ke {vol_ratio:.2f}x baseline. Spot: {format_price(btc_spot, 'BTCUSDT')}."
            )
            metrics = (
                f"📦 **FSM STATE 1: SQUEEZE DETECTED**\n"
                f"• **BTC 15m Range**: `{range_pct:.3f}%` (Threshold: `≤ {SQUEEZE_RANGE_MAX_PCT}%`)\n"
                f"• **Volume Ratio**: `{vol_ratio:.2f}x` baseline (Volume drying up)\n"
                f"• **BTC Spot**: `{format_price(btc_spot, 'BTCUSDT')}`\n"
                f"• **Timestamp**: `{wib_str} WIB`"
            )
            asyncio.create_task(asyncio.to_thread(post_persona_discord, "SQUEEZE_DETECTED (State 1)", details, metrics))

    def transition_to_accumulation(self, bias: str, taker_ratio: float, vol_ratio: float, btc_spot: float):
        self.state = EngineState.ACCUMULATION_BUILDUP
        self.state_entered_ts = time.time()
        self.accumulation_bias = bias
        self.accumulation_taker_ratio = taker_ratio
        self.accumulation_vol_ratio = vol_ratio
        logger.info(f"FSM State 2 (ACCUMULATION_BUILDUP): Bias={bias.upper()}, Taker={taker_ratio*100:.1f}%, Vol={vol_ratio:.2f}x")

        now = time.time()
        if (now - self.last_state_2_notify_ts) >= STATE_2_COOLDOWN_SECS:
            self.last_state_2_notify_ts = now
            wib_str = datetime.now(UTC).astimezone(WIB).strftime("%H:%M:%S")
            dir_emoji = "🐂" if bias == "bullish" else "🐻"
            details = (
                f"BTC akumulasi {bias.upper()}: volume lonjak {vol_ratio:.2f}x baseline (threshold >= {ACCUMULATION_MIN_VOL_RATIO}x), "
                f"Taker Buy/Sell ratio {taker_ratio*100:.1f}%. Spot: {format_price(btc_spot, 'BTCUSDT')}."
            )
            metrics = (
                f"🔋 **FSM STATE 2: ACCUMULATION BUILDUP**\n"
                f"• **Bias**: **{bias.upper()}** {dir_emoji}\n"
                f"• **Taker Ratio**: `{taker_ratio * 100:.1f}%` (Threshold: `≥ 60%` buy or `≤ 40%` sell)\n"
                f"• **Volume Surge**: `{vol_ratio:.2f}x` baseline (Threshold: `≥ 2.0x`)\n"
                f"• **BTC Spot**: `{format_price(btc_spot, 'BTCUSDT')}`\n"
                f"• **Timestamp**: `{wib_str} WIB`"
            )
            asyncio.create_task(asyncio.to_thread(post_persona_discord, "ACCUMULATION_BUILDUP (State 2)", details, metrics))

    def transition_to_armed_velocity(self, direction: str, btc_move: float, eth_move: float):
        self.state = EngineState.ARMED_VELOCITY
        self.state_entered_ts = time.time()
        self.state_3_armed_ts = time.time()
        self.armed_direction = direction
        logger.info(f"FSM State 3 (ARMED_VELOCITY): Dir={direction.upper()}, BTC move={btc_move:+.3f}%, ETH confirm={eth_move:+.3f}%, TTL=45s")

        now = time.time()
        if (now - self.last_state_3_notify_ts) >= STATE_3_DEBOUNCE_SECS:
            self.last_state_3_notify_ts = now
            wib_str = datetime.now(UTC).astimezone(WIB).strftime("%H:%M:%S")
            dir_emoji = "🐂" if direction == "bullish" else "🐻"
            details = (
                f"BTC velocity impulse {btc_move:+.3f}% dalam 20s (threshold {ARMED_VELOCITY_MIN_PCT}% - {ARMED_VELOCITY_MAX_PCT}%), "
                f"ETH konfirmasi {eth_move:+.3f}%. Direction: {direction.upper()}. Armed TTL: 45 detik."
            )
            metrics = (
                f"🚨 **FSM STATE 3: ARMED VELOCITY**\n"
                f"• **BTC Velocity**: `{btc_move:+.3f}%` in 20s (Threshold: `0.08% - 0.12%`)\n"
                f"• **ETH Confirmation**: `{eth_move:+.3f}%` (Aligned {direction.upper()})\n"
                f"• **Direction**: **{direction.upper()}** {dir_emoji}\n"
                f"• **TTL**: `45s` (Window transmisi likuiditas)\n"
                f"• **Timestamp**: `{wib_str} WIB`"
            )
            asyncio.create_task(asyncio.to_thread(post_persona_discord, "ARMED_VELOCITY (State 3)", details, metrics))

    def find_best_lagging_follower(self, direction: str, abs_btc_move: float, btc_move_pct: float, vol_ratio: float) -> dict | None:
        now_ts = time.time()
        candidates = []
        for follower in FOLLOWERS:
            # Slot guard: max 1 active trade per follower symbol
            if any(t.get("symbol") == follower for t in self.active_trades.values()):
                continue

            # Cooldown guard: 15 minutes per follower symbol
            if (now_ts - self.last_trigger_ts.get(follower, 0.0)) < FOLLOWER_COOLDOWN_SECS:
                continue

            f_hist = list(self.rolling_1s[follower])
            if len(f_hist) < 30:
                continue

            f_30 = f_hist[-30:]
            f_start = f_30[0]["open"]
            f_now = f_30[-1]["close"]
            if f_start <= 0:
                continue

            follower_move_pct = (f_now - f_start) / f_start * 100.0

            # Follower move in direction of impulse
            realized_dir_move = follower_move_pct if direction == "bullish" else -follower_move_pct

            # Follower lag check: follower moved < 0.05%
            if realized_dir_move >= FOLLOWER_LAG_MAX_PCT:
                continue

            # Disparity: impulse minus realized move
            disparity = abs_btc_move - max(0.0, realized_dir_move)
            if disparity < 0.07:
                continue

            atr = self.baselines[follower].get("atr") or (f_now * 0.005)
            candidates.append({
                "follower": follower,
                "leader": "BTCUSDT",
                "direction": direction,
                "ref_price": f_now,
                "leader_move_pct": btc_move_pct,
                "leader_vol_ratio": vol_ratio,
                "follower_move_pct": follower_move_pct,
                "disparity": disparity,
                "atr": atr,
            })

        if not candidates:
            return None

        candidates.sort(key=lambda c: c["disparity"], reverse=True)
        return candidates[0]

    def execute_state_4(self, opp: dict):
        self.state = EngineState.DISPARITY_EXECUTION
        self.state_entered_ts = time.time()
        follower = opp["follower"]
        self.last_trigger_ts[follower] = time.time()
        logger.info(
            f"⚡ FSM State 4 (DISPARITY_EXECUTION): {opp['leader']} {opp['direction']} {opp['leader_move_pct']:+.2f}% "
            f"➔ {follower} lag (disparity: {opp['disparity']:.2f}%)"
        )
        trade = sync_record_lead_lag_prediction(
            opp["follower"],
            opp["leader"],
            opp["direction"],
            opp["ref_price"],
            opp["leader_move_pct"],
            opp["leader_vol_ratio"],
            opp["follower_move_pct"],
            opp["disparity"],
            opp["atr"],
        )
        if trade:
            self.active_trades[trade["id"]] = trade

        # Reset FSM back to IDLE after execution
        self.state = EngineState.IDLE
        self.state_entered_ts = time.time()

    def evaluate_fsm(self):
        """Core 5-State Finite State Machine Engine."""
        now_ts = time.time()

        # 1. State 3 TTL Guard (45 seconds)
        if self.state == EngineState.ARMED_VELOCITY:
            if (now_ts - self.state_3_armed_ts) > STATE_3_TTL_SECS:
                logger.info(f"ARMED_VELOCITY TTL expired ({STATE_3_TTL_SECS}s). Reverting to IDLE.")
                self.state = EngineState.IDLE
                self.state_entered_ts = now_ts

        # 2. State 2 Timeout Guard (5 minutes without breakout)
        if self.state == EngineState.ACCUMULATION_BUILDUP:
            if (now_ts - self.state_entered_ts) > 300.0:
                logger.info("ACCUMULATION_BUILDUP window expired without breakout. Reverting to IDLE.")
                self.state = EngineState.IDLE
                self.state_entered_ts = now_ts

        btc_hist = list(self.rolling_1s["BTCUSDT"])
        if len(btc_hist) < 30:
            return

        # 30-second window metrics for BTC
        w30 = btc_hist[-30:]
        btc_start_30 = w30[0]["open"]
        btc_now = w30[-1]["close"]
        if btc_start_30 <= 0:
            return

        btc_30s_move_pct = (btc_now - btc_start_30) / btc_start_30 * 100.0
        abs_btc_30s = abs(btc_30s_move_pct)
        vol_30 = sum(x["vol"] for x in w30)
        taker_buy_30 = sum(x.get("taker_buy_vol", 0.0) for x in w30)
        baseline_1s_btc = self.baselines["BTCUSDT"].get("baseline_1s_vol", 0.15)
        baseline_30_btc = max(1e-6, baseline_1s_btc * 30.0)
        vol_ratio_30 = vol_30 / baseline_30_btc
        taker_ratio_30 = (taker_buy_30 / vol_30) if vol_30 > 0 else 0.50

        # 20-second velocity window for BTC & ETH
        w20_btc = btc_hist[-20:] if len(btc_hist) >= 20 else w30[-20:]
        btc_20s_move_pct = (w20_btc[-1]["close"] - w20_btc[0]["open"]) / w20_btc[0]["open"] * 100.0
        abs_btc_20s = abs(btc_20s_move_pct)

        eth_hist = list(self.rolling_1s["ETHUSDT"])
        eth_20s_move_pct = 0.0
        if len(eth_hist) >= 20:
            w20_eth = eth_hist[-20:]
            if w20_eth[0]["open"] > 0:
                eth_20s_move_pct = (w20_eth[-1]["close"] - w20_eth[0]["open"]) / w20_eth[0]["open"] * 100.0

        # 10-15 minute Squeeze metrics on BTC (min 300 bars)
        highs = [x["high"] for x in btc_hist]
        lows = [x["low"] for x in btc_hist]
        min_l = min(lows)
        max_h = max(highs)
        range_15m_pct = (max_h - min_l) / min_l * 100.0 if min_l > 0 else 1.0
        vol_15m = sum(x["vol"] for x in btc_hist)
        baseline_15m = max(1e-6, baseline_1s_btc * len(btc_hist))
        vol_ratio_15m = vol_15m / baseline_15m
        is_squeeze = (range_15m_pct <= SQUEEZE_RANGE_MAX_PCT) and (vol_ratio_15m <= SQUEEZE_VOL_MAX_RATIO)

        # ====================================================================
        # FSM State Transitions
        # ====================================================================

        # Path to State 4: DISPARITY_EXECUTION
        # (Can fire from ARMED_VELOCITY or directly when strong impulse sweep occurs)
        if self.state in (EngineState.ARMED_VELOCITY, EngineState.ACCUMULATION_BUILDUP):
            direction = "bullish" if btc_30s_move_pct > 0 else "bearish"
            if abs_btc_30s >= DISPARITY_IMPULSE_MIN_PCT and vol_ratio_30 >= ACCUMULATION_MIN_VOL_RATIO:
                candidate = self.find_best_lagging_follower(direction, abs_btc_30s, btc_30s_move_pct, vol_ratio_30)
                if candidate:
                    self.execute_state_4(candidate)
                    return

        # Path to State 3: ARMED_VELOCITY
        if self.state == EngineState.ACCUMULATION_BUILDUP:
            bias = self.accumulation_bias
            if bias == "bullish":
                btc_armed = (ARMED_VELOCITY_MIN_PCT <= btc_20s_move_pct <= 0.16) or (ARMED_VELOCITY_MIN_PCT <= btc_30s_move_pct <= 0.16)
                eth_confirmed = eth_20s_move_pct > 0.0
            else:
                btc_armed = (-0.16 <= btc_20s_move_pct <= -ARMED_VELOCITY_MIN_PCT) or (-0.16 <= btc_30s_move_pct <= -ARMED_VELOCITY_MIN_PCT)
                eth_confirmed = eth_20s_move_pct < 0.0

            if btc_armed and eth_confirmed:
                self.transition_to_armed_velocity(bias, btc_20s_move_pct, eth_20s_move_pct)
                return

        # Path to State 2: ACCUMULATION_BUILDUP
        if self.state in (EngineState.IDLE, EngineState.SQUEEZE_DETECTED):
            if vol_ratio_30 >= ACCUMULATION_MIN_VOL_RATIO:
                if taker_ratio_30 >= TAKER_RATIO_BULLISH:
                    self.transition_to_accumulation("bullish", taker_ratio_30, vol_ratio_30, btc_now)
                    return
                elif taker_ratio_30 <= TAKER_RATIO_BEARISH:
                    self.transition_to_accumulation("bearish", taker_ratio_30, vol_ratio_30, btc_now)
                    return

        # Path to State 1: SQUEEZE_DETECTED
        if self.state == EngineState.IDLE:
            if is_squeeze and len(btc_hist) >= 300:
                self.transition_to_squeeze(range_15m_pct, vol_ratio_15m, btc_now)
                return

        # Squeeze Revert guard (Range expansion without accumulation)
        if self.state == EngineState.SQUEEZE_DETECTED:
            if range_15m_pct > 0.35:
                logger.info(f"Squeeze range expanded ({range_15m_pct:.2f}% > 0.35%). Reverting to IDLE.")
                self.state = EngineState.IDLE
                self.state_entered_ts = now_ts

    async def update_baseline_periodically(self):
        """Fetch 1h ATR14 & 1h average volume baseline for all 8 coins every 15m."""
        while self.running:
            for symbol in ALL_SYMBOLS:
                try:
                    candles = await asyncio.to_thread(fetch_market.fetch_candles, "1h", 100, symbol)
                    if candles and len(candles) >= 20:
                        highs = [c["high"] for c in candles]
                        lows = [c["low"] for c in candles]
                        closes = [c["close"] for c in candles]
                        atr_val = predictor.atr(highs, lows, closes, period=14)
                        if atr_val is None or atr_val <= 0:
                            atr_val = closes[-1] * 0.005

                        avg_1h_vol = sum(c["volume"] for c in candles[-24:]) / min(24, len(candles))
                        baseline_1s = max(1e-6, avg_1h_vol / 3600.0)

                        self.baselines[symbol] = {
                            "atr": atr_val,
                            "baseline_1s_vol": baseline_1s,
                        }
                        logger.info(f"Baseline updated for {symbol:<8}: ATR14={format_price(atr_val, symbol)} | Vol={baseline_1s:.2f}/s")
                except Exception as e:
                    logger.warning(f"Failed to update baseline for {symbol}: {e}")
                await asyncio.sleep(1.0)
            await asyncio.sleep(900)

    async def seed_startup_history(self):
        """Seed initial 15m history for BTC & ETH so FSM doesn't have a 15-minute warmup lag."""
        try:
            logger.info("Seeding initial 15m kline history for BTCUSDT and ETHUSDT FSM warmup...")
            btc_raw = await asyncio.to_thread(fetch_market.fetch_candles, "1m", 60, "BTCUSDT")
            btc_candles = btc_raw[-15:] if btc_raw else []
            if btc_candles:
                for c in btc_candles:
                    t_ms = int(c["open_time"])
                    vol_sample = c["volume"] / 12.0
                    for offset_s in range(0, 60, 5):
                        self.rolling_1s["BTCUSDT"].append({
                            "open": c["open"],
                            "high": c["high"],
                            "low": c["low"],
                            "close": c["close"],
                            "vol": vol_sample,
                            "taker_buy_vol": vol_sample * 0.5,
                            "ts": t_ms + offset_s * 1000,
                        })
                logger.info(f"Seeded {len(self.rolling_1s['BTCUSDT'])} history bars for BTCUSDT.")

            eth_raw = await asyncio.to_thread(fetch_market.fetch_candles, "1m", 60, "ETHUSDT")
            eth_candles = eth_raw[-5:] if eth_raw else []
            if eth_candles:
                for c in eth_candles:
                    t_ms = int(c["open_time"])
                    vol_sample = c["volume"] / 12.0
                    for offset_s in range(0, 60, 5):
                        self.rolling_1s["ETHUSDT"].append({
                            "open": c["open"],
                            "high": c["high"],
                            "low": c["low"],
                            "close": c["close"],
                            "vol": vol_sample,
                            "taker_buy_vol": vol_sample * 0.5,
                            "ts": t_ms + offset_s * 1000,
                        })
                logger.info(f"Seeded {len(self.rolling_1s['ETHUSDT'])} history bars for ETHUSDT.")
        except Exception as e:
            logger.warning(f"Startup history seeding failed (non-critical, rolling buffer will fill live): {e}")

    async def reconcile_startup(self):
        try:
            now = datetime.now(UTC)
            to_remove = []
            for pid, trade in list(self.active_trades.items()):
                symbol = trade.get("symbol", "SOLUSDT")
                spot_data = await asyncio.to_thread(fetch_market.fetch_spot, symbol)
                current_spot = spot_data["price"]
                direction = trade["direction"]
                tp = trade["tp_price"]
                sl = trade["sl_price"]
                exit_reason = None

                if direction == "bullish":
                    if current_spot >= tp:
                        exit_reason = "tp_hit"
                    elif current_spot <= sl:
                        exit_reason = "sl_hit"
                else:
                    if current_spot <= tp:
                        exit_reason = "tp_hit"
                    elif current_spot >= sl:
                        exit_reason = "sl_hit"

                if exit_reason:
                    logger.info(f"Reconciling #{pid} ({symbol} {exit_reason}) at {format_price(current_spot, symbol)}")
                    ok = await asyncio.to_thread(sync_record_outcome, trade, exit_reason, current_spot, now)
                    if ok:
                        to_remove.append(pid)

            for pid in to_remove:
                self.active_trades.pop(pid, None)
        except Exception as e:
            logger.error(f"Error during reconcile startup: {e}")

    async def run_stream(self):
        while self.running:
            try:
                logger.info(f"Connecting to Binance Combined WebSocket Stream: {WS_URI}")
                async with websockets.connect(WS_URI, ping_interval=20, ping_timeout=15) as ws:
                    logger.info(f"Connected to stream. Monitoring 8 coins: Leaders={LEADERS}, Followers={FOLLOWERS}")
                    async for raw_msg in ws:
                        if not self.running:
                            break

                        self.check_db_update()
                        msg = json.loads(raw_msg)
                        data = msg.get("data")
                        if not data:
                            continue

                        k = data.get("k")
                        symbol = data.get("s")
                        if not k or not symbol or symbol not in self.rolling_1s:
                            continue

                        # 1. Store in rolling window
                        self.rolling_1s[symbol].append({
                            "open": float(k["o"]),
                            "high": float(k["h"]),
                            "low": float(k["l"]),
                            "close": float(k["c"]),
                            "vol": float(k["v"]),
                            "taker_buy_vol": float(k.get("V", 0.0)),
                            "ts": k["t"],
                        })

                        # 2. If Follower kline -> check active trades for barrier touch (TP/SL)
                        if self.active_trades and symbol in FOLLOWERS:
                            concluded = []
                            now = datetime.now(UTC)
                            for pid, trade in list(self.active_trades.items()):
                                if trade.get("symbol") == symbol:
                                    exit_reason, trigger_px = self.evaluate_1s_kline(trade, k)
                                    if exit_reason:
                                        concluded.append((trade, exit_reason, trigger_px))

                            for trade, exit_reason, trigger_px in concluded:
                                pid = trade["id"]
                                self.active_trades.pop(pid, None)
                                asyncio.create_task(
                                    asyncio.to_thread(sync_record_outcome, trade, exit_reason, trigger_px, now)
                                )

                        # 3. If Leader kline (BTCUSDT) -> evaluate 5-State FSM transitions
                        if symbol == "BTCUSDT":
                            self.evaluate_fsm()

            except Exception as e:
                logger.error(f"WebSocket error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5.0)

    def stop(self):
        self.running = False


async def main():
    watcher = MultiCoinLeadLagWatcher()
    watcher.check_db_update(force=True)
    await watcher.reconcile_startup()
    await watcher.seed_startup_history()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, watcher.stop)
        except NotImplementedError:
            pass

    await asyncio.gather(
        watcher.run_stream(),
        watcher.update_baseline_periodically(),
    )


if __name__ == "__main__":
    asyncio.run(main())
