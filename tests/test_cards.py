"""Card formatting.

Field set and ordering were read off frames of the source video, so these
tests pin the observed layout rather than our first guess at it.
"""
from __future__ import annotations

import unittest

from screeners.models import Exchange, Side, Signal
from screeners.notify.cards import (chart_button, chart_url, fmt_usd,
                                    signal_card, tradingview_url)


def sig(**kw):
    base = dict(rule="oi_growth", exchange=Exchange.BINANCE, symbol="BANDUSDT",
                side=Side.LONG, ts=1790400000000, price=1.2345, ordinal=1,
                metrics={}, filters={})
    base.update(kw)
    return Signal(**base)


class TestHelpers(unittest.TestCase):
    def test_usd_is_abbreviated_to_magnitudes(self):
        self.assertEqual(fmt_usd(2_910_000), "2.91M $")
        self.assertEqual(fmt_usd(58_070_000), "58.07M $")
        self.assertEqual(fmt_usd(55_000), "55.00K $")
        self.assertEqual(fmt_usd(900), "900 $")


class TestChartLinks(unittest.TestCase):
    def test_tradingview_points_at_the_venue_perpetual(self):
        self.assertEqual(
            tradingview_url(sig(exchange=Exchange.BYBIT, symbol="CLSKUSDT")),
            "https://www.tradingview.com/chart/?symbol=BYBIT:CLSKUSDT.P")
        self.assertIn("symbol=BINANCE:BANDUSDT.P", tradingview_url(sig()))

    def test_button_row_offers_coinglass_and_tradingview(self):
        s = sig()
        urls = [b["url"] for b in chart_button(s)[0]]
        self.assertEqual(urls, [chart_url(s), tradingview_url(s)])


class TestHeader(unittest.TestCase):
    def test_header_is_venue_window_symbol(self):
        s = sig(metrics={"oi_growth_pct": 12.46, "window_min": 30})
        head = signal_card(s).split("\n")[0]
        self.assertIn("Binance", head)
        self.assertIn("30m", head)
        self.assertIn("BANDUSDT", head)      # quote asset is NOT stripped
        self.assertIn(chart_url(s), head)     # the symbol is the chart link

    def test_each_venue_has_its_own_colour_dot(self):
        a = signal_card(sig(exchange=Exchange.BINANCE))[0]
        b = signal_card(sig(exchange=Exchange.BYBIT))[0]
        self.assertNotEqual(a, b)

    def test_bybit_is_spelled_the_way_the_screener_spells_it(self):
        self.assertIn("ByBit", signal_card(sig(exchange=Exchange.BYBIT)))


class TestBody(unittest.TestCase):
    def test_oi_card_carries_growth_total_price_and_counter(self):
        card = signal_card(sig(ordinal=8, metrics={
            "oi_growth_pct": 12.46, "oi_usd": 2_910_000,
            "price_change_pct": 1.5, "window_min": 30}))
        self.assertIn("OI grew 12.46%", card)
        self.assertIn("2.91M $", card)
        self.assertIn("Price change: 1.5%", card)
        self.assertIn("Signal 24h: 8", card)

    def test_negative_price_change_is_shown_not_suppressed(self):
        # His OI signals go out with price down; the read is the human's job.
        card = signal_card(sig(metrics={"oi_growth_pct": 5.1,
                                        "price_change_pct": -1.26}))
        self.assertIn("Price change: -1.26%", card)

    def test_pump_card_shows_the_price_range(self):
        card = signal_card(sig(rule="pump_short", side=Side.SHORT, price=0.6013,
                               metrics={"move_pct": 10.73, "window_min": 20,
                                        "low": 0.54301}))
        self.assertIn("Pump: 10.73%", card)
        self.assertIn("0.54301-0.6013", card)
        self.assertIn("20m", card)

    def test_liquidation_card_names_the_side_that_died(self):
        card = signal_card(sig(rule="liquidation", side=Side.LONG,
                               metrics={"usd": 55_000, "liquidated_side": 1.0}))
        self.assertIn("Liquidated longs", card)
        self.assertIn("55.00K $", card)


class TestNumberStyle(unittest.TestCase):
    """Percentages print the way the video's screener prints them."""

    def test_trailing_zeros_are_trimmed_and_no_plus_is_forced(self):
        card = signal_card(sig(metrics={"oi_growth_pct": 5.0, "price_change_pct": 1.5}))
        self.assertIn("OI grew 5%", card)
        self.assertIn("Price change: 1.5%", card)
        self.assertNotIn("+", card.split("\n", 1)[1])

    def test_oi_card_header_names_the_window(self):
        from screeners.config import OIGrowthConfig
        from screeners.data.series import Series
        from screeners.models import Bar
        from screeners.rules.base import scan
        from screeners.rules.oi_growth import OIGrowthRule
        MIN = 60_000
        oi = [100.0, 100.0, 100.0, 110.0]
        bars = [Bar(ts=n * 5 * MIN, open=1, high=1, low=1, close=1, volume=1,
                    oi=oi[n]) for n in range(4)]
        rule = OIGrowthRule(OIGrowthConfig(window_min=15, cooldown_min=0,
                                           max_ordinal=0, price_up=False, cvd_up=False,
                                           volume_up=False, flat_before=False))
        s = scan(rule, Series(Exchange.BINANCE, "BANDUSDT", "5m", bars))[0]
        self.assertIn("Binance \u2013 15m \u2013", signal_card(s).split("\n")[0])


class TestFilters(unittest.TestCase):
    def test_failing_filters_are_called_out(self):
        card = signal_card(sig(filters={"cvd_up": False, "price_up": True}))
        self.assertIn("fails:", card)
        self.assertIn("cvd_up", card.split("fails:")[1])
        self.assertNotIn("price_up", card.split("fails:")[1])

    def test_clean_signal_has_no_warning_line(self):
        self.assertNotIn("fails:", signal_card(sig(filters={"cvd_up": True})))

    def test_filters_can_be_suppressed(self):
        card = signal_card(sig(filters={"cvd_up": False}), show_filters=False)
        self.assertNotIn("fails:", card)


class TestEscaping(unittest.TestCase):
    def test_symbol_is_escaped_in_both_text_and_href(self):
        card = signal_card(sig(symbol='"><script>USDT'))
        self.assertNotIn("<script>", card)
        self.assertNotIn('"><script>', card)


if __name__ == "__main__":
    unittest.main()
