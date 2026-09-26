"""Append-only record of every live signal: what fired, and what was done.

Log lines say a signal was sent; this says WHAT was sent -- the metrics,
the price, which filters it failed -- so "what did it send today" has an
answer, and live alerts can later be scored against what price did next,
the same way the backtester scores history.

One JSON object per line, one file per UTC day: data/signals/YYYY-MM-DD.jsonl
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .config import ROOT
from .models import Signal

JOURNAL_DIR = ROOT / "data" / "signals"
_lock = threading.Lock()


def _iso(ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


def path_for(day_ms: int, root: Path | None = None) -> Path:
    return (root or JOURNAL_DIR) / time.strftime("%Y-%m-%d.jsonl", time.gmtime(day_ms / 1000))


def record(sig: Signal, action: str, now_ms: int, root: Path | None = None) -> None:
    """action is 'sent' or 'skipped'."""
    failed = [k for k, ok in sig.filters.items() if not ok]
    row = {
        "at": _iso(now_ms),
        "venue": sig.exchange.value,
        "rule": sig.rule,
        "symbol": sig.symbol,
        "side": sig.side.value,
        "bar": _iso(sig.ts),
        "ordinal": sig.ordinal,
        "action": action,
        "failed": failed,
        "price": sig.price,
        "metrics": {k: round(v, 6) for k, v in sig.metrics.items()},
    }
    p = path_for(now_ms, root)
    with _lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(row) + "\n")


def read(days: int = 1, now_ms: int | None = None, root: Path | None = None) -> list[dict]:
    """Rows from the last `days` UTC days, oldest first."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    rows: list[dict] = []
    for d in range(days - 1, -1, -1):
        p = path_for(now_ms - d * 86_400_000, root)
        if p.exists():
            for line in p.read_text().splitlines():
                if line.strip():
                    rows.append(json.loads(line))
    return rows
