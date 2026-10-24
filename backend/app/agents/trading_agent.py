"""
Multi-horizon signal engine: indicator ensemble + online multiplicative weights
+ empirical calibration + rich TUI + paper execution.

Served by the unified SARA backend as the Trading Signal Agent:

  GET  /trading/health         engine + dependency status
  GET  /trading/config         tunable engine defaults
  POST /trading/backtest       run a replay over an uploaded 1-minute OHLCV CSV
  POST /trading/text           same, but CSV sent as a raw JSON string
  POST /trading/demo           run a backtest on generated demo candles

Original CLI usage is preserved:
    python trading_agent.py data.csv --asset EURUSD --auto-trade

Feed: 1-minute OHLCV CSV replay (columns: timestamp,open,high,low,close,volume).
Any iterable yielding Candle objects can replace CsvFeed.

Deps: numpy pandas rich  (pandas_ta is NOT required — indicators are
implemented natively so the engine works on pandas 3.x.)
"""
from __future__ import annotations

import argparse
import csv
import io
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Iterable, Iterator, Optional

import numpy as np
import pandas as pd

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

SUBSYSTEMS = ("momentum", "volatility", "price_action", "trend")


# --------------------------------------------------------------------------- config
@dataclass
class Config:
    buffer_size: int = 200
    warmup: int = 60
    horizons: int = 5
    score_scale: float = 4.0        # sigmoid steepness
    horizon_decay: float = 0.85     # signal decay per step ahead
    neutral_band: float = 0.08      # |aggregate| below this => NEUTRAL
    learning_rate: float = 0.15     # eta for MWU penalty
    reward_rate: float = 0.03       # small reward for correct voters
    weight_floor: float = 0.05
    loss_memory: int = 5            # N candles of error memory
    defense_trigger: int = 3        # errors in memory that trigger defense
    base_threshold: float = 0.85
    defense_threshold: float = 0.90
    accuracy_window: int = 20
    cooldown_sec: int = 60
    auto_trade: bool = False        # paper execution only
    stake: float = 10.0
    payout: float = 0.82            # win +82% of stake, loss -100%

    def as_dict(self) -> dict:
        """JSON-friendly config snapshot (drops the excluded field name)."""
        return {
            "buffer_size": self.buffer_size,
            "warmup": self.warmup,
            "horizons": self.horizons,
            "score_scale": self.score_scale,
            "horizon_decay": self.horizon_decay,
            "neutral_band": self.neutral_band,
            "learning_rate": self.learning_rate,
            "reward_rate": self.reward_rate,
            "weight_floor": self.weight_floor,
            "loss_memory": self.loss_memory,
            "defense_trigger": self.defense_trigger,
            "base_threshold": self.base_threshold,
            "defense_threshold": self.defense_threshold,
            "accuracy_window": self.accuracy_window,
            "cooldown_sec": self.cooldown_sec,
            "auto_trade": self.auto_trade,
            "stake": self.stake,
            "payout": self.payout,
        }


# --------------------------------------------------------------------------- indicators
# Native implementations of the pandas_ta indicators the engine relies on.
# Written against plain numpy/pandas so the engine runs on pandas 3.x without
# the unmaintained pandas_ta package (which breaks under pandas >= 2/3).

def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder's smoothing (seed ignored; equivalent from t=0 onward-ish)."""
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return _rma(tr, length)


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    diff = close.diff()
    up = diff.clip(lower=0.0)
    down = (-diff).clip(lower=0.0)
    ru = _rma(up, n)
    rd = _rma(down, n)
    rs = ru / rd.replace(0.0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def _macd_hist(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    macd = _ema(close, fast) - _ema(close, slow)
    sig = _ema(macd, signal)
    return macd - sig  # histogram


def _stoch(high: pd.Series, low: pd.Series, close: pd.Series,
           k: int = 14, d: int = 3, smooth_k: int = 3) -> tuple[pd.Series, pd.Series]:
    ll = low.rolling(k).min()
    hh = high.rolling(k).max()
    k_raw = 100.0 * (close - ll) / (hh - ll)
    k_sm = k_raw.rolling(smooth_k).mean()
    d_sm = k_sm.rolling(d).mean()
    return k_sm, d_sm


def _bbands(close: pd.Series, length: int = 20, std: float = 2.0) -> tuple[pd.Series, ...]:
    """Returns (lower, mid, upper, bandwidth, %B) mirroring pandas_ta order."""
    mid = _sma(close, length)
    sd = close.rolling(length).std(ddof=0)
    upper = mid + std * sd
    lower = mid - std * sd
    width = (upper - lower) / mid
    pct_b = (close - lower) / (upper - lower)
    return lower, mid, upper, width, pct_b


def _vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """Session (per-calendar-day) anchored VWAP; NaN when no DatetimeIndex."""
    idx = close.index
    if not isinstance(idx, pd.DatetimeIndex) or len(idx) == 0:
        return pd.Series(np.nan, index=idx)
    tp = (high + low + close) / 3.0
    pv = tp * volume
    day = idx.normalize()
    cum_pv = pv.groupby(day).cumsum()
    cum_v = volume.groupby(day).cumsum().replace(0.0, np.nan)
    return cum_pv / cum_v


def _supertrend(high: pd.Series, low: pd.Series, close: pd.Series,
                length: int = 10, multiplier: float = 3.0) -> pd.Series:
    """Returns the supertrend direction series (+1 bullish / -1 bearish)."""
    atr = _atr(high, low, close, length)
    hl2 = (high + low) / 2.0
    ub = (hl2 + multiplier * atr).to_numpy(dtype=float)
    lb = (hl2 - multiplier * atr).to_numpy(dtype=float)
    closes = close.to_numpy(dtype=float)
    n = len(close)
    fub = np.full(n, np.nan)
    flb = np.full(n, np.nan)
    sd = np.ones(n)
    for i in range(1, n):
        pc = closes[i - 1]
        fub[i] = ub[i] if (ub[i] < fub[i - 1]) or (pc > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1]) or (pc < flb[i - 1]) else flb[i - 1]
        if sd[i - 1] == 1.0 and closes[i] < flb[i]:
            sd[i] = -1.0
        elif sd[i - 1] == -1.0 and closes[i] > fub[i]:
            sd[i] = 1.0
        else:
            sd[i] = sd[i - 1]
    return pd.Series(sd, index=close.index)


# --------------------------------------------------------------------------- data
@dataclass
class Candle:
    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def direction(self) -> int:
        return 1 if self.close > self.open else -1 if self.close < self.open else 0


class CandleBuffer:
    """Thread-safe rolling buffer of completed candles."""

    def __init__(self, maxlen: int):
        self._d: Deque[Candle] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def push(self, c: Candle) -> None:
        with self._lock:
            self._d.append(c)

    def frame(self) -> pd.DataFrame:
        with self._lock:
            rows = list(self._d)
        df = pd.DataFrame([c.__dict__ for c in rows])
        if not df.empty:
            df.index = pd.to_datetime(df["ts"], unit="s")
        return df

    def __len__(self) -> int:
        with self._lock:
            return len(self._d)


class CsvFeed:
    """Replays a CSV of 1-minute candles. timestamp may be epoch seconds or ISO-8601."""

    def __init__(self, path: str, speed: float):
        self.path = path
        self.delay = 0.0 if speed <= 0 else 60.0 / speed

    @staticmethod
    def _ts(raw: str) -> float:
        try:
            return float(raw)
        except ValueError:
            return pd.Timestamp(raw).timestamp()

    def __iter__(self) -> Iterator[Candle]:
        with open(self.path, newline="") as f:
            text = f.read()
        for candle in parse_csv_rows(text, ts_fn=self._ts):
            yield candle
            if self.delay:
                time.sleep(self.delay)


def parse_csv_rows(text: str, ts_fn=None) -> list[Candle]:
    """Parse OHLCV CSV text into a list of Candle objects.

    Raises ValueError with a friendly message when required columns are missing
    or a row cannot be parsed.
    """
    required = {"timestamp", "open", "high", "low", "close"}
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = {str(h).strip().lower() for h in (reader.fieldnames or [])}
    missing = required - fieldnames
    if missing:
        raise ValueError(
            f"CSV is missing required column(s): {', '.join(sorted(missing))}. "
            "Expected: timestamp,open,high,low,close[,volume]"
        )
    candles: list[Candle] = []
    for line, row in enumerate(reader, start=2):
        try:
            raw_ts = (row.get("timestamp") or "").strip()
            if not raw_ts:
                continue
            ts = float(raw_ts) if ts_fn is None else ts_fn(raw_ts)
            candles.append(
                Candle(
                    ts=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float((row.get("volume") or "0").strip() or 0),
                )
            )
        except (ValueError, TypeError) as exc:
            raise ValueError(f"CSV parse error on line {line}: {exc}") from exc
    if not candles:
        raise ValueError("CSV contains no candle rows")
    return candles


def make_demo_rows(n: int = 2500, seed: int = 11) -> list[Candle]:
    """Random-walk demo candles with regimes, so the engine has something to chew on."""
    rng = np.random.default_rng(seed)
    t0 = int(time.time()) // 60 * 60 - n * 60
    price = 1.2000
    candles: list[Candle] = []
    regime = 0.0
    for i in range(n):
        if i % 300 == 0:
            regime = rng.normal(0, 1.0) * 6e-5
        drift = regime + rng.normal(0, 4e-5)
        o = price
        c = o * (1.0 + drift + rng.normal(0, 3e-4))
        h = max(o, c) * (1.0 + abs(rng.normal(0, 2e-4)))
        l = min(o, c) * (1.0 - abs(rng.normal(0, 2e-4)))
        candles.append(
            Candle(ts=float(t0 + i * 60), open=round(o, 5), high=round(h, 5),
                   low=round(l, 5), close=round(c, 5), volume=float(rng.uniform(50, 900)))
        )
        price = c
    return candles


# --------------------------------------------------------------------------- helpers
def clip(x: float) -> float:
    x = 0.0 if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)
    return float(np.clip(x, -1.0, 1.0))


def last(series: pd.Series, default: float = 0.0) -> float:
    v = series.iloc[-1]
    return default if pd.isna(v) else float(v)


# --------------------------------------------------------------------------- ensemble
class Ensemble:
    """Four sub-systems, each emitting a directional vector in [-1, +1]."""

    def compute(self, df: pd.DataFrame) -> dict[str, float]:
        return {
            "momentum": self._momentum(df),
            "volatility": self._volatility(df),
            "price_action": self._price_action(df),
            "trend": self._trend(df),
        }

    @staticmethod
    def atr(df: pd.DataFrame) -> float:
        return last(_atr(df.high, df.low, df.close, 14), default=1e-9)

    def _momentum(self, df: pd.DataFrame) -> float:
        c, h, l = df.close, df.high, df.low
        # RSI with dynamic (rolling mean +/- 1.5 std) bands
        rsi = _rsi(c, 14)
        mid = rsi.rolling(50, min_periods=20).mean()
        band = rsi.rolling(50, min_periods=20).std()
        r, m, b = last(rsi, 50.0), last(mid, 50.0), max(last(band, 10.0), 1e-9)
        upper, lower = m + 1.5 * b, m - 1.5 * b
        if r > upper:
            rsi_score = -min((r - upper) / b, 1.0)        # overbought -> revert
        elif r < lower:
            rsi_score = min((lower - r) / b, 1.0)         # oversold -> revert
        else:
            rsi_score = (r - m) / (1.5 * b)               # inside bands -> momentum
        # MACD histogram with divergence damping
        hist = _macd_hist(c, fast=12, slow=26, signal=9)
        scale = last(hist.abs().rolling(30, min_periods=5).mean(), 1e-9) + 1e-12
        h_now, h_prev = last(hist), float(hist.iloc[-2]) if not pd.isna(hist.iloc[-2]) else 0.0
        hist_score = math.tanh(h_now / scale)
        if (h_now > 0) != (h_now > h_prev):               # histogram shrinking vs its sign
            hist_score *= 0.5
        # Stochastic 14,3,3
        k, d = _stoch(h, l, c, k=14, d=3, smooth_k=3)
        kv, dv = last(k, 50.0), last(d, 50.0)
        cross = clip((kv - dv) / 20.0)
        extreme = clip((50 - kv) / 30.0) if (kv > 80 or kv < 20) else 0.0
        stoch_score = clip(0.6 * cross + 0.4 * extreme)
        return clip(0.40 * rsi_score + 0.35 * hist_score + 0.25 * stoch_score)

    def _volatility(self, df: pd.DataFrame) -> float:
        c, h, l = df.close, df.high, df.low
        _, _, _, width, pct_b = _bbands(c, length=20, std=2)
        pb = last(pct_b, 0.5)
        squeeze = last(width) < last(width.rolling(50, min_periods=10).quantile(0.2), float("inf"))
        atr = _atr(h, l, c, length=14)
        atr_ratio = last(atr, 1.0) / (last(atr.rolling(50, min_periods=10).mean(), 1.0) + 1e-12)
        if squeeze:                                        # compression: follow the break
            brk = float(c.iloc[-1] - c.iloc[-2])
            score = math.copysign(0.5, brk) if brk != 0 else 0.0
        else:                                              # extremes: mean reversion
            score = clip((0.5 - pb) * 2.0)
        return clip(score * min(max(atr_ratio, 0.5), 1.5))

    def _price_action(self, df: pd.DataFrame) -> float:
        o, h, l, c = df.open.values, df.high.values, df.low.values, df.close.values
        score = 0.0
        rng = h[-1] - l[-1] + 1e-12
        body = abs(c[-1] - o[-1])
        upper_wick = h[-1] - max(o[-1], c[-1])
        lower_wick = min(o[-1], c[-1]) - l[-1]
        # Pin bar
        if body / rng < 0.33:
            if lower_wick / rng > 0.6:
                score += 0.7
            elif upper_wick / rng > 0.6:
                score -= 0.7
        # Engulfing
        prev_body = abs(c[-2] - o[-2])
        if c[-1] > o[-1] and c[-2] < o[-2] and c[-1] >= o[-2] and o[-1] <= c[-2] and body > prev_body:
            score += 0.8
        elif c[-1] < o[-1] and c[-2] > o[-2] and c[-1] <= o[-2] and o[-1] >= c[-2] and body > prev_body:
            score -= 0.8
        # Inside bar: bias toward mother-bar direction
        if h[-1] <= h[-2] and l[-1] >= l[-2]:
            score += 0.3 if c[-2] > o[-2] else -0.3
        # 3-candle body momentum
        score += 0.2 * float(np.sign((c[-3:] - o[-3:]).sum()))
        return clip(score)

    def _trend(self, df: pd.DataFrame) -> float:
        c = df.close
        atr = self.atr(df) + 1e-12
        e9, e21 = _ema(c, 9), _ema(c, 21)
        slope9 = (last(e9) - float(e9.iloc[-4])) / (3 * atr)
        spread = (last(e9) - last(e21)) / atr
        ema_score = clip(0.5 * math.tanh(2 * slope9) + 0.5 * math.tanh(spread))
        vwap = _vwap(df.high, df.low, c, df.volume)
        vwap_score = clip(math.tanh((last(c) - last(vwap, last(c))) / atr))
        st_dir = last(_supertrend(df.high, df.low, c, length=10, multiplier=3.0), 0.0)
        return clip(0.45 * ema_score + 0.25 * vwap_score + 0.30 * st_dir)


# --------------------------------------------------------------------------- learning
class OnlineWeights:
    """Multiplicative weight update with loss memory and defense regime."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.w = {k: 1.0 / len(SUBSYSTEMS) for k in SUBSYSTEMS}
        self.errors: Deque[bool] = deque(maxlen=cfg.loss_memory)
        self.results: Deque[bool] = deque(maxlen=cfg.accuracy_window)
        self.updates = 0

    def aggregate(self, scores: dict[str, float]) -> float:
        return sum(self.w[k] * scores[k] for k in SUBSYSTEMS)

    def update(self, scores: dict[str, float], predicted: int, actual: int) -> Optional[bool]:
        if predicted == 0 or actual == 0:
            return None
        correct = predicted == actual
        self.results.append(correct)
        self.errors.append(not correct)
        self.updates += 1
        for k in SUBSYSTEMS:
            s = scores[k]
            if s == 0:
                continue
            if np.sign(s) != actual:                       # wrong voter: w *= (1 - eta * loss)
                self.w[k] *= 1.0 - self.cfg.learning_rate * abs(s)
            elif correct:
                self.w[k] *= 1.0 + self.cfg.reward_rate * abs(s)
        self._normalize()
        return correct

    def _normalize(self) -> None:
        for k in SUBSYSTEMS:
            self.w[k] = max(self.w[k], self.cfg.weight_floor)
        total = sum(self.w.values())
        for k in SUBSYSTEMS:
            self.w[k] /= total

    @property
    def defending(self) -> bool:
        return sum(self.errors) >= self.cfg.defense_trigger

    @property
    def state(self) -> str:
        if self.defending:
            return "DEFENDING"
        if len(self.results) < self.cfg.accuracy_window:
            return "LEARNING"
        return "NORMAL"

    @property
    def threshold(self) -> float:
        return self.cfg.defense_threshold if self.defending else self.cfg.base_threshold

    @property
    def accuracy(self) -> float:
        return sum(self.results) / len(self.results) if self.results else float("nan")


class Calibrator:
    """Empirical reliability: realised hit-rate of t+1 predictions per confidence bucket."""

    def __init__(self, bins: int = 10, min_samples: int = 5):
        self.bins, self.min_samples = bins, min_samples
        self.hits = np.zeros(bins)
        self.n = np.zeros(bins)

    def _idx(self, conf: float) -> int:
        return min(int((conf - 0.5) / 0.5 * self.bins), self.bins - 1)

    def record(self, conf: float, correct: bool) -> None:
        i = self._idx(conf)
        self.n[i] += 1
        self.hits[i] += int(correct)

    def empirical(self, conf: float) -> float:
        i = self._idx(conf)
        return self.hits[i] / self.n[i] if self.n[i] >= self.min_samples else float("nan")


# --------------------------------------------------------------------------- forecast
@dataclass
class Horizon:
    step: int
    direction: str
    expected_close: float
    confidence: float      # sigmoid(|score|), NOT a calibrated probability


class Forecaster:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def forecast(self, close: float, atr: float, aggregate: float) -> list[Horizon]:
        out = []
        for k in range(1, self.cfg.horizons + 1):
            bias = aggregate * self.cfg.horizon_decay ** (k - 1)
            conf = 1.0 / (1.0 + math.exp(-abs(bias * self.cfg.score_scale)))
            if bias > self.cfg.neutral_band:
                d = "CALL"
            elif bias < -self.cfg.neutral_band:
                d = "PUT"
            else:
                d = "NEUTRAL"
            out.append(Horizon(k, d, close + k * atr * bias, conf))
        return out


# --------------------------------------------------------------------------- execution
@dataclass
class Trade:
    ts: float
    direction: int
    entry: float
    stake: float
    result: Optional[float] = None


class PaperBroker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.trades: list[Trade] = []
        self.pending: Optional[Trade] = None
        self.last_trade_ts = -math.inf
        self.pnl = 0.0

    def can_trade(self, ts: float) -> bool:
        return self.pending is None and ts - self.last_trade_ts >= self.cfg.cooldown_sec

    def open(self, ts: float, direction: int, entry: float) -> None:
        self.pending = Trade(ts, direction, entry, self.cfg.stake)
        self.last_trade_ts = ts

    def settle(self, candle: Candle) -> None:
        if self.pending is None:
            return
        t = self.pending
        won = candle.direction == t.direction
        t.result = t.stake * self.cfg.payout if won else -t.stake
        self.pnl += t.result
        self.trades.append(t)
        self.pending = None

    @property
    def win_rate(self) -> float:
        return sum(t.result > 0 for t in self.trades) / len(self.trades) if self.trades else float("nan")


# --------------------------------------------------------------------------- engine
class Engine:
    def __init__(self, cfg: Config, asset: str, signal_log_max: int = 0):
        self.cfg, self.asset = cfg, asset
        self.buffer = CandleBuffer(cfg.buffer_size)
        self.ensemble = Ensemble()
        self.weights = OnlineWeights(cfg)
        self.calibrator = Calibrator()
        self.forecaster = Forecaster(cfg)
        self.broker = PaperBroker(cfg)
        self.scores: dict[str, float] = {k: 0.0 for k in SUBSYSTEMS}
        self.horizons: list[Horizon] = []
        self.atr = 0.0
        self.last: Optional[Candle] = None
        self._prev_scores: Optional[dict[str, float]] = None
        self._prev_pred = 0
        self._prev_conf = 0.5
        # Bounded per-candle signal capture for the API (0 = disabled, CLI).
        self.signal_log_max = signal_log_max
        self.signal_log: list[dict] = []
        self._sig_stride = 1
        self._sig_count = 0

    def on_candle(self, candle: Candle) -> None:
        self.buffer.push(candle)
        self.last = candle
        self.broker.settle(candle)
        if self._prev_scores is not None:                  # feedback on t+1 prediction
            correct = self.weights.update(self._prev_scores, self._prev_pred, candle.direction)
            if correct is not None:
                self.calibrator.record(self._prev_conf, correct)
        if len(self.buffer) < self.cfg.warmup:
            return
        df = self.buffer.frame()
        self.scores = self.ensemble.compute(df)
        self.atr = self.ensemble.atr(df)
        agg = self.weights.aggregate(self.scores)
        self.horizons = self.forecaster.forecast(candle.close, self.atr, agg)
        h1 = self.horizons[0]
        self._prev_scores = dict(self.scores)
        self._prev_pred = 1 if h1.direction == "CALL" else -1 if h1.direction == "PUT" else 0
        self._prev_conf = h1.confidence
        if (
            self.cfg.auto_trade
            and self._prev_pred != 0
            and h1.confidence >= self.weights.threshold
            and self.broker.can_trade(candle.ts)
        ):
            self.broker.open(candle.ts, self._prev_pred, candle.close)
        self._capture_signal(candle, agg, h1)

    def _capture_signal(self, candle: Candle, agg: float, h1: Horizon) -> None:
        """Bounded, evenly-spaced signal history for charts (skips fine detail)."""
        if not self.signal_log_max:
            return
        if self._sig_count % self._sig_stride == 0:
            self.signal_log.append({
                "t": candle.ts,
                "c": candle.close,
                "a": round(float(agg), 5),
                "p": 1 if h1.direction == "CALL" else -1 if h1.direction == "PUT" else 0,
                "f": round(float(h1.confidence), 5),
            })
            if len(self.signal_log) > self.signal_log_max:
                self.signal_log = self.signal_log[::2]
                self._sig_stride *= 2
        self._sig_count += 1


# --------------------------------------------------------------------------- backtest API
def run_backtest(candles: Iterable[Candle], *,
                 asset: str = "ASSET",
                 auto_trade: bool = False,
                 threshold: float = 0.85,
                 eta: float = 0.15,
                 signal_log_max: int = 4000) -> dict:
    """Replay a candle series and return a JSON-friendly result snapshot."""
    cfg = Config(auto_trade=auto_trade, base_threshold=threshold, learning_rate=eta)
    engine = Engine(cfg, asset, signal_log_max=signal_log_max)
    count = 0
    for candle in candles:
        engine.on_candle(candle)
        count += 1

    weights = {k: round(float(v), 5) for k, v in engine.weights.w.items()}
    acc = engine.weights.accuracy
    trades = [
        {
            "ts": float(t.ts),
            "direction": int(t.direction),
            "entry": round(float(t.entry), 6),
            "result": round(float(t.result), 4) if t.result is not None else None,
        }
        for t in engine.broker.trades
    ]
    win_rate = engine.broker.win_rate
    return {
        "asset": asset,
        "candles": count,
        "warmup": cfg.warmup,
        "signals": len(engine.signal_log),
        "config": cfg.as_dict(),
        "final": {
            "close": round(float(engine.last.close), 6) if engine.last else None,
            "atr": round(float(engine.atr), 8),
            "weights": weights,
            "state": engine.weights.state,
            "defending": engine.weights.defending,
            "threshold": round(float(engine.weights.threshold), 4),
            "accuracy": round(float(acc), 5) if not math.isnan(acc) else None,
            "updates": engine.weights.updates,
        },
        "trading": {
            "enabled": cfg.auto_trade,
            "count": len(trades),
            "win_rate": round(float(win_rate), 5) if not math.isnan(win_rate) else None,
            "pnl": round(float(engine.broker.pnl), 4),
            "stake": cfg.stake,
            "payout": cfg.payout,
            "trades": trades[:2000],
        },
        "series": engine.signal_log,
    }


# --------------------------------------------------------------------------- TUI
def bar(v: float, width: int = 10) -> str:
    filled = int(round(max(0.0, min(1.0, v)) * width))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def fmt_pct(v: float) -> str:
    return "n/a" if math.isnan(v) else f"{v * 100:5.1f}%"


def render(e: Engine) -> Group:
    c = e.last
    atr_state = "n/a"
    if e.atr and c:
        rel = e.atr / c.close * 1e4
        atr_state = f"{e.atr:.5f} ({'HIGH' if rel > 8 else 'LOW' if rel < 2 else 'NORMAL'})"
    header = Table.grid(expand=True)
    header.add_row(
        f"[bold]{e.asset}[/]  close [cyan]{c.close:.5f}[/]" if c else "waiting...",
        f"ATR14 {atr_state}",
        f"candles {len(e.buffer)}/{e.cfg.buffer_size}",
    )

    proj = Table(title="5-Candle Projection Matrix", expand=True)
    for col in ("Horizon", "Direction", "Target Close", "Sigmoid Conf", "Empirical Hit-Rate"):
        proj.add_column(col)
    for h in e.horizons:
        icon = {"CALL": "[green]CALL 🟢[/]", "PUT": "[red]PUT 🔴[/]", "NEUTRAL": "[white]NEUTRAL ⚪[/]"}[h.direction]
        emp = e.calibrator.empirical(h.confidence) if h.step == 1 else float("nan")
        proj.add_row(f"+{h.step}", icon, f"{h.expected_close:.5f}",
                     f"{h.confidence * 100:5.1f}% {bar(h.confidence)}", fmt_pct(emp))

    diag = Table(title="Adaptation Diagnostics", expand=True)
    diag.add_column("Sub-system"); diag.add_column("Vector"); diag.add_column("Weight")
    for k in SUBSYSTEMS:
        diag.add_row(k, f"{e.scores[k]:+.3f}", f"{e.weights.w[k]:.3f} {bar(e.weights.w[k])}")
    state_color = {"NORMAL": "green", "LEARNING": "yellow", "DEFENDING": "red"}[e.weights.state]
    diag.add_row("", "", "")
    diag.add_row("state", f"[{state_color}]{e.weights.state}[/]", f"entry threshold {e.weights.threshold * 100:.0f}%")
    diag.add_row("rolling acc (20)", fmt_pct(e.weights.accuracy), f"updates {e.weights.updates}")

    exe = Table(title=f"Paper Execution ({'AUTO' if e.cfg.auto_trade else 'OFF'})", expand=True)
    exe.add_column("Trades"); exe.add_column("Win rate"); exe.add_column("PnL"); exe.add_column("Pending")
    pend = e.broker.pending
    exe.add_row(str(len(e.broker.trades)), fmt_pct(e.broker.win_rate),
                f"{e.broker.pnl:+.2f}", f"{'CALL' if pend.direction > 0 else 'PUT'} @ {pend.entry:.5f}" if pend else "-")
    return Group(Panel(header), proj, diag, exe)


# --------------------------------------------------------------------------- main
def main() -> None:
    p = argparse.ArgumentParser(description="Multi-horizon ensemble signal engine (paper trading).")
    p.add_argument("csv", help="1-minute OHLCV CSV: timestamp,open,high,low,close,volume")
    p.add_argument("--asset", default="EURUSD")
    p.add_argument("--speed", type=float, default=0, help="candles per minute replayed (0 = as fast as possible)")
    p.add_argument("--auto-trade", action="store_true", help="enable paper execution")
    p.add_argument("--threshold", type=float, default=0.85)
    p.add_argument("--eta", type=float, default=0.15)
    args = p.parse_args()

    cfg = Config(auto_trade=args.auto_trade, base_threshold=args.threshold, learning_rate=args.eta)
    engine = Engine(cfg, args.asset)
    console = Console()
    with Live(render(engine), console=console, refresh_per_second=8, screen=False) as live:
        for candle in CsvFeed(args.csv, args.speed):
            engine.on_candle(candle)
            live.update(render(engine))
    console.print(f"\nDone. trades={len(engine.broker.trades)} win_rate={fmt_pct(engine.broker.win_rate)} "
                  f"pnl={engine.broker.pnl:+.2f} final_weights={ {k: round(v, 3) for k, v in engine.weights.w.items()} }")


if __name__ == "__main__":
    main()
