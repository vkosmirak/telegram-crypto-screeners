"""Open-interest growth screener -- the long side.

Trigger, exactly as configured in the video: open interest up >= 5% over the
last 15 minutes. Everything else is a filter reproducing the reads he applies
to a signal before taking it:

  price_up     OI up *and* price up. OI up with price down is shorts stacking.
  cvd_up       market buys are leading, not just passive fills.
  volume_up    this window traded more than the one before it.
  flat_before  the move came out of a quiet range, not off a multi-day run.
  ordinal      only the first few signals of the day (applied in scan()).

Filters are individually togglable so the backtest can price each one instead
of taking his word for it.
"""
from __future__ import annotations

from ..config import OIGrowthConfig
from ..data.series import Series
from ..models import Side, Signal
from .base import bars_for, pct_change


class OIGrowthRule:
    name = "oi_growth"

    def __init__(self, cfg: OIGrowthConfig):
        self.cfg = cfg
        self.cooldown_min = cfg.cooldown_min
        self.max_ordinal = cfg.max_ordinal

    def evaluate(self, series: Series, i: int, cvd: list[float] | None) -> Signal | None:
        cfg = self.cfg
        w = bars_for(series.interval, cfg.window_min)
        j = i - w
        if j < 0:
            return None

        bars = series.bars
        now, then = bars[i], bars[j]
        if now.oi is None or then.oi is None or then.oi <= 0:
            return None

        oi_growth = pct_change(then.oi, now.oi)
        if oi_growth < cfg.growth_pct:
            return None

        price_change = pct_change(then.close, now.close)
        metrics = {
            "oi_growth_pct": oi_growth,
            "price_change_pct": price_change,
            "oi": now.oi,
            "oi_usd": now.oi * now.close,
        }
        filters: dict[str, bool] = {}

        if cfg.price_up:
            filters["price_up"] = price_change > 0

        if cfg.cvd_up:
            # Bybit klines carry no taker-buy volume, so this filter is simply
            # not computable there. Omitting it beats silently passing it.
            if cvd is not None:
                filters["cvd_up"] = cvd[i] > cvd[j]
                metrics["cvd_change"] = cvd[i] - cvd[j]
            else:
                metrics["cvd_available"] = 0.0

        if cfg.volume_up:
            # The comparison window must be the SAME length, or the baseline is
            # systematically undersized and the filter passes for free. At the
            # earliest firable bar it was empty, making prev=0 and the filter
            # unconditionally True while reporting a ratio of 0.0.
            k = j - w
            if k >= 0:
                cur = sum(b.volume for b in bars[j + 1 : i + 1])
                prev = sum(b.volume for b in bars[k + 1 : j + 1])
                if prev > 0:
                    filters["volume_up"] = cur > prev
                    metrics["volume_ratio"] = cur / prev
                else:
                    metrics["volume_available"] = 0.0
            else:
                metrics["volume_available"] = 0.0

        if cfg.flat_before:
            lb = bars_for(series.interval, cfg.flat_lookback_h * 60)
            start = j - lb
            # A truncated lookback means "we cannot see far enough back", not
            # "it was quiet". Passing there made the filter silently weakest
            # exactly where the data is thinnest.
            if start >= 0:
                prior = bars[start : j + 1]
                low = min(b.low for b in prior)
                runup = pct_change(low, then.close)
                filters["flat_before"] = runup < cfg.flat_max_runup_pct
                metrics["prior_runup_pct"] = runup
            else:
                metrics["flat_available"] = 0.0

        return Signal(
            rule=self.name, exchange=series.exchange, symbol=series.symbol,
            side=Side.LONG, ts=now.ts, price=now.close, ordinal=0,
            metrics=metrics, filters=filters,
        )
