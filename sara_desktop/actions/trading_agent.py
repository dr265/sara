# actions/trading_agent.py
"""Trading Signal Agent action - real backtests from voice mode.

Calls the real backend engine (POST /trading/demo or /trading/text), the
same routes the Hub workspace uses. The ensemble replays real candle data
(generated demo candles, or a CSV the user points at) and returns real
metrics - nothing is simulated by this action.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import requests

from core import hub_agents_client as hub

NAME = "Trading Signal Agent"
DEMO_TIMEOUT = 180
MAX_CSV_BYTES = 8 * 1024 * 1024
DEFAULT_CANDLES = 2500


def _fmt_float(value: Any) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return ""
    if f != f:  # NaN
        return ""
    return f"{f:.4g}"


def _report_run(result: Dict[str, Any], label: str) -> str:
    signals = result.get("signals")
    final = result.get("final") or {}
    trading = result.get("trading") or {}
    parts = [f"Backtest on {label}: {result.get('candles', '?')} candles, {signals} signals"]
    accuracy = _fmt_float(final.get("accuracy"))
    if accuracy:
        parts.append(f"ensemble accuracy {accuracy}")
    if trading.get("count"):
        bits = [f"{trading.get('count')} paper trades"]
        win_rate = _fmt_float(trading.get("win_rate"))
        if win_rate:
            bits.append(f"win rate {win_rate}")
        pnl = _fmt_float(trading.get("pnl"))
        if pnl:
            bits.append(f"PnL {pnl}")
        parts.append(", ".join(bits))
    else:
        parts.append("no trades executed (paper mode off)")
    return " - ".join(parts) + "."


def _run_demo(p: Dict[str, Any]) -> str:
    try:
        candles = int(p.get("candles") or DEFAULT_CANDLES)
    except (TypeError, ValueError):
        candles = DEFAULT_CANDLES
    candles = max(300, min(20000, candles))
    auto_trade = bool(p.get("auto_trade"))

    data = {
        "candles": str(candles),
        "auto_trade": "true" if auto_trade else "false",
    }
    for key in ("threshold", "eta"):
        if p.get(key) is not None:
            data[key] = str(p[key])

    try:
        resp = hub.post_form("/trading/demo", data, timeout=DEMO_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        return hub.unreachable(NAME, exc)
    if resp.status_code >= 400:
        return hub.http_failure(NAME, resp)
    try:
        result = resp.json()
    except Exception:
        return "The trading engine returned an unreadable response."
    return _report_run(result, "generated demo candles (not market data)")


def _run_csv(p: Dict[str, Any]) -> str:
    csv_text = str(p.get("csv") or "").strip()
    csv_path = str(p.get("csv_path") or p.get("file") or "").strip()
    if not csv_text and csv_path:
        path = Path(csv_path).expanduser()
        if not path.exists():
            return f"I cannot find a CSV file at {path}."
        try:
            if path.stat().st_size > MAX_CSV_BYTES:
                return "That CSV is too large for a voice run (limit 8 MB)."
            csv_text = path.read_text(encoding="utf-8-sig", errors="replace")
        except Exception as exc:
            return f"I could not read {path} ({exc.__class__.__name__})."
    if len(csv_text) < 10:
        return ("Point me at an OHLCV CSV file (timestamp,open,high,low,close,volume) "
                "or ask for a demo backtest instead.")

    body: Dict[str, Any] = {
        "csv": csv_text,
        "asset": str(p.get("asset") or "ASSET").strip()[:32] or "ASSET",
        "auto_trade": bool(p.get("auto_trade")),
    }
    for key in ("threshold", "eta"):
        if p.get(key) is not None:
            body[key] = p[key]

    try:
        resp = hub.post("/trading/text", body, timeout=DEMO_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        return hub.unreachable(NAME, exc)
    if resp.status_code >= 400:
        return hub.http_failure(NAME, resp)
    try:
        result = resp.json()
    except Exception:
        return "The trading engine returned an unreadable response."
    return _report_run(result, f"your CSV ({body['asset']})")


def _status() -> str:
    data = hub.health("/trading/health")
    if not data:
        return f"{NAME} is not reachable right now. {hub.BACKEND_HINT}"
    subsystems = ", ".join(data.get("subsystems") or [])
    return (f"{NAME} is online - engine: {data.get('engine', 'unknown')}"
            + (f", subsystems: {subsystems}" if subsystems else "")
            + ". Ask me for a demo backtest, or point me at an OHLCV CSV.")


def run_trading_agent(parameters: dict) -> str:
    p = parameters or {}
    action = str(p.get("action") or "").strip().lower()

    if not action:
        action = "demo" if (p.get("candles") or not (p.get("csv") or p.get("csv_path"))) else "csv"
    if action in ("demo", "backtest", "test"):
        return _run_demo(p)
    if action in ("csv", "file"):
        return _run_csv(p)
    if action in ("status", "health"):
        return _status()
    return "Options: action=demo (generated candles), action=csv (your CSV file), or action=status."


TOOL = {
    "name": "trading_agent",
    "description": (
        "Runs the Trading Signal Agent's REAL ensemble backtest - on generated demo candles "
        "or on an OHLCV CSV file the user points at - and reports signals, accuracy and paper-trade "
        "stats. Use for 'test the trading agent', 'run a backtest', 'try it on my CSV'. "
        "It does not fetch live market data by itself. "
        "To OPEN the Trading workspace page (not run it), use open_agency_workspace."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Optional: 'demo' (default), 'csv' (use csv_path), or 'status'.",
            },
            "csv_path": {
                "type": "STRING",
                "description": "Path to a 1-minute OHLCV CSV (timestamp,open,high,low,close,volume) for action=csv.",
            },
            "asset": {
                "type": "STRING",
                "description": "Optional instrument label, e.g. EURUSD.",
            },
            "candles": {
                "type": "NUMBER",
                "description": "Optional demo candle count (300-20000, default 2500).",
            },
            "threshold": {
                "type": "NUMBER",
                "description": "Optional entry confidence threshold (0-1, default 0.85).",
            },
            "auto_trade": {
                "type": "BOOLEAN",
                "description": "Optional paper-trade execution inside the backtest (default false).",
            },
        },
        "required": [],
    },
    "handler": run_trading_agent,
}
