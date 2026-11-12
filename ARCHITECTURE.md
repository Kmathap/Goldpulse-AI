# GoldPulse AI – Architecture (v0.2, Phase 1)

GoldPulse AI is an **independent, read-only analysis app** for XAUUSD.
It is not connected to MT5 or to any broker account, so it stays completely
separate from the account you trade with. It never places trades.

Every number comes from a named source with a timestamp. When a source fails
the app shows **DATA UNAVAILABLE** instead of guessing.

---

## 1. System overview

```
   Internet data sources                         YOUR PC
 ┌───────────────────────────┐     ┌──────────────────────────────────────┐
 │ xaus.com (free)           │     │ backend (Python, FastAPI)            │
 │  • /spot  live spot       │◄────┤  data/      providers + cache        │
 │  • /chart OHLC candles    │     │  analysis/  indicators (Phase 1)     │
 │  • /chart DXY (Phase 4)   │     │             trend engine (3), USD(4) │
 ├───────────────────────────┤     │             news scoring (5)         │
 │ Twelve Data (paid, later) │◄────┤  api/       JSON endpoints           │
 ├───────────────────────────┤     │                  │ 127.0.0.1:8010    │
 │ FRED, Finnhub, Fed RSS,   │◄────┤ frontend (Phase 2) dark dashboard    │
 │ ForexFactory calendar,    │     └──────────────────────────────────────┘
 │ Claude API (optional)     │
 └───────────────────────────┘
```

| Layer | Folder | Job | Rule |
|---|---|---|---|
| Providers | `backend/data/*_provider.py` | One outside source each | Return real data or raise `DataUnavailableError`. Never invent values. |
| Helpers | `backend/data/candles.py`, `http.py` | Timestamp parsing, clean candle frames, caching, safe HTTP | Incomplete candles are dropped, never filled in. API keys are masked in errors. |
| Service | `backend/data/market_service.py` | Snapshots, staleness, spot-vs-chart gap | Every output carries source, timestamps and status. |
| Analysis | `backend/analysis/` | Indicators (later: trend engine, scoring) | **Closed candles only** (the forming candle repaints). |
| API | `backend/api/main.py` | FastAPI JSON endpoints | Keys stay on the backend. |
| Frontend | `frontend/` (Phase 2) | Dashboard | Only calls our own API. |

---

## 2. Data providers

Checked November 2026. Switching provider needs only a change in `.env`.

### Price and candles

| | **xaus.com – used now** | **Twelve Data – upgrade later** |
|---|---|---|
| Cost | Free, no key, no account | Grow plan, from about $29/month (the free plan has no XAU/USD) |
| Live price | `/spot` – indicative mid-market spot, refreshed about every 60 s | `/quote` – XAU/USD spot |
| Candles | `/chart`: 5m and 15m (last 5 days), 30m (1 month), 1h (3 months), 1d (2 years). H4 is built from H1 | `/time_series`: all six timeframes, 600 candles each |
| Limits | 60 spot and 20 chart requests per minute; cache at least 60 s (the app caches 60–300 s) | 55+ credits per minute on Grow |
| Caveats | Prices are reference rates, not tradable broker quotes. xaus.com does not say whether the chart series is spot or futures, so the app measures the gap to spot on every refresh and warns above 0.25% | Paid |
| Turn on | default | put `TWELVEDATA_API_KEY` in `.env` |

### Later phases

| Need | Provider | Cost | Phase |
|---|---|---|---|
| DXY (US Dollar Index) | xaus.com `/chart?symbol=dxy` | Free | 4 |
| Treasury yields, Fed funds, CPI, PCE, payrolls, unemployment, GDP | FRED API | Free key | 4 |
| Economic calendar | ForexFactory weekly JSON feed (unofficial, cached) | Free | 5 |
| News | Finnhub market news + Federal Reserve RSS | Free key | 5 |
| News interpretation | Claude API, or a free rule-based classifier | **Paid** if Claude API is used | 5 |

Rejected: MT5 (you want the app separate from your trading account),
NewsAPI.org free plan (24-hour delay, development only), Finnhub economic
calendar (premium), Yahoo Finance directly (unofficial, intermittent gaps).

### Decisions still open (paid items)
1. Twelve Data upgrade – when you want spot-based candles. The code is ready.
2. Claude API for news interpretation (Phase 5) – yes / no.
3. Phone access when the laptop is off needs a small cloud server. With a
   non-MT5 feed this is now possible; decide after Phase 2.

---

## 3. Data quality rules

| Rule | How it is enforced |
|---|---|
| No fake prices | Providers return real data or raise an error → status `DATA_UNAVAILABLE` (HTTP 503) with no numbers. No sample-data path exists in the app. |
| Source + timestamp | Every response has the provider, source, source time (UTC + IST) and fetch time. |
| Stale data | Spot older than `STALE_AFTER_SECONDS` (300) or newest 5-min candle older than `CANDLE_STALE_AFTER_MINUTES` (30) → `STALE` with the reason. The source's own "stale" flag is also respected. |
| Weekend | `MARKET_CLOSED`, last known values clearly marked. |
| Real vs reference | `spot.feed_type` states what kind of price it is. |
| Consistent analysis | Change, daily range, indicators and levels all use one series (the chart series); spot is shown beside it with the measured gap. |
| Indicators on real candles | Closed candles only; `forming_candle_excluded` flag in every response. |
| Not enough history | EMA 200 needs ≥ 250 closed candles, otherwise reported unavailable (e.g. H4 on the free feed has ~150 H4 candles). Under 60 candles → timeframe `DATA_UNAVAILABLE`. |
| AI ≠ fact | (Phase 5) AI output kept in a separate `ai_interpretation` field. |

---

## 4. Folder structure

```
goldpulse-ai/
├── ARCHITECTURE.md
├── README.md                 ← install + run (Windows)
├── requirements.txt
├── .env.example
├── backend/
│   ├── config.py
│   ├── timeutils.py          ← UTC / IST, weekend detection
│   ├── data/
│   │   ├── base.py           ← Quote, CandleSet, timeframes, DataUnavailableError
│   │   ├── http.py           ← safe HTTP (keys masked)
│   │   ├── candles.py        ← parsing, H4 resampling, cache
│   │   ├── xaus_provider.py  ← free feed
│   │   ├── twelvedata_provider.py ← paid upgrade
│   │   └── market_service.py
│   ├── analysis/indicators.py
│   └── api/main.py
├── scripts/check_feed.py     ← Step-1 feed check
├── tests/
└── frontend/                 ← Phase 2
```

---

## 5. API (Phase 1)

| Endpoint | Returns |
|---|---|
| `GET /api/health` | Provider, quote check, candle check |
| `GET /api/price` | Live spot, chart price, gap, daily high/low, previous close, change %, volatility regime, status, warnings |
| `GET /api/candles/{tf}?count=200` | OHLC candles (M5, M15, M30, H1, H4, D1) in UTC + IST, forming flag |
| `GET /api/technicals/{tf}` | EMA20/50/200, RSI14, MACD, ADX/+DI/−DI, Bollinger, ATR14 on closed candles |
| `GET /api/technicals` | All six timeframes |

Interactive docs: `http://127.0.0.1:8010/docs`

---

## 6. Indicator definitions

Written in pandas/NumPy (no TA-Lib) and checked against hand calculations in
the tests. None of them need the candle open, so they work on any OHLC feed.

| Indicator | Definition |
|---|---|
| EMA n | α = 2/(n+1), seeded with the SMA of the first n closes |
| RSI 14 | Wilder smoothing, seeded with the average of the first 14 gains/losses |
| MACD | EMA12 − EMA26; signal EMA9; histogram |
| ATR 14 | Wilder-smoothed true range |
| ADX 14 | Wilder +DM/−DM/TR → +DI, −DI → DX → ADX |
| Bollinger | SMA20 ± 2 × population standard deviation |
| Volatility regime | H1 ATR14 vs its 100-candle median: < 0.8× LOW, > 1.25× HIGH |

---

## 7. Phase plan

| Phase | Delivers | Status |
|---|---|---|
| 1 | Architecture, independent data feed, price snapshot, candles, indicators, API, tests | **this delivery** |
| 2 | Dark dashboard: price card, interactive chart, multi-timeframe table, Asian/London/New York sessions in IST | next |
| 3 | Trend engine: structure (HH/HL, LH/LL), support/resistance, breakouts, multi-timeframe score, confidence, invalidation, no forced signal | |
| 4 | USD analyzer: DXY, FRED yields and macro data, USD strength meter | |
| 5 | News feed, economic calendar (IST), news impact scoring | |
| 6 | Trading analysis panel, educational setups from real levels, alerts | |
| 7 | Optional phone access (cloud) | decision later |
