"""Signal formatting.

Follows the card grammar from telegram-copy-trading (`tct/cards.py`): a bold
subject line carrying the verdict, a quoted block of detail, a muted footer.
Signals are read on a phone in a hurry, so the first line has to be enough.

Every card deep-links to Coinglass, which is what the video's bot does and
what makes a signal actionable in one tap -- OI and CVD are on that chart.
"""
from __future__ import annotations

import html
import time
import urllib.parse

from ..models import Side, Signal

COINGLASS = "https://www.coinglass.com/tv/{exchange}_{symbol}"
_EXCHANGE_SLUG = {"binance": "Binance", "bybit": "Bybit"}
# Second chart, for when Coinglass's heavy /tv page will not load (it hangs
# in Telegram's iOS in-app browser). ".P" is TradingView's perpetual suffix.
TRADINGVIEW = "https://www.tradingview.com/chart/?symbol={exchange}:{symbol}.P"

_TITLE = {
    "oi_growth": ("OI", "🟢"),
    "pump_long": ("PUMP", "🟢"),
    "pump_short": ("PUMP", "🔴"),
    "liquidation": ("LIQ", "⚡"),
}


def chart_url(signal: Signal) -> str:
    """Coinglass deep link. The symbol is percent-encoded and the result is
    HTML-escaped at the call site: it lands inside an href, and exchange
    symbols are attacker-adjacent data even if today they are all
    alphanumeric."""
    slug = _EXCHANGE_SLUG.get(signal.exchange.value, signal.exchange.value)
    return COINGLASS.format(exchange=slug,
                            symbol=urllib.parse.quote(signal.symbol, safe=""))


def tradingview_url(signal: Signal) -> str:
    """TradingView chart of the same perpetual, e.g. BYBIT:CLSKUSDT.P."""
    return TRADINGVIEW.format(exchange=signal.exchange.value.upper(),
                              symbol=urllib.parse.quote(signal.symbol, safe=""))


def fmt_usd(v: float) -> str:
    """$2.91M -- magnitudes, the way the screener quotes open interest."""
    for cut, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= cut:
            return f"{v / cut:.2f}{suffix} $"
    return f"{v:.0f} $"


def _pct(v: float) -> str:
    """5.1%, 12.46%, 5%, -1.26% -- up to two decimals, no forced plus sign,
    matching how the screener in the video prints percentages."""
    txt = f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{'0' if txt in ('-0', '') else txt}%"


def _fmt_price(p: float) -> str:
    if p >= 100:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}".rstrip("0").rstrip(".")
    return f"{p:.8f}".rstrip("0").rstrip(".")


# A venue is identified by a colour dot before its name, so a glance at the
# chat tells you which exchange fired without reading a word.
_VENUE_DOT = {"binance": "\U0001f7e1", "bybit": "\u26ab"}


def signal_card(signal: Signal, *, show_filters: bool = True) -> str:
    """One signal as Telegram HTML.

    The field set and ordering follow the screener demonstrated in the video,
    read off frames of it (see docs/investigation.md): venue, window and
    symbol on the header line, the screener's own magnitude next, then the
    supporting number, then the per-day counter. The symbol is the chart link.

    Our one addition is the optional `fails:` line. His bot sends the raw
    trigger and leaves the reads to the viewer -- his own OI signals go out
    with negative price change. We keep that behaviour but say which reads a
    signal flunks, because the backtest showed the filters are what carry the
    edge (docs/findings.md).
    """
    m = signal.metrics
    venue = signal.exchange.value
    dot = _VENUE_DOT.get(venue, "\u26aa")
    name = "Binance" if venue == "binance" else "ByBit"
    sym = html.escape(signal.symbol)
    link = html.escape(chart_url(signal), quote=True)

    window = int(m.get("window_min") or m.get("oi_window_min") or 0)
    win = f" \u2013 {window}m" if window else ""
    head = f'{dot} {name}{win} \u2013 <a href="{link}">{sym}</a>'

    lines: list[str] = []

    if "oi_growth_pct" in m:
        total = f' ({fmt_usd(m["oi_usd"])})' if "oi_usd" in m else ""
        lines.append(f'\U0001f4c8 <b>OI grew {_pct(m["oi_growth_pct"])}</b>{total}')
    if "price_change_pct" in m:
        lines.append(f'\U0001f4b2 Price change: {_pct(m["price_change_pct"])}')

    if "move_pct" in m:
        # Red for the big accelerated move he shorts, green for a small impulse.
        icon = "\U0001f534" if signal.side is Side.SHORT else "\U0001f4c8"
        rng = ""
        if "low" in m:
            rng = f' ({_fmt_price(m["low"])}-{_fmt_price(signal.price)})'
        lines.append(f'{icon} <b>Pump: {_pct(m["move_pct"])}</b>{rng}')
        if "oi_change_pct" in m:
            lines.append(f'\U0001f4b2 OI change: {_pct(m["oi_change_pct"])}')

    if "usd" in m:
        side_txt = "longs" if m.get("liquidated_side", 0) > 0 else "shorts"
        lines.append(f'\u26a1 <b>Liquidated {side_txt}: {fmt_usd(m["usd"])}</b>')
        lines.append(f'\U0001f4b2 Price: {_fmt_price(signal.price)}')

    lines.append(f'\U0001f514 Signal 24h: {signal.ordinal}')

    parts = [head, "\n".join(lines)]

    if show_filters and signal.filters:
        failed = [k for k, ok in signal.filters.items() if not ok]
        if failed:
            parts.append("\u26a0\ufe0f <i>fails: "
                         + ", ".join(html.escape(f) for f in failed) + "</i>")

    return "\n".join(parts)


def chart_button(signal: Signal) -> list[list[dict]]:
    return [[{"text": "📊 Coinglass", "url": chart_url(signal)},
             {"text": "📈 TradingView", "url": tradingview_url(signal)}]]
