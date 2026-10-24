"""Live trading agent runtime — served by the unified backend (no side server).

This is the project-conform port of the leftover standalone code that used to
run its own `ThreadingHTTPServer` on a side port to feed a TSX panel. Here the
same "live" state machine lives as a module singleton inside the FastAPI app
and is exposed through the normal /trading/* router, so everything stays on the
one unified server:

    GET  /trading/live/state    <- what GET /state used to return
    POST /trading/live/control  <- start / pause / reset (+ tuning)
    POST /trading/live/tick     <- feed a price tick (1-minute candle build)
    POST /trading/live/seed     <- warm the engine from an OHLCV CSV

It reuses app.agents.trading_agent.Engine (weights, forecast, paper broker) —
there is exactly one canonical engine implementation in the project.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import Any, Optional

from app.agents.trading_agent import Candle, Config, Engine


class CandleAggregator:
    """Builds 1-minute candles from raw price ticks (mirrors the frontend).

    A candle is finalised when the first tick of the *following* minute bucket
    arrives, so `add()` returns the closed candle at that moment (None before
    the first bucket rolls over — mirrors the TSX panel's partial logic).
    """

    def __init__(self) -> None:
        self.cur: Optional[Candle] = None
        self._partial = True

    @staticmethod
    def bucket(ts: float) -> float:
        return math.floor(ts / 60.0) * 60.0

    @staticmethod
    def seconds_left(now: float) -> float:
        return 60.0 - (now % 60.0)

    def add(self, ts: float, price: float) -> Optional[Candle]:
        b = self.bucket(ts)
        done: Optional[Candle] = None
        if self.cur is None:
            self.cur = Candle(ts=b, open=price, high=price, low=price, close=price, volume=1)
        elif b != self.cur.ts:
            if not self._partial:
                done = self.cur
            self._partial = False
            self.cur = Candle(ts=b, open=price, high=price, low=price, close=price, volume=1)
        else:
            self.cur.high = max(self.cur.high, price)
            self.cur.low = min(self.cur.low, price)
            self.cur.close = price
            self.cur.volume += 1
        return done


class LiveAlert:
    """A CALL/PUT that fired above the entry threshold, valid until `expires`."""

    __slots__ = ("direction", "confidence", "target", "expires")

    def __init__(self, direction: str, confidence: float, target: float, expires: float):
        self.direction = direction
        self.confidence = confidence
        self.target = target
        self.expires = expires


class LiveAgent:
    """Screen of the live signal engine: ticks -> 1m candles -> engine -> alerts."""

    def __init__(self, asset: str = "LIVE", cfg: Optional[Config] = None, log_len: int = 24) -> None:
        self.cfg = cfg or Config()
        self.engine = Engine(self.cfg, asset)
        self.agg = CandleAggregator()
        self.asset = asset
        self.price: Optional[float] = None
        self.active = False
        self.alert: Optional[LiveAlert] = None
        self.log: deque[str] = deque(maxlen=log_len)
        self._lock = threading.Lock()

    # ------------------------------------------------------------- control
    def start(self) -> None:
        with self._lock:
            self.active = True
            self._push("agent started")

    def pause(self) -> None:
        with self._lock:
            self.active = False
            self._push("agent paused")

    def reset(self) -> None:
        """Drop candles, weights, alerts and history — clean slate (keeps config)."""
        with self._lock:
            self.engine = Engine(self.cfg, self.asset)
            self.agg = CandleAggregator()
            self.price = None
            self.alert = None
            self.log.clear()
            self._push("reset")

    def seed(self, candles: list[Candle]) -> int:
        """Warm the engine from historical candles (e.g. a CSV replay)."""
        with self._lock:
            for candle in candles:
                self.engine.on_candle(candle)
            self._push(f"seeded {len(candles)} candles")
            return len(candles)

    # ------------------------------------------------------------- data feed
    def on_tick(self, ts: float, price: float) -> dict[str, Any]:
        """Feed one raw price tick. Returns what the tick did (never raises)."""
        with self._lock:
            self.price = price
            result: dict[str, Any] = {"closed_candle": False, "alert": None, "active": self.active}
            if not self.active:
                return result
            closed = self.agg.add(ts, price)
            if closed is not None:
                result["closed_candle"] = True
                fired = self._on_candle(closed)
                if fired is not None:
                    result["alert"] = fired
            return result

    def _on_candle(self, candle: Candle) -> Optional[dict[str, Any]]:
        self.engine.on_candle(candle)
        fired: Optional[dict[str, Any]] = None
        marker = "UP" if candle.direction > 0 else "DOWN" if candle.direction < 0 else "FLAT"
        self._push(f"close {candle.close:.5f} {marker}")
        h1 = self.engine.horizons[0] if self.engine.horizons else None
        if h1 is not None and h1.direction != "NEUTRAL" and h1.confidence >= self.engine.weights.threshold:
            # Signal applies to the candle that just closed; act before the next
            # minute boundary + one minute passes.
            expires = candle.ts + 120.0
            self.alert = LiveAlert(h1.direction, h1.confidence, h1.expected_close, expires)
            fired = {
                "direction": h1.direction,
                "confidence": h1.confidence,
                "target": h1.expected_close,
                "expires": expires,
            }
            self._push(f"ALERT {h1.direction} {h1.confidence * 100:.0f}% target {h1.expected_close:.5f}")
        else:
            self.alert = None
        return fired

    def _push(self, msg: str) -> None:
        self.log.appendleft(f"{time.strftime('%H:%M:%S')}  {msg}")

    # ------------------------------------------------------------- snapshot
    def snapshot(self) -> dict[str, Any]:
        """JSON snapshot — shape kept compatible with the leftover panel's GET /state."""
        e = self.engine
        now = time.time()
        a = self.alert if (self.alert is not None and now < self.alert.expires) else None
        acc = e.weights.accuracy
        return {
            "asset": self.asset,
            "active": self.active,
            "price": None if self.price is None else round(float(self.price), 6),
            "secondsLeft": round(self.agg.seconds_left(now), 1),
            "warmup": [len(e.buffer), e.cfg.warmup],
            "state": e.weights.state,
            "threshold": round(float(e.weights.threshold), 4),
            "accuracy": None if math.isnan(acc) else round(float(acc), 5),
            "weights": {k: round(float(v), 4) for k, v in e.weights.w.items()},
            "alert": None if a is None else {
                "direction": a.direction,
                "confidence": round(float(a.confidence), 5),
                "target": round(float(a.target), 6),
                "expiresIn": round(a.expires - now),
            },
            "horizons": [
                {
                    "step": h.step,
                    "direction": h.direction,
                    "target": round(float(h.expected_close), 6),
                    "confidence": round(float(h.confidence), 5),
                }
                for h in e.horizons
            ],
            "log": list(self.log),
        }


# Module-level singleton — the one live agent the unified backend serves.
live_agent = LiveAgent()
