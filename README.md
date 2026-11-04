# Algo Trader — Django Algorithmic Trading System

A private Django trading bot that:

- **Day trades US stocks intraday** following [`docs/day_trading_strategy.md`](docs/day_trading_strategy.md): a daily 50 SMA trend filter, then
  5- or 15-minute 9/21 EMA crosses confirmed by RSI (14) and MACD (12/26/9).
- **Fully automatic:** every morning an **auto screener** (Massive top gainers or most active) builds the
  New Stock List. A loop every 1–5 minutes manages holdings and the Observe List. **Everything is sold by
  3:45 PM ET**, so nothing is held overnight.
- **Places orders on Alpaca** through the [SnapTrade](https://snaptrade.com) API: limit buys at ask + $0.02,
  market sells.
- **Provides a web dashboard** (Django templates + Chart.js) and **Django admin** tools.

> ⚠️ **Safety defaults.** `TRADING_ENABLED=False` (dry run: signals are logged but no orders are sent), and
> `LIVE_TRADING=False` (onboarding connects the Alpaca **paper** account, and orders to real-money accounts are refused).

## Project layout

```text
manage.py
run.py                       # one-command launcher: migrate + web server + scheduler
algo_trader/                 # project config (settings, urls, wsgi/asgi)
trading/                     # main app: models, views, admin, templates, management commands
  management/commands/
    run_scheduler.py         # python manage.py run_scheduler  (the day-trading loop)
    run_daytrade.py          # python manage.py run_daytrade [--scan | --liquidate | --force]
    backfill_data.py         # python manage.py backfill_data --symbol AAPL --days 365
  templates/trading/         # dashboard.html, strategy.html, positions.html
services/
  massive_service.py         # Massive REST client + fetch_historical_data() backfill
  snaptrade_service.py       # SnapTrade SDK wrapper: register, connect link, balances, positions, execute_order()
  indicators.py              # EMA / RSI / MACD / SMA and the spec's signal rules
  daytrader.py               # day-trading engine: screener, holdings, observe list, stops, 3:45 PM liquidation
requirements.txt
.env.example
```

## Setup

```bash
python3 -m venv .venv
```

```bash
source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Edit `.env` to add your keys (`MASSIVE_API_KEY`, `SNAPTRADE_CLIENT_ID`, `SNAPTRADE_CONSUMER_KEY`), then:

```bash
python manage.py migrate
```

```bash
python manage.py createsuperuser
```

```bash
python manage.py runserver
```

**Or start everything with one command.** This runs the migrations, the web server and the scheduler together
(Ctrl+C stops both):

```bash
python run.py
```

Options: `--port 8080`, `--interval 5`, `--ignore-market-hours`, `--no-scheduler`. The script uses `.venv`
automatically, even if it isn't activated.

Open http://127.0.0.1:8000, log in, and click **Connect Alpaca**. This registers you with SnapTrade and sends you
to the SnapTrade Connection Portal. When you finish, SnapTrade redirects you to `/accounts/connect/callback/`,
which syncs your accounts. You can also click **Sync accounts** at any time.

### Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | dev key | Set a real value in production |
| `DJANGO_DEBUG` | `True` | |
| `DJANGO_ALLOWED_HOSTS` | `127.0.0.1,localhost` | Comma-separated |
| `DATABASE_URL` | *(empty → SQLite)* | `postgres://user:pass@host:5432/db` (requires `pip install "psycopg[binary]"`) |
| `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` | | Alpaca market data keys (default data provider) |
| `ALPACA_DATA_FEED` | `iex` | `sip` (full market) needs a paid Alpaca data plan |
| `MASSIVE_API_KEY` | | Massive / Polygon API key (optional provider) |
| `MASSIVE_BASE_URL` | `https://api.massive.com` | Use `https://api.polygon.io` for legacy keys |
| `SNAPTRADE_CLIENT_ID` / `SNAPTRADE_CONSUMER_KEY` | | SnapTrade credentials |
| `SNAPTRADE_KEY_TYPE` | `commercial` | `personal` if you use SnapTrade personal API keys |
| `SNAPTRADE_REDIRECT_URL` | `http://127.0.0.1:8000/accounts/connect/callback/` | Return URL after connecting |
| `TRADING_ENABLED` | `False` | Must be `True` for any order to be sent |
| `LIVE_TRADING` | `False` | `True` connects/permits real-money Alpaca (`ALPACA` instead of `ALPACA-PAPER`) |

## Usage: day trading

> **Market data:** by default the bot uses **Alpaca's free market data**: real-time prices from the IEX
> exchange, 200 requests/min, with bars fetched for many tickers in one request. Add your Alpaca API keys to
> `.env` (`ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`; paper keys work). Orders still go through SnapTrade.
> **Massive** is still available as an alternative on the Strategy page, but it needs a paid plan for
> intraday data and the screener.
>
> **IEX caveat:** IEX is one exchange (about 2–3% of US volume). Prices track the full market closely, but
> thinly traded stocks can have missing 5-minute bars, and indicators can differ slightly from charts based on
> the full market.
>
> ⚠️ **Pattern day trader rule:** US margin accounts with less than $25,000 are limited to 3 day trades in 5
> business days. Alpaca enforces this, including on paper accounts.

### 1. Configure the strategy

Open **Strategy** in the web UI and set the options. The defaults follow the spec:

| Setting | Default | Meaning |
|---|---|---|
| Account | none | Alpaca account for orders. With none (or `TRADING_ENABLED=False`) all fills are **simulated** |
| Market data | Alpaca (free IEX) | Or Massive (paid plan) |
| Chart timeframe | 5-minute | Execution timeframe (5 or 15 min) |
| Loop every | 2 min | Monitoring loop interval (1–5 min) |
| Block size | $1,000 | Dollars per entry block; shares = block ÷ limit price, rounded down |
| Max blocks / ticker | 2 | Pyramiding cap |
| Max open positions | 5 | Tickers held at once (protects cash) |
| Stop-loss | 1.5% | Hard stop below the average entry price |
| Limit = ask + $ | 0.02 | Limit buy price |
| Cancel unfilled buy after | 5 min | Unfilled limit buys are cancelled |
| Liquidate at | 15:45 ET | Sell everything; no new entries after this |
| Observe expiry | 60 min | Unowned Observe List entries drop off |
| Screener | Top gainers | Or **Most active** (by volume), or **My watchlist only**. Filters: price $5–$500, volume ≥ 1M, top 10 |
| Watchlist | empty | Your tickers. Used when chosen as the screener, and **automatically as a fallback** if the screener fails or returns nothing. Price/volume filters don't apply |

Click **Test data** first. It checks 5-minute bars, daily bars, a quote and both screeners with your keys, and
shows which ones your plan allows. Then click **Turn auto ON**. **Scan now**, **Run loop now** and
**Close all now** are manual overrides.

With Alpaca data, the **top gainers** list has no volume figures, so volume comes from Alpaca's top 100 most
active list. With a minimum volume set, only gainers that are also among the most active pass. That's a
liquidity guard: set **Min volume** to 0 to allow all gainers.

### 2. Run it

```bash
python run.py
```

This starts the web server plus `run_scheduler`, which runs the loop every *N* minutes during market hours
(09:30–16:00 ET) while auto is ON. It wakes up exactly at the liquidation time.

### What each loop does

1. **Liquidation (at or after 3:45 PM ET):** cancels pending buys, market-sells every bot position, and clears
   the Observe List.
2. **Pending orders:** applies broker fills. Limit buys not filled within the timeout are cancelled.
3. **Case 2, current holdings:**
   - **Stop-loss:** price ≤ entry − 1.5% → market sell.
   - **Exit:** 9 EMA crosses below 21 EMA, or the MACD histogram turns negative → market sell.
   - **Add-on (pyramiding):** 9 EMA > 21 EMA, RSI bounced off 50, MACD expanding, and fewer than 2 blocks
     held → buy 1 more block.
   - **Momentum warning:** RSI < 40, or price closed below the 9 EMA in an uptrend → flagged on the Observe
     List. The position is kept until an exit or stop.
4. **Case 3, Observe List:**
   - Unowned entries older than 60 min are removed.
   - Otherwise a **confirmation buy** fires when 9 EMA > 21 EMA, RSI is 45–65 and the MACD histogram is
     positive and growing.
5. **Case 1, morning screener (first loop of the day, or Scan now):** for each candidate:
   - **Skip** if already held (bot positions and your Alpaca holdings).
   - **Discard** if the price is below the daily 50 SMA.
   - **Buy** if 9/21 EMA bullish cross + RSI 45–65 + MACD expanding.
   - **Observe** if uptrend but momentum isn't confirmed yet.
   - **Discard** if bearish.

Signals use **closed bars only**, and only regular-session bars (09:30–16:00 ET). Stops use the latest price,
so they're checked once per loop: price can move past the stop between loops.

### Dry run vs. real orders

| `TRADING_ENABLED` | Account set | Behaviour |
|---|---|---|
| `False` (default) | any | **Simulated**: buys fill at the limit price, sells at the last price. Shown as "Simulated (dry run)" |
| `True` | Alpaca paper | Real orders to your **paper** account |
| `True` + `LIVE_TRADING=True` | Alpaca live | Real money |

### Manual commands

```bash
python manage.py run_daytrade --scan
```

```bash
python manage.py run_daytrade --force
```

```bash
python manage.py run_daytrade --liquidate
```

`--force` runs one full loop even when the market is closed.

### Time zone

All times in the app are shown in **US Eastern time** (New York, the stock market's own time zone). This
covers the trade log, "last checked" times, chart dates, the admin and log lines. Daylight saving (EST/EDT) is
handled automatically. The database still stores UTC. To use another zone, set `DJANGO_TIME_ZONE` in `.env`.

The bar at the top of every page shows a live **US market clock**: the New York time, and whether the market
is open, in pre-market (04:00–09:30), after hours (16:00–20:00) or closed, with a countdown to the next open or
close. Exchange holidays aren't included.

### Error handling

- **Massive:** retries on HTTP 429 and 5xx, honouring `Retry-After` with exponential backoff, and follows `next_url`
  pagination.
- **SnapTrade:** retries read calls on 429 and 5xx with backoff and jitter. Orders are retried **only on 429**, so a
  server error can never cause a duplicate fill. Brokerage rejections are logged as `FAILED` with the broker's message.

## Tests

```bash
python manage.py test trading
```

All external APIs are mocked in the tests.

## Notes

- SnapTrade user secrets are stored in the database (`BrokerageAccount.snaptrade_user_secret`). Keep the database
  private, or add field-level encryption before deploying anywhere shared.
- The dashboard loads Chart.js from the jsDelivr CDN.
