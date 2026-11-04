# Optimized Day Trading Strategy Specification
**Asset Class:** U.S. Equities  
**Trading Style:** Intraday Trend Following / Momentum  
**Target Timeframes:** Daily (Macro Filter), 5-Minute / 15-Minute (Execution)  

---

## 1. Core Operating Principles

1. **Intraday Scope Only:** No positions are held overnight. All open holdings are forcibly liquidated 15 minutes prior to market close (**3:45 PM ET**).
2. **Dual-Timeframe Alignment:** Daily charts serve as a macro filter (ensuring trades only align with institutional trends), while 5-minute or 15-minute charts generate trade entries and exits.
3. **Strict Position Caps:** Position scaling (pyramiding) is limited to a maximum of **2 entry blocks** per ticker to prevent over-allocation and rapid cash depletion.
4. **Time-Bound Watchlists:** Candidate stocks placed on the `Observe List` without active ownership expire and drop off after **60 minutes**.

---

## 2. Technical Indicators & Parameter Matrix

| Indicator / Layer | Timeframe | Settings | Rule / Purpose |
| :--- | :--- | :--- | :--- |
| **Macro Trend Filter** | Daily Chart | 50-period SMA | **Bullish Baseline:** Stock price must trade above the daily 50 SMA to qualify for long entries. |
| **Execution Trigger** | 5-Min / 15-Min | 9 EMA / 21 EMA | **Cross Signal:** 9 EMA crossing 21 EMA identifies primary intraday momentum shifts. |
| **Momentum Filter** | 5-Min / 15-Min | 14-period RSI | **Range:** 45 – 65 for initial buy entries (filters out overbought conditions > 70). |
| **Confirmation Trigger** | 5-Min / 15-Min | 12, 26, 9 MACD | **Confirmation:** MACD line > Signal line with expanding positive histogram. |

---

## 3. Modular System Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│ CASE 1: Pre-Market & Opening Routine (Daily / Morning Scanning)        │
│                                                                        │
│ 1. Fetch ticker from New Stock List.                                   │
│ 2. Already in Current Holdings? ──► YES ──► [Skip]                    │
│ 3. Check Daily Chart: Price > Daily 50 SMA? ─► NO ──► [Discard Stock]  │
│ 4. Evaluate 5-Min / 15-Min Setup (9/21 EMA + RSI + MACD):              │
│    • BUY SIGNAL (Bullish Cross + RSI 45-65 + MACD Expansion):           │
│      └─► Submit Limit Buy Order ──► If filled ──► Add to Holdings      │
│    • DEVELOPING SETUP (Hold with Uptrend + Weak Momentum):             │
│      └─► Move to Observe List (Set 60-min expiration timer)            │
│    • BEARISH / INVALID SIGNAL:                                         │
│      └─► Discard Ticker immediately                                    │
└────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────┐
│ CASE 2: Current Holdings Monitoring Loop (Run Every 1–5 Minutes)       │
│                                                                        │
│ 1. Iterate over every ticker in Current Holding List:                  │
│ 2. Risk Check: Price ≤ Hard Stop-Loss (-1.5%) OR Time ≥ 3:45 PM ET?     │
│    └─► YES ──► Place Immediate Limit/Market Sell ──► Remove Holding    │
│ 3. Plot 5-Min / 15-Min Indicators:                                     │
│    • SELL SIGNAL (9 EMA crosses below 21 EMA OR MACD Bear Cross):     │
│      └─► Execute Sell Order ──► Remove from Holdings                   │
│    • BUY MORE SIGNAL (Confirmed Bullish Continuation):                 │
│      └─► Position Cap < Max (2 Blocks)? ──► Buy 1 Add-on Block         │
│    • MOMENTUM FADING (Hold with Downtrend / RSI Breakdown):            │
│      └─► Flag position in Observe List for tight exit monitoring       │
└────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────┐
│ CASE 3: Active Observe List Scanning Loop (Run Every 1–5 Minutes)      │
│                                                                        │
│ 1. Iterate over tickers in Observe List:                               │
│ 2. Expiration Check (Unowned Stocks): Age > 60 Minutes?                │
│    └─► YES ──► Remove from Observe List                                │
│ 3. Plot 5-Min / 15-Min Indicators:                                     │
│    • BUY SIGNAL (Unowned):                                             │
│      └─► Place Limit Buy ──► If filled ──► Move to Current Holdings    │
│    • SELL SIGNAL (Owned Holding):                                      │
│      └─► Execute Sell Order ──► Remove from Holdings & Observe List    │
│    • HOLD SIGNAL:                                                      │
│      └─► Retain in Observe List (Subject to 60-min timer if unowned)   │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Execution Workflow & Rules Logic

### Case 1: Pre-Market & New Stock Screener (Daily Execution)
1. **Holding Verification:** Query candidate ticker. If symbol exists in `Current Holding List`, skip.
2. **Macro Filter Verification:** Evaluate **Daily Chart**. If price sits below the 50-day SMA, discard the ticker.
3. **Intraday Signal Analysis (5-Min / 15-Min Chart):**
   * **Buy Trigger:** 9 EMA crosses above 21 EMA **AND** RSI is between 45 and 65 **AND** MACD Histogram is expanding positively.
     * *Execution:* Submit a Limit Buy Order (pegged to `Ask + $0.02`). Upon order confirmation, add ticker to `Current Holding List`, set hard Stop-Loss at -1.5%, and mark position block as `1`.
   * **Developing Setup:** 9 EMA > 21 EMA, but RSI or MACD has not fully expanded.
     * *Execution:* Add ticker to `Observe List` with an entry timestamp.
   * **Bearish / Neutral Signal:** 9 EMA < 21 EMA or MACD is negative.
     * *Execution:* Discard ticker. Do not track non-owned bearish stocks.

---

### Case 2: Current Holdings Monitoring (Intraday Loop: 1–5 Min Intervals)
1. **Safety Controls (Priority Check):**
   * **End-of-Day Liquidation:** If local time reaches **≥ 3:45 PM ET**, execute market sell orders for all open holdings and clear `Current Holding List`.
   * **Hard Stop-Loss:** If stock price touches or breaches the **-1.5% Stop-Loss**, submit an immediate limit/market sell order and remove ticker from holdings.
2. **Technical Signal Evaluation (5-Min / 15-Min Chart):**
   * **Full Exit Signal:** 9 EMA crosses **below** 21 EMA **OR** MACD histogram turns negative.
     * *Execution:* Place Sell Order. Upon fill confirmation, clear ticker from `Current Holding List` and `Observe List`.
   * **Pyramiding (Add-On Buy) Signal:** 9 EMA > 21 EMA **AND** RSI bounces off 50 **AND** MACD expands positively.
     * *Execution:* Verify active position block count. If `Position Blocks < 2`, submit a secondary buy order and set block count to `2`. If cap is reached, maintain existing position without buying.
   * **Momentum Warning:** RSI falls below 40 or price closes below 9 EMA while 9 EMA remains above 21 EMA.
     * *Execution:* Flag ticker in `Observe List` for high-frequency exit monitoring. Retain position until a full bearish cross or stop-loss trigger occurs.

---

### Case 3: Observe List Management (Intraday Loop: 1–5 Min Intervals)
1. **Timer Expiration Rule:**
   * If ticker is **NOT** in `Current Holding List` **AND** `Time on Observe List > 60 minutes`, purge ticker from `Observe List`.
2. **Signal Resolution:**
   * **Buy Trigger (Unowned Ticker):** 9 EMA crosses above 21 EMA with RSI (45–65) and positive MACD confluence.
     * *Execution:* Submit Limit Buy Order. Upon fill, move ticker from `Observe List` to `Current Holding List`.
   * **Sell Trigger (Owned Holding):** Confirmed bearish 9/21 EMA cross or stop-loss breach.
     * *Execution:* Submit Sell Order. Purge ticker from both `Current Holding List` and `Observe List`.
   * **Hold Signal:**
     * *If Owned:* Retain position and monitor inside current loop.
     * *If Unowned:* Maintain inside `Observe List` subject to the 60-minute expiration rule.