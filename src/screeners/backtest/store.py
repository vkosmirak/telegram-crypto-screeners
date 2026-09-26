"""Persist scanned signals to JSON so they can be replayed without refetching.

The history cache is keyed on an exact time range, and `--days 30` means a
different range every minute. Re-running a backtest just to get its signals
back would therefore miss the cache entirely. Signals are small; write them
once and reuse them.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import ROOT
from ..models import Exchange, Side, Signal

OUT_DIR = ROOT / "out"


def path_for(exchange: Exchange | str, rule: str) -> Path:
    ex = exchange.value if isinstance(exchange, Exchange) else exchange
    return OUT_DIR / f"signals_{ex}_{rule}.json"


def save(signals: list[Signal], exchange: Exchange, rule: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = path_for(exchange, rule)
    p.write_text(json.dumps([{
        "rule": s.rule, "exchange": s.exchange.value, "symbol": s.symbol,
        "side": s.side.value, "ts": s.ts, "price": s.price,
        "ordinal": s.ordinal, "metrics": s.metrics, "filters": s.filters,
    } for s in signals], indent=1))
    return p


def load(exchange: Exchange | str, rule: str) -> list[Signal]:
    p = path_for(exchange, rule)
    if not p.exists():
        return []
    return [Signal(
        rule=r["rule"], exchange=Exchange(r["exchange"]), symbol=r["symbol"],
        side=Side(r["side"]), ts=r["ts"], price=r["price"], ordinal=r["ordinal"],
        metrics=r.get("metrics") or {}, filters=r.get("filters") or {},
    ) for r in json.loads(p.read_text())]


def available() -> list[Path]:
    return sorted(OUT_DIR.glob("signals_*.json")) if OUT_DIR.exists() else []
