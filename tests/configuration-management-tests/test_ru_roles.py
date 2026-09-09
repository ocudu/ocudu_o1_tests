# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""NACM account-role coverage for the adapter's O-RU client (``ru_config.RuConfig``).

The client acts as one of the NACM account groups of O-RAN WG4 M-plane
Table 6.5-1 (``role=sudo`` or ``role=hybrid-odu``) and skips, before any RPC,
the writes that group may not perform. This module proves two things:

* the client's own gating (dry-run ``RuConfig``, no server): the writable-module
  table, which edits ``set_full_config`` still emits as ``hybrid-odu`` (everything
  but ``ietf-interfaces``), that PM and sync writes are skipped with one INFO
  line each, that an unknown role is rejected and that the CLI exposes ``--role``;
* the mock RU enforces the same table (``ocudu_netconf --config ru`` puts the
  ``hybrid-odu`` TLS identity in a NACM group with the Table 6.5-1 rule-list):
  a ``sudo``-role client on that account is refused with ``access-denied`` where
  the table says read-only, while the ``hybrid-odu``-role client provisions the
  full layout without ever tripping NACM. The root SSH session keeps its write
  access, so the two paths are proven side by side.

sysrepo skips the NACM check when a merge produces no diff, so every denial
test writes a value that differs from what the mock already holds.
"""

import logging
import subprocess
import sys
from pathlib import Path

import yaml
from ncclient.operations import RPCError
from pytest import fixture, mark, raises

logger = logging.getLogger(__name__)

PROFILE_DIR = Path(__file__).parent.parent / "configs" / "ru"

# ru_config's two log lines for role gating (see RuConfig._writable / edit_config).
_SKIP_MARK = "is not writable for role"
_DENIED_MARK = "NACM denied"

# edit-config descriptions set_full_config emits as sudo, in order.
_FULL_CONFIG_EDITS = [
    "IETF interfaces",
    "ORAN processing elements",
    "ORAN Uplane Tx endpoints elements",
    "ORAN Uplane Rx endpoints elements",
    "ORAN Uplane Tx array carriers",
    "ORAN Uplane Rx array carriers",
    "ORAN Uplane low level Tx links",
    "ORAN Uplane low level Rx links",
    "ORAN Uplane TDD pattern",
    "ORAN Uplane TDD carrier binding",
    "ORAN Uplane carrier active",
]


def _load_mplane_profile(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)["mplane"]


PROFILE = _load_mplane_profile(PROFILE_DIR / "n78_4x2_reference_timing.yaml")


def _as_list(node) -> list:
    """xmltodict yields a dict for single elements and a list otherwise."""
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


def _by_name(nodes) -> dict:
    return {entry["name"]: entry for entry in _as_list(nodes)}


def _full_config_layout(profile: dict) -> dict:
    """The layout test_ru_controller's full-config test provisions from the same
    profile — the mock's keyed lists merge, so re-using it keeps them coherent."""
    return {
        "interface": {"ru_mac_addr": profile["ru_mac_addr"], "vlan": profile["vlan"]},
        "processing": {
            "ru_mac_addr": profile["ru_mac_addr"],
            "du_mac_addr": profile["du_mac_addr"],
            "vlan": profile["vlan"],
        },
        "endpoint": {key: profile[key] for key in ("iq_bitwidth", "compression_type", "num_prb", "frame_structure")},
        "carrier": {
            key: profile[key] for key in ("dl_arfcn", "dl_freq", "ul_arfcn", "ul_freq", "tx_gain", "rf_bandwidth_hz")
        },
        "activation": {"state": "ACTIVE"},
    }


def _skip_lines(caplog) -> list:
    return [record for record in caplog.records if _SKIP_MARK in record.getMessage()]


def _denied_lines(caplog) -> list:
    return [record for record in caplog.records if _DENIED_MARK in record.getMessage()]


def _dry_run_client(role):
    """A dry-run RuConfig whose edit-config descriptions are captured instead of sent."""
    from ru_config import RuConfig

    client = RuConfig(None, "running", role=role) if role is not None else RuConfig(None, "running")
    client._is_configurable_tdd_supported = lambda: True
    sent = []
    client.edit_config = lambda xml_request, description="XML config": sent.append(description)
    return client, sent


# --- client-side gating (no server) -------------------------------------------


@mark.timeout(60)
def test_writable_modules_follow_table_6_5_1(o1_adapter_src):
    """WRITABLE_MODULES is the Table 6.5-1 column for each role the client offers."""
    from ru_config import ROLE_HYBRID_ODU, ROLE_SUDO, ROLES, RuConfig, WRITABLE_MODULES

    _ = o1_adapter_src
    assert set(ROLES) == {ROLE_SUDO, ROLE_HYBRID_ODU} == set(WRITABLE_MODULES)
    assert WRITABLE_MODULES[ROLE_HYBRID_ODU] == {"o-ran-supervision", "o-ran-uplane-conf", "o-ran-processing-element"}
    assert WRITABLE_MODULES[ROLE_SUDO] >= WRITABLE_MODULES[ROLE_HYBRID_ODU]
    assert {"ietf-interfaces", "o-ran-sync", "o-ran-performance-management"} <= WRITABLE_MODULES[ROLE_SUDO]

    hybrid_odu = RuConfig(None, "running", role=ROLE_HYBRID_ODU)
    assert all(hybrid_odu.can_write(module) for module in WRITABLE_MODULES[ROLE_HYBRID_ODU])
    assert not any(hybrid_odu.can_write(module) for module in ("ietf-interfaces", "o-ran-sync", "o-ran-performance-management"))


@mark.timeout(60)
def test_hybrid_odu_full_config_skips_only_the_interface_edit(o1_adapter_src, caplog):
    """As hybrid-odu, set_full_config emits every edit but ietf-interfaces and logs one skip line."""
    _ = o1_adapter_src
    client, sent = _dry_run_client("hybrid-odu")
    with caplog.at_level(logging.INFO):
        client.set_full_config(_full_config_layout(PROFILE), skip_activation=False)

    assert sent == _FULL_CONFIG_EDITS[1:]
    assert "IETF interfaces" not in sent
    skips = _skip_lines(caplog)
    assert len(skips) == 1
    assert skips[0].levelno == logging.INFO
    assert skips[0].getMessage().startswith("IETF interfaces: ietf-interfaces is not writable for role hybrid-odu")
    assert not _denied_lines(caplog)


@mark.timeout(60)
def test_sudo_full_config_emits_the_full_sequence(o1_adapter_src, caplog):
    """As sudo (explicit or the default role) nothing is skipped: the interface edit leads the sequence."""
    _ = o1_adapter_src
    for role in ("sudo", None):
        caplog.clear()
        client, sent = _dry_run_client(role)
        with caplog.at_level(logging.INFO):
            client.set_full_config(_full_config_layout(PROFILE), skip_activation=False)
        assert sent == _FULL_CONFIG_EDITS, role
        assert not _skip_lines(caplog), role


@mark.timeout(60)
def test_hybrid_odu_skips_pm_and_sync_writes(o1_adapter_src, caplog):
    """PM and sync are SMO-provisioned for hybrid-odu: no edit, one skip line each."""
    _ = o1_adapter_src
    client, sent = _dry_run_client("hybrid-odu")

    with caplog.at_level(logging.INFO):
        assert client.configure_perf_measurement() == []
    assert sent == []
    skips = _skip_lines(caplog)
    assert len(skips) == 1
    assert skips[0].getMessage().startswith(
        "ORAN Performance measurements: o-ran-performance-management is not writable for role hybrid-odu"
    )

    caplog.clear()
    with caplog.at_level(logging.INFO):
        client.set_oran_sync_config(domain_number=24)
    assert sent == []
    skips = _skip_lines(caplog)
    assert len(skips) == 1
    assert skips[0].getMessage().startswith("ORAN sync configuration: o-ran-sync is not writable for role hybrid-odu")


@mark.timeout(60)
def test_unknown_role_rejected_and_cli_exposes_role(o1_adapter_src):
    """A role outside Table 6.5-1 is refused at construction; ru_controller.py offers --role."""
    from ru_config import RuConfig

    with raises(ValueError):
        RuConfig(None, "running", role="odu")

    result = subprocess.run(
        [sys.executable, "ru_controller.py", "--help"],
        cwd=o1_adapter_src,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "--role" in result.stdout
    assert "hybrid-odu" in result.stdout


# --- against the mock RU over the hybrid-odu identity -------------------------


@fixture(scope="module")
def hybrid_odu_client(o1_adapter_src, hybrid_odu_tls_manager):
    """The client as the O-DU of a hybrid deployment: hybrid-odu role on the hybrid-odu account."""
    from ru_config import ROLE_HYBRID_ODU, RuConfig

    _ = o1_adapter_src
    return RuConfig(hybrid_odu_tls_manager, "running", role=ROLE_HYBRID_ODU)


@fixture(scope="module")
def sudo_client_on_hybrid_odu_account(o1_adapter_src, hybrid_odu_tls_manager):
    """A sudo-role client on the hybrid-odu account: the client gates nothing,
    so whatever comes back is the mock's NACM verdict."""
    from ru_config import ROLE_SUDO, RuConfig

    _ = o1_adapter_src
    return RuConfig(hybrid_odu_tls_manager, "running", role=ROLE_SUDO)


@mark.timeout(60)
def test_root_session_still_writes_interfaces(ru_config):
    """The root SSH client keeps writing ietf-interfaces: the same values the
    RU controller suite already provisioned, so the edit is a no-op re-apply and
    leaves the mock as it found it (it also guarantees the VLAN interface the
    processing element below references exists)."""
    logger.info("Root SSH session: ietf-interfaces edit is accepted (NACM bypass for the recovery user).")
    ru_config.set_ietf_interfaces({"ru_mac_addr": PROFILE["ru_mac_addr"], "vlan": PROFILE["vlan"]})

    interfaces = _by_name((ru_config.get_ietf_interfaces().get("interfaces") or {}).get("interface"))
    vlan_interface = interfaces[f"uc-vlan{PROFILE['vlan']}"]
    assert vlan_interface["vlan-id"] == str(PROFILE["vlan"])
    assert vlan_interface["mac-address"].lower() == PROFILE["ru_mac_addr"].lower()
    assert interfaces["fronthaul1"]["mac-address"].lower() == PROFILE["ru_mac_addr"].lower()


@mark.timeout(60)
def test_mock_denies_interface_write_on_hybrid_odu_account(sudo_client_on_hybrid_odu_account, caplog):
    """The mock enforces Table 6.5-1: an ietf-interfaces write on the hybrid-odu
    account is refused with access-denied, logged as a NACM denial and re-raised."""
    logger.info("sudo-role client on the hybrid-odu account: ietf-interfaces edit -> access-denied.")
    client = sudo_client_on_hybrid_odu_account
    interfaces = _by_name((client.get_ietf_interfaces().get("interfaces") or {}).get("interface"))
    configured_vlans = {int(entry["vlan-id"]) for entry in interfaces.values() if entry.get("vlan-id")}
    # a VLAN the mock does not hold yet, so the merge creates a node and NACM is consulted
    vlan = next(candidate for candidate in range(PROFILE["vlan"] + 1, 4095) if candidate not in configured_vlans)

    with caplog.at_level(logging.INFO):
        with raises(RPCError) as exc_info:
            client.set_ietf_interfaces({"ru_mac_addr": PROFILE["ru_mac_addr"], "vlan": vlan})
    assert exc_info.value.tag == "access-denied"
    denied = _denied_lines(caplog)
    assert len(denied) == 1
    assert denied[0].levelno == logging.ERROR
    assert denied[0].getMessage().startswith("NACM denied IETF interfaces for role sudo")
    assert not _skip_lines(caplog)

    interfaces = _by_name((client.get_ietf_interfaces().get("interfaces") or {}).get("interface"))
    assert f"uc-vlan{vlan}" not in interfaces, "a denied edit must not land"


@mark.timeout(180)
def test_hybrid_odu_full_config_never_trips_nacm(hybrid_odu_client, caplog):
    """The role-scoped client provisions the full layout on the hybrid-odu
    account without a single NACM denial: the interface edit is skipped
    client-side, everything else is accepted and readable over the same session."""
    logger.info("hybrid-odu role on the hybrid-odu account: set_full_config completes; readback matches.")
    with caplog.at_level(logging.INFO):
        hybrid_odu_client.set_full_config(_full_config_layout(PROFILE), skip_activation=True)

    skips = _skip_lines(caplog)
    assert len(skips) == 1
    assert skips[0].getMessage().startswith("IETF interfaces: ietf-interfaces is not writable for role hybrid-odu")
    assert not _denied_lines(caplog)

    processing = hybrid_odu_client.get_processing_elements().get("processing-elements") or {}
    element = _by_name(processing.get("ru-elements"))["processing-element01"]
    eth_flow = element["transport-flow"]["eth-flow"]
    assert eth_flow["ru-mac-address"].lower() == PROFILE["ru_mac_addr"].lower()
    assert eth_flow["o-du-mac-address"].lower() == PROFILE["du_mac_addr"].lower()
    assert eth_flow["vlan-id"] == str(PROFILE["vlan"])
    assert element["transport-flow"]["interface-name"] == f"uc-vlan{PROFILE['vlan']}"

    uplane = hybrid_odu_client.get_uplane_config().get("user-plane-configuration") or {}
    tx_endpoint = _by_name(uplane.get("low-level-tx-endpoints"))["sep_txch1"]
    assert tx_endpoint["compression"]["iq-bitwidth"] == str(PROFILE["iq_bitwidth"])
    assert tx_endpoint["compression"]["compression-type"] == PROFILE["compression_type"]
    tx_carrier = _by_name(uplane.get("tx-array-carriers"))["Tx-Array-Carrier-00"]
    assert tx_carrier["absolute-frequency-center"] == str(PROFILE["dl_arfcn"])
    assert tx_carrier["channel-bandwidth"] == str(PROFILE["rf_bandwidth_hz"])
    tx_link = _by_name(uplane.get("low-level-tx-links"))["Low-Level-Tx-Links-000"]
    assert tx_link["processing-element"] == "processing-element01"


@mark.timeout(60)
def test_sync_readable_and_write_gated_on_hybrid_odu_account(
    hybrid_odu_client, sudo_client_on_hybrid_odu_account, caplog
):
    """o-ran-sync is read/exec for hybrid-odu: config and status reads work over
    the session; the write is access-denied for a sudo-role client and skipped
    (no RPC) by the hybrid-odu-role client."""
    logger.info("hybrid-odu account: sync reads permitted, domain-number write denied / skipped.")
    ptp = (hybrid_odu_client.get_oran_sync().get("sync") or {}).get("ptp-config") or {}
    classes = {entry["clock-classes"] for entry in _as_list(ptp.get("accepted-clock-classes"))}
    assert {"6", "7", "135"} <= classes, "seeded accepted-clock-classes not readable"
    status = hybrid_odu_client.get_sync_status(strict=True)
    assert set(status) == {"sync_state", "time_error", "frequency_error", "supported_reference_types"}

    current_domain = int(ptp.get("domain-number", 24))
    with caplog.at_level(logging.INFO):
        with raises(RPCError) as exc_info:
            sudo_client_on_hybrid_odu_account.set_oran_sync_config(domain_number=current_domain + 1)
    assert exc_info.value.tag == "access-denied"
    assert len(_denied_lines(caplog)) == 1

    caplog.clear()
    with caplog.at_level(logging.INFO):
        hybrid_odu_client.set_oran_sync_config(domain_number=current_domain + 1)
    skips = _skip_lines(caplog)
    assert len(skips) == 1
    assert skips[0].getMessage().startswith("ORAN sync configuration: o-ran-sync is not writable for role hybrid-odu")
    assert not _denied_lines(caplog)

    ptp = (hybrid_odu_client.get_oran_sync().get("sync") or {}).get("ptp-config") or {}
    assert int(ptp.get("domain-number", 24)) == current_domain, "neither path may change the domain"


@mark.timeout(60)
def test_pm_write_gated_on_hybrid_odu_account(hybrid_odu_client, sudo_client_on_hybrid_odu_account, caplog):
    """o-ran-performance-management is read/exec for hybrid-odu: the PM write is
    access-denied for a sudo-role client and skipped by the hybrid-odu-role client."""
    logger.info("hybrid-odu account: PM activation denied / skipped.")
    before = hybrid_odu_client.get_perf_measurement_config().get("performance-measurement-objects") or {}
    # an interval the mock does not hold, so the merge carries a diff
    interval = int(before.get("rx-window-measurement-interval", 60)) + 1

    with caplog.at_level(logging.INFO):
        with raises(RPCError) as exc_info:
            sudo_client_on_hybrid_odu_account.set_oran_perf_measurement({"rx_window_interval": interval})
    assert exc_info.value.tag == "access-denied"
    denied = _denied_lines(caplog)
    assert len(denied) == 1
    assert denied[0].getMessage().startswith("NACM denied ORAN Performance measurements for role sudo")

    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert hybrid_odu_client.set_oran_perf_measurement({"rx_window_interval": interval}) is False
        assert hybrid_odu_client.configure_perf_measurement({"rx_window_interval": interval}) == []
    skips = _skip_lines(caplog)
    assert len(skips) == 2
    assert all(
        record.getMessage().startswith(
            "ORAN Performance measurements: o-ran-performance-management is not writable for role hybrid-odu"
        )
        for record in skips
    )
    assert not _denied_lines(caplog)

    after = hybrid_odu_client.get_perf_measurement_config().get("performance-measurement-objects") or {}
    assert after.get("rx-window-measurement-interval") == before.get("rx-window-measurement-interval")
