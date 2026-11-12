"""Password protection for the online (cloud) version of GoldPulse AI.

When APP_PASSWORD is set, every page and API call needs the username and
password (HTTP Basic auth – the phone browser shows a login box and remembers
it). When APP_PASSWORD is empty (your PC at home) nothing changes.

/healthz stays open so the hosting service can check the server is alive; it
returns only {"ok": true}.

After MAX_FAILURES wrong passwords from one address, that address is locked
out for LOCK_SECONDS, which makes password guessing impractical.
"""
from __future__ import annotations

import base64
import binascii
import secrets
import time
from typing import Callable

OPEN_PATHS = {"/healthz"}
MAX_FAILURES = 10
LOCK_SECONDS = 15 * 60


class BasicAuthMiddleware:
    def __init__(self, app, username: str, password: str, clock: Callable[[], float] = time.monotonic):
        if not password:
            raise ValueError("BasicAuthMiddleware needs a password")
        self.app = app
        self._user = username.encode()
        self._pass = password.encode()
        self._clock = clock
        self._failures: dict[str, tuple[int, float]] = {}   # ip -> (count, first_failure_time)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _client_ip(scope) -> str:
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-for":          # set by the hosting proxy
                return value.decode("latin-1").split(",")[0].strip()
        client = scope.get("client")
        return client[0] if client else "unknown"

    def _check(self, scope) -> bool:
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                kind, _, token = value.partition(b" ")
                if kind.lower() != b"basic":
                    return False
                try:
                    decoded = base64.b64decode(token.strip(), validate=True)
                except (binascii.Error, ValueError):
                    return False
                user, sep, pwd = decoded.partition(b":")
                if not sep:
                    return False
                ok_user = secrets.compare_digest(user, self._user)
                ok_pass = secrets.compare_digest(pwd, self._pass)
                return ok_user and ok_pass
        return False

    def _locked(self, ip: str) -> bool:
        count, first = self._failures.get(ip, (0, 0.0))
        if count and self._clock() - first > LOCK_SECONDS:
            self._failures.pop(ip, None)
            return False
        return count >= MAX_FAILURES

    def _record_failure(self, ip: str) -> None:
        count, first = self._failures.get(ip, (0, self._clock()))
        self._failures[ip] = (count + 1, first)

    # ------------------------------------------------------------------ #
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in OPEN_PATHS:
            return await self.app(scope, receive, send)

        ip = self._client_ip(scope)
        if self._locked(ip):
            return await _plain(send, 429, b"Too many wrong passwords. Try again in 15 minutes.")

        has_header = any(name == b"authorization" for name, _ in scope.get("headers", []))
        if self._check(scope):
            self._failures.pop(ip, None)
            return await self.app(scope, receive, send)

        if has_header:                      # a wrong password, not just the first visit
            self._record_failure(ip)
        return await _plain(send, 401, b"Login required.",
                            extra=[(b"www-authenticate", b'Basic realm="GoldPulse AI", charset="UTF-8"')])


async def _plain(send, status: int, body: bytes, extra=None):
    headers = [(b"content-type", b"text/plain; charset=utf-8"),
               (b"content-length", str(len(body)).encode()),
               (b"cache-control", b"no-store")] + (extra or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
