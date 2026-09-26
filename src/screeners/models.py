"""The two records everything else is built from: a Bar and a Signal."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class Exchange(str, Enum):
    BINANCE = "binance"
    BYBIT = "bybit"


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV bar, optionally carrying taker-buy volume and open interest.

    `taker_buy` is base-asset volume bought by takers. Binance reports it on
    every kline; Bybit does not report it at all, so CVD-dependent filters are
    unavailable there and must degrade rather than crash.

    `oi` is open interest in base units as of the bar's close, merged in from
    a separate endpoint (neither venue puts OI on a kline). See
    `data.series.merge_oi` -- the anchor is the close so that OI and a
    close-to-close price change describe the same instant.
    """

    ts: int  # bar open time, epoch ms
    open: float
    high: float
    low: float
    close: float
    volume: float
    taker_buy: float | None = None
    oi: float | None = None

    @property
    def delta(self) -> float | None:
        """Taker buy volume minus taker sell volume. The per-bar increment of CVD."""
        if self.taker_buy is None:
            return None
        return 2.0 * self.taker_buy - self.volume

    @property
    def has_cvd(self) -> bool:
        return self.taker_buy is not None


@dataclass(frozen=True, slots=True)
class Signal:
    """A fired rule. Pure data -- the same object feeds the backtester and Telegram."""

    rule: str
    exchange: Exchange
    symbol: str
    side: Side
    ts: int  # bar open time that triggered, epoch ms
    price: float
    ordinal: int  # nth signal for this (rule, exchange, symbol) today, 1-based
    metrics: dict[str, float] = field(default_factory=dict)
    # Filters that were evaluated: name -> passed. Empty when filters are off.
    filters: dict[str, bool] = field(default_factory=dict)

    @property
    def passed_all_filters(self) -> bool:
        return all(self.filters.values())

    @property
    def dt(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(self.ts / 1000))

    def key(self) -> tuple[str, str, str, int]:
        return (self.rule, self.exchange.value, self.symbol, self.ts)


@dataclass(frozen=True, slots=True)
class Liquidation:
    """A single forced order. Only ever recorded live -- see docs/investigation.md."""

    exchange: Exchange
    symbol: str
    ts: int
    side: Side  # side of the position that got closed out
    qty: float
    price: float

    @property
    def usd(self) -> float:
        return self.qty * self.price
