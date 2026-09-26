"""Rule protocol and the scan driver.

A rule decides only "did the trigger fire on this bar, and did its filters
pass". Everything stateful -- the per-day signal counter and the per-symbol
cooldown -- lives in `scan`, because both are properties of the stream, not of
a window.

The ordinal is assigned to every *trigger* firing, before filters. That is
deliberate: in the video the bot numbers every alert it sends and "only take
the first three of the day" is a judgement the human applies afterwards. If we
numbered only filtered signals, "#1 of the day" would mean something different
from what he demonstrates and the backtest would flatter itself.

Two caveats worth knowing, both deliberate:

* A bar suppressed by cooldown never gets an ordinal, so the numbering counts
  what the bot would have SENT, not every bar that met the threshold. Tuning
  `cooldown_min` therefore also retunes what `max_ordinal` means -- the two
  are not independent knobs.
* Numbering is per (rule, exchange, symbol), i.e. "third signal on this coin
  on this venue today", which is what the video's screenshots show.
"""
from __future__ import annotations

from typing import Iterable, Protocol

from ..models import Signal
from ..data.series import Series

INTERVAL_MIN = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
                "1h": 60, "4h": 240, "1d": 1440}


def bars_for(interval: str, minutes: float) -> int:
    """How many bars of `interval` span `minutes`. At least one."""
    return max(1, round(minutes / INTERVAL_MIN[interval]))


def pct_change(a: float, b: float) -> float:
    """Percent change from a to b. 0.0 when a is not usable."""
    return 0.0 if a == 0 else (b / a - 1.0) * 100.0


def utc_day(ts_ms: int) -> int:
    """Days since epoch, UTC. The reset boundary for the daily ordinal."""
    return int(ts_ms // 86_400_000)


class Rule(Protocol):
    name: str
    cooldown_min: int
    max_ordinal: int

    def evaluate(self, series: Series, i: int, cvd: list[float] | None) -> Signal | None:
        """Return an unnumbered Signal if the trigger fired on bar i, else None."""
        ...


class SignalState:
    """Per (rule, exchange, symbol) daily ordinal and cooldown.

    Held OUTSIDE `scan` so it can survive across calls. The backtester scans a
    whole series once and can happily use a throwaway instance; the live
    screener re-scans a rolling 8-hour tail every sweep, and if the counter
    reset each time then "signal #1-3 of the day" would silently mean "of the
    last 8 hours" -- so a signal that is genuinely #9 would pass the filter and
    get dispatched, disagreeing with the backtest on the one read the whole
    strategy leans on.

    It also de-duplicates: a bar that already fired can be re-scanned on the
    next sweep without emitting twice.
    """

    __slots__ = ("_day", "_ordinal", "_last_fire")

    def __init__(self) -> None:
        self._day: dict[tuple, int] = {}
        self._ordinal: dict[tuple, int] = {}
        self._last_fire: dict[tuple, int] = {}

    def may_fire(self, key: tuple, ts: int, cooldown_ms: int) -> bool:
        last = self._last_fire.get(key)
        if last is None:
            return True
        if ts <= last:
            return False  # already emitted for this bar or an older one
        return ts - last >= cooldown_ms

    def record(self, key: tuple, ts: int) -> int:
        """Mark a firing and return its ordinal for the day."""
        day = utc_day(ts)
        if self._day.get(key) != day:
            self._day[key] = day
            self._ordinal[key] = 0
        self._ordinal[key] += 1
        self._last_fire[key] = ts
        return self._ordinal[key]


def scan(rule: Rule, series: Series, state: SignalState | None = None,
         since_ts: int | None = None) -> list[Signal]:
    """Replay a series bar by bar, numbering and de-duplicating what fires.

    Strictly causal: bar i is evaluated using bars <= i only. No lookahead.

    Pass `state` to keep numbering and cooldown continuous across calls; omit
    it for a one-shot scan over a complete series. Pass `since_ts` to skip
    evaluating bars older than it -- the live screener uses this so each
    sweep only re-judges the recent edge of an 8-hour window instead of all
    of it. Skipped bars still count as history for the bars that are judged.
    """
    from dataclasses import replace

    out: list[Signal] = []
    cvd = series.cvd()
    cooldown_ms = rule.cooldown_min * 60_000
    st = state if state is not None else SignalState()
    key = (rule.name, series.exchange.value, series.symbol)

    for i in range(len(series.bars)):
        bar = series.bars[i]
        if since_ts is not None and bar.ts < since_ts:
            continue
        if not st.may_fire(key, bar.ts, cooldown_ms):
            continue
        sig = rule.evaluate(series, i, cvd)
        if sig is None:
            continue

        ordinal = st.record(key, bar.ts)
        filters = dict(sig.filters)
        if rule.max_ordinal:
            filters["ordinal"] = ordinal <= rule.max_ordinal
        out.append(replace(sig, ordinal=ordinal, filters=filters))

    return out


def scan_all(rule: Rule, all_series: Iterable[Series]) -> list[Signal]:
    """Scan many symbols, returned in chronological order."""
    out: list[Signal] = []
    for s in all_series:
        out.extend(scan(rule, s))
    out.sort(key=lambda s: s.ts)
    return out
