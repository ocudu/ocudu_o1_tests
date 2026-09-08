# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Direct M-plane command/response tests for the adapter's RU controller.

Unlike the adapter-integration suites (SMO -> DU netconf -> adapter), these
drive the M-plane NETCONF client (``ru_config.RuConfig``) straight against
the mock RU (``ocudu_netconf --config ru``) and assert on the RU's running
datastore: command in -> validated state out. sysrepo/libyang enforce the
O-RAN WG4 schemas, so every accepted edit is schema-proven.

RU parameter profiles live in ``tests/configs/ru/*.yaml``. Their ``mplane``
block holds what the client can push today; the ``expected_du`` block records
the matching DU-side ``ru_ofh`` values (timing windows, eAxC layout) that a
future delay-management/capability-driven client should derive.
"""

import logging
from pathlib import Path

import yaml
from ncclient.operations import RPCError
from pytest import mark, raises

logger = logging.getLogger(__name__)

PROFILE_DIR = Path(__file__).parent.parent / "configs" / "ru"
PROFILE_PATHS = sorted(PROFILE_DIR.glob("*.yaml"))


def _load_mplane_profile(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)["mplane"]


def _as_list(node) -> list:
    """xmltodict yields a dict for single elements and a list otherwise."""
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


def _by_name(nodes) -> dict:
    return {entry["name"]: entry for entry in _as_list(nodes)}


def _first_cell(ofh_config):
    """build_ofh_config may return the cell dict directly or a ru_ofh wrapper."""
    if isinstance(ofh_config, dict) and "cells" in ofh_config:
        return ofh_config["cells"][0]
    return ofh_config


def _endpoint_config(profile: dict) -> dict:
    return {key: profile[key] for key in ("iq_bitwidth", "compression_type", "num_prb", "frame_structure")}


def _carrier_config(profile: dict) -> dict:
    return {
        key: profile[key]
        for key in ("dl_arfcn", "dl_freq", "ul_arfcn", "ul_freq", "tx_gain", "rf_bandwidth_hz")
    }


def _get_uplane(ru_config) -> dict:
    return ru_config.get_uplane_config().get("user-plane-configuration") or {}


PROFILE = _load_mplane_profile(PROFILE_DIR / "n78_4x2_reference_timing.yaml")


@mark.timeout(60)
def test_ru_baseline_seed(ru_config):
    logger.info("The mock RU exposes the seeded o-ran-uplane-conf configurable TDD pattern.")
    patterns = _as_list(_get_uplane(ru_config).get("configurable-tdd-patterns"))
    assert patterns, "seeded configurable-tdd-patterns missing from the RU running config"
    assert patterns[0]["tdd-pattern-id"] == "1"
    assert len(_as_list(patterns[0]["switching-points"])) == 6


@mark.timeout(60)
def test_interfaces_roundtrip(ru_config):
    logger.info("edit-config ietf-interfaces; RU running config reflects fronthaul VLAN + MAC.")
    ru_config.set_ietf_interfaces({"ru_mac_addr": PROFILE["ru_mac_addr"], "vlan": PROFILE["vlan"]})

    interfaces = _by_name((ru_config.get_ietf_interfaces().get("interfaces") or {}).get("interface"))
    assert "fronthaul1" in interfaces
    vlan_interface = interfaces[f"uc-vlan{PROFILE['vlan']}"]
    assert vlan_interface["vlan-id"] == str(PROFILE["vlan"])
    assert vlan_interface["mac-address"].lower() == PROFILE["ru_mac_addr"].lower()
    assert vlan_interface["base-interface"] == "fronthaul1"


@mark.timeout(60)
def test_processing_elements_roundtrip(ru_config):
    logger.info("edit-config o-ran-processing-element; eth-flow carries RU/DU MAC + VLAN.")
    ru_config.set_oran_processing_elements(
        {
            "ru_mac_addr": PROFILE["ru_mac_addr"],
            "du_mac_addr": PROFILE["du_mac_addr"],
            "vlan": PROFILE["vlan"],
        }
    )

    processing = ru_config.get_processing_elements().get("processing-elements") or {}
    assert processing["transport-session-type"] == "ETH-INTERFACE"
    element = _by_name(processing.get("ru-elements"))["processing-element01"]
    eth_flow = element["transport-flow"]["eth-flow"]
    assert eth_flow["ru-mac-address"].lower() == PROFILE["ru_mac_addr"].lower()
    assert eth_flow["o-du-mac-address"].lower() == PROFILE["du_mac_addr"].lower()
    assert eth_flow["vlan-id"] == str(PROFILE["vlan"])
    assert element["transport-flow"]["interface-name"] == f"uc-vlan{PROFILE['vlan']}"


@mark.timeout(120)
def test_uplane_endpoints_and_carriers_roundtrip(ru_config):
    logger.info("edit-config o-ran-uplane-conf endpoints/carriers/links; datastore reflects RF + compression.")
    ru_config.set_oran_uplane_tx_endpoints(_endpoint_config(PROFILE))
    ru_config.set_oran_uplane_rx_endpoints(_endpoint_config(PROFILE))
    ru_config.set_oran_uplane_tx_array_carriers(_carrier_config(PROFILE))
    ru_config.set_oran_uplane_rx_array_carriers(_carrier_config(PROFILE))
    ru_config.set_oran_uplane_low_level_tx_links()
    ru_config.set_oran_uplane_low_level_rx_links()

    uplane = _get_uplane(ru_config)

    tx_endpoint = _by_name(uplane.get("low-level-tx-endpoints"))["sep_txch1"]
    assert tx_endpoint["compression"]["iq-bitwidth"] == str(PROFILE["iq_bitwidth"])
    assert tx_endpoint["compression"]["compression-type"] == PROFILE["compression_type"]
    assert tx_endpoint["number-of-prb-per-scs"]["number-of-prb"] == str(PROFILE["num_prb"])

    rx_endpoints = _as_list(uplane.get("low-level-rx-endpoints"))
    assert rx_endpoints, "no low-level-rx-endpoints written"
    assert rx_endpoints[0]["compression"]["iq-bitwidth"] == str(PROFILE["iq_bitwidth"])

    tx_carrier = _by_name(uplane.get("tx-array-carriers"))["Tx-Array-Carrier-00"]
    assert tx_carrier["absolute-frequency-center"] == str(PROFILE["dl_arfcn"])
    assert tx_carrier["center-of-channel-bandwidth"] == str(PROFILE["dl_freq"])
    assert tx_carrier["channel-bandwidth"] == str(PROFILE["rf_bandwidth_hz"])
    assert float(tx_carrier["gain"]) == float(PROFILE["tx_gain"])

    rx_carrier = _by_name(uplane.get("rx-array-carriers"))["Rx-Array-Carrier-00"]
    assert rx_carrier["absolute-frequency-center"] == str(PROFILE["ul_arfcn"])
    assert rx_carrier["center-of-channel-bandwidth"] == str(PROFILE["ul_freq"])

    tx_link = _by_name(uplane.get("low-level-tx-links"))["Low-Level-Tx-Links-000"]
    assert tx_link["processing-element"] == "processing-element01"
    assert tx_link["tx-array-carrier"] == "Tx-Array-Carrier-00"
    assert tx_link["low-level-tx-endpoint"] == "sep_txch1"


@mark.timeout(120)
def test_carrier_activation_toggle(ru_config):
    logger.info("edit-config carrier activation; every tx/rx array carrier follows the requested state.")
    ru_config.set_oran_uplane_tx_array_carriers(_carrier_config(PROFILE))
    ru_config.set_oran_uplane_rx_array_carriers(_carrier_config(PROFILE))

    for state in ("ACTIVE", "INACTIVE", "ACTIVE"):
        ru_config.set_oran_uplane_carrier_active({"state": state})
        uplane = _get_uplane(ru_config)
        carriers = _as_list(uplane.get("tx-array-carriers")) + _as_list(uplane.get("rx-array-carriers"))
        assert carriers, "no array carriers present after activation edit"
        assert {carrier["active"] for carrier in carriers} == {state}


@mark.timeout(60)
def test_invalid_vlan_rejected(ru_config):
    logger.info("libyang rejects an out-of-range VLAN id (uint16 overflow); edit-config fails atomically.")
    with raises(RPCError):
        ru_config.set_ietf_interfaces({"ru_mac_addr": PROFILE["ru_mac_addr"], "vlan": 70000})


@mark.timeout(60)
def test_invalid_carrier_state_rejected(ru_config):
    logger.info("libyang rejects an unknown carrier activation enum value.")
    with raises(RPCError):
        ru_config.set_oran_uplane_carrier_active({"state": "POWERED_MAYBE"})


@mark.timeout(180)
@mark.parametrize("profile_path", PROFILE_PATHS, ids=lambda path: path.stem)
def test_full_config_generates_du_ofh(ru_config, o1_adapter_src, profile_path):
    """Provision the RU end-to-end over M-plane, then regenerate the DU-side
    ru_ofh config from the RU's own datastore (ofh_config_builder)."""
    profile = _load_mplane_profile(profile_path)
    logger.info("Full M-plane provisioning + DU config synthesis for profile %s.", profile_path.stem)

    ru_config.set_full_config(
        {
            "interface": {"ru_mac_addr": profile["ru_mac_addr"], "vlan": profile["vlan"]},
            "processing": {
                "ru_mac_addr": profile["ru_mac_addr"],
                "du_mac_addr": profile["du_mac_addr"],
                "vlan": profile["vlan"],
            },
            "endpoint": _endpoint_config(profile),
            "carrier": _carrier_config(profile),
            "activation": {"state": "ACTIVE"},
        }
    )

    from ofh_config_builder import build_ofh_config

    ofh_config, cell_cfg = build_ofh_config(
        _get_uplane(ru_config),
        ru_config.get_processing_elements().get("processing-elements") or {},
        ru_config.get_ietf_interfaces().get("interfaces") or {},
    )
    assert ofh_config, "ofh_config_builder produced no ru_ofh cell from the RU datastore"
    cell = _first_cell(ofh_config)

    assert cell["network_interface"] == f"uc-vlan{profile['vlan']}"
    assert cell["ru_mac_addr"].lower() == profile["ru_mac_addr"].lower()
    assert cell["du_mac_addr"].lower() == profile["du_mac_addr"].lower()
    assert int(cell["vlan_tag_cp"]) == int(profile["vlan"])
    assert int(cell["vlan_tag_up"]) == int(profile["vlan"])
    # STATIC (o-ran compression-type) normalizes to OCUDU's bfp compression method.
    assert cell["compr_method_dl"] == "bfp"
    assert cell["compr_method_ul"] == "bfp"
    assert int(cell["compr_bitwidth_dl"]) == int(profile["iq_bitwidth"])
    assert int(cell["compr_bitwidth_ul"]) == int(profile["iq_bitwidth"])

    assert int(cell_cfg["dl_arfcn"]) == int(profile["dl_arfcn"])
    assert int(round(float(cell_cfg["channel_bandwidth_MHz"]))) == int(profile["rf_bandwidth_hz"]) // 1_000_000


@mark.timeout(60)
def test_carrier_spec_leaves(ru_config):
    logger.info("Carriers carry the o-ran-uplane-conf type and n-ta-offset leaves.")
    carrier_config = _carrier_config(PROFILE)
    ru_config.set_oran_uplane_tx_array_carriers(carrier_config)
    ru_config.set_oran_uplane_rx_array_carriers(carrier_config)

    uplane = _get_uplane(ru_config)
    tx_carrier = _by_name(uplane.get("tx-array-carriers"))["Tx-Array-Carrier-00"]
    rx_carrier = _by_name(uplane.get("rx-array-carriers"))["Rx-Array-Carrier-00"]
    assert tx_carrier["type"] == "NR"
    assert rx_carrier["type"] == "NR"
    # TS 38.133 table 7.1.2-2: n-TimingAdvanceOffset defaults to 25600 Tc for FR1 TDD
    assert rx_carrier["n-ta-offset"] == "25600"

    # explicit override wins (39936 is the other FR1 value); re-setting
    # without an override restores the TDD default
    ru_config.set_oran_uplane_rx_array_carriers({**carrier_config, "n_ta_offset": 39936})
    assert _by_name(_get_uplane(ru_config).get("rx-array-carriers"))["Rx-Array-Carrier-00"]["n-ta-offset"] == "39936"
    ru_config.set_oran_uplane_rx_array_carriers(carrier_config)
    assert _by_name(_get_uplane(ru_config).get("rx-array-carriers"))["Rx-Array-Carrier-00"]["n-ta-offset"] == "25600"


_PROBE_CARRIER = """<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
 <user-plane-configuration xmlns="urn:o-ran:uplane-conf:1.0">
  <tx-array-carriers>
   <name>wd-probe-tx</name>
   <type>NR</type>
   <absolute-frequency-center>640000</absolute-frequency-center>
   <center-of-channel-bandwidth>3600000000</center-of-channel-bandwidth>
   <channel-bandwidth>100000000</channel-bandwidth>
   <gain>27.0</gain>
   <downlink-radio-frame-offset>0</downlink-radio-frame-offset>
   <downlink-sfn-offset>0</downlink-sfn-offset>
  </tx-array-carriers>
 </user-plane-configuration></config>"""

_DELETE_PROBE_CARRIER = """<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
 <user-plane-configuration xmlns="urn:o-ran:uplane-conf:1.0">
  <tx-array-carriers xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0" nc:operation="delete">
   <name>wd-probe-tx</name></tx-array-carriers></user-plane-configuration></config>"""


@mark.timeout(60)
def test_get_reports_default_valued_leaves(ru_config, mock_ru_ssh_manager):
    logger.info("RFC 6243: the client's gets request report-all so default leaves are visible.")
    import xmltodict

    try:
        mock_ru_ssh_manager.edit_config(target="running", config=_PROBE_CARRIER)

        # a plain get-config (basic-mode explicit) omits the default-valued
        # active leaf on the freshly created carrier...
        raw = mock_ru_ssh_manager.get_config(
            source="running",
            filter=("subtree", '<user-plane-configuration xmlns="urn:o-ran:uplane-conf:1.0"/>'),
        )
        parsed = xmltodict.parse(
            raw.xml, process_namespaces=True, namespaces={"urn:o-ran:uplane-conf:1.0": None, "urn:ietf:params:xml:ns:netconf:base:1.0": None}
        )
        uplane = (parsed.get("rpc-reply", {}).get("data") or {}).get("user-plane-configuration") or {}
        probe = _by_name(uplane.get("tx-array-carriers"))["wd-probe-tx"]
        assert "active" not in probe

        # ...while the client's readback reports it with its YANG default
        probe = _by_name(_get_uplane(ru_config).get("tx-array-carriers"))["wd-probe-tx"]
        assert probe["active"] == "INACTIVE"
    finally:
        mock_ru_ssh_manager.edit_config(target="running", config=_DELETE_PROBE_CARRIER)


_SYNC_LOCKED_REPLY = """<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
 <data><sync xmlns="urn:o-ran:sync:1.0"><sync-status>
  <sync-state>LOCKED</sync-state></sync-status></sync></data></rpc-reply>"""


class _LockedSyncManager:
    """Stub NETCONF manager whose operational get always reports LOCKED."""

    server_capabilities = ()

    def get(self, filter=None, with_defaults=None):  # noqa: A002 - ncclient's parameter name
        class _Reply:
            xml = _SYNC_LOCKED_REPLY

        return _Reply()


@mark.timeout(60)
def test_wait_for_sync_locked(ru_config, o1_adapter_src):
    logger.info("wait_for_sync_locked: times out on the sync-less sim, returns on LOCKED.")
    from ru_config import RuConfig

    # the sim exposes no sync-status at all -> not LOCKED, bounded wait
    assert ru_config.wait_for_sync_locked(timeout_s=2, poll_interval_s=1) is False

    locked = RuConfig(_LockedSyncManager(), "running")
    assert locked.wait_for_sync_locked(timeout_s=5, poll_interval_s=1) is True


_CARRIER_STATE_REPLY = """<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
 <data><user-plane-configuration xmlns="urn:o-ran:uplane-conf:1.0">
  <tx-array-carriers><name>Tx-Array-Carrier-00</name><state>READY</state></tx-array-carriers>
  <tx-array-carriers><name>Tx-Array-Carrier-01</name><state>BUSY</state></tx-array-carriers>
  <rx-array-carriers><name>Rx-Array-Carrier-00</name><state>DISABLED</state></rx-array-carriers>
 </user-plane-configuration></data></rpc-reply>"""


class _CarrierStateManager:
    """Stub NETCONF manager whose operational get reports carrier states."""

    server_capabilities = ()

    def get(self, filter=None, with_defaults=None):  # noqa: A002 - ncclient's parameter name
        class _Reply:
            xml = _CARRIER_STATE_REPLY

        return _Reply()


@mark.timeout(60)
def test_get_array_carriers_state_is_the_activation_receipt(o1_adapter_src):
    """The carrier-state readback maps every tx/rx array carrier to its
    asynchronous state (DISABLED/BUSY/READY) — the receipt an activation is
    judged by; wait_for_carriers_ready polls it to completion."""
    from ru_config import RuConfig

    _ = o1_adapter_src
    states = RuConfig(_CarrierStateManager(), "running").get_array_carriers_state()
    assert states == {
        "Tx-Array-Carrier-00": "READY",
        "Tx-Array-Carrier-01": "BUSY",
        "Rx-Array-Carrier-00": "DISABLED",
    }

    # wait_for_carriers_ready returns the map (not-all-READY) after the bounded wait
    polled = []
    not_ready = RuConfig(_CarrierStateManager(), "running").wait_for_carriers_ready(
        timeout_s=0.05, poll_interval_s=0.01, on_poll=lambda: polled.append(True)
    )
    assert not_ready["Tx-Array-Carrier-01"] == "BUSY"
    assert polled, "on_poll must run between polling rounds"


@mark.timeout(60)
def test_array_carriers_state_against_sim(ru_config, o1_adapter_src):
    """Whatever carriers the sim exposes parse cleanly into legal states."""
    _ = o1_adapter_src
    sim_states = ru_config.get_array_carriers_state()
    assert all(state in (None, "DISABLED", "BUSY", "READY") for state in sim_states.values())


class _MuteActivationManager:
    """Stub manager whose edit-config never answers (reply timeout)."""

    server_capabilities = ()

    def edit_config(self, config=None, format=None, target=None, default_operation=None):  # noqa: A002
        from ncclient.operations.errors import TimeoutExpiredError

        raise TimeoutExpiredError("no reply within the RPC timeout")


@mark.timeout(60)
def test_activation_reply_timeout_tolerated_only_when_opted_in(o1_adapter_src):
    """Some O-RU servers accept a re-activation edit but never reply; the
    tolerate_reply_timeout activation option downgrades exactly that case to
    a warning (state readback is the receipt). Without the option the
    timeout stays fatal — spec behavior by default."""
    from ncclient.operations.errors import TimeoutExpiredError
    from pytest import raises as expect_raises
    from ru_config import RuConfig

    layout = {
        "endpoint": {"dl_port_id": [0, 1], "ul_port_id": [0, 1]},
        "activation": {"state": "ACTIVE", "tolerate_reply_timeout": True},
    }
    RuConfig(_MuteActivationManager(), "running").activate_full_config(layout)  # must not raise

    strict_layout = {
        "endpoint": {"dl_port_id": [0, 1], "ul_port_id": [0, 1]},
        "activation": {"state": "ACTIVE"},
    }
    with expect_raises(TimeoutExpiredError):
        RuConfig(_MuteActivationManager(), "running").activate_full_config(strict_layout)


class _MuteReadManager:
    """Stub manager whose <get>/<get-config> never answer (reply timeout)."""

    def get_config(self, source=None, filter=None, with_defaults=None):  # noqa: A002 - ncclient signature
        from ncclient.operations.errors import TimeoutExpiredError

        raise TimeoutExpiredError("no reply within the RPC timeout")

    def get(self, filter=None, with_defaults=None):  # noqa: A002 - ncclient signature
        from ncclient.operations.errors import TimeoutExpiredError

        raise TimeoutExpiredError("no reply within the RPC timeout")


@mark.timeout(60)
def test_non_strict_read_degrades_to_empty_on_reply_timeout(o1_adapter_src, caplog):
    """A non-strict read promises to degrade to {} on retrieval failure. An
    ncclient RPC-reply timeout (TimeoutExpiredError — a direct NCClientError
    subclass, neither TransportError nor RPCError) must be caught too, or the
    get/CLI path dies with an unhandled traceback instead of degrading."""
    from ncclient.operations.errors import TimeoutExpiredError
    from pytest import raises as expect_raises
    from ru_config import RuConfig

    _ = o1_adapter_src
    controller = RuConfig(_MuteReadManager(), "running")

    with caplog.at_level(logging.ERROR):
        assert controller.get_perf_measurement_config() == {}, "non-strict read must degrade to {}"
    assert "Failed to retrieve" in caplog.text

    # a strict read still propagates — a genuinely dead session must recycle
    with expect_raises(TimeoutExpiredError):
        controller._get_and_print_config("<x/>", "strict probe", strict=True)
