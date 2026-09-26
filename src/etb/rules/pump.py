"""Pump screener -- sharp price moves.

Both presets detect the same thing, an *upward* move; they differ in size and
in what you do about it:

  long   2% within 2 minutes   -- catch an impulse early
  short  10% within 20 minutes -- fade the accelerated move

"Within N minutes" is a sliding window, exactly as described: a 10%/20min rule
fires as soon as the move is 10%, even if it took 5 minutes, and never fires if
the same 10% took half an hour. Measured lowest-low-to-close over the window;
close rather than high, so a wick alone does not trigger an alert you could not
have acted on.

Filters check that the flow agrees with the side we intend to take:

  cvd_confirms  short: CVD falling while price rises = being sold into.
                long:  CVD rising with price = real buying.
  oi_confirms   short: OI falling = positions closing, not new money.
                long:  OI rising = new money behind the move.
"""
from __future__ import annotations

from ..config import PumpConfig, PumpSideConfig
from ..data.series import Series
from ..models import Side, Signal
from .base import bars_for, pct_change


class PumpRule:
    """One instance per side; the two presets are independent screeners."""

    def __init__(self, cfg: PumpConfig, side: Side):
        self.cfg = cfg
        self.side = side
        self.side_cfg: PumpSideConfig = cfg.short if side is Side.SHORT else cfg.long
        self.name = f"pump_{side.value}"
        self.cooldown_min = cfg.cooldown_min
        self.max_ordinal = cfg.max_ordinal

    def evaluate(self, series: Series, i: int, cvd: list[float] | None) -> Signal | None:
        cfg, sc = self.cfg, self.side_cfg
        if not sc.enabled:
            return None

        w = bars_for(series.interval, sc.window_min)
        # A partial window is legitimate here, unlike in oi_growth: this rule
        # asks "did price move X% WITHIN N minutes", and a move completed in
        # six minutes satisfies a twenty-minute rule. Only the window's upper
        # bound matters.
        j = max(0, i - w + 1)
        # The old `if i - j + 1 < 2` guard rejected any single-bar window,
        # which silently disabled the documented window_min=1 setting -- the
        # screener just went dark, with no warning and no error.
        if i < j:
            return None

        bars = series.bars
        window = bars[j : i + 1]
        low = min(b.low for b in window)
        now = bars[i]
        move = pct_change(low, now.close)
        if move < sc.move_pct:
            return None

        metrics = {
            "move_pct": move,
            "window_min": float(sc.window_min),
            "low": low,
            "volume": sum(b.volume for b in window),
        }
        filters: dict[str, bool] = {}
        want_rise = self.side is Side.LONG

        if cfg.cvd_down:
            # cvd[k] includes bar k's own delta, so the change ACROSS the
            # window [j..i] is cvd[i] - cvd[j-1]. Using cvd[j] dropped the
            # first bar of the pump -- for the 2-bar long preset that left a
            # single bar of CVD and routinely voted the wrong way.
            if cvd is not None and j >= 1:
                change = cvd[i] - cvd[j - 1]
                filters["cvd_confirms"] = change > 0 if want_rise else change < 0
                metrics["cvd_change"] = change
            else:
                metrics["cvd_available"] = 0.0

        if cfg.oi_down:
            # Span the whole window, matching CVD above.
            oi_now = now.oi
            oi_then = bars[j - 1].oi if j >= 1 else None
            if oi_now is not None and oi_then is not None and oi_then > 0:
                change = pct_change(oi_then, oi_now)
                if change == 0.0:
                    # OI history is 5m at best, so a short window on 1m bars
                    # often reads the SAME forward-filled sample at both ends.
                    # That is "no information", not "did not move" -- calling
                    # it False left pump_long structurally unable to fire.
                    metrics["oi_available"] = 0.0
                else:
                    filters["oi_confirms"] = change > 0 if want_rise else change < 0
                metrics["oi_change_pct"] = change
            else:
                metrics["oi_available"] = 0.0

        return Signal(
            rule=self.name, exchange=series.exchange, symbol=series.symbol,
            side=self.side, ts=now.ts, price=now.close, ordinal=0,
            metrics=metrics, filters=filters,
        )
