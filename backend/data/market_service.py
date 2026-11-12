"""Combines provider data into dashboard-ready, source-stamped snapshots.

Statuses:
  LIVE              fresh data
  STALE             data is older than the limits in .env (reason given)
  MARKET_CLOSED     weekend closure – last known values shown, clearly marked
  DATA_UNAVAILABLE  no real data could be fetched – no numbers are shown
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta
from typing import Any, Callable

import pandas as pd

from backend.analysis import indicators as ind
from backend.analysis.sessions import build_sessions
from backend.analysis import trend_engine as te
from backend.analysis.news_scoring import build_news
from backend.config import Settings
from backend.data.base import (
    TIMEFRAME_SECONDS,
    TIMEFRAMES,
    CandleSet,
    DataUnavailableError,
    MarketDataProvider,
)
from backend.timeutils import from_epoch_utc, iso, ist_label, to_ist, weekend_closure_reason

STATUS_LIVE = "LIVE"
STATUS_STALE = "STALE"
STATUS_CLOSED = "MARKET_CLOSED"
STATUS_UNAVAILABLE = "DATA_UNAVAILABLE"
PRICE_DIGITS = 2
DELAYED_AFTER_MINUTES = 5   # chart series older than this is labelled "delayed"


def clean_number(value: Any, ndigits: int | None = None) -> float | None:
    """Convert numpy/pandas numbers to plain floats; NaN/inf -> None."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, ndigits) if ndigits is not None else f


class MarketService:
    def __init__(
        self,
        provider: MarketDataProvider,
        settings: Settings,
        clock: Callable[[], float] = time.time,
        news_provider: Any = None,
    ) -> None:
        self._news = news_provider
        self.journal = None          # set by the API (backend.journal.Journal)
        self._provider = provider
        self._settings = settings
        self._clock = clock

    @property
    def provider(self) -> MarketDataProvider:
        return self._provider

    # ------------------------------------------------------------------ #
    def _now(self) -> datetime:
        return from_epoch_utc(self._clock())

    def _split_forming(self, cs: CandleSet) -> tuple[pd.DataFrame, pd.Series | None]:
        """Return (closed candles, forming candle or None).

        A candle counts as closed only when the DATA has reached its end time.
        With a delayed feed, the clock can pass a candle's end while the source
        has only sent part of it – that candle is still incomplete.
        """
        df = cs.df
        if df.empty:
            return df, None
        reference = pd.Timestamp(self._now())
        if cs.last_point_utc is not None:
            reference = min(reference, pd.Timestamp(cs.last_point_utc))
        close_time = df.index[-1] + pd.Timedelta(seconds=TIMEFRAME_SECONDS[cs.timeframe])
        if close_time > reference:
            return df.iloc[:-1], df.iloc[-1]
        return df, None

    def _stamp(self) -> dict[str, Any]:
        now = self._now()
        info = self._provider.info()
        return {
            "provider": info.name,
            "provider_cost": info.cost,
            "fetched_at_utc": iso(now),
            "fetched_at_ist": ist_label(now),
        }

    # ------------------------------------------------------------------ #
    def health(self) -> dict[str, Any]:
        info = self._provider.info()
        out: dict[str, Any] = {**self._stamp(), "symbol": info.symbol, "notes": list(info.notes)}
        checks = {}
        try:
            q = self._provider.get_quote()
            checks["quote"] = {"ok": True, "time_utc": iso(q.time_utc), "source_state": q.source_state}
        except DataUnavailableError as exc:
            checks["quote"] = {"ok": False, "reason": str(exc)}
        try:
            cs = self._provider.get_candles("M5")
            checks["candles_m5"] = {"ok": True, "count": len(cs.df), "last_open_utc": iso(cs.df.index[-1].to_pydatetime())}
        except DataUnavailableError as exc:
            checks["candles_m5"] = {"ok": False, "reason": str(exc)}
        out["checks"] = checks
        out["status"] = "OK" if all(c["ok"] for c in checks.values()) else STATUS_UNAVAILABLE
        return out

    # ------------------------------------------------------------------ #
    def price_snapshot(self) -> dict[str, Any]:
        now = self._now()
        base = {
            **self._stamp(),
            "symbol": "XAUUSD",
            "trend_direction": None,
            "trend_note": "The bullish/bearish trend engine is built in Phase 3.",
        }
        warnings: list[str] = []
        try:
            m5 = self._provider.get_candles("M5")
            d1 = self._provider.get_candles("D1")
            h1 = self._provider.get_candles("H1")
        except DataUnavailableError as exc:
            return {**base, "status": STATUS_UNAVAILABLE, "status_reason": str(exc)}

        try:
            quote = self._provider.get_quote()
        except DataUnavailableError as exc:
            quote = None
            warnings.append(f"Live spot price unavailable ({exc}); showing the chart series price only.")

        # --- chart series (all analysis uses this one consistent series) ---
        last_bar_open = m5.df.index[-1].to_pydatetime()
        chart_price = float(m5.df["close"].iloc[-1])
        last_point = m5.last_point_utc or (last_bar_open + timedelta(minutes=5))
        chart_delay_min = max((now - last_point).total_seconds() / 60, 0.0)
        candle_age_min = chart_delay_min
        if chart_delay_min > DELAYED_AFTER_MINUTES:
            chart_feed = f"delayed (newest point about {round(chart_delay_min)} min old)"
        else:
            chart_feed = "near real-time"

        d1_closed, d1_forming = self._split_forming(d1)
        prev_close = float(d1_closed["close"].iloc[-1]) if len(d1_closed) else None
        if d1_forming is not None:
            day_high = max(float(d1_forming["high"]), chart_price)
            day_low = min(float(d1_forming["low"]), chart_price)
            day_start = d1.df.index[-1].to_pydatetime()
        else:
            day_high = day_low = day_start = None
        change = chart_price - prev_close if prev_close is not None else None
        change_pct = change / prev_close * 100 if prev_close else None

        # --- spot vs chart series gap ------------------------------------
        basis = basis_pct = None
        if quote is not None:
            basis = chart_price - quote.price
            basis_pct = basis / quote.price * 100
            if abs(basis_pct) > self._settings.basis_warning_pct:
                warnings.append(
                    f"The chart series is {basis:+.2f} USD ({basis_pct:+.2f}%) away from live spot – it may be "
                    "futures-based. Levels and indicators follow the chart series."
                )

        if chart_delay_min > DELAYED_AFTER_MINUTES:
            warnings.append(
                f"The chart series is delayed by about {round(chart_delay_min)} minutes (newest point "
                f"{ist_label(last_point)}). Candles and indicators reflect data up to that time."
            )

        # --- status ------------------------------------------------------
        closure = weekend_closure_reason(now)
        quote_age = (now - quote.time_utc).total_seconds() if quote else None
        reasons = []
        if quote is not None and quote_age is not None and quote_age > self._settings.stale_after_seconds:
            reasons.append(f"spot quote is {int(quote_age)} s old")
        if quote is not None and quote.source_state == "stale":
            reasons.append("the source marks its spot price as stale")
        if candle_age_min > self._settings.candle_stale_after_minutes:
            reasons.append(f"newest chart data point is {int(candle_age_min)} min old")

        if closure and closure.startswith("Weekend"):
            status, status_reason = STATUS_CLOSED, closure + " Showing the last known values."
        elif reasons:
            status = STATUS_STALE
            status_reason = "; ".join(reasons).capitalize() + "." + (f" {closure}" if closure else "")
        else:
            status, status_reason = STATUS_LIVE, None

        h1_closed, _ = self._split_forming(h1)
        atr_h1 = ind.atr(h1_closed["high"], h1_closed["low"], h1_closed["close"], 14)
        vol = ind.volatility_regime(atr_h1)
        atr_last = clean_number(atr_h1.iloc[-1]) if len(atr_h1) else None
        r = lambda x: clean_number(x, PRICE_DIGITS)  # noqa: E731

        return {
            **base,
            "status": status,
            "status_reason": status_reason,
            "warnings": warnings,
            "price": r(quote.price if quote else chart_price),
            "price_basis": "live spot" if quote else "chart series (spot unavailable)",
            "spot": {
                "price": r(quote.price) if quote else None,
                "source": quote.source if quote else None,
                "feed_type": quote.feed_type if quote else None,
                "source_state": quote.source_state if quote else None,
                "time_utc": iso(quote.time_utc) if quote else None,
                "time_ist": ist_label(quote.time_utc) if quote else None,
                "age_seconds": round(quote_age, 1) if quote_age is not None else None,
            },
            "chart": {
                "price": r(chart_price),
                "source": m5.source,
                "feed": chart_feed,
                "delay_minutes": round(chart_delay_min, 1),
                "last_point_utc": iso(last_point),
                "last_point_ist": ist_label(last_point),
                "last_5m_candle_open_utc": iso(last_bar_open),
                "last_5m_candle_open_ist": ist_label(last_bar_open),
                "basis_vs_spot": r(basis),
                "basis_vs_spot_pct": clean_number(basis_pct, 3),
            },
            "daily_high": r(day_high),
            "daily_low": r(day_low),
            "previous_close": r(prev_close),
            "change": r(change),
            "change_pct": clean_number(change_pct, 3),
            "daily_basis": "chart series D1 candle (change = chart price vs previous D1 close)",
            "trading_day_start_utc": iso(day_start),
            "trading_day_start_ist": ist_label(day_start),
            "volatility": {
                "atr14_h1": r(atr_last),
                "atr14_h1_pct_of_price": clean_number(atr_last / chart_price * 100, 3) if atr_last else None,
                "regime": vol["regime"],
                "atr_vs_100h_median": clean_number(vol["ratio_to_median"], 2),
                "method": "H1 ATR14 compared with its median over the last 100 closed H1 candles",
            },
            "stale_after_seconds": self._settings.stale_after_seconds,
        }

    # ------------------------------------------------------------------ #
    def sessions_payload(self) -> dict[str, Any]:
        cs = self._provider.get_candles("M5")
        now = self._now()
        return {
            **self._stamp(),
            "status": STATUS_LIVE,
            "source": cs.source,
            "basis": "5-minute candles of the chart series; session hours follow local time in Tokyo, London and New York",
            "data_until_ist": ist_label(cs.last_point_utc) if cs.last_point_utc else None,
            "sessions": build_sessions(cs.df, now, cs.last_point_utc),
        }

    def candles_payload(self, timeframe: str, count: int, with_ema: bool = False) -> dict[str, Any]:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe {timeframe!r}")
        cs = self._provider.get_candles(timeframe)
        closed, forming = self._split_forming(cs)
        emas: dict[str, pd.Series] = {}
        if with_ema:
            # EMAs from closed candles only; the forming candle gets no EMA value
            for n in (20, 50, 200):
                if n == 200 and len(closed) < ind.EMA200_MIN_BARS:
                    continue
                emas[f"ema{n}"] = ind.ema(closed["close"], n)
        tail = cs.df.iloc[-count:]
        rows = []
        for ts, row in tail.iterrows():
            dt = ts.to_pydatetime()
            extra = {k: clean_number(s.get(ts), PRICE_DIGITS) for k, s in emas.items()}
            rows.append(
                {
                    "time_utc": iso(dt),
                    "time_ist": iso(to_ist(dt)),
                    "epoch": int(dt.timestamp()),
                    "open": clean_number(row["open"], PRICE_DIGITS),
                    "high": clean_number(row["high"], PRICE_DIGITS),
                    "low": clean_number(row["low"], PRICE_DIGITS),
                    "close": clean_number(row["close"], PRICE_DIGITS),
                    "volume": clean_number(row["volume"]),
                    "forming": forming is not None and ts == cs.df.index[-1],
                    **extra,
                }
            )
        return {
            **self._stamp(),
            "status": STATUS_LIVE,
            "timeframe": timeframe,
            "source": cs.source,
            "notes": list(cs.notes),
            "count": len(rows),
            "candle_time_basis": "candle OPEN time in UTC (IST also given)",
            "candles": rows,
        }

    # ------------------------------------------------------------------ #
    def technicals(self, timeframe: str) -> dict[str, Any]:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe {timeframe!r}")
        cs = self._provider.get_candles(timeframe)
        closed, forming = self._split_forming(cs)
        if len(closed) < 60:
            raise DataUnavailableError(
                f"Only {len(closed)} closed {timeframe} candles available – not enough for indicators."
            )
        data = ind.compute_all(closed)
        last = data.iloc[-1]
        bar_open = data.index[-1].to_pydatetime()
        bar_close = bar_open + timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
        close = float(last["close"])

        def rel(col: str) -> str | None:
            v = last[col]
            if pd.isna(v):
                return None
            return "above" if close > v else "below" if close < v else "at"

        n = lambda col, nd=PRICE_DIGITS: clean_number(last[col], nd)  # noqa: E731
        return {
            **self._stamp(),
            "status": STATUS_LIVE,
            "timeframe": timeframe,
            "source": cs.source,
            "notes": list(cs.notes),
            "computed_on": "closed candles only",
            "forming_candle_excluded": forming is not None,
            "bars_used": int(len(closed)),
            "last_closed_candle_open_utc": iso(bar_open),
            "last_closed_candle_close_utc": iso(bar_close),
            "last_closed_candle_close_ist": ist_label(bar_close),
            "close": clean_number(close, PRICE_DIGITS),
            "ema20": n("ema20"),
            "ema50": n("ema50"),
            "ema200": n("ema200"),
            "ema200_note": None if not pd.isna(last["ema200"]) else f"needs at least {ind.EMA200_MIN_BARS} closed candles",
            "close_vs_ema20": rel("ema20"),
            "close_vs_ema50": rel("ema50"),
            "close_vs_ema200": rel("ema200"),
            "rsi14": n("rsi14", 2),
            "macd": n("macd", 3),
            "macd_signal": n("macd_signal", 3),
            "macd_hist": n("macd_hist", 3),
            "adx14": n("adx", 2),
            "plus_di14": n("plus_di", 2),
            "minus_di14": n("minus_di", 2),
            "bb_upper": n("bb_upper"),
            "bb_mid": n("bb_mid"),
            "bb_lower": n("bb_lower"),
            "bb_width_pct": n("bb_width_pct", 3),
            "bb_pct_b": n("bb_pct_b", 3),
            "atr14": n("atr14"),
        }

    def technicals_all(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for tf in TIMEFRAMES:
            try:
                out[tf] = self.technicals(tf)
            except DataUnavailableError as exc:
                out[tf] = {"status": STATUS_UNAVAILABLE, "timeframe": tf, "status_reason": str(exc)}
        return {**self._stamp(), "symbol": "XAUUSD", "timeframes": out}

    # ------------------------------------------------------------------ #
    def news_payload(self) -> dict[str, Any]:
        if self._news is None:
            raise DataUnavailableError("News is not configured.")
        items, errors = self._news.fetch()
        now = self._now()
        return {
            **{k: v for k, v in self._stamp().items() if not k.startswith("provider")},
            "status": STATUS_LIVE,
            "sources": "Google News RSS (many publishers) + Federal Reserve press releases (official RSS)",
            "refresh": self._news.schedule() if hasattr(self._news, "schedule") else None,
            "feed_errors": errors,
            **build_news(items, now),
        }

    def _backtest_status(self) -> dict[str, Any]:
        from backend.backtest import latest_status
        from backend.config import PROJECT_ROOT

        return latest_status(PROJECT_ROOT / "backtest_results")

    # ------------------------------------------------------------------ #
    def analysis(self) -> dict[str, Any]:
        """Phase 3: multi-timeframe verdict, levels, invalidation and educational trade plan."""
        snap = self.price_snapshot()
        if snap["status"] == STATUS_UNAVAILABLE:
            return {**self._stamp(), "status": STATUS_UNAVAILABLE, "status_reason": snap.get("status_reason")}
        tfs: dict[str, dict | None] = {}
        problems = {}
        for tf in TIMEFRAMES:
            try:
                cs = self._provider.get_candles(tf)
                closed, _ = self._split_forming(cs)
                if len(closed) < 60:
                    raise DataUnavailableError(f"only {len(closed)} closed candles")
                tfs[tf] = te.evaluate_timeframe(tf, ind.compute_all(closed))
            except DataUnavailableError as exc:
                tfs[tf] = None
                problems[tf] = str(exc)
        if not tfs.get("H1"):
            return {**self._stamp(), "status": STATUS_UNAVAILABLE,
                    "status_reason": f"H1 data is needed for the analysis: {problems.get('H1')}"}
        verdict = te.combine({k: v for k, v in tfs.items() if v})
        scen = te.invalidation_and_scenarios(verdict, tfs["H1"], tfs.get("H4"))
        price = snap["chart"]["price"]
        from backend.strategy_lab import ACTIVE_KEY, get_variant, live_params
        plan = te.trade_plan(verdict, tfs["H1"], price, snap["status"], snap["chart"]["basis_vs_spot"],
                             snap["chart"].get("delay_minutes"), params=live_params())
        plan["strategy"] = get_variant(ACTIVE_KEY).name
        news_summary = None
        try:
            n = self.news_payload()
            news_summary = {**n["fundamental_sentiment"], "recent_high_impact": n["recent_high_impact"]}
            for h in n["recent_high_impact"]:
                plan["avoid_when"].insert(0, f"HIGH-IMPACT NEWS {h['minutes_ago']} min ago: \"{h['title']}\" – wait at least 30 min after major news.")
            if n["recent_high_impact"] and plan["action"] != "NO TRADE":
                plan["timing"] = "High-impact news just broke – wait 30 minutes for the reaction to settle. " + (plan.get("timing") or "")
        except DataUnavailableError as exc:
            news_summary = {"label": "NO DATA", "error": str(exc)}
        return {
            **self._stamp(),
            "status": snap["status"],
            "status_reason": snap.get("status_reason"),
            "engine": "Rules-based trend engine (EMA, market structure, ADX/DI, MACD, RSI across 6 timeframes)",
            "price_basis": "All levels are on the chart series; spot-equivalent levels subtract the measured gap.",
            "chart_price": price,
            "spot_price": snap["spot"]["price"],
            "chart_vs_spot": snap["chart"]["basis_vs_spot"],
            "chart_delay_minutes": snap["chart"].get("delay_minutes"),
            "verdict": verdict,
            **scen,
            "plan": plan,
            "fundamental_sentiment": news_summary,
            "backtest": self._backtest_status(),
            "journal": self.journal.status()["current"] if self.journal else None,
            "timeframes": tfs,
            "unavailable_timeframes": problems,
            "disclaimer": "Educational analysis from public market data. Not financial advice; no outcome is guaranteed.",
        }
