"""Small HTTP helper: every failure becomes DataUnavailableError with a clear reason.

Secrets in query parameters (apikey, token) are never included in error messages.
"""
from __future__ import annotations

from typing import Any, Callable

import requests

from backend.data.base import DataUnavailableError

_SECRET_PARAMS = {"apikey", "api_key", "token", "key"}

HttpGet = Callable[[str, dict[str, Any] | None], Any]


def _safe(url: str, params: dict[str, Any] | None) -> str:
    shown = {k: ("***" if k.lower() in _SECRET_PARAMS else v) for k, v in (params or {}).items()}
    return f"{url} {shown}" if shown else url


def make_http_get(timeout: float = 15.0, session: requests.Session | None = None) -> HttpGet:
    sess = session or requests.Session()
    sess.headers.update({"User-Agent": "GoldPulseAI/0.2 (personal dashboard)"})

    def get_json(url: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = sess.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            raise DataUnavailableError(f"Network error calling {_safe(url, params)}: {type(exc).__name__}") from None
        if resp.status_code == 429:
            raise DataUnavailableError(f"Rate limit reached at {_safe(url, None)} – wait a minute.")
        if resp.status_code >= 500:
            raise DataUnavailableError(f"{_safe(url, None)} is unavailable (HTTP {resp.status_code}).")
        try:
            body = resp.json()
        except ValueError:
            raise DataUnavailableError(f"{_safe(url, None)} returned non-JSON data (HTTP {resp.status_code}).") from None
        if resp.status_code >= 400:
            msg = body.get("message") or body.get("error") if isinstance(body, dict) else None
            raise DataUnavailableError(f"{_safe(url, None)} refused the request (HTTP {resp.status_code}): {msg}")
        return body

    return get_json
