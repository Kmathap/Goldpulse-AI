# GoldPulse AI – Live Gold Market Intelligence (Phase 2)

Independent XAUUSD analysis backend. It does **not** use MT5 or any broker
account, so it stays separate from the account you trade with. It never places
trades.

* **Free now:** xaus.com public gold API (no key, no account)
* **Upgrade later:** paste a Twelve Data key into `.env`; nothing else changes

See `ARCHITECTURE.md` for the design, data sources and phase plan.

---

## What you need
* Windows PC with internet
* Python 3.13 or 3.14 (you already have 3.14)
* About 50 MB of disk space

---

## Step 1 – Put the project in place
1. Unzip `goldpulse-ai.zip` to `C:\Users\HP\goldpulse-ai`
2. Open **Command Prompt**:
   ```
   cd C:\Users\HP\goldpulse-ai
   ```

## Step 2 – Install packages
```
python -m pip install -r requirements.txt
```

## Step 3 – Create your settings file
```
copy .env.example .env
```
Nothing needs editing for the free feed.

## Step 4 – Check the data feed
```
python scripts\check_feed.py
```
Expected ending: `RESULT: OK – the gold data feed is reachable.`
It prints the spot price, the candle count for each timeframe, and the gap
between the chart series and live spot.

## Step 5 – Start the API
```
python -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8010
```
Leave this window open, then open in your browser:

* **http://127.0.0.1:8010 – the dashboard** (price card, chart, indicators, sessions; refreshes itself)
* http://127.0.0.1:8010/api/price – live price snapshot
* http://127.0.0.1:8010/api/technicals – indicators for all six timeframes
* http://127.0.0.1:8010/docs – clickable API explorer

Stop with **Ctrl + C**.

## Step 6 – Run the tests (optional)
```
python -m pytest -q
```
Expected: `44 passed`. The tests use fake responses stored only in the `tests`
folder; the app itself has no fake-data path.

---

## Reading `/api/price`

| Field | Meaning |
|---|---|
| `status` | `LIVE`, `STALE` (with reason), `MARKET_CLOSED` (weekend, last values shown), or `DATA_UNAVAILABLE` (no numbers shown) |
| `price` | Live spot (xaus.com indicative mid-market rate, refreshed about every 60 s) |
| `spot.time_ist`, `spot.age_seconds` | When that price was valid and how old it is |
| `chart.price` | Latest close of the candle series used for all analysis |
| `chart.basis_vs_spot` | Gap between the chart series and spot. A gap above 0.25% is flagged because the series may be futures-based |
| `daily_high` / `daily_low` / `previous_close` / `change` | From the chart series' daily candle |
| `volatility.regime` | LOW / NORMAL / HIGH: H1 ATR14 vs its 100-hour median |
| `warnings` | Anything you should know about data quality |
| `trend_direction` | `null` until the trend engine is built in Phase 3, so no fake signal is shown |

## Dashboard panels (Phase 2)

| Panel | Shows |
|---|---|
| Header | Status (LIVE / STALE / MARKET CLOSED / DATA UNAVAILABLE), last update in IST, provider |
| Warnings | Chart delay and chart-vs-spot gap, whenever they apply |
| Price card | Live spot; chart-series price and its change; day high/low, previous close (chart series); volatility, ATR, spot age |
| Chart | Candles for M5–D1 in IST with EMA 20/50/200 and a dashed live-spot line |
| Indicator table | Six timeframes, closed candles only: price vs EMAs, RSI, MACD histogram, ADX, +DI/−DI, Bollinger %B, ATR |
| Sessions | Asian (Tokyo), London, New York: IST hours (adjusted for daylight saving), open/high/low/last, range, move |

Refresh: price every 30 s, chart, indicators and sessions every 60 s.

## Upgrading to Twelve Data later
1. Buy the Grow plan (or higher) at twelvedata.com and copy your API key
2. In `.env` set `TWELVEDATA_API_KEY=your_key`
3. Restart the API. `/api/health` will show `Twelve Data (paid plan)`

## Troubleshooting

| Message | Fix |
|---|---|
| `Network error` | Check the internet connection / firewall |
| `Rate limit reached` | Wait a minute; the app caches data so this should be rare |
| `unexpected format (keys: …)` | xaus.com changed its response. Send the full message to Claude |
| `Twelve Data … Grow plan` | Your Twelve Data plan does not include XAU/USD |
| `MARKET_CLOSED` | Normal at weekends; gold reopens Monday ~03:30–04:30 IST |
| Port 8010 in use | Change `API_PORT` in `.env` and use the same port in the uvicorn command |
