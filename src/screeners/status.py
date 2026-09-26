"""One-shot health report: `python3 -m screeners.cli status`.

Built so that "what is happening on the server?" is one command, not a tour
of several logs. Everything here is read-only.
"""
from __future__ import annotations

import calendar
import re
import sqlite3
import subprocess
import time
from collections import Counter
from pathlib import Path

from . import journal
from .config import ROOT

LOG_DIR = ROOT / "data" / "logs"
LIQ_DB = ROOT / "data" / "liquidations.db"
LIVE_LOGS = ("live-binance", "live-bybit")

# Log lines since the dated-UTC format: "2026-09-26 12:45:53Z LEVEL name msg".
_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})Z\s+(\w+)\s+(\S+)\s+(.*)$")


def _parse(line: str) -> tuple[float, str, str] | None:
    m = _STAMP.match(line)
    if not m:
        return None
    # timegm reads the struct as UTC. mktime would read it as LOCAL time and
    # apply daylight saving -- correct only on a machine whose zone is UTC,
    # which is exactly the box where nobody would notice it was wrong.
    ts = float(calendar.timegm(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")))
    return ts, m.group(2), m.group(4)


def _ago(seconds: float) -> str:
    s = int(seconds)
    if s < 120:
        return f"{s}s ago"
    if s < 7200:
        return f"{s // 60}m ago"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m ago"


def _stages() -> list[str]:
    try:
        out = subprocess.run(["ps", "-eo", "pid,etime,args"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines():
        if "screeners.cli" in line and " status" not in line and "grep" not in line:
            parts = line.split(None, 2)
            if len(parts) == 3:
                pid, etime, args = parts
                rows.append(f"{pid:>7}  up {etime:>11}  {args.split('screeners.cli', 1)[1].strip()}")
    return rows


def _log_summary(name: str, since: float, now: float, show_warnings: int) -> list[str]:
    p = LOG_DIR / f"{name}.log"
    if not p.exists():
        return [f"  {name}: no log"]
    lines = p.read_text(errors="replace").splitlines()
    last_sweep = last_warm = None
    warnings: list[tuple[float, str, str]] = []
    for line in lines:
        parsed = _parse(line)
        if not parsed:
            continue
        ts, level, msg = parsed
        if msg.startswith("sweep "):
            last_sweep = (ts, msg)
        elif msg.startswith("warm-up"):
            last_warm = (ts, msg)
        if ts >= since and level in ("WARNING", "ERROR", "CRITICAL"):
            warnings.append((ts, level, msg))
    out = [f"  {name}:"]
    if last_sweep:
        age = now - last_sweep[0]
        flag = "   <-- STALE" if age > 240 else ""
        out.append(f"    last sweep  {_ago(age):>10}  {last_sweep[1]}{flag}")
    else:
        out.append("    last sweep  none recorded")
    if last_warm:
        out.append(f"    last start  {_ago(now - last_warm[0]):>10}  {last_warm[1]}")
    kinds = Counter(re.sub(r"\d+(\.\d+)?", "N", m)[:60] for _, _, m in warnings)
    out.append(f"    warnings    {len(warnings)} in window"
               + (": " + "; ".join(f"{n}x {k}" for k, n in kinds.most_common(3)) if kinds else ""))
    for ts, level, msg in warnings[-show_warnings:]:
        out.append(f"      {time.strftime('%m-%d %H:%M', time.gmtime(ts))}Z {level} {msg[:110]}")
    return out


def _signals(hours: float, now_ms: int, show: int) -> list[str]:
    days = int(hours // 24) + 2
    since = now_ms - hours * 3_600_000
    rows = [r for r in journal.read(days, now_ms)
            if calendar.timegm(time.strptime(r["at"], "%Y-%m-%dT%H:%M:%SZ"))
            >= since / 1000]
    if not rows:
        return ["  none in window (journal starts with the version that added it)"]
    tally = Counter((r["venue"], r["rule"], r["action"]) for r in rows)
    out = []
    for venue in sorted({r["venue"] for r in rows}):
        for rule in sorted({r["rule"] for r in rows if r["venue"] == venue}):
            out.append(f"  {venue:<8} {rule:<11} sent {tally[(venue, rule, 'sent')]:>3}"
                       f"   skipped {tally[(venue, rule, 'skipped')]:>3}")
    sent = [r for r in rows if r["action"] == "sent"]
    if sent:
        out.append(f"  last {min(show, len(sent))} sent:")
        for r in sent[-show:]:
            m = r["metrics"]
            key = (f"OI {m['oi_growth_pct']:.1f}%" if "oi_growth_pct" in m
                   else f"move {m.get('move_pct', 0):.1f}%")
            out.append(f"    {r['at'][5:16]}Z  {r['venue']:<7} {r['rule']:<11} "
                       f"{r['symbol']:<14} #{r['ordinal']:<3} {key}")
    return out


def _liquidations(hours: float, now_ms: int) -> list[str]:
    if not LIQ_DB.exists():
        return ["  no database yet"]
    try:
        c = sqlite3.connect(f"file:{LIQ_DB}?mode=ro", uri=True, timeout=5)
        total = dict(c.execute("SELECT exchange, COUNT(*) FROM liquidation GROUP BY exchange"))
        since = now_ms - int(hours * 3_600_000)
        recent = dict(c.execute(
            "SELECT exchange, COUNT(*) FROM liquidation WHERE ts >= ? GROUP BY exchange", (since,)))
        big = c.execute(
            "SELECT exchange, symbol, side, qty*price, ts FROM liquidation WHERE ts >= ? "
            "ORDER BY qty*price DESC LIMIT 3", (since,)).fetchall()
        c.close()
    except sqlite3.Error as e:
        return [f"  unreadable: {e}"]
    out = [f"  total {total}   in window {recent}"]
    for ex, sym, side, usd, ts in big:
        out.append(f"    biggest: {time.strftime('%m-%d %H:%M', time.gmtime(ts / 1000))}Z "
                   f"{ex:<7} {sym:<14} {side:<5} ${usd:,.0f}")
    return out


def _disk() -> str:
    def size(p: Path) -> float:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e6 if p.exists() else 0.0
    return (f"  logs {size(LOG_DIR):.1f}MB   signal journal {size(journal.JOURNAL_DIR):.1f}MB   "
            f"liquidations {LIQ_DB.stat().st_size / 1e6 if LIQ_DB.exists() else 0:.1f}MB")


def report(hours: float = 24.0, show: int = 8, show_warnings: int = 5) -> str:
    now = time.time()
    now_ms = int(now * 1000)
    since = now - hours * 3600
    stages = _stages()
    parts = [f"status at {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(now))} "
             f"(window: last {hours:g}h)", "", "stages:"]
    parts += stages or ["  NONE RUNNING"]
    parts += ["", "screeners:"]
    for name in LIVE_LOGS:
        parts += _log_summary(name, since, now, show_warnings)
    parts += ["", "signals:"] + _signals(hours, now_ms, show)
    parts += ["", "liquidations:"] + _liquidations(hours, now_ms)
    parts += ["", "disk:", _disk()]
    return "\n".join(parts)
