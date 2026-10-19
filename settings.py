"""Solana Lead-Lag & Multi-Coin Testnet Trading Bot Settings.
Provides persistent configuration for leverage, margin, sizing, and risk limits.
Saved in state/settings.json and shared between the dashboard and execution engine.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("settings")

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
SETTINGS_FILE = STATE_DIR / "settings.json"

DEFAULT_SETTINGS: dict[str, Any] = {
    "leverage": 10,
    "margin_per_trade_usd": 4.0,       # $4.00 margin per position (~4% of $100)
    "target_notional_usd": 40.0,       # margin * leverage = $40.00
    "virtual_capital": 100.0,          # $100.00 virtual bankroll
    "max_concurrent": 2,               # max 2 open positions
    "max_daily_loss": 10.0,            # $10.00 daily loss circuit breaker
    "margin_type": "ISOLATED",
    "updated_at": "2026-10-20T00:00:00Z",
}

_SETTINGS_CACHE: dict[str, Any] | None = None
_CACHE_MTIME: float = 0.0


def _ensure_state_dir() -> None:
    if not STATE_DIR.exists():
        STATE_DIR.mkdir(parents=True, exist_ok=True)


def get_settings() -> dict[str, Any]:
    """Load settings from state/settings.json with mtime-based cache."""
    global _SETTINGS_CACHE, _CACHE_MTIME
    _ensure_state_dir()

    if not SETTINGS_FILE.exists():
        # Initialize default settings file
        save_settings(DEFAULT_SETTINGS)
        return dict(DEFAULT_SETTINGS)

    try:
        mtime = SETTINGS_FILE.stat().st_mtime
        if _SETTINGS_CACHE is not None and mtime == _CACHE_MTIME:
            return dict(_SETTINGS_CACHE)

        content = SETTINGS_FILE.read_text(encoding="utf-8")
        data = json.loads(content)
        # Merge with defaults in case new keys were added
        merged = dict(DEFAULT_SETTINGS)
        merged.update(data)
        _SETTINGS_CACHE = merged
        _CACHE_MTIME = mtime
        return dict(merged)
    except Exception as e:
        logger.warning(f"Failed to read {SETTINGS_FILE}: {e}. Returning defaults.")
        if _SETTINGS_CACHE is not None:
            return dict(_SETTINGS_CACHE)
        return dict(DEFAULT_SETTINGS)


def validate_settings(data: dict[str, Any]) -> tuple[bool, str | dict[str, Any]]:
    """Validate settings payload. Returns (is_valid, error_msg_or_sanitized_dict)."""
    try:
        leverage = int(data.get("leverage", 10))
        if leverage < 1 or leverage > 50:
            return False, "Leverage must be between 1x and 50x."

        virtual_capital = float(data.get("virtual_capital", 100.0))
        if virtual_capital < 10.0 or virtual_capital > 100000.0:
            return False, "Virtual capital must be between $10 and $100,000."

        margin_per_trade = float(data.get("margin_per_trade_usd", 4.0))
        if margin_per_trade < 0.5 or margin_per_trade > virtual_capital:
            return False, f"Margin per trade must be between $0.50 and ${virtual_capital:.2f}."

        # Compute or validate notional
        if "target_notional_usd" in data and float(data["target_notional_usd"]) > 0:
            target_notional = round(float(data["target_notional_usd"]), 2)
            # Recompute margin_per_trade if notional explicitly provided
            margin_per_trade = round(target_notional / leverage, 2)
        else:
            target_notional = round(margin_per_trade * leverage, 2)

        if target_notional < 5.0:
            return False, "Target notional must be >= $5.00 (Binance Futures minimum notional)."

        max_concurrent = int(data.get("max_concurrent", 2))
        if max_concurrent < 1 or max_concurrent > 10:
            return False, "Max concurrent positions must be between 1 and 10."

        max_daily_loss = float(data.get("max_daily_loss", 10.0))
        if max_daily_loss < 1.0 or max_daily_loss > virtual_capital:
            return False, f"Max daily loss must be between $1.00 and ${virtual_capital:.2f}."

        sanitized: dict[str, Any] = {
            "leverage": leverage,
            "margin_per_trade_usd": margin_per_trade,
            "target_notional_usd": target_notional,
            "virtual_capital": virtual_capital,
            "max_concurrent": max_concurrent,
            "max_daily_loss": max_daily_loss,
            "margin_type": "ISOLATED",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return True, sanitized
    except (ValueError, TypeError) as e:
        return False, f"Invalid value in settings: {e}"


def save_settings(new_data: dict[str, Any]) -> dict[str, Any]:
    """Validate and atomically save settings to state/settings.json."""
    global _SETTINGS_CACHE, _CACHE_MTIME
    _ensure_state_dir()

    is_valid, validated_or_err = validate_settings(new_data)
    if not is_valid:
        raise ValueError(str(validated_or_err))

    clean_data = validated_or_err
    assert isinstance(clean_data, dict)

    # Atomic write via temp file
    temp_fd, temp_path = tempfile.mkstemp(dir=STATE_DIR, prefix="settings_", suffix=".tmp")
    with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
        json.dump(clean_data, f, indent=2)
    os.replace(temp_path, SETTINGS_FILE)

    _SETTINGS_CACHE = clean_data
    _CACHE_MTIME = SETTINGS_FILE.stat().st_mtime
    logger.info(f"Updated bot settings: {clean_data}")
    return clean_data
