#!/bin/sh

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

set -eu

RESULT_FILE="/tmp/gnb-dryrun.result"
LOG_FILE="/tmp/gnb-dryrun.log"

rm -f "$RESULT_FILE" "$LOG_FILE"

until [ -s /tmp/config.yaml ]; do
  sleep 1
done

if gnb -c /tmp/config.yaml --dryrun >"$LOG_FILE" 2>&1; then
  status=0
else
  status=$?
fi

{
  printf '%s\n' "$status"
  cat "$LOG_FILE"
} >"$RESULT_FILE"

tail -f /dev/null
