"""Liquidation screener.

This one cannot be backtested. Neither venue exposes historical liquidations
over REST -- they are websocket-only, so the dataset has to be *recorded*
before any of this can be measured (`etb record-liquidations`). It is
implemented here so the live screener is complete and so the recorder has
something to write against.

The read from the video: price is often walked into a cluster of resting
liquidations, and once the largest one is taken the move reverses. So the
signal fades the liquidation:

    a LONG was liquidated  -> forced selling -> look for a bounce  -> LONG
    a SHORT was liquidated -> forced buying  -> look for a fade    -> SHORT

Side normalisation differs per venue and is handled by `normalise_side` below.
The two feeds are *inverted* relative to each other, which is an easy way to
build a screener that is exactly backwards on one of them.
"""
from __future__ import annotations

from ..config import LiquidationConfig
from ..models import Exchange, Liquidation, Side, Signal


class LiquidationRule:
    name = "liquidation"

    def __init__(self, cfg: LiquidationConfig):
        self.cfg = cfg
        self.cooldown_min = cfg.cooldown_min
        self.max_ordinal = 0
        self._last_fire: dict[tuple[str, str], int] = {}
        self._day: dict[tuple[str, str], int] = {}
        self._ordinal: dict[tuple[str, str], int] = {}

    def evaluate_event(self, liq: Liquidation) -> Signal | None:
        """Stateful by necessity: liquidations arrive as a stream, not as bars."""
        cfg = self.cfg
        if liq.symbol in cfg.exclude:
            return None
        usd = liq.usd
        if usd < cfg.min_usd:
            return None

        key = (liq.exchange.value, liq.symbol)
        last = self._last_fire.get(key)  # absent means never fired, not "fired at epoch"
        if last is not None and liq.ts - last < cfg.cooldown_min * 60_000:
            return None
        self._last_fire[key] = liq.ts

        day = int(liq.ts // 86_400_000)
        if self._day.get(key) != day:
            self._day[key] = day
            self._ordinal[key] = 0
        self._ordinal[key] += 1

        # Fade the forced flow.
        side = Side.LONG if liq.side is Side.LONG else Side.SHORT
        return Signal(
            rule=self.name, exchange=liq.exchange, symbol=liq.symbol,
            side=side, ts=liq.ts, price=liq.price, ordinal=self._ordinal[key],
            metrics={"usd": usd, "qty": liq.qty,
                     "liquidated_side": 1.0 if liq.side is Side.LONG else -1.0},
            filters={},
        )


def normalise_side(exchange: Exchange, raw_side: str) -> Side:
    """Return the side of the position that was liquidated.

    The two venues mean opposite things by the same letter, so this must be
    done per venue:

    * Binance `!forceOrder@arr` -- `o` is a standard order object and `o.S` is
      that *order's* side. A long is closed by a SELL, so SELL => LONG died.
    * Bybit `allLiquidation` -- `S` is the *position* side. The docs are
      explicit: "Position side. Buy, Sell. When you receive a Buy update, this
      means that a long position has been liquidated." So BUY => LONG died.

    Getting this backwards on one venue would make that venue's signals point
    the wrong way while still looking perfectly plausible.
    """
    s = raw_side.upper()
    if exchange is Exchange.BINANCE:
        return Side.LONG if s == "SELL" else Side.SHORT
    return Side.LONG if s == "BUY" else Side.SHORT
