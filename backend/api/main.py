"""GoldPulse AI – FastAPI application (Phase 1).

Run from the project folder:
    python -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8010
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from backend import __version__
from backend.config import PROJECT_ROOT, Settings, load_settings
from backend.data.base import TIMEFRAMES, DataUnavailableError
from backend.data.market_service import STATUS_UNAVAILABLE, MarketService


def build_provider(settings: Settings):
    if settings.resolved_provider == "twelvedata":
        from backend.data.twelvedata_provider import TwelveDataProvider

        return TwelveDataProvider(settings)
    from backend.data.xaus_provider import XausProvider

    return XausProvider(settings)


def create_app(service: MarketService | None = None, settings: Settings | None = None,
               start_journal: bool | None = None) -> FastAPI:
    settings = settings or load_settings()
    if start_journal is None:
        start_journal = service is None          # real server: yes; tests: no
    if service is None:
        from backend.data.news_provider import NewsProvider

        service = MarketService(build_provider(settings), settings, news_provider=NewsProvider())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        stop = None
        if start_journal:
            from backend.journal import Journal, start_background

            # one journal per data source, so free-feed and Twelve Data results never mix
            folder = "journal" if settings.resolved_provider == "xaus" else f"journal_{settings.resolved_provider}"
            service.journal = Journal(service.provider, settings.data_dir / folder)
            stop = start_background(service.journal)
        yield
        if stop:
            stop.set()
        service.provider.shutdown()

    app = FastAPI(
        title="GoldPulse AI – Live Gold Market Intelligence",
        version=f"{__version__} (Phase 3)",
        description="Independent XAUUSD market data and indicators (no MT5, no broker connection, no trading).",
        lifespan=lifespan,
    )
    if settings.app_password:
        from backend.security import BasicAuthMiddleware

        app.add_middleware(BasicAuthMiddleware, username=settings.app_username, password=settings.app_password)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    def unavailable(exc: Exception, **extra) -> JSONResponse:
        return JSONResponse({"status": STATUS_UNAVAILABLE, "status_reason": str(exc), **extra}, status_code=503)

    def check_tf(timeframe: str) -> str:
        tf = timeframe.upper()
        if tf not in TIMEFRAMES:
            raise HTTPException(status_code=400, detail=f"timeframe must be one of {list(TIMEFRAMES)}")
        return tf

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    @app.get("/api/health")
    def health():
        body = service.health()
        return JSONResponse(body, status_code=503 if body["status"] == STATUS_UNAVAILABLE else 200)

    @app.get("/api/price")
    def price():
        body = service.price_snapshot()
        return JSONResponse(body, status_code=503 if body["status"] == STATUS_UNAVAILABLE else 200)

    @app.get("/api/candles/{timeframe}")
    def candles(timeframe: str, count: int = Query(200, ge=10, le=2000), ema: bool = False):
        tf = check_tf(timeframe)
        try:
            return service.candles_payload(tf, count, with_ema=ema)
        except DataUnavailableError as exc:
            return unavailable(exc, timeframe=tf)

    @app.get("/api/technicals")
    def technicals_all():
        return service.technicals_all()

    @app.get("/api/technicals/{timeframe}")
    def technicals(timeframe: str):
        tf = check_tf(timeframe)
        try:
            return service.technicals(tf)
        except DataUnavailableError as exc:
            return unavailable(exc, timeframe=tf)

    @app.get("/api/analysis")
    def analysis():
        body = service.analysis()
        return JSONResponse(body, status_code=503 if body["status"] == STATUS_UNAVAILABLE else 200)

    @app.get("/api/journal")
    def journal():
        if service.journal is None:
            return JSONResponse({"status": STATUS_UNAVAILABLE, "status_reason": "Signal journal is not running."}, status_code=503)
        return service.journal.status()

    @app.get("/api/news")
    def news():
        try:
            return service.news_payload()
        except DataUnavailableError as exc:
            return unavailable(exc)

    @app.get("/api/sessions")
    def sessions():
        try:
            return service.sessions_payload()
        except DataUnavailableError as exc:
            return unavailable(exc)

    @app.get("/", include_in_schema=False)
    def dashboard():
        page = PROJECT_ROOT / "frontend" / "index.html"
        if not page.exists():
            raise HTTPException(status_code=404, detail="frontend/index.html is missing")
        return FileResponse(page, headers={"Cache-Control": "no-store"})

    @app.get("/api")
    def api_index():
        return {
            "app": "GoldPulse AI",
            "phase": 3,
            "docs": "/docs",
            "endpoints": ["/api/health", "/api/price", "/api/candles/{tf}", "/api/technicals", "/api/technicals/{tf}", "/api/sessions", "/api/analysis", "/api/news", "/api/journal"],
            "timeframes": list(TIMEFRAMES),
        }

    return app


app = create_app()
