"""Command line entry point.

    screeners backtest --rule oi_growth --exchange binance --days 30 --top 100
    screeners universe --exchange bybit
    screeners cache
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
import time

from . import logging as etb_logging
from .backtest.engine import build_rule, interval_for, run_backtest
from .config import load_rules
from .data import binance, bybit
from .data.cache import Cache
from .models import Exchange

ADAPTERS = {Exchange.BINANCE: binance, Exchange.BYBIT: bybit}
# Binance retains only ~30 days of open-interest history; asking for more
# silently yields a shorter window than requested, so we say so instead.
MAX_OI_DAYS = 30


def _window(days: float, interval: str) -> tuple[int, int]:
    """Align the window to the hour.

    The history cache is keyed on an exact range, so snapping to whole bars
    still produced a fresh key every few minutes and a total cache miss on
    every re-run. Hour alignment makes repeated runs within the same hour
    free, which is what makes threshold sweeps practical.
    """
    hour = 3_600_000
    end = int(time.time() * 1000) // hour * hour
    return end - int(days * 86_400_000), end


def _universe(exchange: Exchange, top: int | None) -> list[str]:
    adapter = ADAPTERS[exchange]
    return adapter.top_by_turnover(top) if top else adapter.universe()


def cmd_backtest(args: argparse.Namespace) -> int:
    rules = load_rules(args.config)
    rule = build_rule(args.rule, rules)
    exchange = Exchange(args.exchange)
    interval = interval_for(args.rule)

    days = args.days
    if args.rule == "oi_growth" and days > MAX_OI_DAYS:
        print(f"note: open-interest history is ~{MAX_OI_DAYS}d on both venues; "
              f"clamping {days:g}d -> {MAX_OI_DAYS}d", file=sys.stderr)
        days = MAX_OI_DAYS

    start, end = _window(days, interval)
    symbols = _universe(exchange, args.top)
    if args.symbols:
        wanted = {s.upper() for s in args.symbols.split(",")}
        symbols = [s for s in symbols if s in wanted]
    if not symbols:
        print("no symbols matched", file=sys.stderr)
        return 1

    cache = None if args.no_cache else Cache()
    print(f"{exchange.value}  {args.rule}  {interval} bars  {days:g}d  "
          f"{len(symbols)} symbols", file=sys.stderr)

    last = [0.0]

    def progress(done: int, total: int, sym: str) -> None:
        now = time.monotonic()
        if now - last[0] > 2.0 or done == total:
            last[0] = now
            print(f"\r  fetching {done}/{total}  {sym:<14}", end="", file=sys.stderr)

    result = run_backtest(
        exchange, rule, symbols, interval, start, end,
        horizons=rules.horizons_min, cache=cache,
        workers=args.workers, on_progress=progress,
    )
    print(file=sys.stderr)

    if not result.outcomes:
        print("no signals fired -- loosen the thresholds or widen the window")
        return 0

    from .backtest import store
    saved = store.save([o.signal for o in result.outcomes], exchange, args.rule)
    print(f"saved {len(result.outcomes)} signals to {saved}", file=sys.stderr)

    _report(result, rules.horizons_min, args.rule, exchange)
    return 0


def _report(result, horizons, rule_name: str, exchange: Exchange) -> None:
    n = len(result.outcomes)
    days = (result.end_ms - result.start_ms) / 86_400_000
    print()
    print(f"=== {exchange.value} / {rule_name} ===")
    print(f"{n} trigger firings over {days:.0f}d across {result.symbols} symbols "
          f"({n / max(days, 1):.0f}/day)")
    if not result.cvd_available:
        print("CVD filters were not evaluated: this venue does not report "
              "taker-buy volume on klines.")
    print()

    rows = result.ablation()
    for h in horizons:
        print(f"-- forward return at +{h}m " + "-" * 44)
        for r in rows:
            print("  " + r.line(h))
        print()

    names = result.filter_names()
    best = result.subset(names)
    if best and len(best) != n:
        kept = 100.0 * len(best) / n
        print(f"all filters keep {len(best)}/{n} signals ({kept:.0f}%), "
              f"{len(best) / max(days, 1):.1f}/day")

    print()
    print("edge over random entry (hit-rate percentage points):")
    for h in horizons:
        raw = result.edge(h)
        filt = result.edge(h, names) if names else None
        parts = [f"  +{h:>3}m  trigger {raw:+5.1f}pp" if raw is not None else f"  +{h:>3}m  n/a"]
        if filt is not None:
            parts.append(f"all filters {filt:+5.1f}pp")
        print("   ".join(parts))
    if n < 200:
        print()
        print(f"WARNING: only {n} signals. Treat every number above as noise; "
              "widen --days or --top before drawing any conclusion.")


def cmd_universe(args: argparse.Namespace) -> int:
    exchange = Exchange(args.exchange)
    syms = _universe(exchange, args.top)
    print(f"{exchange.value}: {len(syms)} symbols", file=sys.stderr)
    for s in syms:
        print(s)
    return 0


def cmd_telegram_setup(args: argparse.Namespace) -> int:
    """Discover the group, create the four topics, write .env."""
    from .notify import setup

    from .config import load_notify
    token = args.token or load_notify().bot_token
    if not token:
        print("no token given and NOTIFY_BOT_TOKEN is not set in .env",
              file=sys.stderr)
        return 1
    token = setup.validate_token(token)
    me = setup.botapi.get_me(token)
    print(f"bot: @{me['username']} ({me['first_name']})", file=sys.stderr)

    print("\nWaiting for a message in the group.\n"
          "  1. create a supergroup and turn ON Topics in its settings\n"
          f"  2. add @{me['username']} as an admin with 'Manage topics'\n"
          "  3. post any message in the group\n", file=sys.stderr)

    chat_id, title = setup.discover_chat(token, timeout_s=args.wait)
    print(f"found group: {title}  chat_id={chat_id}", file=sys.stderr)

    if not setup.is_forum(token, chat_id):
        print("\nThat group does not have Topics enabled. Turn on "
              "Settings -> Topics, then run this again.", file=sys.stderr)
        return 1

    topics = setup.create_topics(token, chat_id)
    if not topics:
        print("no topics created -- does the bot have 'Manage topics'?", file=sys.stderr)
        return 1

    path = setup.write_env({"NOTIFY_BOT_TOKEN": token,
                            "NOTIFY_CHAT_ID": chat_id, **topics})
    print(f"\nwrote {path} (chmod 600). Topics: "
          + ", ".join(f"{k}={v}" for k, v in topics.items()), file=sys.stderr)
    print("\nNow run:  python3 -m screeners.cli telegram-test", file=sys.stderr)
    return 0


def cmd_telegram_test(args: argparse.Namespace) -> int:
    """Replay real backtested signals into the topics.

    Deliberately uses genuine signals off cached history rather than invented
    ones: it exercises the real card formatting, topic routing, batching and
    rate limiting, and what lands in Telegram is what the live screener would
    actually have sent.
    """
    from .backtest import store
    from .config import load_notify
    from .notify.dispatch import Dispatcher

    settings = load_notify()
    if not settings.configured and not args.dry_run:
        settings.require()

    exchange = Exchange(args.exchange)
    files = store.available()
    if not files:
        print("no saved signals. Run a backtest first, e.g.\n"
              "  python3 -m screeners.cli backtest --rule oi_growth --days 30 --top 200",
              file=sys.stderr)
        return 1

    rule_names = args.rules.split(",") if args.rules else ["oi_growth", "pump_short"]
    per_rule = max(1, args.count // max(1, len(rule_names)))

    collected = []
    for name in rule_names:
        sigs = store.load(exchange, name)
        if not sigs:
            print(f"  {name}: nothing saved, skipping", file=sys.stderr)
            continue
        if args.filtered:
            sigs = [s for s in sigs if s.passed_all_filters]
        take = sigs[-per_rule:]
        collected.extend(take)
        print(f"  {name}: {len(sigs)} saved, replaying {len(take)}", file=sys.stderr)

    if not collected:
        print("nothing to replay", file=sys.stderr)
        return 1

    collected.sort(key=lambda s: s.ts)
    d = Dispatcher(settings, dry_run=args.dry_run, rate_per_sec=args.rate,
                   burst=3, batch_window_s=args.batch, max_batch=args.max_batch)
    d.start()
    d.submit_text(f"\U0001f6a6 <b>screeners test run</b>\nreplaying {len(collected)} real "
                  f"signals from {exchange.value} history into their topics.")
    for sig in collected:
        d.submit(sig)
    print(f"queued {len(collected)} signals; draining…", file=sys.stderr)

    deadline = time.monotonic() + 600
    while d.sent + d.dropped < len(collected) + 1 and time.monotonic() < deadline:
        time.sleep(1.0)
    d.submit_text(f"\u2705 test complete \u2014 {d.sent} sent, {d.dropped} dropped.")
    time.sleep(args.batch + 3)
    d.stop()
    print(f"done. sent={d.sent} dropped={d.dropped}", file=sys.stderr)
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    """Run the screeners against the live market and alert to Telegram."""
    from .config import load_notify
    from .live import Screener, build_dispatcher

    rules = load_rules(args.config)
    exchange = Exchange(args.exchange)
    settings = load_notify()
    if not settings.configured and not args.dry_run:
        print("Telegram is not configured; running in dry-run (signals logged, "
              "not sent). See README -> Telegram setup.", file=sys.stderr)

    symbols = _universe(exchange, args.top)
    rule_names = args.rules.split(",")
    dispatcher = build_dispatcher(settings, dry_run=args.dry_run)
    screener = Screener(exchange, rule_names, symbols, rules, dispatcher,
                        sweep_s=args.sweep, workers=args.workers,
                        require_filters=not args.unfiltered)

    stop = threading.Event()

    def shutdown(*_):
        stop.set()

    try:
        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
    except ValueError:
        pass

    print(f"{exchange.value}: screening {len(symbols)} symbols with "
          f"{rule_names} every {args.sweep:.0f}s (ctrl-c to stop)", file=sys.stderr)
    try:
        screener.run(stop)
    finally:
        stop.set()
        dispatcher.stop()
    print(f"stopped. {screener.emitted} signal(s) dispatched, "
          f"{dispatcher.dropped} dropped", file=sys.stderr)
    return 0


def cmd_record_liquidations(args: argparse.Namespace) -> int:
    """Record both venues' liquidation feeds to sqlite, forever.

    The liquidation screener has no historical data anywhere, so this is the
    only way it will ever be measurable. Start it early; the dataset is only
    as good as how long it has been running.
    """
    from .config import ROOT
    from .data import liquidations as lq

    store = lq.LiquidationStore(args.db or ROOT / "data" / "liquidations.db")
    stop = threading.Event()
    seen = {"n": 0}

    def on_event(liq) -> None:
        store.add(liq)
        seen["n"] += 1
        if liq.usd >= args.echo_usd:
            print(f"{time.strftime('%H:%M:%S')}  {liq.exchange.value:<8} "
                  f"{liq.symbol:<14} {liq.side.value:<5} ${liq.usd:>12,.0f}", flush=True)

    feeds = []
    if args.exchange in ("binance", "both"):
        feeds.append(("binance", lambda: lq.stream_binance()))
    if args.exchange in ("bybit", "both"):
        syms = bybit.universe()
        print(f"bybit: subscribing {len(syms)} symbols", file=sys.stderr)
        feeds.append(("bybit", lambda: lq.stream_bybit(syms)))

    threads = [
        threading.Thread(target=lq.run_forever, args=(name, factory, on_event, stop),
                         name=f"liq-{name}", daemon=True)
        for name, factory in feeds
    ]
    for t in threads:
        t.start()

    def shutdown(*_):
        stop.set()

    try:
        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
    except ValueError:
        pass  # embedded off the main thread (tests); ctrl-c handling is optional
    print(f"recording to {store.path}  (ctrl-c to stop)", file=sys.stderr)
    try:
        while not stop.wait(30.0):
            span = store.span()
            hours = (span[1] - span[0]) / 3_600_000 if span else 0.0
            print(f"  recorded {seen['n']} this session; "
                  f"db totals {store.count()} spanning {hours:.1f}h", file=sys.stderr)
    finally:
        stop.set()
    print(f"stopped. {store.count()}", file=sys.stderr)
    return 0


def cmd_ops(args: argparse.Namespace) -> int:
    """Post one line to the Ops topic. Used by start.sh to report a stage that
    keeps dying; silently does nothing when Telegram is not configured."""
    from .config import load_notify
    from .notify import botapi

    s = load_notify()
    if not s.configured:
        return 0
    botapi.send(s.bot_token, s.chat_id, args.text, html=False,
                topic_id=s.topic_for("ops"))
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    c = Cache()
    stats = c.stats()
    size = c.path.stat().st_size / 1e6 if c.path.exists() else 0.0
    print(f"{c.path}  {size:.1f} MB")
    for kind, count in sorted(stats.items()):
        print(f"  {kind:<8} {count} ranges")
    if not stats:
        print("  (empty)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="screeners", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log-level", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backtest", help="replay a rule over history and score it")
    b.add_argument("--rule", default="oi_growth",
                   choices=["oi_growth", "pump_short", "pump_long"])
    b.add_argument("--exchange", default="binance", choices=["binance", "bybit"])
    b.add_argument("--days", type=float, default=30)
    b.add_argument("--top", type=int, default=100,
                   help="restrict to the N most-traded symbols (0 = all)")
    b.add_argument("--symbols", default=None, help="comma-separated whitelist")
    b.add_argument("--workers", type=int, default=8)
    b.add_argument("--config", default=None)
    b.add_argument("--no-cache", action="store_true")
    b.set_defaults(func=cmd_backtest)

    u = sub.add_parser("universe", help="list tradable perpetuals")
    u.add_argument("--exchange", default="binance", choices=["binance", "bybit"])
    u.add_argument("--top", type=int, default=None)
    u.set_defaults(func=cmd_universe)

    ts = sub.add_parser("telegram-setup",
                        help="discover the group, create topics, write .env")
    ts.add_argument("token", nargs="?", default=None,
                    help="bot token from @BotFather; defaults to NOTIFY_BOT_TOKEN in .env")
    ts.add_argument("--wait", type=float, default=300.0,
                    help="seconds to wait for a group message")
    ts.set_defaults(func=cmd_telegram_setup)

    tt = sub.add_parser("telegram-test",
                        help="replay real backtested signals into the topics")
    tt.add_argument("--exchange", default="binance", choices=["binance", "bybit"])
    tt.add_argument("--count", type=int, default=30, help="signals to replay")
    tt.add_argument("--rules", default="oi_growth,pump_short")
    tt.add_argument("--rate", type=float, default=0.5, help="messages per second")
    tt.add_argument("--batch", type=float, default=4.0, help="batch window seconds")
    tt.add_argument("--max-batch", type=int, default=5)
    tt.add_argument("--filtered", action="store_true",
                    help="only signals passing every filter")
    tt.add_argument("--config", default=None)
    tt.add_argument("--dry-run", action="store_true")
    tt.set_defaults(func=cmd_telegram_test)

    lv = sub.add_parser("live", help="run the screeners live and alert to Telegram")
    lv.add_argument("--exchange", default="binance", choices=["binance", "bybit"])
    lv.add_argument("--rules", default="oi_growth,pump_short")
    lv.add_argument("--top", type=int, default=60,
                    help="rate limits cap this; see Screener.required_sweep_s")
    lv.add_argument("--sweep", type=float, default=60.0, help="seconds between sweeps")
    lv.add_argument("--workers", type=int, default=8)
    lv.add_argument("--config", default=None)
    lv.add_argument("--dry-run", action="store_true", help="log signals, never send")
    lv.add_argument("--unfiltered", action="store_true",
                    help="alert on every trigger, not just ones passing all filters")
    lv.set_defaults(func=cmd_live)

    r = sub.add_parser("record-liquidations",
                       help="stream liquidations into sqlite (they cannot be backfilled)")
    r.add_argument("--exchange", default="both", choices=["binance", "bybit", "both"])
    r.add_argument("--db", default=None)
    r.add_argument("--echo-usd", type=float, default=20000.0,
                   help="print liquidations at least this large")
    r.set_defaults(func=cmd_record_liquidations)

    o = sub.add_parser("ops", help="post a line to the Ops topic")
    o.add_argument("text")
    o.set_defaults(func=cmd_ops)

    c = sub.add_parser("cache", help="show what history is cached locally")
    c.set_defaults(func=cmd_cache)

    args = p.parse_args(argv)
    etb_logging.setup(args.log_level)
    if getattr(args, "top", None) == 0:
        args.top = None
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
