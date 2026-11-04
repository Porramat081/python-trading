"""Technical indicators and the day-trading signal rules from the strategy spec.

Execution timeframe (5-min / 15-min bars):
    9 EMA / 21 EMA cross, 14-period RSI (Wilder), MACD 12/26/9.
Macro filter (daily bars):
    price must be above the 50-period SMA.

All rules are evaluated on the latest *closed* bar.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

EMA_FAST, EMA_SLOW = 9, 21
RSI_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
DAILY_SMA = 50
MIN_BARS = MACD_SLOW + MACD_SIGNAL + 2  # enough history for a stable MACD histogram


# -- indicator maths ---------------------------------------------------------------
def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Wilder's RSI."""
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss
    out = 100 - (100 / (1 + rs))
    out = out.where(loss != 0, 100.0)            # only gains -> 100
    return out.where(~((gain == 0) & (loss == 0)), 50.0)  # no movement -> neutral


def macd(series: pd.Series, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL) -> pd.DataFrame:
    line = series.ewm(span=fast, adjust=False).mean() - series.ewm(span=slow, adjust=False).mean()
    sig = line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def compute_frame(closes: pd.Series) -> pd.DataFrame:
    """All execution-timeframe indicators for a series of closes."""
    df = pd.DataFrame({"close": closes.astype(float)})
    df["ema_fast"] = ema(df["close"], EMA_FAST)
    df["ema_slow"] = ema(df["close"], EMA_SLOW)
    df["rsi"] = rsi(df["close"])
    df = df.join(macd(df["close"]))
    return df


# -- signal rules --------------------------------------------------------------------
@dataclass
class Snapshot:
    """Indicator values on the latest closed bar plus the rule outcomes."""

    close: float
    ema_fast: float
    ema_slow: float
    rsi: float
    rsi_prev: float
    macd: float
    macd_hist: float
    macd_hist_prev: float
    cross_up: bool          # 9 EMA crossed above 21 EMA on this bar
    cross_down: bool        # 9 EMA crossed below 21 EMA on this bar
    rsi_recent_low: float   # lowest RSI over the previous 3 bars (for the "bounce off 50" rule)

    # -- building blocks --
    @property
    def uptrend(self) -> bool:
        return self.ema_fast > self.ema_slow

    @property
    def rsi_in_range(self) -> bool:
        return 45 <= self.rsi <= 65

    @property
    def macd_expanding(self) -> bool:
        """Positive MACD histogram that is growing."""
        return self.macd_hist > 0 and self.macd_hist > self.macd_hist_prev

    # -- spec rules --
    @property
    def buy_signal(self) -> bool:
        """Case 1 buy: bullish 9/21 cross AND RSI 45-65 AND MACD histogram expanding positively."""
        return self.cross_up and self.rsi_in_range and self.macd_expanding

    @property
    def confirmation_buy(self) -> bool:
        """Observe-list buy: uptrend already in place and momentum has now confirmed."""
        return self.uptrend and self.rsi_in_range and self.macd_expanding

    @property
    def developing(self) -> bool:
        """Uptrend, MACD not negative, but RSI/MACD not fully confirmed yet."""
        return self.uptrend and self.macd_hist >= 0 and not self.buy_signal

    @property
    def bearish(self) -> bool:
        """9 EMA below 21 EMA, or MACD (histogram) negative."""
        return (not self.uptrend) or self.macd_hist < 0

    @property
    def exit_signal(self) -> bool:
        """Case 2 full exit: 9 EMA crosses below 21 EMA OR MACD histogram negative."""
        return self.cross_down or self.macd_hist < 0

    @property
    def add_on_signal(self) -> bool:
        """Pyramiding: uptrend AND RSI bounced off 50 AND MACD expanding positively."""
        bounced = self.rsi_recent_low <= 52 and self.rsi > 50 and self.rsi > self.rsi_prev
        return self.uptrend and bounced and self.macd_expanding

    @property
    def momentum_warning(self) -> bool:
        """RSI below 40, or price closed below the 9 EMA, while still in an uptrend."""
        return self.uptrend and (self.rsi < 40 or self.close < self.ema_fast)

    def classify_new(self) -> str:
        """Case 1 outcome for a not-owned candidate: BUY / OBSERVE / DISCARD."""
        if self.buy_signal:
            return "BUY"
        if self.developing:
            return "OBSERVE"
        return "DISCARD"

    def describe(self) -> str:
        return (
            f"close {self.close:.2f} · EMA9 {self.ema_fast:.2f} / EMA21 {self.ema_slow:.2f} · "
            f"RSI {self.rsi:.1f} · MACD hist {self.macd_hist:+.3f}"
        )


def snapshot(closes: pd.Series) -> Snapshot | None:
    """Evaluate the rules on the last value of `closes` (which must be closed bars only)."""
    closes = closes.dropna()
    if len(closes) < MIN_BARS:
        return None
    df = compute_frame(closes)
    last, prev = df.iloc[-1], df.iloc[-2]
    diff_now = last["ema_fast"] - last["ema_slow"]
    diff_prev = prev["ema_fast"] - prev["ema_slow"]
    return Snapshot(
        close=float(last["close"]),
        ema_fast=float(last["ema_fast"]),
        ema_slow=float(last["ema_slow"]),
        rsi=float(last["rsi"]),
        rsi_prev=float(prev["rsi"]),
        macd=float(last["macd"]),
        macd_hist=float(last["macd_hist"]),
        macd_hist_prev=float(prev["macd_hist"]),
        cross_up=bool(diff_prev <= 0 < diff_now),
        cross_down=bool(diff_prev >= 0 > diff_now),
        rsi_recent_low=float(df["rsi"].iloc[-4:-1].min()),
    )


def above_daily_sma(daily_closes: pd.Series, price: float, window: int = DAILY_SMA) -> tuple[bool, float | None]:
    """Macro filter: is `price` above the daily SMA? Returns (passes, sma_value)."""
    daily_closes = daily_closes.dropna()
    if len(daily_closes) < window:
        return False, None
    value = float(sma(daily_closes.astype(float), window).iloc[-1])
    return price > value, value
