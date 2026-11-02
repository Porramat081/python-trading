# Algo Trader — Django Algorithmic Trading System

A private Django trading bot that:

- **Backfills market data** from the [Massive](https://massive.com) (formerly Polygon.io) aggregates API into `StockData`.
- **Runs a momentum strategy:** a moving-average crossover (SMA or EMA) computed with pandas.
- **Fully automatic:** adding a strategy backfills its data, evaluates it, switches it ON and enters a position
  if the trend is up. A built-in scheduler keeps every active strategy running during market hours.
- **Places orders on Alpaca** through the [SnapTrade](https://snaptrade.com) API.
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
    run_scheduler.py         # python manage.py run_scheduler  (keeps active strategies running)
    run_strategy.py          # python manage.py run_strategy --symbol AAPL
    backfill_data.py         # python manage.py backfill_data --symbol AAPL --days 365
  templates/trading/         # dashboard.html, positions.html, strategies.html, ...
services/
  massive_service.py         # Massive REST client + fetch_historical_data() backfill
  snaptrade_service.py       # SnapTrade SDK wrapper: register, connect link, balances, positions, execute_order()
  momentum_strategy.py       # SMA/EMA crossover engine -> BUY / SELL / HOLD and order execution
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
| `MASSIVE_API_KEY` | | Massive / Polygon API key |
| `MASSIVE_BASE_URL` | `https://api.massive.com` | Use `https://api.polygon.io` for legacy keys |
| `SNAPTRADE_CLIENT_ID` / `SNAPTRADE_CONSUMER_KEY` | | SnapTrade credentials |
| `SNAPTRADE_KEY_TYPE` | `commercial` | `personal` if you use SnapTrade personal API keys |
| `SNAPTRADE_REDIRECT_URL` | `http://127.0.0.1:8000/accounts/connect/callback/` | Return URL after connecting |
| `TRADING_ENABLED` | `False` | Must be `True` for any order to be sent |
| `LIVE_TRADING` | `False` | `True` connects/permits real-money Alpaca (`ALPACA` instead of `ALPACA-PAPER`) |

## Usage

### 1. Add a strategy

On **Strategies**, type a symbol (e.g. `SPY`, `AAPL`, `MSFT`) and a quantity, then click **Add & start**. The MA
type and windows (default SMA 10/50) and an optional name are under **Advanced**. If you have exactly one linked
account, it is selected automatically.

Adding a strategy automatically:

1. **Backfills** about 2 years of daily candles from Massive.
2. **Evaluates** the signal and the trend (short MA above the long MA = uptrend).
3. **Switches the strategy ON.**
4. **Enters a position (trend-based entry):**
   - If the signal is BUY, or the trend is up and the signal isn't SELL, it **buys** the quantity now. It skips the
     buy if you already hold the symbol.
   - If the signal is SELL, or the trend is down, it **doesn't buy**. It waits for the next BUY crossover.

The same happens when a strategy is added through the Django admin.

### 2. Keep it running: the scheduler

In a second terminal (with the virtual environment activated), run:

```bash
python manage.py run_scheduler
```

During US market hours (Mon–Fri 09:30–16:00 New York time), it refreshes recent candles and runs every **ON**
strategy every 15 minutes. It also does one final run after the close, when the daily candle is complete. Leave it
running next to `runserver`. Options:

```bash
python manage.py run_scheduler --interval 5
```

```bash
python manage.py run_scheduler --once
```

`--ignore-market-hours` runs around the clock. Exchange holidays aren't detected; on those days the runs just
find no new data.

### Manual commands (optional)

```bash
python manage.py backfill_data --symbol AAPL MSFT --days 730
```

```bash
python manage.py run_strategy --symbol AAPL
```

```bash
python manage.py run_strategy --strategy-id 1 --evaluate-only
```

If you'd rather use cron than `run_scheduler`, schedule `run_strategy --all --backfill-days 5`.
On **Strategies**, each row also has **Run** (evaluate now) and an **Auto** switch to turn a strategy on or off.

### Signal logic

On the latest candle:

- **BUY:** the short MA crosses **above** the long MA.
- **SELL:** the short MA crosses **below** the long MA. The order is capped at the units currently held; with no
  position, the bot never shorts.
- **HOLD:** otherwise.

Each crossover candle is acted on only once (`StrategyConfig.last_signal_bar`), so running the command repeatedly
won't duplicate orders. Every attempt is recorded in `TradeLog` with one of these statuses: `PENDING`, `EXECUTED`,
`FAILED`, or `SKIPPED` (dry run / nothing to sell).

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
