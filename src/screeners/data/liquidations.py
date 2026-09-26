"""Live liquidation feeds, and a recorder that persists them.

Neither venue serves historical liquidations over REST, so the only way to ever
backtest the liquidation screener is to start recording now. That is what
`screeners record-liquidations` is for.

Two things to know about the feeds:

* Binance `!forceOrder@arr` is a *snapshot* stream. It pushes at most one
  liquidation per symbol per 1000ms, so recorded Binance totals undercount
  during exactly the cascades we care about most. Bybit's `allLiquidation`
  batches at 500ms without dropping, so it is the trustworthy side.
* The two report opposite polarity for the same letter -- see
  `rules.liquidation.normalise_side`.
"""
from __future__ import annotations

import json
import logging
import random
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

from ..models import Exchange, Liquidation
from ..rules.liquidation import normalise_side
from .ws import WebSocket, WebSocketClosed

log = logging.getLogger(__name__)

BINANCE_WS = "wss://fstream.binance.com/ws/!forceOrder@arr"
BYBIT_WS = "wss://stream.bybit.com/v5/public/linear"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS liquidation (
    exchange TEXT NOT NULL,
    symbol   TEXT NOT NULL,
    ts       INTEGER NOT NULL,
    side     TEXT NOT NULL,   -- side of the POSITION that was liquidated
    qty      REAL NOT NULL,
    price    REAL NOT NULL,
    PRIMARY KEY (exchange, symbol, ts, side, qty, price)
);
CREATE INDEX IF NOT EXISTS liq_ts ON liquidation(ts);
CREATE INDEX IF NOT EXISTS liq_sym ON liquidation(exchange, symbol, ts);
"""


class LiquidationStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, timeout=30.0,
                                     isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    def add(self, liq: Liquidation) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO liquidation VALUES (?,?,?,?,?,?)",
                (liq.exchange.value, liq.symbol, liq.ts, liq.side.value, liq.qty, liq.price),
            )

    def count(self) -> dict[str, int]:
        """Counts per venue.

        Kept off the ingest path: this is a full scan, and holding the write
        lock across it while the recorder has been running for a week stalls
        both feed threads -- on Bybit a stalled reader fills the receive
        buffer and gets dropped."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT exchange, COUNT(*) FROM liquidation GROUP BY exchange"
            ).fetchall()
        return dict(rows)

    def span(self) -> tuple[int, int] | None:
        with self._lock:
            row = self._conn.execute("SELECT MIN(ts), MAX(ts) FROM liquidation").fetchone()
        return (row[0], row[1]) if row and row[0] else None


def stream_binance() -> Iterator[Liquidation]:
    """Yields liquidations until the socket drops. Caller handles reconnect.

    The long read timeout is deliberate: this stream is silent whenever nobody
    is being liquidated, which is most of the time. Binance pings every ~3
    minutes, so anything under that reconnects on a calm market forever.
    """
    with WebSocket(BINANCE_WS, timeout=300.0) as ws:
        for raw in ws.messages():
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            o = msg.get("o") if isinstance(msg, dict) else None
            if not o:
                continue
            try:
                qty = float(o["q"])
                price = float(o.get("ap") or o["p"])  # average fill beats limit price
                yield Liquidation(
                    exchange=Exchange.BINANCE, symbol=o["s"], ts=int(o["T"]),
                    side=normalise_side(Exchange.BINANCE, o["S"]), qty=qty, price=price,
                )
            except (KeyError, ValueError, TypeError) as e:
                log.debug("skipping malformed binance liquidation: %s", e)


def stream_bybit(symbols: list[str], chunk: int = 50) -> Iterator[Liquidation]:
    """Subscribes in chunks; Bybit caps how much one subscribe request may carry."""
    # Bybit disconnects a client that goes 20s without an application ping.
    with WebSocket(BYBIT_WS, timeout=120.0,
                   heartbeat='{"op":"ping"}', heartbeat_s=20.0) as ws:
        for i in range(0, len(symbols), chunk):
            ws.send_json({"op": "subscribe",
                          "args": [f"allLiquidation.{s}" for s in symbols[i:i + chunk]]})
        for raw in ws.messages():
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict) or not str(msg.get("topic", "")).startswith("allLiquidation"):
                continue
            for d in msg.get("data") or []:
                try:
                    yield Liquidation(
                        exchange=Exchange.BYBIT, symbol=d["s"], ts=int(d["T"]),
                        side=normalise_side(Exchange.BYBIT, d["S"]),
                        qty=float(d["v"]), price=float(d["p"]),
                    )
                except (KeyError, ValueError, TypeError) as e:
                    log.debug("skipping malformed bybit liquidation: %s", e)


def run_forever(
    name: str,
    factory: Callable[[], Iterator[Liquidation]],
    on_event: Callable[[Liquidation], None],
    stop: threading.Event,
    max_backoff: float = 60.0,
) -> None:
    """Consume a feed, reconnecting with backoff. A dropped socket must never
    end the recorder -- these feeds are quiet for minutes and then deafening."""
    backoff = 1.0
    while not stop.is_set():
        try:
            log.info("%s: connecting", name)
            stream = factory()
            backoff = 1.0  # reset on a successful connect, not on traffic:
                           # these feeds are silent for minutes at a time, and
                           # a socket that dies right after one event would
                           # otherwise reconnect in a 1/s hot loop.
            for liq in stream:
                try:
                    on_event(liq)
                except Exception as e:
                    # A sqlite hiccup must not tear down a healthy socket and
                    # trigger a full re-subscribe.
                    log.warning("%s: handler failed: %s", name, e)
                if stop.is_set():
                    return
        except WebSocketClosed as e:
            log.warning("%s: %s", name, e)
        except Exception as e:
            log.warning("%s: %s: %s", name, type(e).__name__, e)
        if stop.is_set():
            return
        # Jitter, so both feeds do not re-storm in lockstep after a blip.
        delay = backoff * (0.5 + random.random())
        log.info("%s: reconnecting in %.0fs", name, delay)
        stop.wait(delay)
        backoff = min(max_backoff, backoff * 2)
