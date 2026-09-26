#!/usr/bin/env bash
# Run the screeners and keep them running.
#
#   ./start.sh              # everything: recorder + Binance and Bybit screeners
#   ./start.sh --no-bybit   # skip the Bybit screener (no CVD there)
#   ./start.sh --no-live    # recorder only -- no Telegram alerts
#   ./start.sh --dry-run    # screeners log signals instead of sending
#
# Each stage is its own process, so a wedged Telegram send can never stall
# liquidation ingest. A stage that dies is restarted with a growing delay
# (5s..60s; the count resets after 10 minutes up). The 3rd restart in a row
# posts one line to the Ops topic. Logs: data/logs/<stage>.log, rotated to .1
# past 20MB. Ctrl-C stops everything.
set -u
umask 077  # logs, .env and the recorder db are for this user only
cd "$(dirname "$0")"

PY=${PY:-python3}
export PYTHONPATH=src
LOG_DIR=data/logs
LOCK=data/start.pid
STATUS_EVERY=300
FLAP_RESTARTS=3

mkdir -p "$LOG_DIR"
[ -f .env ] && chmod 600 .env

# A pid is only "us" if it's still a start.sh: after a reboot it may be anything.
if [ -f "$LOCK" ] && ps -p "$(cat "$LOCK")" -o command= 2>/dev/null | grep -q start.sh; then
  echo "already running (pid $(cat "$LOCK")); stop it first" >&2
  exit 1
fi
# Stages started by hand would double-record and double-alert.
stray=$(pgrep -fl -- "-m screeners.cli (live|record-liquidations)" || true)
if [ -n "$stray" ]; then
  echo "screener stages are already running outside start.sh -- stop them first:" >&2
  echo "$stray" | sed 's/^/  /' >&2
  exit 1
fi

WANT_LIVE=1; WANT_BYBIT=1; LIVE_FLAGS=""
for arg in "$@"; do
  case "$arg" in
    --no-bybit) WANT_BYBIT=0 ;;
    --no-live)  WANT_LIVE=0 ;;
    --dry-run)  LIVE_FLAGS="--dry-run" ;;
    *) echo "usage: ./start.sh [--no-bybit] [--no-live] [--dry-run]" >&2; exit 2 ;;
  esac
done
echo $$ > "$LOCK"

NAMES=(liquidations); CMDS=("record-liquidations --exchange both")
if [ "$WANT_LIVE" -eq 1 ]; then
  NAMES+=(live-binance); CMDS+=("live --exchange binance $LIVE_FLAGS")
  [ "$WANT_BYBIT" -eq 1 ] && { NAMES+=(live-bybit); CMDS+=("live --exchange bybit $LIVE_FLAGS"); }
fi
LAST=$(( ${#NAMES[@]} - 1 ))
PIDS=(); STARTED=(); RESTARTS=(); RETRY_AT=()
for i in $(seq 0 "$LAST"); do PIDS[$i]=0; STARTED[$i]=0; RESTARTS[$i]=0; RETRY_AT[$i]=0; done

say() { echo "$(date '+%F %T') $*"; }

rotate_logs() {
  # Copy-and-truncate: every stage holds its log open with O_APPEND, so this is safe live.
  for f in "$LOG_DIR"/*.log; do
    [ -f "$f" ] || continue
    if [ "$(wc -c < "$f")" -gt $((20 * 1024 * 1024)) ]; then
      cp "$f" "$f.1" && : > "$f"
    fi
  done
}

start() {
  local i=$1
  # shellcheck disable=SC2086  # CMDS holds several words on purpose
  $PY -m screeners.cli ${CMDS[$i]} >> "$LOG_DIR/${NAMES[$i]}.log" 2>&1 &
  PIDS[$i]=$!
  STARTED[$i]=$(date +%s)
  say "started ${NAMES[$i]} (pid ${PIDS[$i]}) -> $LOG_DIR/${NAMES[$i]}.log"
}

stop_all() {
  say "stopping..."
  for p in "${PIDS[@]}"; do [ "$p" -gt 0 ] && kill "$p" 2>/dev/null; done
  for p in "${PIDS[@]}"; do [ "$p" -gt 0 ] && wait "$p" 2>/dev/null; done
  rm -f "$LOCK"
  say "stopped"
  exit 0
}
trap stop_all INT TERM

for i in $(seq 0 "$LAST"); do start "$i"; done
[ "$WANT_LIVE" -eq 1 ] && [ -z "$LIVE_FLAGS" ] && \
  $PY -m screeners.cli ops "screeners started: ${NAMES[*]}" >/dev/null 2>&1

last_status=$(date +%s)
while true; do
  sleep 5
  now=$(date +%s)
  for i in $(seq 0 "$LAST"); do
    kill -0 "${PIDS[$i]}" 2>/dev/null && continue
    if [ "${RETRY_AT[$i]}" -eq 0 ]; then
      # Just died. A long clean run earns a fresh restart count.
      [ $((now - STARTED[$i])) -gt 600 ] && RESTARTS[$i]=0
      RESTARTS[$i]=$((RESTARTS[$i] + 1))
      delay=$((5 * RESTARTS[$i])); [ "$delay" -gt 60 ] && delay=60
      RETRY_AT[$i]=$((now + delay))
      say "${NAMES[$i]} exited (restart #${RESTARTS[$i]} in ${delay}s). Last log lines:"
      tail -n 5 "$LOG_DIR/${NAMES[$i]}.log" | sed 's/^/    /'
      if [ "${RESTARTS[$i]}" -eq "$FLAP_RESTARTS" ]; then
        # Not a blip: say so once, in the Ops topic.
        $PY -m screeners.cli ops "⚠️ ${NAMES[$i]} keeps dying (restart #${RESTARTS[$i]}): $(tail -n 1 "$LOG_DIR/${NAMES[$i]}.log" | cut -c1-200)" \
          >/dev/null 2>&1 || true
      fi
    elif [ "$now" -ge "${RETRY_AT[$i]}" ]; then
      RETRY_AT[$i]=0
      start "$i"
    fi
  done
  if [ $((now - last_status)) -ge "$STATUS_EVERY" ]; then
    last_status=$now
    rotate_logs
    up=""; for i in $(seq 0 "$LAST"); do kill -0 "${PIDS[$i]}" 2>/dev/null && up+="${NAMES[$i]} "; done
    say "status: up = ${up:-none}"
    $PY -c "from screeners.data.liquidations import LiquidationStore as S; from screeners.config import ROOT; print('    liquidations:', S(ROOT/'data'/'liquidations.db').count())" 2>/dev/null
  fi
done
