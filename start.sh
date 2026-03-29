#!/usr/bin/env bash
# Prism — process manager
# Usage: start.sh {start|stop|restart|status|switch|logs} [1|2]
#
# Designed by SK. Built by Claude.

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="${DIR}/prism.log"
STATE="${DIR}/.backend"
PORT="${LISTEN_PORT:-1319}"

# ── backends ─────────────────────────────────────────────────────────────
BACKEND_1_URL="${BACKEND_1_URL:-http://localhost:8000}"
BACKEND_1_LABEL="${BACKEND_1_LABEL:-Backend 1}"
BACKEND_2_URL="${BACKEND_2_URL:-http://localhost:8080}"
BACKEND_2_LABEL="${BACKEND_2_LABEL:-Backend 2}"

[ -f "${DIR}/prism.conf" ] && source "${DIR}/prism.conf"

backend() {
    local which="${1:-$(cat "$STATE" 2>/dev/null || echo backend1)}"
    [ "$which" != "backend2" ] && which="backend1"
    case "$2" in
        url)   [ "$which" = "backend1" ] && echo "$BACKEND_1_URL"   || echo "$BACKEND_2_URL" ;;
        label) [ "$which" = "backend1" ] && echo "$BACKEND_1_LABEL" || echo "$BACKEND_2_LABEL" ;;
        name)  echo "$which" ;;
    esac
}

set_backend() { echo "backend${1}" > "$STATE"; }

is_running() { pgrep -f "uvicorn prism:app.*${PORT}" >/dev/null 2>&1; }

# ── commands ─────────────────────────────────────────────────────────────
do_start() {
    if is_running; then
        echo "Prism already running ($(backend "" label))"
        return 1
    fi
    cd "$DIR"
    [ -f "${DIR}/venv/bin/activate" ] && source "${DIR}/venv/bin/activate"
    BACKEND_URL="$(backend "" url)" \
    BACKEND_1_LABEL="$BACKEND_1_LABEL" BACKEND_2_LABEL="$BACKEND_2_LABEL" \
    nohup python3 -m uvicorn prism:app \
        --host 0.0.0.0 --port "$PORT" --log-level info \
        > "$LOG" 2>&1 &
    echo "Prism started  PID=$!  $(backend "" label) → $(backend "" url)  :${PORT}"
}

do_stop() {
    is_running || { echo "Prism is not running"; return 0; }
    pkill -f "uvicorn prism:app.*${PORT}"
    echo "Prism stopped"
}

do_status() {
    if is_running; then
        local pid; pid=$(pgrep -f "uvicorn prism:app.*${PORT}" | head -1)
        echo "RUNNING  PID=$pid  $(backend "" label) → $(backend "" url)  :${PORT}"
        local resp; resp=$(curl -s --max-time 3 "http://localhost:${PORT}/v1/models")
        if [ -n "$resp" ]; then
            echo "  Model:  $(echo "$resp" | jq -r '.data[0].id // "unknown"' 2>/dev/null)"
            echo "  Health: OK"
        else
            echo "  Health: backend unreachable"
        fi
    else
        echo "NOT RUNNING  configured=$(backend "" label) → $(backend "" url)"
    fi
}

do_switch() {
    case "${1:-}" in
        1) set_backend 1 ;;
        2) set_backend 2 ;;
        "") [ "$(backend "" name)" = "backend1" ] && set_backend 2 || set_backend 1 ;;
        *) echo "Usage: prism switch [1|2]"; return 1 ;;
    esac
    echo "Backend → $(backend "" label) → $(backend "" url)"
    is_running && { do_stop; sleep 1; do_start; }
}

# ── main ─────────────────────────────────────────────────────────────────
case "${1:-start}" in
    1|2)     set_backend "$1"; do_start ;;
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop; sleep 1; do_start ;;
    status)  do_status ;;
    switch)  do_switch "${2:-}" ;;
    logs)    tail -f "$LOG" ;;
    *)
        cat <<USAGE
Usage: prism {start|stop|restart|status|switch|logs} [1|2]

  start          Start Prism (default)
  stop           Stop Prism
  restart        Restart
  status         Show state, backend, model, health
  switch [1|2]   Switch backend (no arg = toggle)
  logs           Tail log file
  1 / 2          Start with backend 1 or 2
USAGE
        ;;
esac
