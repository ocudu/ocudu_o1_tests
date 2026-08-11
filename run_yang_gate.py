#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
Run the OpenDaylight YANG compatibility gate against one or more netconf profiles.

Boots just the netconf container for a profile, has the gate read back the module
set the server advertises and rebuild it with ODL's parser, then tears the
container down. Nothing else from the compose file is needed, so this is quick
enough to gate every merge request -- unlike a real SDNR mount, which needs the
whole SMO stack.

One run per profile is enough: the advertised module set comes from the profile's
setup script, and the custom XMLs under tests/configs/<profile>/ only change
datastore contents, not which modules are installed.

The device's `ru` profile is covered transitively and so is not listed: netconf's
setup_du.sh sources setup_ru.sh and setup_gnb.sh sources setup_du.sh, making ru's
module set a subset of du's and gnb's. If that chain is ever broken, add `ru` here
-- it is a mountable NETCONF device in its own right, and nothing else asserts the
subset relation.

Usage:
    python run_yang_gate.py [profile ...]      # defaults to every profile
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROFILES = ("gnb", "cu", "cucp", "cuup", "du")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # No `choices=` here: with nargs="*" argparse validates the default list
    # against it as a single value and rejects it.
    parser.add_argument("profiles", nargs="*", metavar="|".join(PROFILES))
    parser.add_argument("--build", action="store_true",
                        help="rebuild the netconf image from the submodule checkout")
    args = parser.parse_args()

    profiles = args.profiles or list(PROFILES)
    unknown = [profile for profile in profiles if profile not in PROFILES]
    if unknown:
        parser.error(f"unknown profile(s): {', '.join(unknown)}")

    compose_file = Path(__file__).resolve().parent / "docker-compose.yml"
    compose_dir = compose_file.parent
    compose = ["docker", "compose", "-f", str(compose_file), "--profile", "yang-gate"]
    down_cmd = compose + ["down", "--volumes", "--remove-orphans"]

    netconf_sha = subprocess.check_output(
        ["git", "-C", str(compose_dir / "ocudu_elements" / "ocudu_netconf"),
         "rev-parse", "HEAD"],
        text=True,
    ).strip()

    base_env = os.environ.copy()
    base_env.setdefault(
        "NETCONF_IMAGE_REPO",
        "registry.gitlab.com/ocudu/ocudu_elements/ocudu_oran_apps/ocudu_netconf/netconf_amd64",
    )
    base_env["NETCONF_COMMIT"] = netconf_sha

    # Neither image depends on the profile, so build once rather than per iteration.
    if args.build:
        subprocess.run(compose + ["build", "netconf", "odl_yang_gate"],
                       cwd=compose_dir, check=True, env=base_env)

    # Clear any leftovers from an interrupted previous run before fixed
    # container names and host ports are reused.
    subprocess.run(down_cmd, cwd=compose_dir, check=False, env=base_env)

    results: list[tuple[str, int]] = []
    for profile in profiles:
        banner = f"=== ODL YANG gate: profile={profile} ==="
        print("\n" + "=" * len(banner))
        print(banner)
        print("=" * len(banner), flush=True)

        env = base_env.copy()
        env["O1_ADAPTER_PROFILE"] = profile
        env["NETCONF_ARGS"] = f"--config {profile}"
        # Unused by the gate -- the module set comes from the profile's setup
        # script -- but the netconf service mounts it, so point it somewhere real.
        env["NETCONF_CONFIGS_DIR"] = str(compose_dir / "tests" / "configs" / profile)

        # `run` brings the netconf container up as a dependency; the gate's own
        # connect retry covers the server still booting.
        gate = subprocess.run(compose + ["run", "--rm", "odl_yang_gate"],
                              cwd=compose_dir, check=False, env=env)
        subprocess.run(down_cmd, cwd=compose_dir, check=False, env=env)
        results.append((profile, gate.returncode))

    print("\n=== Summary ===")
    for profile, returncode in results:
        status = "PASS" if returncode == 0 else f"FAIL (exit {returncode})"
        print(f"  {profile}: {status}")

    return 0 if all(returncode == 0 for _, returncode in results) else 1


if __name__ == "__main__":
    sys.exit(main())
