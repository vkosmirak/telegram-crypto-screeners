"""Replay rules over history and score what they fired.

The rules compute every filter on every trigger firing, and `scan` numbers the
firings. That means one pass produces enough information to price *any* subset
of filters afterwards -- no refetch, no rescan per combination. Ablation is
just a predicate over `Signal.filters`.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field

from ..config import Rules
from ..data.cache import Cache
from ..data.series import Series, load_many
from ..models import Exchange, Side, Signal
from ..rules.base import Rule, scan
from ..rules.oi_growth import OIGrowthRule
from ..rules.pump import PumpRule
from .stats import Outcome, Summary, score, summarise

log = logging.getLogger(__name__)


@dataclass(slots=True)
class BacktestResult:
    exchange: Exchange
    rule: str
    interval: str
    start_ms: int
    end_ms: int
    symbols: int
    outcomes: list[Outcome] = field(default_factory=list)
    horizons: tuple[int, ...] = (5, 15, 30, 60, 240)
    cvd_available: bool = True
    baseline: list[Outcome] = field(default_factory=list)

    def subset(self, require: list[str] | None = None) -> list[Outcome]:
        """Outcomes whose named filters all passed. `None` means no filtering."""
        if not require:
            return self.outcomes
        out = []
        for o in self.outcomes:
            f = o.signal.filters
            # A filter that was never computed (e.g. CVD on Bybit) cannot
            # exclude a signal -- absence is not failure.
            if all(f.get(name, True) for name in require):
                out.append(o)
        return out

    def filter_names(self) -> list[str]:
        names: list[str] = []
        for o in self.outcomes:
            for k in o.signal.filters:
                if k not in names:
                    names.append(k)
        return names

    def ablation(self) -> list[Summary]:
        """Baseline first, then the bare trigger, each filter alone, and all of them.

        The baseline is the whole point. In a trending month a long screener
        can show a 90% hit rate purely because everything went up; the only
        way to see whether the rule carries information is to compare it with
        random entries on the same symbols over the same window, scored the
        same way.
        """
        names = self.filter_names()
        total = len(self.outcomes)
        rows = []
        if self.baseline:
            rows.append(summarise("BASELINE same-time peer", self.baseline, self.horizons))
        rows.append(summarise("trigger only", self.outcomes, self.horizons))
        for n in names:
            # A filter absent from a signal cannot exclude it, so a row can be
            # a blend of "passed" and "never computed". Saying how many were
            # actually computed stops a diluted row reading as a clean one.
            computed = sum(1 for o in self.outcomes if n in o.signal.filters)
            tag = f"+ {n}" if computed == total else f"+ {n} [{computed}/{total} eval]"
            rows.append(summarise(tag, self.subset([n]), self.horizons))
        if len(names) > 1:
            rows.append(summarise("+ all filters", self.subset(names), self.horizons))
        return rows

    def edge(self, h: int, require: list[str] | None = None) -> float | None:
        """Hit-rate percentage points above random entry at horizon h."""
        if not self.baseline:
            return None
        a = summarise("x", self.subset(require), self.horizons).per_horizon.get(h)
        b = summarise("y", self.baseline, self.horizons).per_horizon.get(h)
        return None if not a or not b else a["hit"] - b["hit"]


def _index_by_ts(series: Series) -> dict[int, int]:
    return {b.ts: i for i, b in enumerate(series.bars)}


def run_backtest(
    exchange: Exchange,
    rule: Rule,
    symbols: list[str],
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    horizons: tuple[int, ...] = (5, 15, 30, 60, 240),
    cache: Cache | None = None,
    workers: int = 8,
    on_progress=None,
    seed: int = 0,
) -> BacktestResult:
    """Fetch, scan, score. Returns every trigger firing with its filter results."""
    series_map = load_many(
        exchange, symbols, interval, start_ms, end_ms,
        with_oi=True, cache=cache, workers=workers, on_progress=on_progress,
    )

    outcomes: list[Outcome] = []
    cvd_seen = False
    for sym, series in series_map.items():
        if series.has_cvd:
            cvd_seen = True
        idx = _index_by_ts(series)
        for sig in scan(rule, series):
            i = idx.get(sig.ts)
            if i is None:
                continue
            outcomes.append(score(sig, series, i, horizons))

    outcomes.sort(key=lambda o: o.signal.ts)
    baseline = _paired_baseline(
        series_map, [o.signal for o in outcomes], rule, horizons, seed=seed,
    )
    return BacktestResult(
        exchange=exchange, rule=rule.name, interval=interval,
        start_ms=start_ms, end_ms=end_ms, symbols=len(series_map),
        outcomes=outcomes, horizons=horizons, cvd_available=cvd_seen,
        baseline=baseline,
    )


def _paired_baseline(
    series_map: dict[str, Series],
    signals: list[Signal],
    rule: Rule,
    horizons: tuple[int, ...],
    per_signal: int = 5,
    seed: int = 0,
) -> list[Outcome]:
    """A control matched in TIME to the signals it is compared against.

    The previous version drew bars uniformly from every symbol. That made the
    control roughly half "coins that never move", while the signal set is
    concentrated on whatever was volatile -- so "edge over random entry" was
    partly measuring symbol selection and partly measuring that signals
    cluster on the month's big days. Both confounds flatter the rule.

    Instead, for every signal at time t on symbol S, draw random bars at the
    SAME t on other symbols. The question becomes the one that matters for a
    screener: at this moment, in this market, did the coin it picked beat a
    coin picked at random? Market-wide moves cancel, because both sides live
    through them.

    This deliberately does not also match on symbol -- doing both at once
    leaves nothing to vary. Symbol selection is part of what the screener
    claims to do, so it stays inside the measurement.
    """
    if not signals:
        return []
    side = getattr(rule, "side", None) or Side.LONG
    rng = random.Random(seed)
    symbols = list(series_map)
    # ts -> index, per symbol. Built once; far cheaper than materialising
    # every (series, bar) pair, which for a 1m pump run is millions of tuples.
    idx = {sym: {b.ts: i for i, b in enumerate(s.bars)}
           for sym, s in series_map.items()}

    out: list[Outcome] = []
    for sig in signals:
        pool = [s for s in symbols if s != sig.symbol and sig.ts in idx[s]]
        if not pool:
            continue
        for sym in rng.sample(pool, min(per_signal, len(pool))):
            series = series_map[sym]
            i = idx[sym][sig.ts]
            bar = series.bars[i]
            if bar.close <= 0:
                continue
            ctrl = Signal(rule=f"baseline_{rule.name}", exchange=series.exchange,
                          symbol=sym, side=side, ts=bar.ts, price=bar.close,
                          ordinal=0)
            out.append(score(ctrl, series, i, horizons))
    return out


def build_rule(name: str, rules: Rules) -> Rule:
    match name:
        case "oi_growth":
            return OIGrowthRule(rules.oi_growth)
        case "pump_short":
            return PumpRule(rules.pump, Side.SHORT)
        case "pump_long":
            return PumpRule(rules.pump, Side.LONG)
        case _:
            raise SystemExit(
                f"unknown rule {name!r}. backtestable: oi_growth, pump_short, pump_long\n"
                "(liquidation has no historical feed -- see docs/investigation.md)"
            )


def interval_for(rule_name: str) -> str:
    """Bar size a rule needs.

    The OI rule is pinned to 5m because that is the finest open-interest
    history either venue retains; asking for 1m would silently forward-fill the
    same OI value across five bars and invent signals.
    """
    return "5m" if rule_name == "oi_growth" else "1m"
