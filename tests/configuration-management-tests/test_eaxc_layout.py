# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Fronthaul eAxC/carrier layout tests for the M-plane client.

The endpoint/carrier/link/activation templates are parametrized by eAxC port
lists (defaults preserve the legacy 4x4 layout). These tests push a 4x2
O-RU layout — dl [0-3] / ul [0,1] / prach [4,5] — end to end
via set_full_config and assert the named datastore entries, including the
deliberate crossed PRACH-to-carrier pairing. Assertions are by entry name
(keyed lists merge, so counts are not meaningful across suite runs). This
module runs after test_ru_controller.py in run_tests.py, which seeds the 4x4
array-carrier layout the default link setters reference.
"""

import logging

from pytest import mark

logger = logging.getLogger(__name__)

FOUR_BY_TWO_LAYOUT = {
    "interface": {"ru_mac_addr": "00:a0:0a:01:a4:42", "vlan": 5},
    "processing": {"ru_mac_addr": "00:a0:0a:01:a4:42", "du_mac_addr": "9c:69:b4:66:cd:48", "vlan": 5},
    "endpoint": {
        "iq_bitwidth": 9,
        "compression_type": "STATIC",
        "num_prb": 273,
        "frame_structure": 193,
        "dl_port_id": [0, 1, 2, 3],
        "ul_port_id": [0, 1],
        "prach_port_id": [4, 5],
    },
    "carrier": {
        "dl_arfcn": 637212,
        "dl_freq": 3558180000,
        "ul_arfcn": 637212,
        "ul_freq": 3558180000,
        "tx_gain": 39,
        "rf_bandwidth_hz": 100000000,
    },
    "activation": {"state": "ACTIVE"},
}


def _as_list(node):
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


def _by_name(nodes):
    return {entry["name"]: entry for entry in _as_list(nodes)}


@mark.timeout(120)
def test_4x2_eaxc_layout_roundtrip(ru_config):
    """A 4x2 port layout lands in the RU datastore verbatim."""
    ru_config.set_full_config(FOUR_BY_TWO_LAYOUT)
    uplane = ru_config.get_uplane_config().get("user-plane-configuration", {})

    tx_endpoints = _by_name(uplane.get("low-level-tx-endpoints"))
    assert [tx_endpoints[f"sep_txch{i + 1}"]["e-axcid"]["eaxc-id"] for i in range(4)] == ["0", "1", "2", "3"]

    rx_endpoints = _by_name(uplane.get("low-level-rx-endpoints"))
    assert rx_endpoints["sep_rxch1"]["e-axcid"]["eaxc-id"] == "0"
    assert rx_endpoints["sep_rxch2"]["e-axcid"]["eaxc-id"] == "1"
    assert rx_endpoints["sep_prach1"]["e-axcid"]["eaxc-id"] == "4"
    assert rx_endpoints["sep_prach2"]["e-axcid"]["eaxc-id"] == "5"
    # PRACH endpoints keep their dedicated frame parameters
    assert rx_endpoints["sep_prach1"]["frame-structure"] == "129"
    assert rx_endpoints["sep_prach1"]["number-of-prb-per-scs"]["number-of-prb"] == "12"
    assert rx_endpoints["sep_rxch1"]["frame-structure"] == "193"

    links = _by_name(uplane.get("low-level-rx-links"))
    assert links["Low-Level-Rx-Links-000"]["low-level-rx-endpoint"] == "sep_rxch1"
    assert links["Low-Level-Rx-Links-000"]["rx-array-carrier"] == "Rx-Array-Carrier-00"
    # the deliberate crossed PRACH pairing: prach1 -> carrier 01, prach2 -> carrier 00
    assert links["Low-Level-Rx-Links-002"]["low-level-rx-endpoint"] == "sep_prach1"
    assert links["Low-Level-Rx-Links-002"]["rx-array-carrier"] == "Rx-Array-Carrier-01"
    assert links["Low-Level-Rx-Links-003"]["low-level-rx-endpoint"] == "sep_prach2"
    assert links["Low-Level-Rx-Links-003"]["rx-array-carrier"] == "Rx-Array-Carrier-00"

    # carriers carry the technology type, the FR1 TDD n-ta-offset default,
    # and the configurable-tdd-pattern binding written before activation
    tx_carriers = _by_name(uplane.get("tx-array-carriers"))
    rx_carriers = _by_name(uplane.get("rx-array-carriers"))
    for name in ("Tx-Array-Carrier-00", "Tx-Array-Carrier-01", "Tx-Array-Carrier-02", "Tx-Array-Carrier-03"):
        assert tx_carriers[name]["type"] == "NR"
        assert tx_carriers[name]["configurable-tdd-pattern"] == "1"
    for name in ("Rx-Array-Carrier-00", "Rx-Array-Carrier-01"):
        assert rx_carriers[name]["type"] == "NR"
        assert rx_carriers[name]["n-ta-offset"] == "25600"
        assert rx_carriers[name]["configurable-tdd-pattern"] == "1"


@mark.timeout(60)
def test_prach_frame_parameters_overridable(ru_config):
    """Caller-supplied PRACH frame-structure/PRB override the defaults."""
    endpoint = dict(FOUR_BY_TWO_LAYOUT["endpoint"])
    endpoint.update({"prach_frame_structure": 145, "prach_num_prb": 24})
    ru_config.set_oran_uplane_rx_endpoints(endpoint)

    uplane = ru_config.get_uplane_config().get("user-plane-configuration", {})
    rx_endpoints = _by_name(uplane.get("low-level-rx-endpoints"))
    assert rx_endpoints["sep_prach1"]["frame-structure"] == "145"
    assert rx_endpoints["sep_prach1"]["number-of-prb-per-scs"]["number-of-prb"] == "24"

    # restore the defaults for whatever runs next
    ru_config.set_oran_uplane_rx_endpoints(dict(FOUR_BY_TWO_LAYOUT["endpoint"]))
    uplane = ru_config.get_uplane_config().get("user-plane-configuration", {})
    assert _by_name(uplane.get("low-level-rx-endpoints"))["sep_prach1"]["frame-structure"] == "129"


@mark.timeout(60)
def test_default_layout_preserved(ru_config):
    """Calling the setters without port lists reproduces the legacy 4x4 layout."""
    ru_config.set_oran_uplane_tx_endpoints(
        {"iq_bitwidth": 9, "compression_type": "STATIC", "num_prb": 273, "frame_structure": 193}
    )
    ru_config.set_oran_uplane_rx_endpoints(
        {"iq_bitwidth": 9, "compression_type": "STATIC", "num_prb": 273, "frame_structure": 193}
    )
    # the legacy links reference four carriers per direction; an earlier
    # suite may have left the mock with fewer, so write the legacy set first
    ru_config.set_oran_uplane_tx_array_carriers(dict(FOUR_BY_TWO_LAYOUT["carrier"]))
    ru_config.set_oran_uplane_rx_array_carriers(dict(FOUR_BY_TWO_LAYOUT["carrier"]))
    ru_config.set_oran_uplane_low_level_rx_links()

    uplane = ru_config.get_uplane_config().get("user-plane-configuration", {})
    rx_endpoints = _by_name(uplane.get("low-level-rx-endpoints"))
    assert [rx_endpoints[f"sep_rxch{i + 1}"]["e-axcid"]["eaxc-id"] for i in range(4)] == ["0", "1", "2", "3"]
    assert [rx_endpoints[f"sep_prach{i + 1}"]["e-axcid"]["eaxc-id"] for i in range(4)] == ["6", "7", "8", "9"]

    links = _by_name(uplane.get("low-level-rx-links"))
    prach_carriers = {
        links[name]["low-level-rx-endpoint"]: links[name]["rx-array-carrier"]
        for name in links
        if links[name]["low-level-rx-endpoint"].startswith("sep_prach")
    }
    assert prach_carriers == {
        "sep_prach1": "Rx-Array-Carrier-01",
        "sep_prach2": "Rx-Array-Carrier-00",
        "sep_prach3": "Rx-Array-Carrier-03",
        "sep_prach4": "Rx-Array-Carrier-02",
    }


@mark.timeout(60)
def test_prach_links_never_dangle(ru_config):
    """More PRACH ports than rx carriers must clamp into the existing carrier
    range — a dangling rx-array-carrier leafref would be rejected by a
    validating O-RU. The sim validates leafrefs, so the edit being accepted
    plus the readback proves it."""
    ru_config.set_oran_uplane_low_level_rx_links([0, 1], [4, 5, 6, 7])

    uplane = ru_config.get_uplane_config().get("user-plane-configuration", {})
    links = _by_name(uplane.get("low-level-rx-links"))
    valid_carriers = {"Rx-Array-Carrier-00", "Rx-Array-Carrier-01"}
    for name in ("Low-Level-Rx-Links-002", "Low-Level-Rx-Links-003", "Low-Level-Rx-Links-004", "Low-Level-Rx-Links-005"):
        assert links[name]["low-level-rx-endpoint"].startswith("sep_prach")
        assert links[name]["rx-array-carrier"] in valid_carriers

    # restore the default 4x4 link layout
    ru_config.set_oran_uplane_low_level_rx_links()
