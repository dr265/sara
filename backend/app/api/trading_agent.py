"""Trading Signal Agent routes, served by the unified SARA backend.

Mounted by app.main under /trading/*:
  GET  /trading/health      engine + runtime status
  GET  /trading/config      tunable engine defaults
  POST /trading/backtest    replay an uploaded 1-minute OHLCV CSV (multipart)
  POST /trading/text        same replay, CSV supplied as a raw JSON string
  POST /trading/demo        replay generated demo candles (no file needed)

  Live mode (the leftover StateServer behaviour, folded onto this server):
  GET  /trading/live/state      snapshot (was: standalone GET /state)
  POST /trading/live/control    start / pause / reset (+ threshold/eta tuning)
  POST /trading/live/tick       feed a raw price tick
  POST /trading/live/seed       warm the engine from an OHLCV CSV

The engine itself lives in app.agents.trading_agent (no pandas_ta dependency —
all indicators are implemented natively against numpy/pandas). The live agent
lives in app.agents.trading_live and reuses that same engine.
"""
from __future__ import annotations

import logging
import platform
from typing import Any

import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.agents import trading_agent as engine

log = logging.getLogger("sara.trading")

router = APIRouter(prefix="/trading", tags=["Trading Signal Agent"])

# Replay accepts ISO-8601 or epoch timestamps (same tolerance as the CLI).
_ts = engine.CsvFeed._ts


@router.get("/health")
async def health() -> dict[str, Any]:
    """Engine + runtime status. Never raises — frontends can poll this."""
    try:
        import pandas as pd
        pandas_ok, pandas_ver = True, pd.__version__
    except Exception:
        pandas_ok, pandas_ver = False, ""
    return {
        "status": "ok",
        "engine": "multi-horizon ensemble signal engine",
        "subsystems": list(engine.SUBSYSTEMS),
        "python": platform.python_version(),
        "numpy": np.__version__,
        "pandas": pandas_ver if pandas_ok else None,
        "pandas_ok": pandas_ok,
        "native_indicators": True,
        "module": engine.__name__,
    }


@router.get("/config")
async def config() -> dict[str, Any]:
    """Current engine defaults, so the UI can mirror the tunables."""
    return {
        "config": engine.Config().as_dict(),
        "subsystems": list(engine.SUBSYSTEMS),
        "csv_columns": ["timestamp", "open", "high", "low", "close", "volume"],
    }


def _run(csv_text: str, asset: str, auto_trade: bool,
         threshold: float, eta: float) -> dict[str, Any]:
    """Shared replay path: parse CSV text -> run engine -> JSON result."""
    try:
        candles = engine.parse_csv_rows(csv_text, ts_fn=_ts)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if len(candles) < engine.Config().warmup + 10:
        raise HTTPException(
            status_code=422,
            detail=f"Not enough candles: need at least {engine.Config().warmup + 10}, got {len(candles)}.",
        )
    started = len(candles)
    try:
        result = engine.run_backtest(
            candles,
            asset=asset or "ASSET",
            auto_trade=auto_trade,
            threshold=threshold,
            eta=eta,
        )
    except Exception as exc:  # engine-internal failure -> readable 500
        log.exception("trading backtest failed")
        raise HTTPException(status_code=500, detail=f"engine error: {exc}")
    log.info("backtest asset=%s candles=%s auto_trade=%s", asset, started, auto_trade)
    return result


@router.post("/backtest")
async def backtest(
    file: UploadFile = File(..., description="1-minute OHLCV CSV (timestamp,open,high,low,close,volume)"),
    asset: str = Form("ASSET", description="instrument label, e.g. EURUSD"),
    auto_trade: bool = Form(False, description="enable paper execution"),
    threshold: float = Form(0.85, description="entry confidence threshold"),
    eta: float = Form(0.15, description="learning rate for weight updates"),
) -> dict[str, Any]:
    """Replay an uploaded OHLCV CSV through the full ensemble pipeline."""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=422, detail="Upload a .csv file")
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 text")
    return _run(text, asset, auto_trade, threshold, eta)


class TextBacktestBody(BaseModel):
    """JSON alternative to the multipart upload — handy for curl/scripts."""

    csv: str = Field(..., min_length=10, description="full OHLCV CSV text")
    asset: str = "ASSET"
    auto_trade: bool = False
    threshold: float = Field(0.85, ge=0.0, le=1.0)
    eta: float = Field(0.15, ge=0.0, le=1.0)


@router.post("/text")
async def backtest_text(payload: TextBacktestBody) -> dict[str, Any]:
    """Same replay as /backtest but the CSV arrives as a JSON string body."""
    return _run(payload.csv, payload.asset, payload.auto_trade, payload.threshold, payload.eta)


@router.post("/demo")
async def demo(
    auto_trade: bool = Form(False),
    threshold: float = Form(0.85),
    eta: float = Form(0.15),
    candles: int = Form(2500, ge=300, le=20000),
) -> dict[str, Any]:
    """Replay generated demo candles — lets the UI exercise the engine with no CSV."""
    rows = engine.make_demo_rows(n=candles)
    return engine.run_backtest(
        rows,
        asset="DEMO",
        auto_trade=auto_trade,
        threshold=threshold,
        eta=eta,
    )


@router.get("/demo-csv")
async def demo_csv() -> Any:
    """Download a generated demo CSV (useful as a template/sample file)."""
    from fastapi.responses import PlainTextResponse

    rows = engine.make_demo_rows(n=500)
    lines = ["timestamp,open,high,low,close,volume"]
    for c in rows:
        lines.append(
            f"{int(c.ts)},{c.open:.5f},{c.high:.5f},{c.low:.5f},{c.close:.5f},{c.volume:.0f}"
        )
    return PlainTextResponse("\n".join(lines), media_type="text/csv")


# --------------------------------------------------------------------------- #
# Live mode — one unified server, no standalone state HTTP server
# --------------------------------------------------------------------------- #
from app.agents.trading_live import live_agent as _live  # noqa: E402


@router.get("/live/state")
async def live_state() -> dict[str, Any]:
    """Live-agent snapshot (replaces the leftover standalone GET /state)."""
    return _live.snapshot()


class LiveControlBody(BaseModel):
    action: str = Field(..., description="start | pause | reset")
    threshold: float | None = Field(None, ge=0.0, le=1.0)
    eta: float | None = Field(None, ge=0.0, le=1.0)
    auto_trade: bool | None = None
    stake: float | None = Field(None, gt=0.0)


@router.post("/live/control")
async def live_control(payload: LiveControlBody) -> dict[str, Any]:
    """Start / pause / reset the live agent, or tune it while it runs."""
    action = (payload.action or "").strip().lower()
    if payload.threshold is not None:
        _live.engine.cfg.base_threshold = payload.threshold
    if payload.eta is not None:
        _live.engine.cfg.learning_rate = payload.eta
    if payload.auto_trade is not None:
        _live.engine.cfg.auto_trade = payload.auto_trade
    if payload.stake is not None:
        _live.engine.cfg.stake = payload.stake
    if action == "start":
        _live.start()
    elif action == "pause":
        _live.pause()
    elif action == "reset":
        _live.reset()
    else:
        raise HTTPException(status_code=422, detail="action must be start | pause | reset")
    return _live.snapshot()


class LiveTickBody(BaseModel):
    price: float = Field(..., gt=0.0, description="current price readout")
    ts: float | None = Field(None, description="epoch seconds (defaults to now)")


@router.post("/live/tick")
async def live_tick(payload: LiveTickBody) -> dict[str, Any]:
    """Feed one raw price tick. Ignored (but recorded as price) while paused."""
    import time as _time

    ts = payload.ts if payload.ts is not None else _time.time()
    return _live.on_tick(ts, payload.price)


class LiveSeedBody(BaseModel):
    csv: str = Field(..., min_length=10, description="OHLCV CSV text to warm the engine")


@router.post("/live/seed")
async def live_seed(payload: LiveSeedBody) -> dict[str, Any]:
    """Warm the live engine from historical candles before starting ticks."""
    try:
        candles = engine.parse_csv_rows(payload.csv, ts_fn=_ts)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    seeded = _live.seed(candles)
    return {"seeded": seeded, **_live.snapshot()}
