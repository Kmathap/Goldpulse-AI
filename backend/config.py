"""Application settings, read from environment variables / the .env file.

API keys are only read here, on the backend, and are never returned by any
endpoint or written to logs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

PROVIDERS = ("auto", "xaus", "twelvedata")


def _str(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _int(name: str, default: int) -> int:
    value = _str(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Setting {name} must be a whole number, got {value!r}") from exc


@dataclass(frozen=True)
class Settings:
    # "auto" = Twelve Data if TWELVEDATA_API_KEY is set, otherwise the free xaus.com feed
    market_data_provider: str = "auto"
    twelvedata_api_key: str | None = field(default=None, repr=False)
    http_timeout_seconds: int = 15

    # Data quality
    stale_after_seconds: int = 300          # live spot quote older than this -> STALE
    candle_stale_after_minutes: int = 30    # newest M5 candle older than this -> STALE
    basis_warning_pct: float = 0.25         # chart series vs spot gap that triggers a warning

    # API server
    api_host: str = "127.0.0.1"
    api_port: int = 8010
    cors_origins: tuple[str, ...] = ("http://127.0.0.1:8010", "http://localhost:8010")

    # Online version: login + where the journal is stored
    app_username: str = "goldpulse"
    app_password: str | None = field(default=None, repr=False)   # empty = no login (home PC)
    data_dir: Path = PROJECT_ROOT                                   # cloud: the persistent disk

    @property
    def resolved_provider(self) -> str:
        if self.market_data_provider == "auto":
            return "twelvedata" if self.twelvedata_api_key else "xaus"
        return self.market_data_provider


def load_settings() -> Settings:
    provider = (_str("MARKET_DATA_PROVIDER", "auto") or "auto").lower()
    if provider not in PROVIDERS:
        raise ValueError(f"MARKET_DATA_PROVIDER must be one of {PROVIDERS}, got {provider!r}")
    key = _str("TWELVEDATA_API_KEY")
    if provider == "twelvedata" and not key:
        raise ValueError("MARKET_DATA_PROVIDER=twelvedata needs TWELVEDATA_API_KEY in .env")
    origins = _str("CORS_ORIGINS")
    return Settings(
        market_data_provider=provider,
        twelvedata_api_key=key,
        http_timeout_seconds=_int("HTTP_TIMEOUT_SECONDS", 15),
        stale_after_seconds=_int("STALE_AFTER_SECONDS", 300),
        candle_stale_after_minutes=_int("CANDLE_STALE_AFTER_MINUTES", 30),
        api_host=_str("API_HOST", "127.0.0.1") or "127.0.0.1",
        api_port=_int("API_PORT", 8010),
        cors_origins=tuple(o.strip() for o in origins.split(",")) if origins else Settings.cors_origins,
        app_username=_str("APP_USERNAME", "goldpulse") or "goldpulse",
        app_password=_str("APP_PASSWORD"),
        data_dir=Path(_str("DATA_DIR") or PROJECT_ROOT),
    )
