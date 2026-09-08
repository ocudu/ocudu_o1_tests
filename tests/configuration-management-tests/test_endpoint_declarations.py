# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Explicit endpoint declaration tests for the M-plane client.

Endpoint names are the O-RU's device data, and prefix+index generation
cannot express every vendor's fixed names — an O-RU may report zero-padded,
stride-five, type-blind names (PORT-00, PORT-05, PORT-10, ...) that neither
the generator nor the PRACH name heuristic can handle. Config may now declare the names outright
(endpoint.tx_endpoints/rx_endpoints/prach_endpoints), and the same
declarations drive rx-endpoint classification when deriving the DU config.

The PORT fixture reproduces that shape end to end: the baseline
tests document the exact failure the heuristic produces without
declarations, and the declaration tests prove the fix. This module runs after
test_ru_controller.py in run_tests.py, which seeds the 4x4 array-carrier
layout the default link setters reference.
"""

import logging

from pytest import mark, raises

logger = logging.getLogger(__name__)

# A type-blind O-RU naming shape: names reveal neither ordering nor type.
PORT_ENDPOINT_CONFIG = {
    "iq_bitwidth": 9,
    "compression_type": "STATIC",
    "num_prb": 273,
    "frame_structure": 193,
    "prach_frame_structure": 129,
    "prach_num_prb": 12,
    "tx_endpoints": [{"name": "PORT-00", "eaxc_id": 0}, {"name": "PORT-10", "eaxc_id": 1}],
    "rx_endpoints": [{"name": "PORT-05", "eaxc_id": 0}, {"name": "PORT-15", "eaxc_id": 1}],
    "prach_endpoints": [{"name": "PORT-25", "eaxc_id": 4}, {"name": "PORT-35", "eaxc_id": 5}],
}

PORT_LAYOUT = {
    "interface": {"ru_mac_addr": "aa:bb:cc:dd:ee:01", "vlan": 31},
    "processing": {"ru_mac_addr": "aa:bb:cc:dd:ee:01", "du_mac_addr": "aa:bb:cc:dd:ee:02", "vlan": 31},
    "endpoint": PORT_ENDPOINT_CONFIG,
    "carrier": {
        "dl_arfcn": 640000,
        "dl_freq": 3600000000,
        "ul_arfcn": 640000,
        "ul_freq": 3600000000,
        "tx_gain": 21.0,
        "rf_bandwidth_hz": 100000000,
    },
    "activation": {"state": "ACTIVE"},
    "tdd": {"pattern_upload": False, "carrier_binding": False},
}

# What that O-RU's user-plane-configuration readback looks like (parsed).
PORT_UPLANE = {
    "low-level-tx-endpoints": [
        {"name": "PORT-00", "e-axcid": {"eaxc-id": "0"}, "compression": {"compression-type": "STATIC"}},
        {"name": "PORT-10", "e-axcid": {"eaxc-id": "1"}, "compression": {"compression-type": "STATIC"}},
    ],
    "low-level-rx-endpoints": [
        {"name": "PORT-05", "e-axcid": {"eaxc-id": "0"}, "compression": {"compression-type": "STATIC"}},
        {"name": "PORT-15", "e-axcid": {"eaxc-id": "1"}, "compression": {"compression-type": "STATIC"}},
        {"name": "PORT-25", "e-axcid": {"eaxc-id": "4"}, "compression": {"compression-type": "STATIC"}},
        {"name": "PORT-35", "e-axcid": {"eaxc-id": "5"}, "compression": {"compression-type": "STATIC"}},
    ],
}


def _capturing_controller(o1_adapter_src):
    from ru_config import RuConfig

    _ = o1_adapter_src
    controller = RuConfig(None, "running")
    sent = []
    controller.edit_config = lambda xml_request, description="": sent.append((description, xml_request))
    return controller, sent


@mark.timeout(60)
def test_declared_names_drive_every_edit(o1_adapter_src):
    """Declared endpoint names flow into the endpoint edits, the low-level
    links, and the carrier/activation counts — no generated name appears."""
    controller, sent = _capturing_controller(o1_adapter_src)
    controller.set_full_config(dict(PORT_LAYOUT))
    edits = dict(sent)

    tx = edits["ORAN Uplane Tx endpoints elements"]
    assert ">PORT-00<" in tx and ">PORT-10<" in tx
    assert "sep_txch" not in tx, "no generated names once declared"

    rx = edits["ORAN Uplane Rx endpoints elements"]
    for name in ("PORT-05", "PORT-15", "PORT-25", "PORT-35"):
        assert f">{name}<" in rx
    # PRACH entries keep their dedicated frame parameters
    assert ">129<" in rx and ">12<" in rx and ">193<" in rx and ">273<" in rx

    assert ">PORT-00<" in edits["ORAN Uplane low level Tx links"]
    assert ">PORT-15<" in edits["ORAN Uplane low level Rx links"]

    # counts follow the declared lists: 2 tx / 2 rx carriers, not the 4x4 default
    for description in ("ORAN Uplane Tx array carriers", "ORAN Uplane carrier active"):
        assert "Tx-Array-Carrier-01" in edits[description]
        assert "Tx-Array-Carrier-02" not in edits[description]


@mark.timeout(60)
def test_declared_prach_keeps_crossed_carrier_pairing(o1_adapter_src):
    """The legacy crossed PRACH-to-carrier pairing applies to declared names
    exactly as it does to generated ones."""
    import re

    controller, sent = _capturing_controller(o1_adapter_src)
    controller.set_full_config(dict(PORT_LAYOUT))
    rx_links = dict(sent)["ORAN Uplane low level Rx links"]
    blocks = re.findall(r"<low-level-rx-links>.*?</low-level-rx-links>", rx_links, re.S)
    pairing = {
        re.search(r"low-level-rx-endpoint>(.*?)<", block).group(1): re.search(
            r"rx-array-carrier>(.*?)<", block
        ).group(1)
        for block in blocks
    }
    assert pairing["PORT-25"] == "Rx-Array-Carrier-01"
    assert pairing["PORT-35"] == "Rx-Array-Carrier-00"


@mark.timeout(60)
def test_generated_names_remain_the_default(o1_adapter_src):
    """Without declarations the generated prefix+index path is untouched:
    bare config produces the legacy sep_* 4x4 layout, and prefix config
    produces consecutively numbered vendor names."""
    controller, sent = _capturing_controller(o1_adapter_src)

    bare = {**PORT_LAYOUT, "endpoint": {"iq_bitwidth": 9, "compression_type": "STATIC", "num_prb": 273, "frame_structure": 193}}
    controller.set_full_config(bare)
    edits = dict(sent)
    assert ">sep_txch1<" in edits["ORAN Uplane Tx endpoints elements"]
    assert ">sep_prach4<" in edits["ORAN Uplane Rx endpoints elements"]
    assert "Tx-Array-Carrier-03" in edits["ORAN Uplane carrier active"], "defaults keep the 4x4 counts"

    sent.clear()
    prefixed = {
        **bare,
        "endpoint": {
            "iq_bitwidth": 9,
            "compression_type": "STATIC",
            "num_prb": 24,
            "frame_structure": 145,
            "dl_port_id": [0, 1, 2, 3],
            "ul_port_id": [0, 1],
            "prach_port_id": [4, 5],
            "tx_endpoint_prefix": "LowLevelTxEndpoint",
            "rx_endpoint_prefix": "LowLevelRxEndpoint",
            "prach_endpoint_prefix": "LowLevelRxPrachEndpoint",
            "endpoint_index_base": 0,
        },
    }
    controller.set_full_config(prefixed)
    edits = dict(sent)
    assert ">LowLevelTxEndpoint0<" in edits["ORAN Uplane Tx endpoints elements"]
    assert ">LowLevelRxPrachEndpoint1<" in edits["ORAN Uplane Rx endpoints elements"]
    assert ">LowLevelRxEndpoint1<" in edits["ORAN Uplane low level Rx links"]


@mark.timeout(60)
def test_malformed_declarations_rejected(o1_adapter_src):
    """Malformed explicit lists raise instead of rendering broken edits."""
    from ofh_config_builder import normalize_endpoint_entries

    _ = o1_adapter_src
    assert normalize_endpoint_entries([{"name": "PORT-00", "eaxc_id": "7"}], "endpoint.tx_endpoints") == [
        {"name": "PORT-00", "eaxc_id": 7}
    ], "eaxc ids coerced to int"
    for bad in ([], "not a list", [{"name": "X"}], [{"eaxc_id": 1}], [{"name": "X", "eaxc_id": "nope"}], [7]):
        with raises(ValueError):
            normalize_endpoint_entries(bad, "endpoint.tx_endpoints")


@mark.timeout(60)
def test_heuristic_misclassifies_type_blind_names(o1_adapter_src):
    """BASELINE (the reported defect): without declarations the \"rach\"
    heuristic finds nothing in type-blind names, so the PRACH eAxC ids are
    folded into ul_port_id, nof_antennas_ul over-counts, and no
    prach_port_id is emitted — an actively wrong derived DU config."""
    from ofh_config_builder import build_ofh_config

    _ = o1_adapter_src
    cell, cell_cfg = build_ofh_config(PORT_UPLANE, {}, {})
    assert cell["ul_port_id"] == [0, 1, 4, 5], "PRACH ids folded into UL"
    assert "prach_port_id" not in cell
    assert cell_cfg["nof_antennas_ul"] == 4, "UL antenna count inflated by the PRACH endpoints"


@mark.timeout(60)
def test_declared_names_classify_rx_endpoints(o1_adapter_src):
    """Declared PRACH names split the rx endpoints correctly: real UL ids and
    counts, PRACH ids in prach_port_id."""
    from ofh_config_builder import build_ofh_config

    _ = o1_adapter_src
    cell, cell_cfg = build_ofh_config(PORT_UPLANE, {}, {}, endpoint_config=PORT_ENDPOINT_CONFIG)
    assert cell["ul_port_id"] == [0, 1]
    assert cell["prach_port_id"] == [4, 5]
    assert cell_cfg["nof_antennas_ul"] == 2


@mark.timeout(60)
def test_declared_rx_names_protect_lookalikes(o1_adapter_src):
    """A declared UL name is never reclassified by the substring heuristic,
    even when it happens to contain \"rach\"."""
    from ofh_config_builder import build_ofh_config

    _ = o1_adapter_src
    uplane = {
        "low-level-tx-endpoints": [],
        "low-level-rx-endpoints": [
            {"name": "brachiosaur", "e-axcid": {"eaxc-id": "0"}, "compression": {}},
            {"name": "PORT-25", "e-axcid": {"eaxc-id": "4"}, "compression": {}},
        ],
    }
    declarations = {
        "rx_endpoints": [{"name": "brachiosaur", "eaxc_id": 0}],
        "prach_endpoints": [{"name": "PORT-25", "eaxc_id": 4}],
    }
    cell, _unused = build_ofh_config(uplane, {}, {}, endpoint_config=declarations)
    assert cell["ul_port_id"] == [0]
    assert cell["prach_port_id"] == [4]


def _link(kind, index, carrier, endpoint):
    """One low-level-tx/rx-links entry as the user-plane-configuration readback carries it."""
    return {
        "name": f"Low-Level-{kind.title()}-Links-{index:03d}",
        f"{kind}-array-carrier": carrier,
        f"low-level-{kind}-endpoint": endpoint,
    }


@mark.timeout(60)
def test_unlinked_endpoints_do_not_count_once_links_exist(o1_adapter_src):
    """Without declarations the endpoints that count are the ones the O-RU's
    low-level links reference: an extra static endpoint no carrier is wired
    to (a monitoring or spare port) must not inflate the DU port lists. On an
    O-RU without links every endpoint counts, and a declared name counts even
    when unlinked."""
    from ofh_config_builder import build_ofh_config

    _ = o1_adapter_src
    uplane = {
        "low-level-tx-endpoints": [
            {"name": "PORT-00", "e-axcid": {"eaxc-id": "0"}, "compression": {}},
            {"name": "PORT-10", "e-axcid": {"eaxc-id": "1"}, "compression": {}},
            {"name": "aux_tx_mon", "e-axcid": {"eaxc-id": "2"}, "compression": {}},
        ],
        "low-level-rx-endpoints": [
            {"name": "PORT-05", "e-axcid": {"eaxc-id": "0"}, "compression": {}},
            {"name": "sep_prach1", "e-axcid": {"eaxc-id": "4"}, "compression": {}},
            {"name": "aux_rx_mon", "e-axcid": {"eaxc-id": "3"}, "compression": {}},
        ],
    }
    fresh_cell, fresh_cfg = build_ofh_config(uplane, {}, {})
    assert fresh_cell["dl_port_id"] == [0, 1, 2], "no links yet: every endpoint counts"
    assert fresh_cell["ul_port_id"] == [0, 3]
    assert fresh_cfg["nof_antennas_dl"] == 3

    linked = {
        **uplane,
        "low-level-tx-links": [
            _link("tx", 0, "Tx-Array-Carrier-00", "PORT-00"),
            _link("tx", 1, "Tx-Array-Carrier-01", "PORT-10"),
        ],
        "low-level-rx-links": [
            _link("rx", 0, "Rx-Array-Carrier-00", "PORT-05"),
            _link("rx", 1, "Rx-Array-Carrier-01", "sep_prach1"),
        ],
    }
    cell, cell_cfg = build_ofh_config(linked, {}, {})
    assert cell["dl_port_id"] == [0, 1] and cell_cfg["nof_antennas_dl"] == 2
    assert cell["ul_port_id"] == [0] and cell_cfg["nof_antennas_ul"] == 1
    assert cell["prach_port_id"] == [4]

    declared, _unused = build_ofh_config(
        linked, {}, {}, endpoint_config={"tx_endpoints": [{"name": "aux_tx_mon", "eaxc_id": 2}]}
    )
    assert declared["dl_port_id"] == [0, 1, 2], "declared names keep precedence over the link rule"


@mark.timeout(60)
def test_prefix_and_heuristic_classification_still_work(o1_adapter_src):
    """Regression: the configured PRACH prefix classifies without explicit
    lists, and the bare heuristic still handles *RxPrachEndpoint*-style and
    legacy generated names."""
    from ofh_config_builder import build_ofh_config

    _ = o1_adapter_src
    uplane = {
        "low-level-tx-endpoints": [],
        "low-level-rx-endpoints": [
            {"name": "LowLevelRxEndpoint0", "e-axcid": {"eaxc-id": "0"}, "compression": {}},
            {"name": "LowLevelRxPrachEndpoint0", "e-axcid": {"eaxc-id": "4"}, "compression": {}},
        ],
    }
    with_prefix, _unused = build_ofh_config(
        uplane, {}, {}, endpoint_config={"prach_endpoint_prefix": "LowLevelRxPrachEndpoint"}
    )
    bare, _unused = build_ofh_config(uplane, {}, {})
    for cell in (with_prefix, bare):
        assert cell["ul_port_id"] == [0]
        assert cell["prach_port_id"] == [4]


@mark.timeout(120)
def test_declared_endpoint_names_roundtrip(ru_config):
    """Push the PORT-shaped layout to the (mock) O-RU by declared names and
    read it back: the datastore carries the exact names, the links reference
    them, and the DU-config derivation splits UL/PRACH via the declarations."""
    from ofh_config_builder import build_ofh_config

    ru_config.set_full_config(dict(PORT_LAYOUT), skip_activation=True)
    try:
        uplane = ru_config.get_uplane_config().get("user-plane-configuration", {})

        def by_name(nodes):
            nodes = nodes if isinstance(nodes, list) else [nodes] if nodes else []
            return {entry["name"]: entry for entry in nodes}

        tx_endpoints = by_name(uplane.get("low-level-tx-endpoints"))
        assert tx_endpoints["PORT-00"]["e-axcid"]["eaxc-id"] == "0"
        assert tx_endpoints["PORT-10"]["e-axcid"]["eaxc-id"] == "1"
        rx_endpoints = by_name(uplane.get("low-level-rx-endpoints"))
        assert rx_endpoints["PORT-05"]["e-axcid"]["eaxc-id"] == "0"
        assert rx_endpoints["PORT-25"]["e-axcid"]["eaxc-id"] == "4"
        assert rx_endpoints["PORT-25"]["frame-structure"] == "129"

        links = by_name(uplane.get("low-level-rx-links"))
        linked = {entry["low-level-rx-endpoint"]: entry["rx-array-carrier"] for entry in links.values()}
        assert linked["PORT-05"] == "Rx-Array-Carrier-00"
        assert linked["PORT-25"] == "Rx-Array-Carrier-01", "crossed PRACH pairing survives the datastore"

        # the mock's keyed lists merge across suite runs (entries from other
        # modules stay put), so derive the DU config from this layout's
        # endpoints only and assert by name, never by count
        port_only = {
            **uplane,
            "low-level-tx-endpoints": [
                entry for entry in tx_endpoints.values() if entry["name"].startswith("PORT-")
            ],
            "low-level-rx-endpoints": [
                entry for entry in rx_endpoints.values() if entry["name"].startswith("PORT-")
            ],
        }
        cell, cell_cfg = build_ofh_config(port_only, {}, {}, endpoint_config=PORT_ENDPOINT_CONFIG)
        assert cell["prach_port_id"] == [4, 5]
        assert cell["ul_port_id"] == [0, 1]
        assert cell_cfg["nof_antennas_ul"] == 2
    finally:
        # restore the default 4x4 link layout for whatever runs next (keyed
        # link names are shared across the suite)
        ru_config.set_oran_uplane_low_level_tx_links()
        ru_config.set_oran_uplane_low_level_rx_links()
