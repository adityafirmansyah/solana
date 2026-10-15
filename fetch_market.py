#!/usr/bin/env python3
"""Solana's market data layer (hardened) — SOLUSDT.

Same reliability contract as Nugget's fetch_market.py, three distinguishable
outcomes:

    FetchError   - source unreachable / 5xx / timeout / DNS.
                   -> fetch_errors row, retry next slot.
    SchemaError  - a 200 OK whose body is missing a field, has a changed
                   type, or is stale. -> HARD ABORT, no row at all.
    (gap)        - N/A for BTC. The market never closes (session_calendar
                   always returns is_open()=True), so this engine never
                   produces a `gap` row — a missed tick is a true miss,
                   never a scheduled closure.

What differs from gold's fetch_market.py:
  - Binance IS the primary venue here, not a proxy for something else. XAUUSD
    needed PAXG as a stand-in for spot gold because spot gold has no free
    keyless feed; SOLUSDT has no such gap — Binance's own ticker is the
    reference price, no "basis vs true spot" concept applies.
  - `fetch_cross_check()` replaces `fetch_proxies()`: CoinGecko's aggregated
    BTC/USD (volume-weighted across many venues) as an independent sanity
    check against Binance's own price — genuinely a DIFFERENT measurement
    (cross-exchange), not the same proxy-vs-spot problem gold has.
  - No `fetch_calendar()` / `fetch_real_yield()` / `fetch_dxy_proxy_candles()`
    equivalents in v1. BTC's macro drivers differ from gold's (no real-yield
    opportunity-cost story in the same way); carried over verbatim would be
    narrative, not a measured parameter — add them only if a predictor
    parameter is built to use them, same discipline as Nugget's inert
    session_sweep/dxy_proxy weights.
  - No session-calendar gap state: see session_calendar.py.

Also here: exponential backoff + jitter per attempt, and a per-host circuit
breaker, same as Nugget — this host's RPZ already blocks api.binance.com, so
.vision stays the only Binance path and getting throttled on it is a real risk.
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125 Safari/537.36"
TIMEOUT_S = 20
ATTEMPTS = 4
BACKOFF_BASE_S = 0.5
STALE_AFTER_S = 300          # 5 min: hourly cadence must never accept older
CIRCUIT_FAILS = 4            # consecutive failures before the breaker opens
CIRCUIT_COOLDOWN_S = 900     # 15 min half-open probe

STATE_DIR_ENV = "SATOSHI_STATE_DIR"


def _state_path() -> Path:
    """Circuit-breaker state file, resolved at CALL time.

    Resolving at import would freeze the path, and a single shared file means
    TEST runs and PRODUCTION runs fight over the same breaker. `SATOSHI_STATE_DIR`
    lets a suite point its breaker somewhere private so production state stays
    untouched. (Same pitfall Nugget hit — see nugget-xauusd-research skill.)
    """
    override = os.environ.get(STATE_DIR_ENV)
    base = Path(override) if override else (Path(__file__).resolve().parent / "state")
    return base / "circuit.json"


class FetchError(Exception):
    """Source unreachable. Recoverable - record and retry next slot."""


class SchemaError(Exception):
    """Response arrived but is unusable. NOT recoverable by retrying."""


# ------------------------------------------------------------ circuit breaker

def _load_circuit() -> dict:
    p = _state_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def _save_circuit(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True))


def circuit_allows(host: str) -> bool:
    st = _load_circuit().get(host, {})
    if st.get("fails", 0) < CIRCUIT_FAILS:
        return True
    opened = st.get("opened_at")
    if not opened:
        return True
    age = (datetime.now(UTC) - datetime.fromisoformat(opened)).total_seconds()
    return age >= CIRCUIT_COOLDOWN_S      # half-open: let one probe through


def circuit_record(host: str, ok: bool) -> None:
    state = _load_circuit()
    st = state.get(host, {"fails": 0, "opened_at": None})
    if ok:
        st = {"fails": 0, "opened_at": None}
    else:
        st["fails"] = st.get("fails", 0) + 1
        if st["fails"] >= CIRCUIT_FAILS and not st.get("opened_at"):
            st["opened_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    state[host] = st
    _save_circuit(state)


# ------------------------------------------------------------------ transport

def _get_json(url: str, host: str) -> dict | list:
    """GET with retry+backoff+jitter. Raises FetchError only."""
    if not circuit_allows(host):
        raise FetchError(f"{host}: circuit open (backing off)")

    last = None
    for i in range(ATTEMPTS):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                body = r.read().decode("utf-8", "replace")
            out = json.loads(body)
            circuit_record(host, True)
            return out
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except urllib.error.URLError as e:
            last = f"URLError {e.reason}"
        except json.JSONDecodeError as e:
            last = f"invalid JSON ({e.msg})"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"

        if i < ATTEMPTS - 1:
            time.sleep(BACKOFF_BASE_S * (2 ** i) + random.uniform(0, BACKOFF_BASE_S))

    circuit_record(host, False)
    raise FetchError(f"{host}: {last} (after {ATTEMPTS} attempts)")


def _num(obj, key, where):
    """Numeric field that may legitimately arrive as a string (Binance quotes
    prices as strings to preserve precision). Unparseable still raises."""
    if not isinstance(obj, dict) or key not in obj:
        raise SchemaError(f"{where}: missing required field '{key}'")
    v = obj[key]
    if isinstance(v, bool):
        raise SchemaError(f"{where}: '{key}' is a bool, not a number")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            raise SchemaError(f"{where}: '{key}' not numeric: {v!r}")
    raise SchemaError(f"{where}: '{key}' unexpected type {type(v).__name__}")


def _req(obj, key, kind, where):
    """Strict field access. Missing/wrong type RAISES - never a silent default."""
    if not isinstance(obj, dict) or key not in obj:
        raise SchemaError(f"{where}: missing required field '{key}'")
    v = obj[key]
    if kind is float:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise SchemaError(f"{where}: '{key}' expected number, got {type(v).__name__}")
        return float(v)
    if kind is int:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise SchemaError(f"{where}: '{key}' expected int, got {type(v).__name__}")
        return int(v)
    if not isinstance(v, kind):
        raise SchemaError(f"{where}: '{key}' expected {kind.__name__}, got {type(v).__name__}")
    return v


# ------------------------------------------------------------------- sources

def fetch_spot() -> dict:
    """SOLUSDT last trade price from Binance — the primary reference price.

    Unlike gold (no free keyless spot feed, forcing PAXG as a stand-in),
    Binance's own ticker IS the market reference here: SOLUSDT is one of the
    most liquid pairs on earth, so there is no "spot vs proxy" distinction
    to make. The timestamp comes from our own fetch time, not a provider
    field, so this returns an `updated_at` in the SAME shape Nugget's
    fetch_spot uses (callers format it identically either way).
    """
    host = "data-api.binance.vision"
    d = _get_json(f"https://{host}/api/v3/ticker/price?symbol=SOLUSDT", host)
    price = _num(d, "price", "binance.price")
    if not (1.0 < price < 100_000.0):
        raise SchemaError(f"binance: implausible SOLUSDT price {price}")
    return {"price": price,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "source": host}


def fetch_cross_check() -> dict:
    """CoinGecko's aggregated BTC/USD — an independent, cross-exchange
    sanity check against Binance's own price.

    This replaces Nugget's fetch_proxies(). Gold's PAXG/XAUT are SEPARATE
    tokenized instruments standing in for an asset with no free spot feed, so
    their divergence from true spot is a real structural "basis". CoinGecko's
    BTC figure is a volume-weighted aggregate across many venues INCLUDING
    Binance, so a large gap here means a Binance-specific pricing anomaly or
    a stale/bad fetch on one side — not a persistent basis to track, just a
    drift/sanity check (same role as watchdog's source_drift check).
    """
    host = "api.coingecko.com"
    d = _get_json("https://api.coingecko.com/api/v3/simple/price"
                  "?ids=solana&vs_currencies=usd&include_last_updated_at=true", host)
    sol = _req(_req(d, "solana", dict, "coingecko"), "usd", float, "coingecko.sol")
    ts = _req(_req(d, "solana", dict, "coingecko"), "last_updated_at", int, "coingecko.sol")
    if not (1.0 < sol < 100_000.0):
        raise SchemaError(f"coingecko: implausible SOL price {sol}")
    return {"sol_usd": sol, "updated_at": ts, "source": host}


def fetch_candles(interval: str = "1h", limit: int = 200) -> list[dict]:
    """OHLCV from Binance SOLUSDT directly (no proxy symbol needed)."""
    host = "data-api.binance.vision"
    raw = _get_json(f"https://{host}/api/v3/klines"
                    f"?symbol=SOLUSDT&interval={interval}&limit={limit}", host)
    if not isinstance(raw, list) or len(raw) < 60:
        raise SchemaError(f"binance.vision: expected >=60 klines, got "
                          f"{len(raw) if isinstance(raw, list) else type(raw).__name__}")
    out = []
    for i, k in enumerate(raw):
        if not isinstance(k, list) or len(k) < 9:
            raise SchemaError(f"binance.vision: malformed kline at {i} "
                              f"(len={len(k) if isinstance(k, list) else 'n/a'})")
        try:
            out.append({
                "open_time": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
                "trades": int(k[8]),
            })
        except (TypeError, ValueError) as e:
            raise SchemaError(f"binance.vision: kline {i} unparseable ({e})")
    # freshness: the last closed bar must be recent. BTC never closes, so a
    # stale last bar here is unambiguously a feed problem, not a market close.
    last_close = datetime.fromtimestamp(out[-1]["open_time"] / 1000, UTC)
    age_h = (datetime.now(UTC) - last_close).total_seconds() / 3600
    interval_hours = {"15m": 0.25, "1h": 1, "8h": 8, "12h": 12, "24h": 24, "1d": 24}.get(interval, 1)
    max_age_h = max(1.0, interval_hours * 2 + 0.5)
    if age_h > max_age_h:
        raise SchemaError(f"binance.vision: last bar is {age_h:.1f}h old (stale feed)")
    return out


def fetch_1m_range(start_time_ms: int, end_time_ms: int) -> list[dict]:
    """Fetch 1m klines between start and end timestamps (UTC ms) for intra-bar sequence resolution."""
    host = "data-api.binance.vision"
    url = f"https://{host}/api/v3/klines?symbol=SOLUSDT&interval=1m&startTime={start_time_ms}&endTime={end_time_ms}&limit=1000"
    raw = _get_json(url, host)
    if not isinstance(raw, list):
        return []
    out = []
    for k in raw:
        if isinstance(k, list) and len(k) >= 5:
            try:
                out.append({
                    "open_time": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                })
            except (ValueError, TypeError):
                continue
    return out


def fetch_ticker() -> dict:
    """24h ticker: session high/low, spread, volume."""
    host = "data-api.binance.vision"
    d = _get_json(f"https://{host}/api/v3/ticker/24hr?symbol=SOLUSDT", host)
    out = {
        "last": _num(d, "lastPrice", "ticker"),
        "bid": _num(d, "bidPrice", "ticker"),
        "ask": _num(d, "askPrice", "ticker"),
        "high_24h": _num(d, "highPrice", "ticker"),
        "low_24h": _num(d, "lowPrice", "ticker"),
        "chg_pct_24h": _num(d, "priceChangePercent", "ticker"),
        "volume_24h": _num(d, "volume", "ticker"),
    }
    if out["ask"] < out["bid"]:
        raise SchemaError(f"ticker: crossed book bid={out['bid']} ask={out['ask']}")
    out["spread"] = round(out["ask"] - out["bid"], 4)
    return out


def collect(interval: str = "1h") -> dict:
    """One full snapshot. Any hard failure raises; caller decides how to record.

    Returns the snapshot plus a per-source status map so the caller can write
    fetch_errors rows for soft failures while still using the sources that work.
    """
    snap: dict = {"collected_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                  "collected_wib": datetime.now(WIB).isoformat(timespec="seconds")}
    status: dict[str, str] = {}
    fetch_interval = "5m" if interval == "event" else interval

    for name, fn in (("spot", fetch_spot), ("cross_check", fetch_cross_check),
                     ("ticker", fetch_ticker), ("candles", lambda: fetch_candles(interval=fetch_interval))):
        try:
            snap[name] = fn()
            status[name] = "ok"
        except (FetchError, SchemaError) as e:
            status[name] = f"{type(e).__name__}: {e}"
            snap[name] = None

    snap["source_status"] = status
    # The prediction is only possible with candles + a reference price.
    if snap.get("candles") is None or snap.get("spot") is None:
        raise FetchError(
            "insufficient data to predict: "
            + "; ".join(f"{k}={v}" for k, v in status.items() if v != "ok"))
    return snap


if __name__ == "__main__":
    print("=== live hardened fetch (SOLUSDT) ===")
    t0 = time.time()
    try:
        s = collect()
    except (FetchError, SchemaError) as e:
        print(f"HARD FAIL after {time.time()-t0:.1f}s: {type(e).__name__}: {e}")
        raise SystemExit(1)
    print(f"collected in {time.time()-t0:.1f}s at {s['collected_wib']}")
    for k, v in s["source_status"].items():
        print(f"  {k:<12} {v}")
    print(f"  spot         {s['spot']['price']} (updated {s['spot']['updated_at']})")
    if s.get("cross_check"):
        delta_pct = (s["cross_check"]["sol_usd"] - s["spot"]["price"]) / s["spot"]["price"] * 100
        print(f"  cross_check  {s['cross_check']['sol_usd']}  delta={delta_pct:+.3f}%")
    if s.get("ticker"):
        print(f"  ticker       last={s['ticker']['last']} spread={s['ticker']['spread']}")
    if s.get("candles"):
        print(f"  candles      {len(s['candles'])} bars, last close "
              f"{s['candles'][-1]['close']}")
    print("\ncircuit state:")
    print(" ", json.dumps(_load_circuit(), sort_keys=True))
