"""Forward returns and hit rates.

A signal is scored by what price did *after* it, signed by the side it asked
for: a short that is followed by a fall scores positive. Entry is the close of
the triggering bar -- the first price you could realistically have acted on.

No position sizing, no stops, no fees. This measures whether the signal carries
information, which is the question worth answering before building an execution
layer on top of it.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from ..data.series import Series
from ..models import Side, Signal
from ..rules.base import bars_for


@dataclass(frozen=True, slots=True)
class Outcome:
    """One signal, plus signed return at each horizon and its best/worst excursion."""

    signal: Signal
    returns: dict[int, float]          # horizon minutes -> signed % return
    mfe: dict[int, float] = field(default_factory=dict)  # max favourable excursion
    mae: dict[int, float] = field(default_factory=dict)  # max adverse excursion


def score(signal: Signal, series: Series, i: int, horizons: tuple[int, ...]) -> Outcome:
    """Signed forward returns for one signal. Index i must be its triggering bar."""
    bars = series.bars
    entry = signal.price
    sign = 1.0 if signal.side is Side.LONG else -1.0
    returns: dict[int, float] = {}
    mfe: dict[int, float] = {}
    mae: dict[int, float] = {}

    for h in horizons:
        k = i + bars_for(series.interval, h)
        if k >= len(bars) or entry <= 0:
            continue
        returns[h] = sign * (bars[k].close / entry - 1.0) * 100.0
        path = bars[i + 1 : k + 1]
        if not path:
            continue
        if sign > 0:
            best = max(b.high for b in path) / entry - 1.0
            worst = min(b.low for b in path) / entry - 1.0
        else:
            best = 1.0 - min(b.low for b in path) / entry
            worst = 1.0 - max(b.high for b in path) / entry
        mfe[h] = best * 100.0
        mae[h] = worst * 100.0

    return Outcome(signal, returns, mfe, mae)


@dataclass(frozen=True, slots=True)
class Summary:
    label: str
    n: int
    per_horizon: dict[int, dict[str, float]]

    def line(self, h: int) -> str:
        """`n` is the count AT THIS HORIZON, not the total.

        Outcomes whose horizon ran off the end of the series are dropped from
        that horizon only, and signals (clustered) and control bars (spread)
        drop different fractions -- printing the total made rows that are not
        comparable look comparable."""
        s = self.per_horizon.get(h)
        if not s:
            return f"{self.label:<34} n={self.n:<6} +{h}m  (no data)"
        return (f"{self.label:<34} n={int(s['n']):<6} +{h:>3}m  "
                f"hit {s['hit']:5.1f}%  med {s['median']:+6.2f}%  "
                f"mean {s['mean']:+6.2f}%  mfe {s['mfe']:5.2f}%  mae {s['mae']:+6.2f}%")


def summarise(label: str, outcomes: list[Outcome], horizons: tuple[int, ...]) -> Summary:
    per: dict[int, dict[str, float]] = {}
    for h in horizons:
        rs = [o.returns[h] for o in outcomes if h in o.returns]
        if not rs:
            continue
        mfes = [o.mfe[h] for o in outcomes if h in o.mfe]
        maes = [o.mae[h] for o in outcomes if h in o.mae]
        per[h] = {
            "n": float(len(rs)),
            "hit": 100.0 * sum(1 for r in rs if r > 0) / len(rs),
            "median": statistics.median(rs),
            "mean": statistics.fmean(rs),
            "mfe": statistics.fmean(mfes) if mfes else 0.0,
            "mae": statistics.fmean(maes) if maes else 0.0,
        }
    return Summary(label, len(outcomes), per)
