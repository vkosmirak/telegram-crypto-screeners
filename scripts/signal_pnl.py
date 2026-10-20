"""What would trading the live signals have earned?

Replays every signal in data/signals/*.jsonl (sent AND skipped) on 1m bars and
runs a few exit plans over it, including the two the video describes:

  long  -- enter on the signal, stop "under the start of the move"
  short -- enter on the signal, average up in parts as the pump continues

Entry is the open of the first full minute after the signal was logged
(a human needs at least that long). Costs: COST_PER_SIDE on every fill,
fee + slippage. When a bar touches both stop and target, the stop wins.

Run from a laptop, not hetzner: kline fetches would eat Jesse's Binance budget.

  PYTHONPATH=src python3 scripts/signal_pnl.py /path/to/signals_dir [out_dir]
"""
from __future__ import annotations

import csv
import glob
import json
import random
import statistics as st
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from screeners.data import binance, bybit
from screeners.data.cache import Cache
from screeners.data.http import WeightBudget

MIN = 60_000
DAY = 86_400_000
COST_PER_SIDE = 0.001  # 0.05% taker + ~0.05% slippage on thin alts
PRE_MIN = 30           # bars kept before the signal (for the "start of the move")
MAX_HOLD_MIN = 24 * 60
CONTROLS = 5           # same-time peers per signal, for the baseline
OUT = Path("out")

# A laptop has its own IP, so the full limit is ours -- but stay polite.
binance.FAPI_BUDGET = WeightBudget(2400, window_s=60.0, headroom=0.7, name="binance /fapi")
FETCH = {"binance": binance.klines, "bybit": bybit.klines}


def ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def load(src: str) -> list[dict]:
    rows = []
    for f in sorted(glob.glob(f"{src}/*.jsonl")):
        rows += [json.loads(l) for l in open(f) if l.strip()]
    return rows


# ---- bars: one fetch per (venue, symbol, UTC day), covering that day's
# signals plus the longest hold -------------------------------------------------

cache = Cache()


def fetch_day(key: tuple[str, str, int]) -> tuple[tuple, list[list]]:
    venue, symbol, day = key
    start, end = day - PRE_MIN * MIN, day + DAY + MAX_HOLD_MIN * MIN + MIN
    end = min(end, int(datetime.now(timezone.utc).timestamp() * 1000) // MIN * MIN)
    hit = cache.get("pnl_klines", venue, symbol, "1m", start, end)
    if hit is None:
        try:
            bars = FETCH[venue](symbol, "1m", start, end)
        except Exception as e:  # delisted symbols, odd names
            print(f"  ! {venue} {symbol}: {e}", file=sys.stderr)
            bars = []
        hit = [[b.ts, b.open, b.high, b.low, b.close] for b in bars]
        cache.put("pnl_klines", venue, symbol, "1m", start, end, hit)
    return key, hit


# ---- exit plans. Each takes (entry_idx, bars, signal) and returns
# % P&L on the full intended size, net of costs, or None if untradeable ---------

def _fill(side: str, entry: float, exit_: float) -> float:
    gross = (exit_ / entry - 1) if side == "long" else (1 - exit_ / entry)
    return gross - 2 * COST_PER_SIDE


def hold(minutes: int):
    def plan(i, bars, s):
        j = i + minutes - 1
        if j >= len(bars):
            return None
        return _fill(s["side"], bars[i][1], bars[j][4])
    return plan


def bracket(stop_pct: float, tp_pct: float, max_min: int):
    """Fixed % stop and target from the entry price."""
    def plan(i, bars, s):
        e = bars[i][1]
        long = s["side"] == "long"
        stop = e * (1 - stop_pct) if long else e * (1 + stop_pct)
        tp = e * (1 + tp_pct) if long else e * (1 - tp_pct)
        return _walk(i, bars, s["side"], e, stop, tp, max_min)
    return plan


def video_long(rr: float | None, max_min: int):
    """Stop under the start of the move: lowest low of the 15m signal window.
    rr=None means no target, only the stop and the time stop."""
    def plan(i, bars, s):
        e = bars[i][1]
        stop = min(b[3] for b in bars[max(0, i - 15):i]) if i else None
        if not stop or stop >= e:
            return None
        tp = e + rr * (e - stop) if rr else float("inf")
        return _walk(i, bars, "long", e, stop, tp, max_min)
    return plan


def _walk(i, bars, side, e, stop, tp, max_min):
    end = min(len(bars), i + max_min)
    if end - i < max_min and end == len(bars):
        return None  # not enough future yet
    for b in bars[i:end]:
        if side == "long":
            if b[3] <= stop:
                return _fill(side, e, stop)
            if b[2] >= tp:
                return _fill(side, e, tp)
        else:
            if b[2] >= stop:
                return _fill(side, e, stop)
            if b[3] <= tp:
                return _fill(side, e, tp)
    return _fill(side, e, bars[end - 1][4])


def video_grid_short(adds=(0.05, 0.10), tp_from_avg=0.05, hard_stop=0.30, max_min=MAX_HOLD_MIN):
    """The video's short: a third at the signal, a third each time price runs
    another 5% / 10% above the first fill. Take profit 5% under the average.
    hard_stop=None is his literal "never stop, just average" -- shown for the tail."""
    def plan(i, bars, s):
        e0 = bars[i][1]
        part = 1 / (1 + len(adds))
        fills = [e0]
        levels = [e0 * (1 + a) for a in adds]
        end = min(len(bars), i + max_min)
        if end - i < max_min and end == len(bars):
            return None
        stop = e0 * (1 + hard_stop) if hard_stop else float("inf")

        def pnl(exit_):
            return sum(part * _fill("short", f, exit_) for f in fills)

        for b in bars[i:end]:
            while levels and b[2] >= levels[0]:
                fills.append(levels.pop(0))
            avg = sum(fills) / len(fills)
            if b[2] >= stop:
                return pnl(stop)
            tp = avg * (1 - tp_from_avg)
            if b[3] <= tp:
                return pnl(tp)
        return pnl(bars[end - 1][4])
    return plan


PLANS = {
    "long": {
        "hold 15m": hold(15),
        "hold 1h": hold(60),
        "hold 4h": hold(240),
        "video: stop under move, no TP, 4h": video_long(None, 240),
        "video: stop under move, TP 2R, 4h": video_long(2.0, 240),
        "stop 3% / TP 6%, 4h": bracket(0.03, 0.06, 240),
    },
    "short": {
        "hold 15m": hold(15),
        "hold 1h": hold(60),
        "hold 4h": hold(240),
        "stop 5% / TP 10%, 4h": bracket(0.05, 0.10, 240),
        "video: grid 3 parts, TP 5%, stop 30%, 24h": video_grid_short(),
        "video: grid 3 parts, TP 5%, no stop, 24h": video_grid_short(hard_stop=None),
    },
}


def summarise(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    eq = peak = dd = 0.0
    for v in vals:  # in signal time order
        eq += v
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {
        "n": len(vals),
        "win%": 100 * sum(v > 0 for v in vals) / len(vals),
        "avg%": 100 * st.mean(vals),
        "median%": 100 * st.median(vals),
        "worst%": 100 * min(vals),
        "best%": 100 * max(vals),
        "$ per $1k/trade": 1000 * sum(vals),
        "max DD $": 1000 * dd,
    }


def main(src: str, out: str = "out") -> None:
    global OUT
    OUT = Path(out)
    sigs = sorted(load(src), key=lambda r: r["at"])
    keys = sorted({(r["venue"], r["symbol"], ms(r["at"]) // DAY * DAY) for r in sigs})
    print(f"{len(sigs)} signals, {len(keys)} symbol-days to fetch", file=sys.stderr)
    bars_by: dict[tuple, list[list]] = {}
    with ThreadPoolExecutor(8) as pool:
        for n, (k, b) in enumerate(pool.map(fetch_day, keys), 1):
            bars_by[k] = b
            if n % 200 == 0:
                print(f"  {n}/{len(keys)}", file=sys.stderr)

    # Control: the same plan, entered at the same minute, on other symbols of
    # the same venue that also signalled that day (so their bars are already
    # here). Same-time cancels market-wide moves -- Oct 15 made every long
    # look brilliant; same-day signallers are volatile coins, not sleepy ones,
    # so the control is not flattered by picking quiet symbols.
    by_day = defaultdict(list)
    for (venue, symbol, day) in bars_by:
        by_day[(venue, day)].append(symbol)
    rng = random.Random(7)

    def run(plans, bars, s, entry_ts):
        idx = next((i for i, b in enumerate(bars) if b[0] >= entry_ts), None)
        if idx is None or bars[idx][0] - entry_ts >= 5 * MIN:
            return None, {name: None for name in plans}
        return bars[idx][1], {name: plan(idx, bars, s) for name, plan in plans.items()}

    per_signal = []
    for s in sigs:
        t = ms(s["at"])
        day = t // DAY * DAY
        entry_ts = (t // MIN + 1) * MIN
        plans = PLANS[s["side"]]
        row = {k: s[k] for k in ("at", "venue", "rule", "symbol", "side", "ordinal", "action")}
        row["failed"] = ",".join(s["failed"])
        row["entry"], res = run(plans, bars_by[(s["venue"], s["symbol"], day)], s, entry_ts)
        row.update(res)
        peers = [x for x in by_day[(s["venue"], day)] if x != s["symbol"]]
        ctl = defaultdict(list)
        for peer in rng.sample(peers, min(CONTROLS, len(peers))):
            _, r = run(plans, bars_by[(s["venue"], peer, day)], s, entry_ts)
            for name, v in r.items():
                if v is not None:
                    ctl[name].append(v)
        for name in plans:
            row[f"ctl: {name}"] = st.mean(ctl[name]) if ctl[name] else None
        per_signal.append(row)

    OUT.mkdir(exist_ok=True)
    for side in ("long", "short"):
        rows = [r for r in per_signal if r["side"] == side]
        fields = list(rows[0]) if rows else []
        with open(OUT / f"pnl_signals_{side}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fields)
            w.writeheader()
            w.writerows(rows)

    groups = {
        "sent": lambda r: r["action"] == "sent",
        "skipped": lambda r: r["action"] == "skipped",
        "sent, ordinal 1": lambda r: r["action"] == "sent" and r["ordinal"] == 1,
        "sent, ex Oct 15": lambda r: r["action"] == "sent" and not r["at"].startswith("2026-10-15"),
    }
    report = defaultdict(dict)
    for (venue, rule, side) in sorted({(r["venue"], r["rule"], r["side"]) for r in per_signal}):
        for gname, pred in groups.items():
            rows = [r for r in per_signal
                    if (r["venue"], r["rule"], r["side"]) == (venue, rule, side) and pred(r)]
            for plan in PLANS[side]:
                paired = [r for r in rows if r[plan] is not None and r[f"ctl: {plan}"] is not None]
                m = summarise([r[plan] for r in rows if r[plan] is not None])
                if paired:
                    m["ctl avg%"] = 100 * st.mean(r[f"ctl: {plan}"] for r in paired)
                    m["edge pp"] = 100 * st.mean(r[plan] - r[f"ctl: {plan}"] for r in paired)
                label = f"{venue} {rule}" + (f" {side}" if rule.startswith("liq") else "")
                report[f"{label} [{gname}]"][plan] = m
    (OUT / "pnl_report.json").write_text(json.dumps(report, indent=1))

    for g, plans in report.items():
        print(f"\n== {g}")
        print(f"  {'plan':44} {'n':>5} {'win%':>6} {'avg%':>7} {'med%':>7} {'worst%':>7} {'$/1k':>9} {'maxDD$':>9} {'ctl%':>7} {'edge':>6}")
        for p, m in plans.items():
            if m["n"]:
                print(f"  {p:44} {m['n']:5d} {m['win%']:6.1f} {m['avg%']:7.2f} {m['median%']:7.2f} "
                      f"{m['worst%']:7.1f} {m['$ per $1k/trade']:9.0f} {m['max DD $']:9.0f} "
                      f"{m.get('ctl avg%', float('nan')):7.2f} {m.get('edge pp', float('nan')):6.2f}")


if __name__ == "__main__":
    main(*(sys.argv[1:3] or ["data/signals"]))
