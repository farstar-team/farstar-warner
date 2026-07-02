#!/usr/bin/env bash
set -uo pipefail

TRACE_URL="https://www.cloudflare.com/cdn-cgi/trace"
CHECK_INTERVAL_SECONDS="${WARP_HEALTH_INTERVAL_SECONDS:-30}"
FAILURE_THRESHOLD="${WARP_HEALTH_FAILURE_THRESHOLD:-3}"

log() {
    printf '[warp-supervisor] %s\n' "$*"
}

/entrypoint.sh &
entrypoint_pid=$!

shutdown() {
    log "Stopping WARP entrypoint."
    kill -TERM "$entrypoint_pid" 2>/dev/null || true
    wait "$entrypoint_pid" 2>/dev/null || true
}
trap shutdown TERM INT

for _ in $(seq 1 30); do
    if curl -fsS --max-time 3 --socks5-hostname 127.0.0.1:1080 \
        "$TRACE_URL" >/dev/null 2>&1; then
        break
    fi
    if ! kill -0 "$entrypoint_pid" 2>/dev/null; then
        wait "$entrypoint_pid"
        exit $?
    fi
    sleep 2
done

# The upstream image skips warp-cli connect when registration data already
# exists. Connecting here makes container restarts deterministic.
warp-cli --accept-tos connect >/dev/null 2>&1 || true

failures=0
while kill -0 "$entrypoint_pid" 2>/dev/null; do
    trace="$(
        curl -fsS --max-time 10 --socks5-hostname 127.0.0.1:1080 \
            "$TRACE_URL" 2>/dev/null || true
    )"
    if grep -qE '^warp=(on|plus)$' <<<"$trace"; then
        if (( failures > 0 )); then
            log "WARP tunnel is healthy again."
        fi
        failures=0
    else
        ((failures += 1))
        log "WARP health probe failed ($failures/$FAILURE_THRESHOLD)."
        if (( failures >= FAILURE_THRESHOLD )); then
            log "Reconnecting the existing WARP tunnel."
            if ! warp-cli --accept-tos debug extra reconnect >/dev/null 2>&1; then
                warp-cli --accept-tos disconnect >/dev/null 2>&1 || true
                sleep 2
                warp-cli --accept-tos connect >/dev/null 2>&1 || true
            fi
            failures=0
        fi
    fi
    sleep "$CHECK_INTERVAL_SECONDS"
done

wait "$entrypoint_pid"
