#!/usr/bin/env python3
#
# Copyright 2021-2026 Software Radio Systems Limited
#
# By using this file, you agree to the terms and conditions set
# forth in the LICENSE file which can be found at the top level of
# the distribution.
#

"""
Utility script to execute the O1 adapter integration tests inside docker-compose.

Usage:
    python run_tests.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    compose_file = Path(__file__).resolve().parent / "docker-compose.yml"
    compose_dir = compose_file.parent

    up_cmd = [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "--profile",
        "test",
        "up",
        "--build",
        "--abort-on-container-exit",
        "--exit-code-from",
        "o1_tests",
        "o1_tests",
    ]

    down_cmd = [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "--profile",
        "test",
        "down",
        "--remove-orphans",
    ]

    env = os.environ.copy()
    env.setdefault("O1_ADAPTER_WS_HOST", "ocudu-mock-gnb")
    env.setdefault("O1_ADAPTER_WS_PORT", "8001")
    env.setdefault("MOCK_GNB_EVENT_LOG", "/tmp/mock_gnb_events.jsonl")

    up_proc = subprocess.run(up_cmd, cwd=compose_dir, check=False, env=env)
    try:
        return up_proc.returncode
    finally:
        subprocess.run(down_cmd, cwd=compose_dir, check=False, env=env)


if __name__ == "__main__":
    sys.exit(main())
