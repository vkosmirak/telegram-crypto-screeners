"""Turn the recorded liquidation feed into signals, one variant per rule name,
in the journal's format -- so scripts/signal_pnl.py can score them.

Each variant is a read from the video (or an idea built on one). All fade the
forced flow: longs liquidated -> long, shorts liquidated -> short.

  liq_base      the live rule: one print >= $20k, alts only, 5m cooldown
  liq_biggest   ...and the biggest print on that symbol so far today (UTC):
                his "the largest liquidation of the day is the target"
  liq_cascade   same-side prints summed over 60s >= $20k, so a cascade of
                small prints counts like one big one
  liq_cascade_biggest  cascade total that is also the day's largest so far
  liq_after_pump  a short liquidation (squeeze) >= $5k within 20m after a
                pump_short trigger (sent or skipped) on the same symbol and venue

  PYTHONPATH=src python3 scripts/liq_signals.py liquidations.db signals_dir out_dir
"""
from __future__ import annotations

import calendar
import glob
import json
import sqlite3
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

from screeners.config import LiquidationConfig
from screeners.models import Exchange, Liquidation, Side
from screeners.rules.liquidation import LiquidationRule

MIN_USD = 20_000.0
EXCLUDE = {"BTCUSDT", "ETHUSDT"}
COOLDOWN_MS = 5 * 60_000
CASCADE_MS = 60_000
PUMP_LINK_MS = 20 * 60_000
DAY = 86_400_000


def iso(ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


def row(rule: str, venue: str, symbol: str, side: str, ts: int, price: float, usd: float) -> dict:
    return {"at": iso(ts), "venue": venue, "rule": rule, "symbol": symbol, "side": side,
            "bar": iso(ts), "ordinal": 1, "action": "sent", "failed": [], "price": price,
            "metrics": {"usd": round(usd, 2)}}


def main(db: str, signals_dir: str, out_dir: str) -> None:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    events = con.execute(
        "SELECT exchange, symbol, ts, side, qty, price FROM liquidation ORDER BY ts").fetchall()

    pumps = defaultdict(list)  # (venue, symbol) -> [ts of pump_short triggers]
    for f in glob.glob(f"{signals_dir}/*.jsonl"):
        for line in open(f):
            r = json.loads(line)
            if r["rule"] == "pump_short":
                t = calendar.timegm(time.strptime(r["at"], "%Y-%m-%dT%H:%M:%SZ")) * 1000
                pumps[(r["venue"], r["symbol"])].append(t)

    live = LiquidationRule(LiquidationConfig(min_usd=MIN_USD, exclude=tuple(EXCLUDE),
                                             cooldown_min=COOLDOWN_MS // 60_000))
    out: list[dict] = []
    day_max: dict[tuple, tuple[int, float]] = {}       # (venue, sym) -> (day, max print)
    casc_max: dict[tuple, tuple[int, float]] = {}      # (venue, sym) -> (day, max cascade)
    window: dict[tuple, deque] = defaultdict(deque)    # (venue, sym, side) -> prints
    last: dict[tuple, int] = {}                        # (rule, venue, sym) -> last fire

    def fire(rule, venue, sym, side, ts, price, usd):
        k = (rule, venue, sym)
        if k in last and ts - last[k] < COOLDOWN_MS:
            return
        last[k] = ts
        out.append(row(rule, venue, sym, side, ts, price, usd))

    for venue, sym, ts, side, qty, price in events:
        if sym in EXCLUDE:
            continue
        usd = qty * price
        day = ts // DAY
        key = (venue, sym)

        sig = live.evaluate_event(Liquidation(Exchange(venue), sym, ts, Side(side), qty, price))
        if sig:
            out.append(row("liq_base", venue, sym, side, ts, price, usd))

        prev_day, prev_max = day_max.get(key, (None, 0.0))
        prev_max = prev_max if prev_day == day else 0.0
        if usd >= MIN_USD and usd > prev_max:
            fire("liq_biggest", venue, sym, side, ts, price, usd)
        day_max[key] = (day, max(prev_max, usd))

        w = window[(venue, sym, side)]
        w.append((ts, usd))
        while w and ts - w[0][0] > CASCADE_MS:
            w.popleft()
        total = sum(u for _, u in w)
        if total >= MIN_USD:
            fire("liq_cascade", venue, sym, side, ts, price, total)
            cd, cm = casc_max.get(key, (None, 0.0))
            cm = cm if cd == day else 0.0
            if total > cm:
                fire("liq_cascade_biggest", venue, sym, side, ts, price, total)
            casc_max[key] = (day, max(cm, total))

        if side == "short" and usd >= MIN_USD / 4:
            # A squeeze after a pump: the blow-off his pump shorts wait for.
            if any(0 <= ts - p <= PUMP_LINK_MS for p in pumps.get(key, ())):
                fire("liq_after_pump", venue, sym, side, ts, price, usd)

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    by_day = defaultdict(list)
    for r in out:
        by_day[r["at"][:10]].append(r)
    for d, rows in by_day.items():
        with open(Path(out_dir) / f"{d}.jsonl", "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in sorted(rows, key=lambda r: r["at"]))
    counts = defaultdict(int)
    for r in out:
        counts[(r["venue"], r["rule"], r["side"])] += 1
    for k in sorted(counts):
        print(*k, counts[k])


if __name__ == "__main__":
    main(*sys.argv[1:4])
