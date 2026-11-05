#!/usr/bin/env python3
"""Top-Down Multi-Timeframe Trading Engine (1D -> 4H -> 1H -> 15m -> 5m).

Execution & Risk Architecture:
1. Multi-Timeframe Alignment:
   - 1D: Trend & directional bias filter (EMA20_1d + structure).
   - 4H: Intermediate trend alignment (EMA20_4h + ATR overextension filter).
   - 1H: Setup identification (pullback into value area, no chasing).
   - 15m: Structural shift (MSS / exhaustion of pullback).
   - 5m: Precision trigger & tight swing SL.
2. Dynamic Risk & Position Sizing Contract:
   - Fixed loss per trade: Exactly $3.00 USD max loss at Stop Loss.
   - Sizing: qty = 3.00 / abs(entry_price - sl_price).
   - Bounds: notional >= $5.00 (Binance minNotional), margin <= 50% of bankroll ($50 max margin).
3. 1:3 Minimum Risk:Reward Ratio:
   - TP distance >= 3.0 * SL distance placed as exchange bracket algoOrder.
4. Execution Contract:
   - Universe: SOLUSDT, SUIUSDT, NEARUSDT, DOGEUSDT, LINKUSDT.
   - Concurrency: Max 2 positions universe-wide.
   - Real-time exchange audit & reconciliation with solana.db testnet_executions.
   - Discord alerts via barrier_notifications.py to #🪙～sol.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
import json
import logging
from pathlib import Path
import sqlite3
import time
from typing import Any

from fapi_executor import BinanceFuturesExecutor, get_db_connection
from fapi_client import round_tick, SYMBOL_SPECS
import fetch_market as fm
import ledger
from barrier_notifications import format_price, post_persona_discord
from trend_analyzer import get_trend_phase
from multi_tf_strategy import (
    evaluate_mtf_pipeline,
    calculate_risk_position_size,
    calculate_bracket_orders,
    check_market_saturation,
    MTFPipelineResult,
    FIXED_LOSS_USD,
    MIN_RISK_REWARD,
)

logger = logging.getLogger("multi_tf_engine")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] multi_tf: %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "solana.db"

FOLLOWERS = ["SOLUSDT", "SUIUSDT", "NEARUSDT", "DOGEUSDT", "LINKUSDT"]
MAX_CONCURRENT_POSITIONS = 2
MAX_CONCURRENT_SAME_DIR_TRADES = 1
SCAN_INTERVAL_SECS = 45.0
EXCHANGE_AUDIT_INTERVAL_SECS = 15.0
MTF_ENTRY_CLAIM_COOLDOWN_SECS = 15 * 60
SYMBOL_EXIT_COOLDOWN_SECS = 900.0          # 15 minutes per-symbol exit cooldown
GLOBAL_CLUSTER_COOLDOWN_SECS = 180.0       # 3 minutes global cluster cooldown
CONSEC_LOSS_2_COOLDOWN_SECS = 900.0        # 15 minutes cooldown after 2 consecutive losses
CONSEC_LOSS_3_FREEZE_SECS = 3600.0         # 60 minutes stand-down after 3+ consecutive losses


def parse_utc_timestamp(ts: Any) -> float:
    """Parse ISO timestamp string or numeric timestamp to float epoch seconds."""
    if ts is None:
        return 0.0
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, TypeError, OverflowError):
            return 0.0
    return 0.0


def get_consecutive_losses(
    db: Path | str | sqlite3.Connection = DB_PATH,
    table_name: str = "testnet_executions",
) -> int:
    """Query recent closed executions to count consecutive losses (pnl_usd < 0).
    Ordered by closed_utc DESC or id DESC. Stops counting at the first trade where pnl_usd >= 0.
    """
    if table_name not in {"testnet_executions", "live_executions"}:
        raise ValueError(f"unexpected execution table: {table_name}")

    query = f"""
        SELECT pnl_usd, exit_reason
        FROM {table_name}
        WHERE status = 'CLOSED'
        ORDER BY id DESC
        LIMIT 50
    """
    try:
        if isinstance(db, sqlite3.Connection):
            rows = db.execute(query).fetchall()
        else:
            with get_db_connection(Path(db)) as con:
                rows = con.execute(query).fetchall()

        losses = 0
        for row in rows:
            pnl = row[0]
            if pnl is None:
                reason = str(row[1] or "").lower()
                is_loss = (reason == "sl_hit")
            else:
                is_loss = float(pnl) < 0.0

            if is_loss:
                losses += 1
            else:
                break
        return losses
    except Exception as e:
        logger.warning(f"Failed to query consecutive losses: {e}")
        return 0


class MultiTimeframeEngine:
    def __init__(self, env_mode: str | None = None, db_path: Path | str | sqlite3.Connection | None = None):
        self.executor = BinanceFuturesExecutor(env_mode=env_mode)
        self.table_name = self.executor.table_name
        self.db_path = db_path or DB_PATH
        self.running = True
        self.last_scan_ts: float = 0.0
        self.last_audit_ts: float = 0.0
        self.last_global_entry_time: float = 0.0
        self.last_loss_time: float = 0.0
        self.symbol_cooldowns: dict[str, float] = {}
        self.active_trades: dict[int, dict] = {}

    def get_consecutive_losses(self) -> int:
        """Query consecutive loss count using engine's configured db and table."""
        return get_consecutive_losses(self.db_path, self.table_name)

    def record_symbol_exit(self, symbol: str, exit_time: float | None = None) -> None:
        """Record symbol exit timestamp for per-symbol cooldown."""
        ts = time.time() if exit_time is None else exit_time
        self.symbol_cooldowns[symbol] = ts

    def reconcile_closed_trade(self, symbol: str, exit_time: float | None = None, is_loss: bool = False) -> None:
        """Reconcile closed trade and update cooldowns."""
        ts = time.time() if exit_time is None else exit_time
        self.symbol_cooldowns[symbol] = ts
        if is_loss:
            self.last_loss_time = ts

    @staticmethod
    def setup_is_eligible(con: sqlite3.Connection, table_name: str, symbol: str,
                          direction: str, entry_price: float, sl_price: float,
                          tp_price: float, now_ts: float | None = None,
                          cooldown_secs: float = MTF_ENTRY_CLAIM_COOLDOWN_SECS) -> bool:
        """Keep one durable symbol claim while a setup persists, regardless of price drift."""
        if table_name not in {"testnet_executions", "live_executions"}:
            raise ValueError("unexpected execution table")
        row = con.execute("""
            SELECT p.id, p.created_utc, p.direction, e.status, e.prediction_id, e.closed_utc
            FROM predictions p LEFT JOIN {table} e ON e.prediction_id = p.id
            WHERE p.symbol=? AND p.horizon='multi_tf'
            ORDER BY p.id DESC LIMIT 1
        """.format(table=table_name), (symbol,)).fetchone()
        if row is None:
            return True
        status = row[3]
        if status and status.upper() == "OPEN":
            return False
        # Terminal executions cool down from closure; unresolved/failed executions
        # cool down from prediction creation. Invalid timestamps fail closed.
        timestamp = row[5] if status and status.upper() in {"CLOSED", "CANCELLED", "REJECTED"} else row[1]
        try:
            created = datetime.fromisoformat(timestamp).timestamp()
        except (TypeError, ValueError, OverflowError):
            return False
        now_ts = time.time() if now_ts is None else now_ts
        if now_ts < created or (now_ts - created) < cooldown_secs:
            return False
        return True

    def load_active_trades(self) -> dict[int, dict]:
        """Load open positions from active execution table & predictions,
        and populate recent closed trade timestamps & global entry timestamp.
        """
        active = {}
        now_ts = time.time()
        db_target = self.db_path

        if isinstance(db_target, sqlite3.Connection):
            con_ctx = None
            con = db_target
        else:
            con_ctx = get_db_connection(Path(db_target))
            con = con_ctx.__enter__()

        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(f"""
                SELECT te.id as exec_id, te.prediction_id, te.symbol, te.direction, te.side,
                       te.entry_price, te.entry_qty, te.tp_price, te.tp_order_id,
                       te.sl_price, te.sl_order_id, te.created_utc,
                       p.features_json, p.params_json
                FROM {self.table_name} te
                JOIN predictions p ON te.prediction_id = p.id
                WHERE te.status = 'OPEN' AND p.horizon IN ('multi_tf', 'hourly_trend')
                ORDER BY te.id ASC
            """).fetchall()

            for r in rows:
                active[r["prediction_id"]] = {
                    "exec_id": r["exec_id"],
                    "prediction_id": r["prediction_id"],
                    "symbol": r["symbol"],
                    "direction": r["direction"],
                    "side": r["side"],
                    "entry_price": float(r["entry_price"]),
                    "entry_qty": float(r["entry_qty"]),
                    "tp_price": float(r["tp_price"]) if r["tp_price"] else None,
                    "tp_order_id": r["tp_order_id"],
                    "sl_price": float(r["sl_price"]) if r["sl_price"] else None,
                    "sl_order_id": r["sl_order_id"],
                    "created_utc": r["created_utc"],
                    "breakeven_locked": False,
                    "peak_pnl_usd": 0.0,
                    "has_expanded": False,
                }

            # Populate recent closed trade timestamps for symbol exit cooldown (15 minutes)
            try:
                closed_rows = con.execute(f"""
                    SELECT symbol, closed_utc
                    FROM {self.table_name}
                    WHERE status = 'CLOSED' AND closed_utc IS NOT NULL
                    ORDER BY id DESC
                    LIMIT 50
                """).fetchall()
                for cr in closed_rows:
                    sym = cr["symbol"]
                    c_utc = cr["closed_utc"]
                    c_ts = parse_utc_timestamp(c_utc)
                    if c_ts > 0 and (now_ts - c_ts) < SYMBOL_EXIT_COOLDOWN_SECS:
                        if sym not in self.symbol_cooldowns or c_ts > self.symbol_cooldowns[sym]:
                            self.symbol_cooldowns[sym] = c_ts
            except Exception as e:
                logger.warning(f"Failed to load recent closed executions for cooldown: {e}")

            # Populate most recent trade entry time
            try:
                recent_entry = con.execute(f"""
                    SELECT created_utc
                    FROM {self.table_name}
                    ORDER BY id DESC
                    LIMIT 1
                """).fetchone()
                if recent_entry and recent_entry["created_utc"]:
                    e_ts = parse_utc_timestamp(recent_entry["created_utc"])
                    if e_ts > self.last_global_entry_time:
                        self.last_global_entry_time = e_ts
            except Exception as e:
                logger.warning(f"Failed to load recent entry time: {e}")

            # Populate most recent loss time
            try:
                recent_loss = con.execute(f"""
                    SELECT closed_utc
                    FROM {self.table_name}
                    WHERE status = 'CLOSED' AND (pnl_usd < 0 OR exit_reason = 'sl_hit') AND closed_utc IS NOT NULL
                    ORDER BY id DESC
                    LIMIT 1
                """).fetchone()
                if recent_loss and recent_loss["closed_utc"]:
                    l_ts = parse_utc_timestamp(recent_loss["closed_utc"])
                    if l_ts > self.last_loss_time:
                        self.last_loss_time = l_ts
            except Exception as e:
                logger.warning(f"Failed to load recent loss time: {e}")

        finally:
            if con_ctx:
                con_ctx.__exit__(None, None, None)

        self.active_trades = active
        return active

    async def fetch_symbol_candles(self, symbol: str) -> dict[str, list[dict]]:
        """Fetch all 5 timeframes concurrently for a symbol."""
        intervals = ["1d", "4h", "1h", "15m", "5m"]
        tasks = [
            asyncio.to_thread(fm.fetch_candles, itv, 60, symbol)
            for itv in intervals
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        tf_data = {}
        for itv, res in zip(intervals, results):
            if isinstance(res, list) and len(res) >= 20:
                tf_data[itv] = res
            else:
                logger.warning(f"Failed to fetch {itv} candles for {symbol}: {res}")
                return {}
        return tf_data

    async def audit_exchange_positions(self):
        """Reconcile active positions with Binance Futures testnet.
        Detects native TP / SL order fills and flattens residual orders.
        """
        if not self.active_trades:
            return

        try:
            positions = await asyncio.to_thread(self.executor._request, "GET", "/fapi/v2/positionRisk")
            if not isinstance(positions, list):
                return

            pos_map = {p.get("symbol"): float(p.get("positionAmt", 0.0)) for p in positions}

            for pid, trade in list(self.active_trades.items()):
                symbol = trade["symbol"]
                pos_amt = pos_map.get(symbol, 0.0)

                # If exchange position is flat (0.0), bracket order triggered on exchange
                if symbol in pos_map and pos_amt == 0.0:
                    exit_reason = "sl_hit"
                    exit_price = trade["sl_price"] or trade["entry_price"]

                    # Check TP algo order status
                    tp_id = trade.get("tp_order_id")
                    if tp_id:
                        try:
                            tp_res = await asyncio.to_thread(
                                self.executor._request, "GET", "/fapi/v1/algoOrder",
                                {"symbol": symbol, "algoId": tp_id}
                            )
                            if isinstance(tp_res, dict) and tp_res.get("algoStatus") == "FINISHED":
                                exit_reason = "tp_hit"
                                exit_price = float(tp_res.get("actualPrice") or trade["tp_price"])
                        except Exception as e:
                            logger.debug(f"Error checking TP algo status: {e}")

                    # Check SL algo order status
                    sl_id = trade.get("sl_order_id")
                    if exit_reason != "tp_hit" and sl_id:
                        try:
                            sl_res = await asyncio.to_thread(
                                self.executor._request, "GET", "/fapi/v1/algoOrder",
                                {"symbol": symbol, "algoId": sl_id}
                            )
                            if isinstance(sl_res, dict) and sl_res.get("algoStatus") == "FINISHED":
                                exit_reason = "sl_hit"
                                exit_price = float(sl_res.get("actualPrice") or trade["sl_price"])
                        except Exception as e:
                            logger.debug(f"Error checking SL algo status: {e}")

                    logger.info(f"⚡ Native exchange exit detected for {symbol} #{pid}: {exit_reason} @ {exit_price}")
                    await self.close_trade(pid, symbol, exit_reason, exit_price)
                elif symbol in pos_map and pos_amt != 0.0:
                    # Active position: manage profit & ratchet SL when trend reaches consolidation
                    await self.manage_active_profit_and_trend(pid, trade, pos_amt)

        except Exception as e:
            logger.error(f"Error auditing exchange positions: {e}", exc_info=True)

    async def manage_active_profit_and_trend(self, pid: int, trade: dict, pos_amt: float):
        """Manage active trade: when trend enters consolidation/exhaustion,
        ratchet SL into positive territory to cover at least $0.25 fee buffer,
        and lock in up to 70% of floating profit if trade moved substantially.
        """
        symbol = trade["symbol"]
        entry_price = float(trade["entry_price"])
        qty = abs(float(trade["entry_qty"]))
        if qty <= 0 or entry_price <= 0:
            return

        direction = trade["direction"]
        is_long = direction == "bullish"
        current_sl = float(trade.get("sl_price") or 0.0)

        # 1. Fetch live ticker price
        try:
            ticker_res = await asyncio.to_thread(self.executor._request, "GET", "/fapi/v1/ticker/price", {"symbol": symbol})
            mark_price = float(ticker_res.get("price") or 0.0)
        except Exception:
            return

        if mark_price <= 0:
            return

        # Current unrealized PnL in USD
        pnl_usd = (mark_price - entry_price) * qty if is_long else (entry_price - mark_price) * qty

        # 2. Query trend lifecycle phase
        trend_info = await asyncio.to_thread(get_trend_phase, symbol, position_direction=direction)
        phase = trend_info.get("phase", "")
        atr = float(trend_info.get("atr") or 0.0)

        ROUNDTRIP_FEE_USD = 0.25
        effective_fixed_loss = getattr(self.executor, "fixed_loss_usd", FIXED_LOSS_USD)
        MIN_EXPANSION_TRIGGER_USD = round(effective_fixed_loss * 0.40, 2)  # Must expand at least +0.4R (+$1.20 for $3 risk) before consolidation lock
        spec = SYMBOL_SPECS.get(symbol, {"price_tick": 0.01, "step_size": 0.01})
        price_tick = spec.get("price_tick", 0.01)

        # High-watermark tracking
        peak_pnl = max(trade.get("peak_pnl_usd", 0.0), pnl_usd)
        trade["peak_pnl_usd"] = peak_pnl

        # Gate 0: Early Take-Profit on Market Saturation / Exhaustion
        # When floating profit is substantial (>= $1.50 / ~0.5R, fees covered) and momentum is exhausted
        tp_px = float(trade.get("tp_price") or 0.0)
        min_saturation_profit_usd = max(ROUNDTRIP_FEE_USD * 4, round(effective_fixed_loss * 0.45, 2))  # e.g. >= $1.35
        if pnl_usd >= min_saturation_profit_usd:
            try:
                c_1h = await asyncio.to_thread(fm.fetch_candles, "1h", 30, symbol)
                c_15m = await asyncio.to_thread(fm.fetch_candles, "15m", 30, symbol)
                is_saturated, sat_reason, sat_diag = check_market_saturation(
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry_price,
                    current_price=mark_price,
                    tp_price=tp_px,
                    candles_1h=c_1h,
                    candles_15m=c_15m,
                )
                if is_saturated:
                    logger.info(
                        f"🛑 [{self.executor.env_mode}] Market SATURATION detected on {symbol} #{pid}: "
                        f"{sat_reason} (Floating: +${pnl_usd:.2f}). Triggering immediate early profit close!"
                    )
                    await self.close_trade(pid, symbol, "saturation_close", mark_price)
                    return
            except Exception as e:
                logger.warning(f"Error checking market saturation for {symbol}: {e}")

        # Gate 1: Check expansion milestone (or recover from previous restart)
        has_expanded = trade.get("has_expanded", False)
        if is_long and current_sl > entry_price:
            has_expanded = True
        elif not is_long and current_sl > 0 and current_sl < entry_price:
            has_expanded = True

        if peak_pnl >= MIN_EXPANSION_TRIGGER_USD:
            has_expanded = True

        trade["has_expanded"] = has_expanded

        # Gate 2: Protection Decision
        # Only activate consolidation profit lock IF the trade has already proven directional expansion!
        # If trade is still in initial consolidation nursery (< MIN_EXPANSION_TRIGGER_USD peak), let initial swing SL breathe.
        should_secure = False
        target_lock_usd = ROUNDTRIP_FEE_USD

        trail_riding_threshold = round(effective_fixed_loss * 0.70, 2)  # 70% to 1R (+$2.10 for $3 risk)
        impulse_threshold = round(effective_fixed_loss * 0.80, 2)       # 80% to 1R (+$2.40 for $3 risk)

        if has_expanded:
            if phase in ("CONSOLIDATION", "END") and pnl_usd >= ROUNDTRIP_FEE_USD:
                should_secure = True
                # Lock in at least 65% of peak gain or 70% of current gain, covering fee buffer ($0.25)
                target_lock_usd = max(ROUNDTRIP_FEE_USD, peak_pnl * 0.65, pnl_usd * 0.70)
            elif pnl_usd >= trail_riding_threshold:
                # Even while riding the wave, if floating profit >= 70% to 1R, trail to lock 55%+
                should_secure = True
                target_lock_usd = max(ROUNDTRIP_FEE_USD, pnl_usd * 0.55)
        elif pnl_usd >= impulse_threshold:
            # Massive single-candle impulse before consolidation check
            should_secure = True
            target_lock_usd = max(ROUNDTRIP_FEE_USD, pnl_usd * 0.50)

        if not should_secure:
            return

        # 3. Calculate candidate SL price ensuring fee buffer ($0.25) is fully covered
        if is_long:
            candidate_sl = entry_price + (target_lock_usd / qty)
            safe_buffer = max(mark_price * 0.0005, atr * 0.10) if atr > 0 else mark_price * 0.0005
            max_safe_sl = mark_price - safe_buffer
            candidate_sl = min(candidate_sl, max_safe_sl)
            candidate_sl = round_tick(candidate_sl, price_tick)

            min_fee_sl = round_tick(entry_price + (ROUNDTRIP_FEE_USD / qty), price_tick)
            if candidate_sl > current_sl and candidate_sl >= min_fee_sl:
                secured_usd = (candidate_sl - entry_price) * qty
                logger.info(
                    f"[{self.executor.env_mode}] Securing profit on {symbol} #{pid} ({phase}): "
                    f"Current SL {current_sl} -> New SL {candidate_sl} (+${secured_usd:.2f} locked, fee covered)"
                )
                success = await asyncio.to_thread(
                    self.executor.update_bracket_orders,
                    prediction_id=pid,
                    symbol=symbol,
                    new_sl_price=candidate_sl
                )
                if success.get("status") == "ok":
                    trade["sl_price"] = candidate_sl
                    trade["breakeven_locked"] = True
                    await self._notify_profit_lock(symbol, direction, entry_price, mark_price, current_sl, candidate_sl, secured_usd, phase)

        else:  # short
            candidate_sl = entry_price - (target_lock_usd / qty)
            safe_buffer = max(mark_price * 0.0005, atr * 0.10) if atr > 0 else mark_price * 0.0005
            min_safe_sl = mark_price + safe_buffer
            candidate_sl = max(candidate_sl, min_safe_sl)
            candidate_sl = round_tick(candidate_sl, price_tick)

            min_fee_sl = round_tick(entry_price - (ROUNDTRIP_FEE_USD / qty), price_tick)
            if (current_sl <= 0 or candidate_sl < current_sl) and candidate_sl <= min_fee_sl:
                secured_usd = (entry_price - candidate_sl) * qty
                logger.info(
                    f"[{self.executor.env_mode}] Securing profit on {symbol} #{pid} ({phase}): "
                    f"Current SL {current_sl} -> New SL {candidate_sl} (+${secured_usd:.2f} locked, fee covered)"
                )
                success = await asyncio.to_thread(
                    self.executor.update_bracket_orders,
                    prediction_id=pid,
                    symbol=symbol,
                    new_sl_price=candidate_sl
                )
                if success.get("status") == "ok":
                    trade["sl_price"] = candidate_sl
                    trade["breakeven_locked"] = True
                    await self._notify_profit_lock(symbol, direction, entry_price, mark_price, current_sl, candidate_sl, secured_usd, phase)

    async def _notify_profit_lock(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        mark_price: float,
        old_sl: float,
        new_sl: float,
        locked_usd: float,
        phase: str,
    ):
        now_utc_dt = datetime.now(UTC)
        wib_str = now_utc_dt.astimezone(WIB).strftime("%H:%M:%S")
        env_badge = "🔴 **LIVE MAINNET**" if self.executor.env_mode == "LIVE" else "🟡 **TESTNET**"
        title = f"🛡️ **PROFIT SECURED / SL RATCHETED** ({env_badge}) · **{symbol}**"
        details = (
            f"[{self.executor.env_mode}] Trend {symbol} memasuki fase {phase}. "
            f"Stop Loss digeser otomatis ke {format_price(new_sl, symbol)} untuk mengunci keuntungan minimal +${locked_usd:.2f} "
            f"(biaya fee $0.25 tertutup aman)."
        )
        metrics = (
            f"{title}\n"
            f"• **Environment**: {env_badge}\n"
            f"• **Symbol / Direction**: **{symbol}** ({direction.upper()})\n"
            f"• **Trend Lifecycle**: `{phase}`\n"
            f"• **Entry Fill**: `{format_price(entry_price, symbol)}`\n"
            f"• **Current Mark Price**: `{format_price(mark_price, symbol)}`\n"
            f"• **Old SL ➔ New SL**: `{format_price(old_sl, symbol)}` ➔ `{format_price(new_sl, symbol)}`\n"
            f"• **Locked Net Profit**: **+${locked_usd:.2f}** (Fee $0.25 covered)\n"
            f"• **Timestamp**: `{wib_str} WIB`"
        )
        await asyncio.to_thread(post_persona_discord, "MTF_PROFIT_SECURED", details, metrics)

    async def scan_market(self):
        """Scan universe coins through Top-Down MTF Pipeline and execute qualifying setups."""
        self.load_active_trades()
        open_count = len(self.active_trades)
        if open_count >= MAX_CONCURRENT_POSITIONS:
            logger.info(f"Concurrency ceiling reached ({open_count}/{MAX_CONCURRENT_POSITIONS}). Skipping scan.")
            return

        current_time = time.time()

        # Rule 4: Dynamic Consecutive Loss Protection
        consec_losses = self.get_consecutive_losses()
        if consec_losses >= 3:
            ref_time = max(self.last_global_entry_time, self.last_loss_time)
            elapsed = current_time - ref_time
            if elapsed < CONSEC_LOSS_3_FREEZE_SECS:
                remaining = int(CONSEC_LOSS_3_FREEZE_SECS - elapsed)
                logger.info(
                    f"Consecutive loss circuit breaker active ({consec_losses} losses). Freezing entries for {remaining}s."
                )
                return

        # Rule 3: Global Cluster Cooldown (scaled to 15m if consec_losses == 2)
        current_global_cooldown = (
            CONSEC_LOSS_2_COOLDOWN_SECS if consec_losses == 2 else GLOBAL_CLUSTER_COOLDOWN_SECS
        )
        ref_time = (
            max(self.last_global_entry_time, self.last_loss_time)
            if consec_losses == 2
            else self.last_global_entry_time
        )
        elapsed = current_time - ref_time
        if elapsed < current_global_cooldown:
            remaining = int(current_global_cooldown - elapsed)
            logger.info(f"Global cluster cooldown active ({remaining}s remaining). Skipping scan.")
            return

        logger.info(f"🔍 Scanning market for Top-Down MTF setups (Open: {open_count}/{MAX_CONCURRENT_POSITIONS})...")

        for symbol in FOLLOWERS:
            # Skip symbol if already actively traded
            if any(t["symbol"] == symbol for t in self.active_trades.values()):
                continue

            # Rule 2: Per-Symbol Exit Cooldown
            exit_ts = self.symbol_cooldowns.get(symbol, 0.0)
            if current_time - exit_ts < SYMBOL_EXIT_COOLDOWN_SECS:
                remaining = int(SYMBOL_EXIT_COOLDOWN_SECS - (current_time - exit_ts))
                logger.info(f"[{symbol}] In exit cooldown ({remaining}s remaining). Skipping.")
                continue

            # Fetch multi-timeframe candle data
            tf_data = await self.fetch_symbol_candles(symbol)
            if not tf_data:
                continue

            # Run full MTF pipeline: 1D -> 4H -> 1H -> 15m -> 5m
            pipeline_res: MTFPipelineResult = evaluate_mtf_pipeline(symbol, tf_data, min_rr=MIN_RISK_REWARD)

            if not pipeline_res.valid:
                stage = pipeline_res.stage_failed or "unknown"
                prefix = stage.split("_")[0] if stage else "unknown"
                diag = pipeline_res.diagnostics.get(prefix) or pipeline_res.diagnostics.get("sr_barrier", {})
                reason = diag.get("reason", stage)
                logger.info(f"[{symbol}] MTF scan: {pipeline_res.direction} | Filter: {stage} ({reason})")
                continue

            # Rule 1: Directional Concurrency Guard
            same_dir_count = sum(
                1 for t in self.active_trades.values() if t.get("direction") == pipeline_res.direction
            )
            if same_dir_count >= MAX_CONCURRENT_SAME_DIR_TRADES:
                logger.info(
                    f"Directional concurrency ceiling reached ({same_dir_count}/{MAX_CONCURRENT_SAME_DIR_TRADES} "
                    f"for {pipeline_res.direction}). Skipping {symbol}."
                )
                continue

            # Valid setup found!
            logger.info(
                f"🎯 VALID MTF SETUP: {symbol} {pipeline_res.direction.upper()} "
                f"@ {pipeline_res.entry_price} (SL: {pipeline_res.sl_price}, TP: {pipeline_res.tp_price}, R:R: 1:{pipeline_res.risk_reward:.2f})"
            )

            await self.execute_entry(pipeline_res)

            # Check if concurrency limit reached after this entry
            if len(self.active_trades) >= MAX_CONCURRENT_POSITIONS:
                break

    async def execute_entry(self, opp: MTFPipelineResult):
        """Calculate dynamic $3 risk sizing and execute bracket orders on testnet/live."""
        symbol = opp.symbol
        direction = opp.direction
        entry_price = opp.entry_price
        sl_price = opp.sl_price
        tp_price = opp.tp_price

        # Rule 1: Directional concurrency guard
        same_dir_count = sum(
            1 for t in self.active_trades.values() if t.get("direction") == direction
        )
        if same_dir_count >= MAX_CONCURRENT_SAME_DIR_TRADES:
            logger.info(
                f"Directional concurrency ceiling reached ({same_dir_count}/{MAX_CONCURRENT_SAME_DIR_TRADES} "
                f"for {direction}). Skipping {symbol}."
            )
            return None

        # Rule 2: Per-symbol exit cooldown
        now_ts = time.time()
        exit_ts = self.symbol_cooldowns.get(symbol, 0.0)
        if now_ts - exit_ts < SYMBOL_EXIT_COOLDOWN_SECS:
            remaining = int(SYMBOL_EXIT_COOLDOWN_SECS - (now_ts - exit_ts))
            logger.info(f"[{symbol}] In exit cooldown ({remaining}s remaining). Skipping.")
            return None

        # Calculate exact $3.00 max loss position sizing
        effective_max_loss = getattr(self.executor, "fixed_loss_usd", FIXED_LOSS_USD)
        sizing = calculate_risk_position_size(
            symbol=symbol,
            entry_price=entry_price,
            sl_price=sl_price,
            leverage=self.executor.leverage,
            max_loss_usd=effective_max_loss,
            bankroll=100.0,
            max_margin_pct=0.50,
        )

        qty = sizing["qty"]
        notional = sizing["notional_usd"]
        margin = sizing["margin_usd"]
        est_loss = sizing["estimated_loss_usd"]

        now_utc_dt = datetime.now(UTC)
        now_utc = now_utc_dt.isoformat(timespec="seconds")
        wib_str = now_utc_dt.astimezone(WIB).strftime("%H:%M:%S")

        logger.info(
            f"🚀 Executing MTF Entry: {symbol} {direction.upper()} | Qty={qty} | Notional=${notional} "
            f"| Margin=${margin} | Risk=${est_loss:.2f} at SL | R:R=1:{opp.risk_reward:.2f}"
        )

        # Prevent repeated scans from appending identical predictions. The ledger
        # remains append-only; unresolved attempts require a changed setup or final execution.
        con = ledger.connect()
        try:
            if not self.setup_is_eligible(con, self.table_name, symbol, direction,
                                          entry_price, sl_price, tp_price):
                logger.info(f"[{symbol}] Suppressing duplicate unresolved MTF setup")
                return
            pid = ledger.record_prediction(
                con,
                horizon="multi_tf",
                target_bar_utc=(now_utc_dt + timedelta(hours=24)).isoformat(timespec="seconds"),
                scheduled_slot_utc=now_utc,
                direction=direction,
                confidence=0.88,
                ref_price=round(entry_price, 6),
                model_version="v4.0-topdown-mtf",
                params={
                    "strategy": "topdown_mtf_5m_trigger",
                    "min_rr": MIN_RISK_REWARD,
                    "fixed_risk_usd": effective_max_loss,
                    "notional_usd": notional,
                    "margin_usd": margin,
                },
                features={
                    "sl_price": sl_price,
                    "tp_price": tp_price,
                    "sl_dist_pct": sizing["sl_dist_pct"],
                    "risk_reward": opp.risk_reward,
                    "diagnostics": opp.diagnostics,
                },
                inputs={
                    "ref_price": entry_price,
                    "stop_loss_price": sl_price,
                    "target_tp_price": tp_price,
                },
                signal_source="multi_tf_engine",
                noise_threshold=round(entry_price * 0.005, 4),
                symbol=symbol,
                leader_symbol="BTCUSDT",
                notes=f"Top-Down MTF (1D->4H->1H->15m->5m) {direction.upper()} on {symbol}. SL: {sl_price}, TP: {tp_price}",
            )
        finally:
            con.close()

        # 2. Place market entry and bracket algo orders on Binance Futures Testnet
        exec_info = await asyncio.to_thread(
            self.executor.open_lead_lag_position,
            prediction_id=pid,
            symbol=symbol,
            direction=direction,
            ref_price=entry_price,
            tp_price=tp_price,
            sl_price=sl_price,
            qty=qty,
        )

        if not exec_info:
            logger.error(f"Failed to place testnet bracket orders for {symbol} #{pid}")
            return None

        # Rule 3: Record global entry time
        self.last_global_entry_time = time.time()

        self.active_trades[pid] = {
            "exec_id": exec_info["execution_id"],
            "prediction_id": pid,
            "symbol": symbol,
            "direction": direction,
            "side": "BUY" if direction == "bullish" else "SELL",
            "entry_price": exec_info["entry_price"],
            "entry_qty": exec_info["qty"],
            "tp_price": tp_price,
            "tp_order_id": exec_info["tp_order_id"],
            "sl_price": sl_price,
            "sl_order_id": exec_info["sl_order_id"],
            "created_utc": now_utc,
            "breakeven_locked": False,
            "peak_pnl_usd": 0.0,
            "has_expanded": False,
        }

        # 3. Post Discord persona alert
        env_badge = "🔴 **LIVE MAINNET**" if self.executor.env_mode == "LIVE" else "🟡 **TESTNET**"
        dir_emoji = "🐂" if direction == "bullish" else "🐻"
        title = f"🌊 **TOP-DOWN MTF EXECUTION** ({env_badge}) · **{symbol}** {dir_emoji}"
        details = (
            f"[{self.executor.env_mode}] Konfirmasi Top-Down Multi-Timeframe lengkap (1D Trend ➔ 4H Alignment ➔ 1H Pullback ➔ 15m MSS ➔ 5m Trigger). "
            f"Eksekusi entry {symbol} di {format_price(entry_price, symbol)}. "
            f"Risk strictly capped at ${est_loss:.2f} max loss jika SL kena. Bracket TP terpasang di target 1:{opp.risk_reward:.2f} R:R."
        )
        metrics = (
            f"{title}\n"
            f"• **Environment**: {env_badge}\n"
            f"• **Strategy**: `Top-Down MTF Engine` (`1D ➔ 4H ➔ 1H ➔ 15m ➔ 5m`)\n"
            f"• **Direction**: {dir_emoji} **{direction.upper()}**\n"
            f"• **Entry**: `{format_price(entry_price, symbol)}`\n"
            f"• **Stop Loss**: `{format_price(sl_price, symbol)}` (Max Risk: **${est_loss:.2f}**)\n"
            f"• **Take Profit**: `{format_price(tp_price, symbol)}` (Target R:R: **1:{opp.risk_reward:.2f}**)\n"
            f"• **Position**: `{qty}` {symbol} · Notional: `${notional:.2f}` (Margin: `${margin:.2f}`)\n"
            f"• **Bracket Status**: 🛡️ Server-Side AlgoOrders Live on Binance\n"
            f"• **Prediction ID**: `#{pid}` · `{wib_str} WIB`"
        )
        await asyncio.to_thread(post_persona_discord, "MTF_ORDER_EXECUTED", details, metrics)

    async def close_trade(self, pid: int, symbol: str, exit_reason: str, exit_price: float):
        """Close trade on testnet, cancel remaining bracket orders, and record outcome."""
        trade = self.active_trades.pop(pid, None)
        if not trade:
            return

        now_utc_dt = datetime.now(UTC)
        wib_str = now_utc_dt.astimezone(WIB).strftime("%H:%M:%S")

        # 1. Close and flatten on testnet (cancels sibling bracket orders and fetches real PnL)
        await asyncio.to_thread(self.executor.close_position_on_exit, pid, symbol, exit_reason, exit_price)

        # 2. Record outcome in ledger
        entry_price = trade["entry_price"]
        direction = trade["direction"]
        move = (exit_price - entry_price) if direction == "bullish" else (entry_price - exit_price)
        move_pct = move / entry_price * 100
        is_win = exit_reason == "tp_hit" or move > 0

        # Rule 2: Per-symbol exit cooldown & Rule 4: last loss tracking
        now_ts = now_utc_dt.timestamp()
        self.symbol_cooldowns[symbol] = now_ts
        if not is_win:
            self.last_loss_time = now_ts

        con = ledger.connect()
        try:
            ledger.record_outcome(
                con,
                prediction_id=pid,
                realized_price=round(exit_price, 6),
                move=round(move, 6),
                move_pct=round(move_pct, 4),
                is_noise=False,
                correct=1 if is_win else 0,
                base_rate=0.5,
            )
        finally:
            con.close()

        # 3. Discord alert
        env_badge = "🔴 **LIVE MAINNET**" if self.executor.env_mode == "LIVE" else "🟡 **TESTNET**"
        emoji = "🏆" if is_win else "🛑"
        if exit_reason == "saturation_close":
            status_label = "EARLY PROFIT SECURED (SATURATION CLOSE)"
            emoji = "🎯"
        elif exit_reason == "tp_hit":
            status_label = "TAKE PROFIT (1:3+ R:R) HIT"
        elif is_win:
            status_label = "PROFITABLE CLOSE"
        else:
            status_label = "STOP LOSS HIT"
        details = (
            f"[{self.executor.env_mode}] {symbol} posisi {direction.upper()} selesai via {exit_reason.upper()} di {format_price(exit_price, symbol)}. "
            f"Hasil akhir move: {move_pct:+.2f}%. Order bracket sisi berlawanan telah otomatis dibersihkan di exchange."
        )
        metrics = (
            f"{emoji} **{status_label}: {symbol}** ({env_badge})\n"
            f"• **Environment**: {env_badge}\n"
            f"• **Direction**: **{direction.upper()}**\n"
            f"• **Entry**: `{format_price(entry_price, symbol)}` ➔ **Exit**: `{format_price(exit_price, symbol)}`\n"
            f"• **Realized Move**: `{move_pct:+.2f}%`\n"
            f"• **Exit Reason**: `{exit_reason.upper()}`\n"
            f"• **Timestamp**: `{wib_str} WIB`"
        )
        await asyncio.to_thread(post_persona_discord, "MTF_TRADE_CLOSED", details, metrics)

    async def run(self):
        """Main engine daemon loop."""
        logger.info("⚡ Starting Top-Down Multi-Timeframe Trading Engine (1D -> 4H -> 1H -> 15m -> 5m)...")
        self.load_active_trades()
        logger.info(f"Loaded {len(self.active_trades)} active positions on boot.")

        while self.running:
            now = time.time()
            try:
                # 1. Audit active exchange positions
                if now - self.last_audit_ts >= EXCHANGE_AUDIT_INTERVAL_SECS:
                    self.last_audit_ts = now
                    await self.audit_exchange_positions()

                # 2. Scan market for new setups
                if now - self.last_scan_ts >= SCAN_INTERVAL_SECS:
                    self.last_scan_ts = now
                    await self.scan_market()

            except Exception as e:
                logger.error(f"Error in engine main loop: {e}", exc_info=True)

            await asyncio.sleep(5.0)


if __name__ == "__main__":
    import sys
    env_arg = None
    if "--live" in sys.argv:
        env_arg = "LIVE"
    elif "--testnet" in sys.argv:
        env_arg = "TESTNET"
    engine = MultiTimeframeEngine(env_mode=env_arg)
    asyncio.run(engine.run())
