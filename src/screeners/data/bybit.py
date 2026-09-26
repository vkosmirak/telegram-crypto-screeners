"""Bybit v5 linear (USDT perpetual) history.

Bybit klines do *not* report taker-buy volume, so CVD cannot be reconstructed
from history here. Rules that depend on CVD degrade to "filter unavailable"
rather than failing -- see rules/base.py. Live, `publicTrade.{symbol}` does
give signed trades, so a live-recorded CVD is possible later; it just cannot be
backfilled.

Open interest, unlike on Binance, is pushed on the `tickers.{symbol}` websocket
at 100ms and is available historically via cursor paging.
"""
from __future__ import annotations

import logging

from ..models import Bar, Exchange
from .http import WeightBudget, get_json

log = logging.getLogger(__name__)

API = "https://api.bybit.com"

# Public endpoints allow 600 req / 5s per IP. This is deliberately far under.
BUDGET = WeightBudget(600, window_s=5.0, headroom=0.5, name="bybit")

KLINE_LIMIT = 1000
OI_LIMIT = 200
# 30d of 1m bars is 43200 = 44 pages; generous headroom, and a hard stop
# so a server-side cursor regression cannot hang the CLI.
MAX_PAGES = 120

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}
# Bybit spells kline intervals as bare minute counts, and OI intervals differently again.
_KLINE_IV = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
             "1h": "60", "4h": "240", "1d": "D"}
_OI_IV = {"5m": "5min", "15m": "15min", "30m": "30min",
          "1h": "1h", "4h": "4h", "1d": "1d"}


def _result(payload: object) -> dict:
    assert isinstance(payload, dict)
    if payload.get("retCode") != 0:
        raise RuntimeError(f"bybit error {payload.get('retCode')}: {payload.get('retMsg')}")
    return payload.get("result") or {}


# Bybit lists tokenized stocks, ETFs, commodities and forex as linear
# perpetuals right beside crypto, tagged by `symbolType`. As of 2026-09 that
# was 257 of 777 USDT perps (e.g. AAPLUSDT, BIIBUSDT, MSFUUSDT), and they were
# reaching the crypto screeners. Allow-list rather than block-list, so a
# category Bybit adds later is excluded until someone decides otherwise.
# "" is ordinary crypto; "innovation" is Bybit's zone for new token listings.
CRYPTO_SYMBOL_TYPES = frozenset({"", "innovation"})


def universe(quote: str = "USDT") -> list[str]:
    """Crypto linear perpetuals currently trading. ~520 for USDT as of 2026-09."""
    out: list[str] = []
    cursor = None
    while True:
        res = _result(get_json(
            f"{API}/v5/market/instruments-info",
            {"category": "linear", "limit": 1000, "cursor": cursor},
            budget=BUDGET,
        ))
        for i in res.get("list", []):
            if (i.get("quoteCoin") == quote and i.get("status") == "Trading"
                    and i.get("contractType") == "LinearPerpetual"
                    and (i.get("symbolType") or "") in CRYPTO_SYMBOL_TYPES):
                out.append(i["symbol"])
        cursor = res.get("nextPageCursor")
        if not cursor:
            break
    return sorted(set(out))


def top_by_turnover(n: int, quote: str = "USDT") -> list[str]:
    res = _result(get_json(f"{API}/v5/market/tickers", {"category": "linear"}, budget=BUDGET))
    perps = set(universe(quote))
    rows = [t for t in res.get("list", []) if t["symbol"] in perps]
    rows.sort(key=lambda t: float(t.get("turnover24h") or 0), reverse=True)
    return [t["symbol"] for t in rows[:n]]


def klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Bar]:
    """OHLCV. `taker_buy` stays None -- Bybit does not report it on klines.

    `/v5/market/kline` is **end-anchored**, like Binance's OI endpoint and
    unlike Binance's klines: it returns the newest `limit` bars of the range.
    Paging forward is therefore a single-page no-op that silently truncates a
    30-day request to ~3.5 days, so walk backwards from `end` instead.
    """
    iv = _KLINE_IV[interval]
    seen: dict[int, Bar] = {}
    cursor_end = end_ms
    for _ in range(MAX_PAGES):
        res = _result(get_json(
            f"{API}/v5/market/kline",
            {"category": "linear", "symbol": symbol, "interval": iv,
             "start": start_ms, "end": cursor_end, "limit": KLINE_LIMIT},
            budget=BUDGET,
        ))
        rows = res.get("list", [])
        if not rows:
            break
        for r in rows:  # bybit returns newest-first
            ts = int(r[0])
            seen[ts] = Bar(ts=ts, open=float(r[1]), high=float(r[2]), low=float(r[3]),
                           close=float(r[4]), volume=float(r[5]), taker_buy=None)
        oldest = min(int(r[0]) for r in rows)
        if oldest <= start_ms or len(rows) < KLINE_LIMIT:
            break
        nxt = oldest - 1
        if nxt >= cursor_end:
            break
        cursor_end = nxt
    return [seen[t] for t in sorted(seen) if start_ms <= t < end_ms]


def open_interest(symbol: str, period: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    """(timestamp, open interest in base units), walked backwards via cursor."""
    iv = _OI_IV[period]
    seen: dict[int, float] = {}
    cursor = None
    for _ in range(200):  # hard stop; 200 pages * 200 rows covers far more than retention
        res = _result(get_json(
            f"{API}/v5/market/open-interest",
            {"category": "linear", "symbol": symbol, "intervalTime": iv,
             "startTime": start_ms, "endTime": end_ms, "limit": OI_LIMIT, "cursor": cursor},
            budget=BUDGET,
        ))
        rows = res.get("list", [])
        if not rows:
            break
        for r in rows:
            seen[int(r["timestamp"])] = float(r["openInterest"])
        cursor = res.get("nextPageCursor")
        if not cursor:
            break
    return [(t, seen[t]) for t in sorted(seen) if start_ms <= t < end_ms]


EXCHANGE = Exchange.BYBIT
