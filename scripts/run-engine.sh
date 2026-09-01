#!/usr/bin/env bash
# Start the tracking engine, optionally supervising dump1090 in the same container.
#
# PYAERIAL_START_DUMP1090=1 (default) runs dump1090 with exponential restart backoff.
# Set PYAERIAL_START_DUMP1090=0 when dump1090 is a separate compose service.
set -euo pipefail

CONFIG="${PYAERIAL_CONFIG:-/opt/PyAerial/config.yaml}"
DUMP1090_BIN="${DUMP1090_BIN:-/opt/dump1090/dump1090}"
START_DUMP1090="${PYAERIAL_START_DUMP1090:-1}"

dump1090_watchdog_pid=""
engine_pid=""

stop_children() {
  if [[ -n "${engine_pid}" ]]; then
    kill "${engine_pid}" 2>/dev/null || true
    wait "${engine_pid}" 2>/dev/null || true
  fi
  if [[ -n "${dump1090_watchdog_pid}" ]]; then
    pkill -P "${dump1090_watchdog_pid}" 2>/dev/null || true
    kill "${dump1090_watchdog_pid}" 2>/dev/null || true
    wait "${dump1090_watchdog_pid}" 2>/dev/null || true
  fi
}

trap stop_children EXIT TERM INT

watch_dump1090() {
  local backoff=1
  while true; do
    echo "Starting dump1090 (${DUMP1090_BIN})"
    "${DUMP1090_BIN}" --net --raw --quiet &
    local child=$!
    wait "${child}" || true
    echo "dump1090 exited; restarting in ${backoff}s"
    sleep "${backoff}"
    backoff=$((backoff * 2))
    if (( backoff > 30 )); then
      backoff=30
    fi
  done
}

if [[ "${START_DUMP1090}" == "1" ]]; then
  if [[ -x "${DUMP1090_BIN}" ]]; then
    watch_dump1090 &
    dump1090_watchdog_pid=$!
  else
    echo "dump1090 not found at ${DUMP1090_BIN}; continuing without it"
  fi
fi

pyaerial run -c "${CONFIG}" &
engine_pid=$!
wait "${engine_pid}"
