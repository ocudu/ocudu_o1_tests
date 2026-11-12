# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""yaml_to_netconf's metrics leaf table must follow metrics-extensions.yang.

The inverse path routes each top-level metrics leaf to the functions whose YANG grouping
declares it, using a hand-kept table (_METRICS_COMMON_LEAVES, _METRICS_FUNCTION_LEAVES). A leaf
added to the YANG but not to the table is silently dropped from converted configs, so the two
are compared here, where both submodules are checked out.
"""

import os
from pathlib import Path

from pytest import mark

YANG = Path(os.getenv("NETCONF_YANG_DIR", "/opt/netconf/custom_yangs")) / "metrics-extensions.yang"

FUNCTION_GROUPINGS = {
    "GNBDUFunction": "metrics_extensions_du_subtree",
    "GNBCUCPFunction": "metrics_extensions_cucp_subtree",
    "GNBCUUPFunction": "metrics_extensions_cuup_subtree",
}


def _grouping_leaves():
    """Map each grouping in the module to its leaf names, with `uses` expanded."""
    leaves, uses = {}, {}
    grouping = None
    for line in YANG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("grouping "):
            grouping = line.split()[1]
            leaves[grouping], uses[grouping] = set(), []
        elif line.startswith("leaf "):
            leaves[grouping].add(line.split()[1])
        elif line.startswith("uses "):
            uses[grouping].append(line.split()[1].rstrip(";"))

    def expand(name):
        return leaves[name].union(*(expand(used) for used in uses[name]))

    return {name: expand(name) for name in leaves}


@mark.timeout(60)
def test_metrics_function_groupings_are_mapped():
    """Every per-function subtree in the YANG has an entry here, so a new function fails loudly."""
    subtrees = {name for name in _grouping_leaves() if name.endswith("_subtree")}
    assert subtrees == set(FUNCTION_GROUPINGS.values())


@mark.timeout(60)
@mark.parametrize("function", FUNCTION_GROUPINGS)
def test_metrics_leaf_table_matches_yang(o1_adapter_src, function):
    """The leaves the inverse path routes to a function are exactly the ones its grouping declares."""
    _ = o1_adapter_src
    from yaml_to_netconf import _METRICS_COMMON_LEAVES, _METRICS_FUNCTION_LEAVES

    routed = _METRICS_COMMON_LEAVES | _METRICS_FUNCTION_LEAVES[function]
    assert routed == _grouping_leaves()[FUNCTION_GROUPINGS[function]]
