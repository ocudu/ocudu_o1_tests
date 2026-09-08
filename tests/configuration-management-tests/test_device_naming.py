# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Device-naming parametrization tests (adapter endpoint naming + TDD gating).

Real O-RUs commonly ship fixed static low-level endpoints that may only be
modified by their exact names, and some firmware manages TDD device-side
and misbehaves on the M-plane TDD writes. These suites pin the engine's
answer to both: endpoint name prefixes/index base are endpoint-config
parameters flowing through endpoints and links (defaults preserve the
legacy generated names), the DU derivation classifies endpoints without
assuming any vendor's names, and the TDD provisioning steps are
individually declinable via the provision config.
"""

import logging

from pytest import mark

logger = logging.getLogger(__name__)

VENDOR_NAMING = {
    "tx_endpoint_prefix": "LowLevelTxEndpoint",
    "rx_endpoint_prefix": "LowLevelRxEndpoint",
    "prach_endpoint_prefix": "LowLevelRxPrachEndpoint",
    "endpoint_index_base": 0,
}


def _capturing_ru(o1_adapter_src):
    """A dry-run RuConfig whose rendered template inputs are captured."""
    from ru_config import RuConfig

    ru = RuConfig(None, "running")
    captured = {}

    def capture(_template, description, config_data=None, **kwargs):
        captured[description] = config_data if config_data is not None else kwargs

    ru._set_config_from_template = capture  # pylint: disable=protected-access
    return ru, captured


@mark.timeout(60)
def test_default_naming_preserves_legacy_names(o1_adapter_src):
    """Without naming keys the generated names are the legacy sep_* set."""
    ru, captured = _capturing_ru(o1_adapter_src)
    cfg = {"num_prb": 273, "frame_structure": 193}
    ru.set_oran_uplane_tx_endpoints(cfg)
    ru.set_oran_uplane_rx_endpoints(cfg)
    tx = [e["name"] for e in captured["ORAN Uplane Tx endpoints elements"]["endpoints"]]
    rx = [e["name"] for e in captured["ORAN Uplane Rx endpoints elements"]["endpoints"]]
    assert tx == ["sep_txch1", "sep_txch2", "sep_txch3", "sep_txch4"]
    assert rx[:4] == ["sep_rxch1", "sep_rxch2", "sep_rxch3", "sep_rxch4"]
    assert rx[4:] == ["sep_prach1", "sep_prach2", "sep_prach3", "sep_prach4"]


@mark.timeout(60)
def test_vendor_naming_flows_through_endpoints_and_links(o1_adapter_src):
    """Vendor prefixes + 0-based index land in endpoints AND the links that
    reference them, including the crossed PRACH pairing."""
    from ofh_config_builder import endpoint_naming

    ru, captured = _capturing_ru(o1_adapter_src)
    cfg = dict(VENDOR_NAMING, num_prb=273, frame_structure=193,
               dl_port_id=[0, 1, 2, 3], ul_port_id=[0, 1], prach_port_id=[4, 5])
    ru.set_oran_uplane_tx_endpoints(cfg)
    ru.set_oran_uplane_rx_endpoints(cfg)
    naming = endpoint_naming(cfg)
    ru.set_oran_uplane_low_level_tx_links(cfg["dl_port_id"], naming=naming)
    ru.set_oran_uplane_low_level_rx_links(cfg["ul_port_id"], cfg["prach_port_id"], naming=naming)

    tx = [e["name"] for e in captured["ORAN Uplane Tx endpoints elements"]["endpoints"]]
    rx = [e["name"] for e in captured["ORAN Uplane Rx endpoints elements"]["endpoints"]]
    assert tx == [f"LowLevelTxEndpoint{i}" for i in range(4)]
    assert rx == ["LowLevelRxEndpoint0", "LowLevelRxEndpoint1",
                  "LowLevelRxPrachEndpoint0", "LowLevelRxPrachEndpoint1"]
    # eAxC ids ride inside the entries, independent of the name index
    assert [e["eaxc_id"] for e in captured["ORAN Uplane Rx endpoints elements"]["endpoints"]] == [0, 1, 4, 5]

    tx_links = captured["ORAN Uplane low level Tx links"]["links"]
    rx_links = captured["ORAN Uplane low level Rx links"]["links"]
    assert [l["endpoint"] for l in tx_links] == [f"LowLevelTxEndpoint{i}" for i in range(4)]
    assert [l["endpoint"] for l in rx_links] == ["LowLevelRxEndpoint0", "LowLevelRxEndpoint1",
                                                 "LowLevelRxPrachEndpoint0", "LowLevelRxPrachEndpoint1"]
    # crossed PRACH pairing: prach i binds the partner carrier (i^1)
    assert [l["carrier"] for l in rx_links[2:]] == ["Rx-Array-Carrier-01", "Rx-Array-Carrier-00"]


@mark.timeout(60)
def test_builder_classifies_endpoints_without_name_assumptions(o1_adapter_src):
    """tx/rx split is structural; PRACH is told apart by name substring —
    covering legacy generated names and fixed vendor names alike."""
    from ofh_config_builder import build_ofh_config

    def entry(name, eaxc):
        return {"name": name, "e-axcid": {"eaxc-id": str(eaxc)},
                "compression": {"compression-type": "STATIC", "iq-bitwidth": "9"}}

    for tx_name, rx_name, prach_name in (
        ("sep_txch1", "sep_rxch1", "sep_prach1"),
        ("LowLevelTxEndpoint0", "LowLevelRxEndpoint0", "LowLevelRxPrachEndpoint0"),
    ):
        uplane = {
            "low-level-tx-endpoints": [entry(tx_name, 0)],
            "low-level-rx-endpoints": [entry(rx_name, 1), entry(prach_name, 4)],
        }
        ofh_cfg, _cell_cfg = build_ofh_config(uplane, {}, {})
        assert ofh_cfg.get("dl_port_id") == [0], (tx_name, ofh_cfg)
        assert ofh_cfg.get("ul_port_id") == [1], (rx_name, ofh_cfg)
        assert ofh_cfg.get("prach_port_id") == [4], (prach_name, ofh_cfg)


@mark.timeout(60)
def test_tdd_steps_follow_provision_config(o1_adapter_src):
    """tdd.pattern_upload / tdd.carrier_binding individually gate the TDD
    writes; both default to true (spec behavior)."""
    from ru_config import RuConfig

    calls = []
    ru = RuConfig(None, "running")
    ru._set_config_from_template = lambda *_a, **_k: None  # pylint: disable=protected-access
    ru.set_oran_uplane_tdd_pattern = lambda _config: calls.append("upload")
    ru.bind_tdd_pattern_to_carriers = lambda **_k: calls.append("bind")

    base = {
        "interface": {"ru_mac_addr": "aa:bb:cc:dd:ee:ff", "vlan": 5},
        "processing": {"ru_mac_addr": "aa:bb:cc:dd:ee:ff", "du_mac_addr": "00:11:22:33:44:55", "vlan": 5},
        "endpoint": {"num_prb": 273, "frame_structure": 193},
        "carrier": {"dl_arfcn": 1, "dl_freq": 1, "ul_arfcn": 1, "ul_freq": 1,
                    "tx_gain": 0, "rf_bandwidth_hz": 100000000},
        "activation": {"state": "INACTIVE"},
    }

    ru.set_full_config(dict(base), skip_activation=True)
    assert calls == ["upload", "bind"], calls

    calls.clear()
    ru.set_full_config(dict(base, tdd={"carrier_binding": False}), skip_activation=True)
    assert calls == ["upload"], calls

    calls.clear()
    ru.set_full_config(dict(base, tdd={"pattern_upload": False, "carrier_binding": False}), skip_activation=True)
    assert calls == [], calls


@mark.timeout(120)
def test_vendor_named_full_config_roundtrip(o1_adapter_src, mock_ru_ssh_manager, ru_config):
    """Sim integration: a full config with vendor-style names lands in the
    datastore — base interface, endpoint names, and links referencing them."""
    config = {
        "interface": {"ru_mac_addr": "aa:bb:cc:dd:ee:01", "vlan": 41, "base_interface": "eth0"},
        "processing": {"ru_mac_addr": "aa:bb:cc:dd:ee:01", "du_mac_addr": "00:11:22:33:44:55", "vlan": 41},
        "endpoint": dict(VENDOR_NAMING, iq_bitwidth=9, compression_type="STATIC",
                         num_prb=273, frame_structure=193,
                         dl_port_id=[0, 1], ul_port_id=[0, 1], prach_port_id=[4, 5]),
        "carrier": {"dl_arfcn": 676334, "dl_freq": 4145010000, "ul_arfcn": 676334,
                    "ul_freq": 4145010000, "tx_gain": 20, "rf_bandwidth_hz": 100000000},
        "activation": {"state": "INACTIVE"},
        "tdd": {"carrier_binding": False},
    }
    ru_config.set_full_config(config, skip_activation=True)

    reply = mock_ru_ssh_manager.get_config(source="running").data_xml
    assert "LowLevelTxEndpoint0" in reply
    assert "LowLevelRxPrachEndpoint1" in reply
    assert "eth0" in reply
