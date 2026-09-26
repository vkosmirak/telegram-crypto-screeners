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


if __name__ == "__main__":
    unittest.main()
