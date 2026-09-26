"""SQLite cache for fetched history.

A 30-day backtest over a few hundred symbols is tens of thousands of HTTP
requests. Fetch once, then iterate on the rules offline -- otherwise every
parameter tweak costs another half hour against the venue's rate limit.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ..config import ROOT

DB_PATH = ROOT / "data" / "cache.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunk (
    kind     TEXT NOT NULL,   -- 'kline' | 'oi'
    exchange TEXT NOT NULL,
    symbol   TEXT NOT NULL,
    interval TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms   INTEGER NOT NULL,
    payload  TEXT NOT NULL,
    PRIMARY KEY (kind, exchange, symbol, interval, start_ms, end_ms)
);
"""


class Cache:
    """Thread-safe key/value store over whole fetch ranges."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def get(self, kind: str, exchange: str, symbol: str, interval: str,
            start_ms: int, end_ms: int) -> Any | None:
        row = self._conn().execute(
            "SELECT payload FROM chunk WHERE kind=? AND exchange=? AND symbol=? "
            "AND interval=? AND start_ms=? AND end_ms=?",
            (kind, exchange, symbol, interval, start_ms, end_ms),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind: str, exchange: str, symbol: str, interval: str,
            start_ms: int, end_ms: int, payload: Any) -> None:
        self._conn().execute(
            "INSERT OR REPLACE INTO chunk VALUES (?,?,?,?,?,?,?)",
            (kind, exchange, symbol, interval, start_ms, end_ms, json.dumps(payload)),
        )

    def stats(self) -> dict[str, int]:
        rows = self._conn().execute(
            "SELECT kind, COUNT(*) FROM chunk GROUP BY kind"
        ).fetchall()
        return dict(rows)
