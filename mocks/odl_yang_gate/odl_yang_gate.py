#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
Check the netconf server's advertised YANG set against OpenDaylight's parser.

ODL/SDNR is stricter than libyang, so a device that boots clean can still lose
whole subtrees at mount time. A libyang-based gate cannot catch that, because it
*is* libyang: it resolves its built-in modules from a byte array compiled into
liblyang.so, binds revision-less imports to the newest revision it can see, and
lazily skips unused typedefs. `yanglint --strict` changes none of that. ODL's own
YangTools runs standalone in about a second per build, so it is both the faithful
oracle and cheap enough for a per-MR gate.

The module set fed to the parser matters as much as the parser does. ODL binds
against what the server advertises as *implemented* and fetches each source over
get-schema. Point a validator at the raw model directory instead -- which carries
several revisions of the same module -- and a revision-less import binds to a
revision the server never advertised, so the check passes on a set ODL would
reject. That false green is the failure mode this script is shaped to avoid, so
the set is read from ietf-yang-library and every source is pulled over
get-schema, exactly as ODL does at mount time.

Three things are asserted:

  1. the modules that should mount build as one schema context, which is what a
     mount actually does;
  2. every module the baseline in known_failures.yaml excuses really is still
     broken, so the baseline cannot quietly rot;
  3. a deliberately poisoned copy of the set is *rejected* -- because a false
     green is indistinguishable from a real pass, the gate proves it has teeth on
     every run.

The per-module sweep only runs when (1) fails, to attribute the failure. On a
green run it would be ~130 redundant JVM starts.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

import yaml
from ncclient import manager

YANG_LIBRARY_NS = "urn:ietf:params:xml:ns:yang:ietf-yang-library"
LIB = "{" + YANG_LIBRARY_NS + "}"

VALIDATOR_MAIN = "org.opendaylight.yangtools.yang.validator.Main"

# Each default-configured JVM sizes its GC and compiler threads off the host core
# count, so a poolful of them oversubscribes a CI runner badly: these flags cut
# the per-module sweep's wall time ~3.6x and its CPU time ~4.9x.
SWEEP_JVM_FLAGS = ("-Xmx1g", "-XX:TieredStopAtLevel=1", "-XX:+UseSerialGC",
                   "-XX:ActiveProcessorCount=1")

# One core per JVM by the flags above, so the sweep can simply take the machine.
SWEEP_WORKERS = min(os.cpu_count() or 4, 8)

# get-schema is latency-bound at ~0.1s per call regardless of module size, and
# ncclient routes replies by message-id, so one session serves a pool fine.
EXPORT_WORKERS = 8

CANARY_NAME = "ocudu-gate-canary"
CANARY_SOURCE = """module ocudu-gate-canary {
  yang-version 1.1;
  namespace "urn:ocudu:gate-canary";
  prefix canary;
  import ietf-yang-types { prefix yang; }
  typedef canary-time { type yang:time; }
  leaf probe { type canary-time; }
}
"""


@dataclass(frozen=True)
class Module:
    """One entry of the server's ietf-yang-library module-set."""

    name: str
    revision: str
    namespace: str
    features: tuple[str, ...]

    @property
    def filename(self) -> str:
        return f"{self.name}@{self.revision}.yang" if self.revision else f"{self.name}.yang"


def connect(host: str, port: int, username: str, password: str, timeout: int):
    """Open a netconf session, waiting for the server to finish coming up."""
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            return manager.connect(
                host=host,
                port=port,
                username=username,
                password=password,
                hostkey_verify=False,
                look_for_keys=False,
                allow_agent=False,
                device_params={"name": "default"},
            )
        except Exception as exc:  # noqa: BLE001 - anything here means "not up yet"
            last_error = exc
            time.sleep(2)
    raise TimeoutError(f"netconf server not reachable within {timeout}s: {last_error}")


def read_yang_library(session) -> tuple[list[Module], list[Module]]:
    """Return (implemented, import_only) modules as the server advertises them."""
    reply = session.get(filter=("subtree", f'<yang-library xmlns="{YANG_LIBRARY_NS}"/>'))
    root = ET.fromstring(reply.data_xml).find(f"{LIB}yang-library")
    if root is None:
        raise RuntimeError(
            "server returned no ietf-yang-library data even though it advertises "
            "the yang-library capability; ODL would have nothing to build from"
        )

    implemented: list[Module] = []
    import_only: list[Module] = []
    # Only direct module-set children: yang-library/schema carries a `module-set`
    # leafref list too, which iter() would pick up as empty entries.
    for module_set in root.findall(f"{LIB}module-set"):
        for element in module_set.findall(f"{LIB}module"):
            implemented.append(parse_module(element))
        for element in module_set.findall(f"{LIB}import-only-module"):
            import_only.append(parse_module(element))
    return implemented, import_only


def parse_module(element: ET.Element) -> Module:
    # Submodules are deliberately not modelled: none of the shipped profiles
    # advertises one. If that changes, the source for it has to be fetched here
    # and module_imports() taught to follow `include`.
    features = tuple(
        feature.text for feature in element.findall(f"{LIB}feature") if feature.text
    )
    return Module(
        name=element.findtext(f"{LIB}name", ""),
        revision=element.findtext(f"{LIB}revision", ""),
        namespace=element.findtext(f"{LIB}namespace", ""),
        features=features,
    )


def export_sources(session, modules: list[Module],
                   out_dir: Path) -> tuple[dict[str, Path], list[Module]]:
    """Fetch each module's source over get-schema, as ODL does at mount time.

    Returns the name -> path map of what was served, and the modules the server
    refused. A refusal is not fatal by itself: libyang compiles a handful of
    built-ins (`yang`, `ietf-yang-metadata`, ...) into liblyang.so with no
    servable file behind them, and YangTools carries its own copies. It only
    matters for the modules importing them, which unresolvable_closure() works
    out.
    """
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    def fetch(module: Module) -> tuple[Module, Path | None]:
        try:
            reply = session.get_schema(module.name, version=module.revision or None)
        except Exception:  # noqa: BLE001 - however it fails, ODL cannot fetch it either
            return module, None
        path = out_dir / module.filename
        path.write_text(str(reply.data))
        return module, path

    with ThreadPoolExecutor(max_workers=EXPORT_WORKERS) as pool:
        fetched = list(pool.map(fetch, modules))

    sources = {module.name: path for module, path in fetched if path is not None}
    unserved = [module for module, path in fetched if path is None]
    return sources, unserved


def module_imports(path: Path) -> set[str]:
    """Names imported or included by a YANG file.

    Deliberately a flat line scan rather than a parse: it only has to be good
    enough to propagate "a dependency is missing" through the graph, and the
    validator is the authority on everything else. Missing an edge is the safe
    direction -- it downgrades a module from "excused" to "unexpected failure",
    which fails the job rather than hiding.
    """
    imports: set[str] = set()
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        tokens = stripped.split()
        if len(tokens) >= 2 and tokens[0] in ("import", "include"):
            imports.add(tokens[1].strip('{;"'))
    return imports


def unresolvable_closure(sources: dict[str, Path], unavailable: set[str]) -> set[str]:
    """Modules that cannot resolve because something they depend on is unavailable.

    ODL reports exactly these as unavailable-capabilities on an otherwise healthy
    mount. Deriving them from the import graph rather than hard-coding a list
    means the set re-derives itself when the unavailable modules change.
    """
    graph = {name: module_imports(path) for name, path in sources.items()}
    blocked = set(unavailable)
    while True:
        newly_blocked = {
            name for name, imports in graph.items()
            if name not in blocked and imports & blocked
        }
        if not newly_blocked:
            return blocked - unavailable
        blocked |= newly_blocked


def feature_arguments(modules: list[Module]) -> list[str]:
    """Render advertised features in yang-system-test's -f syntax.

    Passing them prunes every if-feature node the server did not enable, which is
    what ODL does with the feature list out of ietf-yang-library. Omitting -f
    would enable all features and validate a data model the device never exposes.
    """
    arguments = []
    for module in modules:
        for feature in module.features:
            if module.revision:
                arguments.append(f"({module.namespace}?revision={module.revision}){feature}")
            else:
                arguments.append(f"({module.namespace}){feature}")
    return arguments


def run_validator(arguments: list[str], jvm_flags: tuple[str, ...] = ()) -> tuple[int, str]:
    """Invoke yang-system-test; return (returncode, combined output)."""
    command = ["java", *jvm_flags, "-cp", os.environ["YANGTOOLS_CLASSPATH"],
               VALIDATOR_MAIN, *arguments]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    return completed.returncode, completed.stdout + completed.stderr


def validate_module(name: str, search_dir: Path, features: list[str]) -> tuple[int, str]:
    """Build one module's dependency closure, the way ODL resolves one mount source.

    -p contributes every sibling as a *lib* source so imports resolve, while -m
    names the single *test* source. Exactly one name at a time matters: -m
    resolves names against the lib dirs, so two names make YangTools load those
    files as both lib and test sources and report a bogus namespace collision.
    """
    arguments = ["-f", *features] if features else []
    arguments += ["-p", str(search_dir), "-m", name]
    return run_validator(arguments, jvm_flags=SWEEP_JVM_FLAGS)


def validate_set(sources: list[Path], features: list[str]) -> tuple[int, str]:
    """Build one schema context from all of `sources`, the way a mount does.

    Passing paths positionally makes every one of them a test source with no lib
    dir behind them, which is what keeps the double-load collision away here. -f
    goes last because it is variadic and would otherwise swallow the paths.
    """
    arguments = [str(path) for path in sources]
    if features:
        arguments += ["-f", *features]
    return run_validator(arguments)


def failure_reason(output: str) -> str:
    """Condense a YangTools stack trace into the line a reviewer needs."""
    reason = ""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(("Caused by:", "Suppressed:")):
            # "Caused by: <exception class>: <message>" -- keep the message.
            parts = stripped.split(": ", 2)
            if len(parts) == 3:
                reason = parts[2]
    return reason or "validator failed without a recognisable cause; see the log above"


def self_test(export_dir: Path, sources: dict[str, Path], healthy: list[str],
              features: list[str]) -> str | None:
    """Prove the gate has teeth; return a failure message, or None if it passed.

    Renames `typedef time` out of the advertised ietf-yang-types and adds a module
    that needs it, then requires the validator to reject the result. The property
    being proved is that the validator binds the copy exported from the *device*
    rather than the copy YangTools ships in its own jars -- if it silently
    preferred its own, every check above would be theatre. That is also why the
    poison has to land on a real advertised module and not on a synthetic one.
    (The July 2026 SDNR outage, where the advertised ietf-yang-types had no
    `time`, is what this shape is modelled on.)
    """
    if "ietf-yang-types" not in sources:
        return ("ietf-yang-types is not advertised, so the self-test cannot be "
                "built and a green result above cannot be trusted")

    # Only the two doctored files need a directory of their own: every path
    # handed to validate_set() is a test source in its own right, so the rest of
    # the set is read straight out of the untouched export.
    poisoned_dir = export_dir.with_name(export_dir.name + "-poisoned")
    poisoned_dir.mkdir(parents=True, exist_ok=True)

    lines = sources["ietf-yang-types"].read_text().splitlines()
    for index, line in enumerate(lines):
        # Exact name, so the sibling time-no-zone/timeticks typedefs are not hit.
        if line.split()[:2] == ["typedef", "time"]:
            lines[index] = line.replace("typedef time", "typedef ocudu-poisoned-time", 1)
            break
    poisoned_types = poisoned_dir / sources["ietf-yang-types"].name
    poisoned_types.write_text("\n".join(lines) + "\n")
    canary = poisoned_dir / f"{CANARY_NAME}.yang"
    canary.write_text(CANARY_SOURCE)

    print(f"Self-test: rebuilding with `time` renamed out of "
          f"{poisoned_types.name} ...", flush=True)
    poisoned = [sources[name] for name in healthy if name != "ietf-yang-types"]
    returncode, output = validate_set(poisoned + [poisoned_types, canary], features)
    # Rejected, and rejected over the canary: the set is large enough that "some
    # module failed" could be an unrelated cascade, which would prove nothing.
    if returncode == 0 or f"{CANARY_NAME}.yang" not in output:
        return (f"the poisoned self-test set did not fail on {CANARY_NAME} "
                f"(exit {returncode}), so the gate's teeth are unproven -- the "
                "validator may be binding YangTools' own ietf-yang-types rather "
                "than the device's; fix the harness before trusting it")
    print(f"Self-test OK: the poisoned set was rejected, {CANARY_NAME} with it.",
          flush=True)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("NETCONF_HOST", "ocudu-netconf"))
    parser.add_argument("--port", type=int, default=int(os.getenv("NETCONF_PORT", "830")))
    parser.add_argument("--username", default=os.getenv("NETCONF_USERNAME", "root"))
    parser.add_argument("--password", default=os.getenv("NETCONF_PASSWORD", "root"))
    parser.add_argument("--profile", default=os.getenv("O1_ADAPTER_PROFILE", ""),
                        help="netconf profile under test, for reporting only")
    parser.add_argument("--connect-timeout", type=int, default=300)
    parser.add_argument("--export-dir", type=Path,
                        default=Path(os.getenv("YANG_EXPORT_DIR", "/tmp/yang-advertised")))
    parser.add_argument("--baseline", type=Path,
                        default=Path(__file__).with_name("known_failures.yaml"))
    args = parser.parse_args()

    if not os.getenv("YANGTOOLS_CLASSPATH"):
        print("YANGTOOLS_CLASSPATH is unset; this must run in the odl_yang_gate image",
              file=sys.stderr)
        return 2

    baseline = yaml.safe_load(args.baseline.read_text()) or {}
    known_failures: dict[str, str] = baseline.get("known_failures", {})
    must_mount: list[str] = baseline.get("must_mount", [])

    label = f" [{args.profile}]" if args.profile else ""
    print(f"=== ODL YANG compatibility gate{label} ===", flush=True)
    print(f"Connecting to {args.host}:{args.port} ...", flush=True)
    session = connect(args.host, args.port, args.username, args.password,
                      args.connect_timeout)
    try:
        implemented, import_only = read_yang_library(session)
        print(f"Advertised: {len(implemented)} implemented, {len(import_only)} import-only",
              flush=True)

        # ODL binds against the implemented revisions. An import-only revision of
        # a name that is already implemented is not a second candidate, and
        # feeding it in is precisely the false green this gate exists to prevent
        # -- so keep only the import-only modules whose name is otherwise absent.
        implemented_names = {module.name for module in implemented}
        extra = []
        for module in sorted(import_only, key=lambda entry: entry.name):
            if module.name in implemented_names:
                print(f"  ignoring shadowed import-only revision {module.filename}")
            else:
                extra.append(module)

        print(f"Fetching {len(implemented) + len(extra)} sources over get-schema ...",
              flush=True)
        sources, unserved = export_sources(session, implemented + extra, args.export_dir)
    finally:
        session.close_session()

    failures: list[str] = []

    unserved_names = {module.name for module in unserved}
    for module in sorted(unserved, key=lambda entry: entry.name):
        print(f"  advertised but not served over get-schema: {module.filename}")

    # Whatever ODL cannot fetch, or cannot parse, takes its importers down with
    # it -- exactly the unavailable-capabilities cascade a real mount reports.
    baselined = set(known_failures) & set(sources)
    collateral = unresolvable_closure(sources, unserved_names | baselined)
    for name in sorted(collateral):
        print(f"  unresolvable via an unavailable import: {name}")

    features = feature_arguments(
        [module for module in implemented if module.name not in unserved_names]
    )
    healthy = sorted(set(sources) - collateral - baselined)

    print(f"\nBuilding one schema context from {len(healthy)} modules "
          f"({len(features)} advertised features) ...", flush=True)
    returncode, output = validate_set([sources[name] for name in healthy], features)
    if returncode == 0:
        print("Whole-set build OK.", flush=True)
    else:
        # The set failed, so attribute it: this is the only thing the per-module
        # sweep is needed for, and the only time its ~130 JVM starts are earned.
        print(f"Whole-set build FAILED; sweeping {len(healthy)} modules "
              f"individually to attribute it ({SWEEP_WORKERS} jobs) ...", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=SWEEP_WORKERS) as pool:
            swept = pool.map(
                lambda name: (name, *validate_module(name, args.export_dir, features)),
                healthy,
            )
        broken = {name: failure_reason(text) for name, code, text in swept if code != 0}
        for name in sorted(broken):
            print(f"\nFAIL {name}\n     {broken[name]}", file=sys.stderr)
        if broken:
            failures.append(
                f"{len(broken)} module(s) ODL cannot parse that the baseline does "
                f"not know about: {', '.join(sorted(broken))}"
            )
        else:
            print(output, file=sys.stderr)
            failures.append(
                "every module parses alone but they do not build as one schema "
                f"context: {failure_reason(output)}. That is a conflict between "
                "modules -- which is what a real mount builds."
            )

    # A baselined module that started parsing has to leave the baseline, or the
    # file rots into a permanent blanket exemption.
    stale = []
    for name in sorted(baselined):
        code, text = validate_module(name, args.export_dir, features)
        if code == 0:
            stale.append(name)
        else:
            print(f"  known failure (baselined): {name}\n"
                  f"    expected: {known_failures[name]}\n"
                  f"    observed: {failure_reason(text)}")
    if stale:
        failures.append(
            f"{len(stale)} baselined module(s) now parse cleanly; drop them from "
            f"{args.baseline.name}: {', '.join(stale)}"
        )

    # Whatever the device is mounted *for* has to be genuinely healthy, never
    # merely tolerated. Matched as globs so a new in-house module family is one
    # line of data, not a code change.
    excused = sorted(unserved_names | collateral | baselined)
    protected = [name for name in excused
                 if any(fnmatch.fnmatch(name, pattern) for pattern in must_mount)]
    if protected:
        failures.append(
            f"module(s) that must mount are unavailable to ODL: "
            f"{', '.join(protected)}"
        )

    failure = self_test(args.export_dir, sources, healthy, features)
    if failure:
        failures.append(failure)

    print()
    if failures:
        for text in failures:
            print(f"FAILED: {text}", file=sys.stderr)
        return 1
    print(f"PASSED: ODL's parser accepts the advertised YANG set{label}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
