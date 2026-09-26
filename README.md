# experimental-trading-bots

Market screeners for Binance and Bybit USDT perpetuals. They watch price,
volume, open interest and CVD, and alert to Telegram. **They do not trade** —
no API keys, no order permissions, market data only.

The spec comes from [this video](https://www.youtube.com/watch?v=etxktQKHee4),
which demonstrates a paid closed-source product. `docs/investigation.md` pulls
the spec out of it and checks every claim against the real exchange APIs.

Zero runtime dependencies — stdlib only, Python 3.11+.

## The three screeners

| Rule | Side | Trigger (defaults are the video's) | Backtestable |
|---|---|---|---|
| `oi_growth` | long | open interest +5% in 15 min | yes, ~30d |
| `pump_long` | long | price +2% within 2 min | yes |
| `pump_short` | short | price +10% within 20 min | yes |
| `liquidation` | both | liquidation ≥ $20k on an alt | **no** — must be recorded |

Rules are evaluated **per exchange**. Open interest is not aggregated across
venues; a symbol listed on both can fire twice, independently.

Thresholds live in `config/rules.toml`. Each of the video's "read rules"
(only the first 3 signals of the day, OI up *and* price up, CVD confirming,
came out of a flat range) is a separately togglable **filter**, so the
backtester can price each one instead of taking them on faith.

## Run it

```bash
./start.sh              # liquidation recorder + Binance live screener
./start.sh --bybit      # ...plus a Bybit live screener (no CVD there)
./start.sh --no-live    # recorder only, no alerts
./start.sh --dry-run    # screeners log signals instead of sending
```

One supervisor, one process per stage, so a stuck Telegram send can never
stall liquidation ingest. A dead stage restarts with a growing delay (5s..60s);
the third restart in a row posts one line to the Ops topic. Logs go to
`data/logs/<stage>.log`, rotated past 20MB. Ctrl-C stops everything. It refuses
to start if it is already running, or if any `etb` stage was started by hand —
two recorders would double-count, two screeners would double-alert.

## Backtest first

```bash
PYTHONPATH=src python3 -m etb.cli backtest --rule oi_growth --exchange binance --days 30 --top 200
```

Every run reports a **random-entry baseline** alongside the rule. This is not
decoration. In a trending month a long screener shows a 90% hit rate because
everything went up; the baseline is the only way to see whether the rule
carries information. Read the `edge over random entry` block, not the raw hit
rate.

Results are in `out/`. Summary of what the numbers actually say:
`docs/findings.md`.

Other commands:

```bash
python3 -m etb.cli universe --exchange bybit        # list tradable perps
python3 -m etb.cli cache                            # what history is cached
python3 -m etb.cli record-liquidations              # start collecting (see below)
```

History is cached in `data/cache.db`, so re-running with different thresholds
costs nothing. Fetching is the slow part; scanning is instant. The cache is
keyed on the exact range and windows snap to the hour, so repeat runs inside
the same hour are free. **If you fix a data-layer bug, delete the cache** —
it will happily serve back whatever the buggy fetch stored.

## Start the liquidation recorder early

Neither venue serves historical liquidations over REST — they are
websocket-only. The liquidation screener therefore **cannot be backtested at
all** until a dataset exists, and the dataset is only as good as how long it
has been running:

```bash
PYTHONPATH=src python3 -m etb.cli record-liquidations --exchange both
```

Caveat worth knowing: Binance's `!forceOrder@arr` is a *snapshot* stream that
pushes at most one liquidation per symbol per second, so it undercounts during
exactly the cascades that matter. Bybit's `allLiquidation` does not drop. Treat
Bybit as the trustworthy side.

## Telegram setup

This repo uses **its own bot**, separate from anything else on the machine.
One bot, one forum supergroup, one topic per screener.

Two steps are manual because Telegram does not allow them any other way: only
a *user* account can talk to @BotFather or create a group — a bot can do
neither. Everything after that is automated.

**You do this (~2 minutes):**

1. [@BotFather](https://t.me/BotFather) → `/newbot` → pick a name and a
   username ending in `bot`. Keep the token it gives you.
2. Create a supergroup, open its settings and turn **Topics** on.
3. Add your new bot to the group as an **admin** with *Manage topics*.

**Then this does the rest:**

```bash
PYTHONPATH=src python3 -m etb.cli telegram-setup '<token>'
```

It waits for you to post any message in the group, reads the chat id off that
message, creates the four topics (OI / Pump / Liquidations / Ops), and writes
`.env` with `chmod 600`. The token is never printed back.

**See it work:**

```bash
PYTHONPATH=src python3 -m etb.cli telegram-test --count 30
```

Replays real signals from `out/signals_*.json` into their topics — genuine
backtested signals, not invented ones, so the cards, routing, batching and
rate limiting are all exercised exactly as the live screener would. Add
`--filtered` for only signals that pass every filter, or `--dry-run` to print
instead of send.

With Telegram unconfigured everything still runs; signals are logged instead
of sent, so the backtester and recorder need no credentials.

## Layout

```
src/etb/
  models.py        Bar, Signal, Liquidation
  config.py        rules.toml + .env loading
  data/
    http.py        retries + a shared weight budget per venue limit pool
    binance.py     klines (carry taker-buy volume -> CVD), OI history
    bybit.py       klines (no taker volume -> no historical CVD), OI history
    series.py      merges OI onto bars, forward-filled, never looking ahead
    ws.py          small RFC6455 client (stdlib), heartbeats, reconnect
    liquidations.py live feeds + sqlite recorder
    cache.py       sqlite range cache
  rules/           pure functions over a window; scan() adds ordinal + cooldown
  backtest/        replay, forward returns, random-entry baseline, ablation
  notify/          bot API, card formatting, token-bucket dispatcher
tests/             run: PYTHONPATH=src python3 -m unittest discover -s tests
```

## Constraints that shaped the design

Both verified live, both in `docs/investigation.md`:

* **Binance has no open-interest websocket.** REST only, weight 2400/min per
  IP, ~527 symbols per sweep — a 20s sweep is the practical floor. Open
  interest history is retained ~30 days at 5m granularity, which is the hard
  ceiling on the OI backtest window.
* **Bybit klines carry no taker-buy volume**, so historical CVD is
  unavailable there. CVD filters are omitted rather than silently passed — a
  filter that cannot be computed must not get a vote.
* **Two endpoints are end-anchored and must be paged BACKWARDS.**
  `binance /futures/data/openInterestHist` and `bybit /v5/market/kline` return
  the *newest* N rows of a requested range, not the oldest. Paging forward
  from `startTime` terminates after one page and silently truncates: a 30-day
  OI request returned 1.7 days, a 30-day Bybit kline request returned 3.5
  days. Binance klines are start-anchored and page forward normally, which is
  what makes the inconsistency easy to miss. If you touch an adapter, assert
  the returned bar count against the requested range.

  This bit us once already — every number in an earlier `docs/findings.md`
  was computed on ~6% of its window.

* **The backtest control is time-matched, not uniform.** For each signal it
  draws bars at the same timestamp on *other* symbols. A uniform sample over
  all symbols and all time makes the control mostly quiet coins while signals
  cluster on volatile ones, which manufactures an edge that is not there.
