#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
Utility script to execute the O1 adapter integration tests inside docker-compose.

For a given netconf profile (gnb|cu|cucp|cuup|du), this runs the suite once
with the profile's bundled config (built into the netconf image), then once per
XML under ``tests/configs/<profile>/``. The shared volume is wiped between runs
so stale artifacts don't leak.

Usage:
    python run_tests.py <profile>
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

PROFILES = ("gnb", "cu", "cucp", "cuup", "du")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=PROFILES)
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    profile: str = args.profile

    compose_file = Path(__file__).resolve().parent / "docker-compose.yml"
    compose_dir = compose_file.parent
    configs_dir = compose_dir / "tests" / "configs" / profile

    if not configs_dir.is_dir():
        print(f"Configs dir for profile '{profile}' not found: {configs_dir}", file=sys.stderr)
        return 1

    custom_configs = sorted(configs_dir.glob("*.xml"))

    # First entry is the bundled in-image config (no --custom-config); the rest
    # mount and reference each XML from tests/configs/<profile>/.
    runs: list[tuple[str, Path | None]] = [("bundled", None)]
    runs.extend((cfg.stem, cfg) for cfg in custom_configs)

    up_cmd = [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "--profile",
        "test",
        "up",
        "--abort-on-container-exit",
        "--exit-code-from",
        "o1_tests",
        "o1_tests",
    ]

    if args.build:
        up_cmd.insert(up_cmd.index("up") + 1, "--build")

    down_cmd = [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "--profile",
        "test",
        "down",
        "--volumes",
        "--remove-orphans",
    ]

    netconf_sha = subprocess.check_output(
        ["git", "-C", str(compose_dir / "ocudu_elements" / "ocudu_netconf"),
         "rev-parse", "HEAD"],
        text=True,
    ).strip()

    base_env = os.environ.copy()
    base_env.setdefault("O1_ADAPTER_WS_HOST", "ocudu-mock-gnb")
    base_env.setdefault("O1_ADAPTER_WS_PORT", "8001")
    base_env.setdefault("MOCK_GNB_EVENT_LOG", "/tmp/mock_gnb_events.jsonl")
    base_env.setdefault("MOCK_SMO_EVENT_LOG", "/tmp/mock_smo_events.jsonl")
    base_env.setdefault(
        "NETCONF_IMAGE_REPO",
        "registry.gitlab.com/ocudu/ocudu_elements/ocudu_oran_apps/ocudu_netconf/netconf_amd64",
    )
    base_env["NETCONF_COMMIT"] = netconf_sha
    base_env["NETCONF_CONFIGS_DIR"] = str(configs_dir.resolve())
    base_env["O1_ADAPTER_PROFILE"] = profile

    results_dir = os.environ.get("O1_TEST_RESULTS_DIR")
    if results_dir:
        os.makedirs(results_dir, exist_ok=True)

    overall_rc = 0
    results: list[tuple[str, int]] = []

    for label, cfg in runs:
        banner = f"=== Running tests: profile={profile}, config={label} ==="
        print("\n" + "=" * len(banner))
        print(banner)
        print("=" * len(banner), flush=True)

        env = base_env.copy()
        netconf_args = f"--config {profile}"
        if cfg is not None:
            netconf_args += f" --custom-config /config/{cfg.name}"
        netconf_args += " --enable-tls"
        env["NETCONF_ARGS"] = netconf_args
        env["CURRENT_CONFIG_NAME"] = label
        env["PYTEST_ADDOPTS"] = (
            f"--junitxml=./log/out_{profile}_{label}.xml "
            f"-o junit_suite_name={profile}-{label} "
            f"test_o1_adapter_{profile}.py"
        )

        up_proc = subprocess.run(up_cmd, cwd=compose_dir, check=False, env=env)
        if results_dir:
            # Save the adapter-generated config, named per netconf config, before
            # `down --volumes` wipes it, so CI can publish it as an artifact.
            subprocess.run(
                ["docker", "cp", "ocudu-o1-adapter:/tmp/config.yaml",
                 f"{results_dir}/config_{profile}_{label}.yaml"],
                cwd=compose_dir, check=False, env=env,
            )
        subprocess.run(down_cmd, cwd=compose_dir, check=False, env=env)

        results.append((f"{profile}/{label}", up_proc.returncode))
        if up_proc.returncode != 0 and overall_rc == 0:
            overall_rc = up_proc.returncode

    print("\n=== Summary ===")
    for name, rc in results:
        status = "PASS" if rc == 0 else f"FAIL (exit {rc})"
        print(f"  {name}: {status}")

    if results_dir:
        merge_junit_reports(Path(results_dir), profile)

    return overall_rc


def merge_junit_reports(out_dir: Path, profile: str) -> None:
    paths = sorted(out_dir.glob(f"out_{profile}_*.xml"))
    if not paths:
        return
    merged = ET.Element("testsuites")
    for p in paths:
        root = ET.parse(p).getroot()
        suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
        merged.extend(suites)
    ET.ElementTree(merged).write(
        out_dir / "out.xml", xml_declaration=True, encoding="utf-8"
    )


if __name__ == "__main__":
    sys.exit(main())
