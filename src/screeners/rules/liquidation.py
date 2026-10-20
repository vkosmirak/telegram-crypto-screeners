"""Liquidation screener.

This one cannot be backtested. Neither venue exposes historical liquidations
over REST -- they are websocket-only, so the dataset has to be *recorded*
before any of this can be measured (`screeners record-liquidations`). It is
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
        self._day_max: dict[tuple[str, str], tuple[int, float]] = {}

    def evaluate_event(self, liq: Liquidation) -> Signal | None:
        """Stateful by necessity: liquidations arrive as a stream, not as bars."""
        cfg = self.cfg
        if liq.symbol in cfg.exclude:
            return None
        usd = liq.usd
        key = (liq.exchange.value, liq.symbol)
        day = int(liq.ts // 86_400_000)

        # "The largest liquidation of the day is the target" -- every print
        # counts toward the day's max, not just the ones that alerted.
        # Fading a squeezed short that is the day's largest was the one
        # liquidation read with an edge in the backtest (+1.1-1.7pp over a
        # same-time control, scripts/liq_signals.py).
        max_day, prev_max = self._day_max.get(key, (day, 0.0))
        prev_max = prev_max if max_day == day else 0.0
        self._day_max[key] = (day, max(prev_max, usd))
        day_high = usd > prev_max

        if usd < cfg.min_usd:
            return None

        last = self._last_fire.get(key)  # absent means never fired, not "fired at epoch"
        # A new day high is the print that matters most, so it is never
        # swallowed by the cooldown of a smaller one a minute earlier.
        if last is not None and liq.ts - last < cfg.cooldown_min * 60_000 and not day_high:
            return None
        self._last_fire[key] = liq.ts

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
                     "liquidated_side": 1.0 if liq.side is Side.LONG else -1.0,
                     "day_high": 1.0 if day_high else 0.0},
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
