#!/usr/bin/env python3
"""Discord Persona Commentary & Notification Formatter for Solana Lead-Lag Watcher."""
from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
HERMES_ENV_PATH = Path.home() / ".hermes" / ".env"
ROUTER_URL = "http://127.0.0.1:20128/v1/chat/completions"
ROUTER_MODEL = "glm-4.7"

logger = logging.getLogger("solana_lead_lag_notifications")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] notifications: %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

import report_daily  # noqa: E402


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
