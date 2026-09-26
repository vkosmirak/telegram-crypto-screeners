# AGENTS.md

Crypto perpetual-futures screeners for Binance and Bybit that alert to a
Telegram forum group. They never trade. See README.md for what the screeners
do and docs/findings.md for what the backtests say about them.

## Where it runs

Production is **hetzner** (`ssh root@hetzner`, over Tailscale), checked out at
`/opt/telegram-crypto-screeners`, under the systemd unit
`telegram-crypto-screeners`, which runs `start.sh`. The box also runs a live
trading bot (Jesse, in Docker) -- do not starve it; the unit is capped at
600MB for that reason.

Run it in ONE place. A second copy anywhere doubles every alert.

**It shares Binance's per-IP rate limit with Jesse.** Jesse trades Binance
perpetual futures from the same box, and Binance counts request weight per IP,
so every unit this project spends is taken from the trading bot. That is why
`binance.FAPI_BUDGET` uses only half the limit. Do not raise it without
checking Jesse's own usage first. (Jesse does not use Bybit.)

## "What is happening on the server?"

Start here -- one read-only command that covers stages, the last sweep per
venue, signals sent and skipped, warnings, liquidations and disk:

```bash
ssh root@hetzner 'cd /opt/telegram-crypto-screeners && PYTHONPATH=src python3 -m screeners.cli status'
#   --hours 72      widen the window
#   --show 20       list more recent signals
#   --warnings 20   list more recent warnings per log
```

Then, as needed:

| Question | Where |
|---|---|
| Did the service crash or restart? | `journalctl -u telegram-crypto-screeners --since "-6h" -o cat` |
| What exactly did it send, and why? | `data/signals/YYYY-MM-DD.jsonl` (one JSON line per signal, sent or skipped, with metrics and failed filters) |
| Per-sweep detail, errors | `data/logs/live-binance.log`, `data/logs/live-bybit.log` |
| Liquidation feed | `data/logs/liquidations.log`, `data/liquidations.db` |
| Is it using too much memory? | `systemctl show telegram-crypto-screeners -p MemoryCurrent` |
| Where is a stage stuck? | `kill -USR1 <pid>` dumps every thread's stack into that stage's log |

Log lines are UTC with a date: `2026-09-26 12:45:53Z LEVEL name message`.
Every live stage writes a `sweep ...` heartbeat each minute; a log that has
been silent for more than ~4 minutes means that stage is stuck.

## What is normal

- After a restart, the first open-interest refresh (~5 minutes later) waits
  about 50s on Binance's rate budget, because it lands in the same 5-minute
  window as the warm-up. One warning line and a sweep over 60s is expected;
  it clears by itself.
- Bybit occasionally answers "Too many visits" (retCode 10006), clustered
  at the top of the hour. It is retried with backoff and pauses the whole
  Bybit pool; a `bybit rate limit` warning line is normal, a `refresh
  failures` line naming it every sweep is not.
- Binance records very few liquidations. Its feed sends at most one per
  symbol per second; that is the venue, not a bug.
- Bybit sends far more OI alerts than Binance. Per docs/findings.md they carry
  no edge; they run because the owner chose to keep them on.

## Deploy

```bash
ssh root@hetzner 'cd /opt/telegram-crypto-screeners && git pull && systemctl restart telegram-crypto-screeners'
```

Secrets live only in `/opt/telegram-crypto-screeners/.env` (chmod 600), never
in git. Tests: `PYTHONPATH=src python3 -m unittest discover -s tests`.
