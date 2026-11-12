"""Login (HTTP Basic auth) for the online version – no FastAPI needed."""
import asyncio
import base64

from backend.security import LOCK_SECONDS, MAX_FAILURES, BasicAuthMiddleware


async def _inner(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"dashboard"})


def call(mw, path="/", auth=None, ip="1.2.3.4"):
    headers = [(b"x-forwarded-for", ip.encode())]
    if auth is not None:
        headers.append((b"authorization", auth))
    scope = {"type": "http", "path": path, "headers": headers, "client": ("10.0.0.1", 5)}
    out = []

    async def send(msg):
        out.append(msg)

    asyncio.run(mw(scope, None, send))
    return out[0]["status"], dict(out[0]["headers"]), out[1]["body"]


def basic(user, pwd):
    return b"Basic " + base64.b64encode(f"{user}:{pwd}".encode())


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def make(clock=None):
    return BasicAuthMiddleware(_inner, "goldpulse", "s3cret-Pass", clock=clock or Clock())


def test_no_login_gets_401_with_prompt():
    status, headers, _ = call(make())
    assert status == 401 and b"Basic" in headers[b"www-authenticate"]


def test_correct_login_passes():
    status, _, body = call(make(), auth=basic("goldpulse", "s3cret-Pass"))
    assert status == 200 and body == b"dashboard"


def test_wrong_password_and_garbage_rejected():
    mw = make()
    assert call(mw, auth=basic("goldpulse", "wrong"))[0] == 401
    assert call(mw, auth=basic("other", "s3cret-Pass"))[0] == 401
    assert call(mw, auth=b"Basic !!!notbase64")[0] == 401
    assert call(mw, auth=b"Bearer abc")[0] == 401


def test_api_is_protected_but_healthz_open():
    mw = make()
    assert call(mw, path="/api/analysis")[0] == 401
    assert call(mw, path="/healthz")[0] == 200


def test_lockout_after_many_failures_then_expires():
    clock = Clock()
    mw = make(clock)
    for _ in range(MAX_FAILURES):
        assert call(mw, auth=basic("goldpulse", "guess"))[0] == 401
    # even the right password is refused while locked
    assert call(mw, auth=basic("goldpulse", "s3cret-Pass"))[0] == 429
    # other addresses are not affected
    assert call(mw, auth=basic("goldpulse", "s3cret-Pass"), ip="5.6.7.8")[0] == 200
    clock.t += LOCK_SECONDS + 1
    assert call(mw, auth=basic("goldpulse", "s3cret-Pass"))[0] == 200


def test_first_visit_without_password_does_not_count_as_failure():
    mw = make()
    for _ in range(MAX_FAILURES + 5):
        call(mw)
    assert call(mw, auth=basic("goldpulse", "s3cret-Pass"))[0] == 200
