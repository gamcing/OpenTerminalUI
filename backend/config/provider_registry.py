from __future__ import annotations

from typing import Any

_PROVIDER_DEFS: list[dict[str, Any]] = [
    {
        "id": "kite",
        "name": "Zerodha Kite",
        "markets": ["NSE", "BSE"],
        "env_keys": ["KITE_API_KEY", "KITE_API_SECRET", "KITE_ACCESS_TOKEN"],
        "unlocks": ["Real-time NSE/BSE ticks", "Kite historical bars", "Holdings & positions import"],
    },
    {
        "id": "alpaca",
        "name": "Alpaca",
        "markets": ["US"],
        "env_keys": ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"],
        "unlocks": ["US bars & quotes"],
    },
    {
        "id": "fmp",
        "name": "Financial Modeling Prep",
        "markets": ["US"],
        "env_keys": ["FMP_API_KEY"],
        "unlocks": ["US fundamentals", "Earnings & estimates", "Analyst consensus"],
    },
    {
        "id": "finnhub",
        "name": "Finnhub",
        "markets": ["US"],
        "env_keys": ["FINNHUB_API_KEY"],
        "unlocks": ["Real-time US ticks", "Company profiles", "US news"],
    },
    {
        "id": "fred",
        "name": "FRED",
        "markets": ["GLOBAL"],
        "env_keys": ["FRED_API_KEY"],
        "unlocks": ["Macro series", "Yield curve history"],
    },
    {
        "id": "yahoo",
        "name": "Yahoo Finance",
        "markets": ["GLOBAL"],
        "env_keys": [],
        "unlocks": ["Delayed quotes & history (fallback)"],
    },
    {
        "id": "nse",
        "name": "NSE India (public)",
        "markets": ["NSE"],
        "env_keys": [],
        "unlocks": ["Option chain", "Corporate actions", "Market status"],
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "markets": ["AI"],
        "env_keys": ["OPENROUTER_API_KEY"],
        "unlocks": ["AI research agent", "Debate mode"],
    },
    {
        "id": "lmstudio",
        "name": "LM Studio (local LLM)",
        "markets": ["AI"],
        "env_keys": ["LM_STUDIO_BASE_URL"],
        "unlocks": ["Local sentiment/emotion", "Local agent"],
    },
]

def _build_provider_env_keys() -> dict[str, str]:
    """Build env_name -> provider_id mapping from _PROVIDER_DEFS."""
    result: dict[str, str] = {}
    for pdef in _PROVIDER_DEFS:
        pid = pdef["id"]
        for key in pdef.get("env_keys", []):
            result[key] = pid
    return result


PROVIDER_ENV_KEYS: dict[str, str] = _build_provider_env_keys()
