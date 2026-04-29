#!/bin/sh

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

set -eu

RESULT_FILE="/tmp/dryrun.result"
LOG_FILE="/tmp/dryrun.log"

rm -f "$RESULT_FILE" "$LOG_FILE"

case "${O1_ADAPTER_PROFILE:-}" in
  gnb)  BIN=gnb ;;
  cu)   BIN=ocu ;;
  cucp) BIN=ocucp ;;
  cuup) BIN=ocuup ;;
  du)   BIN=odu ;;
  *)    echo "unknown profile: ${O1_ADAPTER_PROFILE:-<unset>}" >&2; exit 1 ;;
esac

until [ -s /tmp/config.yaml ]; do
  sleep 1
done

if "$BIN" -c /tmp/config.yaml --dryrun >"$LOG_FILE" 2>&1; then
  status=0
else
  status=$?
fi

{
  printf '%s\n' "$status"
  cat "$LOG_FILE"
} >"$RESULT_FILE"

tail -f /dev/null
