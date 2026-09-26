"""The universe is crypto only. Both venues list non-crypto perpetuals."""
from __future__ import annotations

import unittest
from unittest import mock

from screeners.data import binance, bybit


def by_row(symbol, symbol_type=""):
    return {"symbol": symbol, "quoteCoin": "USDT", "status": "Trading",
            "contractType": "LinearPerpetual", "symbolType": symbol_type}


class TestBybitUniverse(unittest.TestCase):
    def test_stocks_etfs_commodities_and_forex_are_excluded(self):
        rows = [by_row("BTCUSDT"), by_row("NEWTOKUSDT", "innovation"),
                by_row("AAPLUSDT", "stock"), by_row("MSFUUSDT", "ETF"),
                by_row("XAUUSDT", "commodity"), by_row("EURUSDT", "forex")]
        payload = {"retCode": 0, "result": {"list": rows, "nextPageCursor": ""}}
        with mock.patch.object(bybit, "get_json", return_value=payload):
            self.assertEqual(bybit.universe(), ["BTCUSDT", "NEWTOKUSDT"])

    def test_an_unknown_new_category_is_excluded_by_default(self):
        rows = [by_row("BTCUSDT"), by_row("WEIRDUSDT", "bond")]
        payload = {"retCode": 0, "result": {"list": rows, "nextPageCursor": ""}}
        with mock.patch.object(bybit, "get_json", return_value=payload):
            self.assertEqual(bybit.universe(), ["BTCUSDT"])


class TestBinanceUniverse(unittest.TestCase):
    def test_index_perpetuals_are_excluded(self):
        rows = [{"symbol": s, "contractType": "PERPETUAL", "quoteAsset": "USDT",
                 "status": "TRADING", "underlyingType": t}
                for s, t in (("BTCUSDT", "COIN"), ("BTCDOMUSDT", "INDEX"))]
        with mock.patch.object(binance, "get_json", return_value={"symbols": rows}):
            self.assertEqual(binance.universe(), ["BTCUSDT"])


class TestBybitInBodyRateLimit(unittest.TestCase):
    """Bybit reports its rate limit as HTTP 200 + retCode 10006. It used to
    reach _result as a plain error and the symbol just failed."""

    def test_throttled_reply_is_retried_then_succeeds(self):
        limited = {"retCode": 10006, "retMsg": "Too many visits"}
        ok = {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}
        with mock.patch.object(bybit, "get_json", side_effect=[limited, limited, ok]) as g, \
             mock.patch.object(bybit.time, "sleep"), \
             mock.patch.object(bybit.BUDGET, "penalize") as pen:
            self.assertEqual(bybit.universe(), [])
        self.assertEqual(g.call_count, 3)
        self.assertEqual(pen.call_count, 2)   # the whole pool paused each time

    def test_persistent_throttling_still_surfaces_as_an_error(self):
        limited = {"retCode": 10006, "retMsg": "Too many visits"}
        with mock.patch.object(bybit, "get_json", return_value=limited), \
             mock.patch.object(bybit.time, "sleep"), \
             mock.patch.object(bybit.BUDGET, "penalize"):
            with self.assertRaises(RuntimeError):
                bybit.universe()

    def test_a_burst_of_throttles_logs_one_summary(self):
        limited = {"retCode": 10006, "retMsg": "Too many visits"}
        ok = {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}
        bybit._throttle.update(n=0, worst=0, since=0.0, endpoints=set())
        with mock.patch.object(bybit, "get_json", side_effect=[limited, ok] * 20), \
             mock.patch.object(bybit.time, "sleep"), \
             mock.patch.object(bybit.BUDGET, "penalize"), \
             self.assertLogs("screeners.data.bybit", level="WARNING") as cm:
            for _ in range(20):
                bybit.universe()
        self.assertEqual(len(cm.output), 1)

    def test_other_errors_are_not_retried(self):
        bad = {"retCode": 10001, "retMsg": "params error"}
        with mock.patch.object(bybit, "get_json", return_value=bad) as g:
            with self.assertRaises(RuntimeError):
                bybit.universe()
        self.assertEqual(g.call_count, 1)


if __name__ == "__main__":
    unittest.main()
