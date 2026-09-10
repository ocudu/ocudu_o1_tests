# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Provision-on-connect tests (adapter ru_provisioner.RuProvisioner).

The provisioner applies a full-config dict on every connect cycle of the
persistent M-plane session and gates carrier activation on the O-RU
reporting sync-state LOCKED. The mock RU exposes no synchronization state,
so the sim integration deliberately exercises the provisioned-but-inactive
outcome — the correct end state for a radio that cannot prove sync.
"""

import asyncio
import logging
import os
import re
import time
from types import SimpleNamespace

import yaml
from ncclient.operations import rpc as rpc_ops
from pytest import mark, raises

logger = logging.getLogger(__name__)


class _StubRpcError(rpc_ops.RPCError):
    """RPCError stand-in that skips the base class's rpc-reply XML parsing."""

    def __init__(self, message="application rejected the edit"):  # pylint: disable=super-init-not-called
        Exception.__init__(self, message)


def _raise(exc):
    """Return a callable that raises exc, for stubbing a rejected edit."""

    def _raiser(*args, **kwargs):  # noqa: ARG001 - stub matches any signature
        raise exc

    return _raiser


SYNTHETIC_LAYOUT = {
    "interface": {"ru_mac_addr": "aa:bb:cc:dd:ee:01", "vlan": 31},
    "processing": {"ru_mac_addr": "aa:bb:cc:dd:ee:01", "du_mac_addr": "aa:bb:cc:dd:ee:02", "vlan": 31},
    "endpoint": {
        "iq_bitwidth": 9,
        "compression_type": "STATIC",
        "num_prb": 273,
        "frame_structure": 193,
        "dl_port_id": [0, 1],
        "ul_port_id": [0, 1],
        "prach_port_id": [4, 5],
    },
    "carrier": {
        "dl_arfcn": 640000,
        "dl_freq": 3600000000,
        "ul_arfcn": 640000,
        "ul_freq": 3600000000,
        "tx_gain": 21.0,
        "rf_bandwidth_hz": 100000000,
    },
    "activation": {"state": "ACTIVE"},
}


class _RecordingRuConfig:
    """RuConfig double with a scripted sync-state sequence, recording calls."""

    def __init__(self, sync_states, carrier_states=None, role="sudo", interfaces=None):
        self.sync_states = list(sync_states)
        self.carrier_states = {"Tx-Array-Carrier-00": "READY"} if carrier_states is None else carrier_states
        self.calls = []
        self.watchdog_resets = 0
        self.role = role
        # scripted ietf-interfaces reads (hybrid-odu): entry lists, None for
        # "no data" or an exception; the last entry repeats
        self.interfaces = list(interfaces or [])
        self.interface_reads = 0
        self.pushed = None

    def can_write(self, module):
        return self.role == "sudo" or module in ("o-ran-supervision", "o-ran-uplane-conf", "o-ran-processing-element")

    def get_ietf_interfaces(self, strict=False):
        assert strict is True, "the provisioner must read the interfaces strictly"
        self.interface_reads += 1
        entries = self.interfaces.pop(0) if len(self.interfaces) > 1 else (self.interfaces[0] if self.interfaces else None)
        if isinstance(entries, Exception):
            raise entries
        return {"interfaces": {"interface": entries}} if entries is not None else {}

    def set_full_config(self, config_dict, skip_activation=False):
        self.pushed = config_dict
        self.calls.append(("set_full_config", skip_activation, config_dict is SYNTHETIC_LAYOUT))

    def get_sync_status(self, strict=False):
        assert strict is True, "the provisioner must read sync state strictly"
        state = self.sync_states.pop(0) if self.sync_states else None
        if isinstance(state, Exception):
            raise state
        return {"sync_state": state}

    def reset_supervision_watchdog(self, interval, guard):  # noqa: ARG002
        self.watchdog_resets += 1

    def activate_full_config(self, config_dict):
        self.calls.append(("activate_full_config", config_dict is SYNTHETIC_LAYOUT))

    def wait_for_carriers_ready(self, timeout_s=120, poll_interval_s=5, on_poll=None, strict=False):  # noqa: ARG002
        assert strict is True, "the provisioner must read carrier state strictly"
        if on_poll is not None:
            on_poll()  # the receipt wait must keep offering watchdog feeds
        self.calls.append(("wait_for_carriers_ready",))
        return dict(self.carrier_states)


@mark.timeout(60)
def test_provision_activates_when_sync_locked(o1_adapter_src, caplog):
    """Provision applies the config inactive, polls to LOCKED, activates, and
    ends with the carrier-state receipt — activation is confirmed by
    readback, never assumed from an accepted edit."""
    from ru_provisioner import RuProvisioner

    _ = o1_adapter_src
    ru_config = _RecordingRuConfig(sync_states=["HOLDOVER", "LOCKED"])
    with caplog.at_level(logging.INFO):
        RuProvisioner(SYNTHETIC_LAYOUT, sync_timeout_s=7, sync_poll_s=0.01).provision(ru_config)

    assert ru_config.calls == [
        ("set_full_config", True, True),
        ("activate_full_config", True),
        ("wait_for_carriers_ready",),
    ]
    assert "carrier activation receipt: Tx-Array-Carrier-00=READY" in caplog.text


@mark.timeout(60)
def test_provision_warns_when_carriers_never_ready(o1_adapter_src, caplog):
    """Carriers stuck short of READY after activation must be a loud warning —
    the receipt is the activation result, not the edit's <ok/>."""
    from ru_provisioner import RuProvisioner

    _ = o1_adapter_src
    ru_config = _RecordingRuConfig(
        sync_states=["LOCKED"], carrier_states={"Tx-Array-Carrier-00": "READY", "Rx-Array-Carrier-00": "DISABLED"}
    )
    with caplog.at_level(logging.INFO):
        RuProvisioner(SYNTHETIC_LAYOUT, sync_timeout_s=7, sync_poll_s=0.01).provision(ru_config)

    assert "not all READY" in caplog.text
    assert "Rx-Array-Carrier-00=DISABLED" in caplog.text


@mark.timeout(60)
def test_provision_inactive_state_skips_receipt_wait(o1_adapter_src):
    """activation.state INACTIVE is configure-only: no sync wait and no
    receipt wait, since carriers are deliberately left down."""
    from ru_provisioner import RuProvisioner

    _ = o1_adapter_src
    inactive_layout = {**SYNTHETIC_LAYOUT, "activation": {"state": "INACTIVE"}}
    ru_config = _RecordingRuConfig(sync_states=["LOCKED"])
    RuProvisioner(inactive_layout, sync_timeout_s=7, sync_poll_s=0.01).provision(ru_config)

    assert [name for name, *_ in ru_config.calls] == ["set_full_config", "activate_full_config"]
    assert ru_config.sync_states == ["LOCKED"], "INACTIVE needs no sync precondition: the state is never read"


@mark.timeout(60)
def test_feed_schedule_spans_the_activation_boundary(o1_adapter_src):
    """One feed schedule covers the whole cycle: a due feed fires before the
    activation edit even when the sync wait returned on its first read, so the
    boundary between the two waits is never an unfed gap."""
    from ru_provisioner import RuProvisioner

    _ = o1_adapter_src
    ru_config = _RecordingRuConfig(sync_states=["LOCKED"])
    provisioner = RuProvisioner(
        SYNTHETIC_LAYOUT, sync_timeout_s=7, sync_poll_s=0.01, supervision_interval=0.1, supervision_guard=0.02
    )
    original_set_full_config = ru_config.set_full_config

    def slow_set_full_config(config_dict, skip_activation=False):
        time.sleep(0.12)  # longer than the 0.05 s feed period: a feed is due right after
        original_set_full_config(config_dict, skip_activation=skip_activation)

    ru_config.set_full_config = slow_set_full_config
    provisioner.provision(ru_config)

    assert ru_config.watchdog_resets >= 1, "a due feed must fire before activation, not only inside the waits"


@mark.timeout(60)
def test_rejected_feed_is_a_warning_once_per_cycle(o1_adapter_src, caplog):
    """A rejected watchdog reset during the waits means the O-RU's watchdog
    is not being fed at all: the first one is a warning, the rest stay quiet."""
    from ru_provisioner import RuProvisioner

    _ = o1_adapter_src
    ru_config = _RecordingRuConfig(sync_states=[])
    ru_config.reset_supervision_watchdog = _raise(_StubRpcError("no matching subscribers"))
    provisioner = RuProvisioner(
        SYNTHETIC_LAYOUT, sync_timeout_s=0.3, sync_poll_s=0.01, supervision_interval=0.02, supervision_guard=0.01
    )
    with caplog.at_level(logging.DEBUG):
        provisioner.provision(ru_config)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "rejected during provisioning" in r.message]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG and "rejected during provisioning" in r.message]
    assert len(warnings) == 1, "exactly one warning per cycle"
    assert debugs, "later rejections are debug noise"


@mark.timeout(60)
def test_provision_leaves_carriers_inactive_without_sync(o1_adapter_src):
    """No LOCKED within the timeout: provisioned, never activated."""
    from ru_provisioner import RuProvisioner

    _ = o1_adapter_src
    ru_config = _RecordingRuConfig(sync_states=[])
    RuProvisioner(SYNTHETIC_LAYOUT, sync_timeout_s=0.05, sync_poll_s=0.01).provision(ru_config)

    assert [name for name, *_ in ru_config.calls] == ["set_full_config"]


@mark.timeout(60)
def test_sync_wait_feeds_the_supervision_watchdog(o1_adapter_src):
    """A long sync wait must keep resetting the watchdog (the O-RU-side
    supervision budget is much shorter than a cold-boot PTP lock), at half
    the notification interval."""
    from ru_provisioner import RuProvisioner

    _ = o1_adapter_src
    ru_config = _RecordingRuConfig(sync_states=[])
    provisioner = RuProvisioner(
        SYNTHETIC_LAYOUT,
        sync_timeout_s=0.5,
        sync_poll_s=0.02,
        supervision_interval=0.1,
        supervision_guard=0.02,
    )
    provisioner.provision(ru_config)

    assert ru_config.watchdog_resets >= 3, "the wait must feed the watchdog throughout"


@mark.timeout(60)
def test_sync_wait_raises_on_dead_session(o1_adapter_src):
    """A transport failure on the strict sync read propagates immediately —
    the provisioner must not poll a dead session for the rest of the timeout."""
    from ncclient.transport import errors as transport_errors
    from ru_provisioner import RuProvisioner

    _ = o1_adapter_src
    ru_config = _RecordingRuConfig(
        sync_states=["HOLDOVER", transport_errors.TransportError("session died")]
    )
    with raises(transport_errors.TransportError):
        RuProvisioner(SYNTHETIC_LAYOUT, sync_timeout_s=30, sync_poll_s=0.01).provision(ru_config)


@mark.timeout(60)
def test_skip_activation_sends_no_carrier_active_payload(o1_adapter_src):
    """skip_activation must suppress exactly the carrier-activation edit;
    activate_full_config sends it alone."""
    from ru_config import RuConfig

    _ = o1_adapter_src
    controller = RuConfig(None, "running")
    sent = []
    controller.edit_config = lambda xml_request, description="": sent.append(xml_request)

    controller.set_full_config(SYNTHETIC_LAYOUT, skip_activation=True)
    assert sent, "provisioning must send edits"
    assert not any("<active>" in xml for xml in sent), "no activation payload while skipped"

    sent.clear()
    controller.activate_full_config(SYNTHETIC_LAYOUT)
    assert len(sent) == 1 and sent[0].count("<active>ACTIVE</active>") == 4, "2 tx + 2 rx carriers activated"


@mark.timeout(60)
def test_sudo_pushes_the_profile_without_reading_interfaces(o1_adapter_src):
    """sudo creates the VLAN interface itself: the profile is pushed as is, no interface read."""
    from ru_provisioner import RuProvisioner

    ru = _RecordingRuConfig(sync_states=["LOCKED"])
    RuProvisioner(SYNTHETIC_LAYOUT, sync_timeout_s=1, sync_poll_s=0.01).provision(ru)
    assert ru.interface_reads == 0
    assert ru.calls[0] == ("set_full_config", True, True)


@mark.timeout(60)
def test_hybrid_binds_the_processing_element_to_the_management_planes_interface(o1_adapter_src, caplog):
    """hybrid-odu: the l2vlan interface carrying processing.vlan names the transport
    flow — read from the O-RU, the profile itself left untouched."""
    from ru_provisioner import RuProvisioner

    ru = _RecordingRuConfig(
        sync_states=["LOCKED"],
        role="hybrid-odu",
        interfaces=[[{"name": "fronthaul1"}, {"name": "smo-vlan31", "vlan-id": "31"}]],
    )
    with caplog.at_level(logging.INFO):
        RuProvisioner(SYNTHETIC_LAYOUT, sync_timeout_s=1, sync_poll_s=0.01, interface_timeout_s=1).provision(ru)

    assert ru.pushed["processing"]["interface_name"] == "smo-vlan31"
    assert ru.pushed is not SYNTHETIC_LAYOUT and "interface_name" not in SYNTHETIC_LAYOUT["processing"]
    assert ru.calls[0] == ("set_full_config", True, False)
    assert any("bound to the management plane's interface smo-vlan31" in r.getMessage() for r in caplog.records)


@mark.timeout(60)
def test_hybrid_declared_interface_name_wins(o1_adapter_src):
    """processing.interface_name names the management plane's interface outright."""
    from ru_provisioner import RuProvisioner

    profile = {**SYNTHETIC_LAYOUT, "processing": {**SYNTHETIC_LAYOUT["processing"], "interface_name": "fh0.31"}}
    ru = _RecordingRuConfig(
        sync_states=["LOCKED"],
        role="hybrid-odu",
        interfaces=[[{"name": "fh0.31", "vlan-id": "31"}, {"name": "uc-vlan31", "vlan-id": "31"}]],
    )
    RuProvisioner(profile, sync_timeout_s=1, sync_poll_s=0.01, interface_timeout_s=1).provision(ru)
    assert ru.pushed["processing"]["interface_name"] == "fh0.31"


@mark.timeout(60)
def test_hybrid_several_interfaces_on_the_vlan_prefer_the_conventional_name(o1_adapter_src, caplog):
    """Two interfaces on the VLAN is an ambiguous profile: uc-vlan<vlan> wins, with a warning."""
    from ru_provisioner import RuProvisioner

    ru = _RecordingRuConfig(
        sync_states=["LOCKED"],
        role="hybrid-odu",
        interfaces=[[{"name": "aaa", "vlan-id": "31"}, {"name": "uc-vlan31", "vlan-id": "31"}]],
    )
    with caplog.at_level(logging.WARNING):
        RuProvisioner(SYNTHETIC_LAYOUT, sync_timeout_s=1, sync_poll_s=0.01, interface_timeout_s=1).provision(ru)
    assert ru.pushed["processing"]["interface_name"] == "uc-vlan31"
    assert any("Several interfaces carry VLAN 31" in r.getMessage() for r in caplog.records)


@mark.timeout(60)
def test_hybrid_waits_for_the_interface_feeding_the_watchdog(o1_adapter_src):
    """The interface appears while the cycle waits: the provisioner polls until it
    does, feeds the supervision watchdog meanwhile, then pushes."""
    from ru_provisioner import RuProvisioner

    ru = _RecordingRuConfig(
        sync_states=["LOCKED"], role="hybrid-odu", interfaces=[None] * 12 + [[{"name": "uc-vlan31", "vlan-id": "31"}]]
    )
    provisioner = RuProvisioner(
        SYNTHETIC_LAYOUT, sync_timeout_s=1, sync_poll_s=0.01, supervision_interval=0.1, interface_timeout_s=5
    )
    provisioner.provision(ru)
    assert ru.interface_reads == 13
    assert ru.watchdog_resets >= 1, "the wait must keep feeding the watchdog"
    assert ru.pushed["processing"]["interface_name"] == "uc-vlan31"


@mark.timeout(60)
def test_hybrid_defers_the_cycle_when_the_interface_never_appears(o1_adapter_src, caplog):
    """Nothing is pushed when the management plane has not created the interface
    within the timeout; the warning names what was missing."""
    from ru_provisioner import RuProvisioner

    ru = _RecordingRuConfig(sync_states=["LOCKED"], role="hybrid-odu", interfaces=[None])
    with caplog.at_level(logging.WARNING):
        RuProvisioner(SYNTHETIC_LAYOUT, sync_timeout_s=1, sync_poll_s=0.01, interface_timeout_s=0.05).provision(ru)
    assert ru.calls == [] and ru.interface_reads >= 1
    assert any(
        "no l2vlan interface for VLAN 31" in r.getMessage() and "deferred to the next connect cycle" in r.getMessage()
        for r in caplog.records
    )


@mark.timeout(60)
def test_hybrid_interface_wait_raises_on_dead_session(o1_adapter_src):
    """The strict interface read propagates a transport failure, so the session recycles."""
    from ru_provisioner import RuProvisioner

    ru = _RecordingRuConfig(sync_states=["LOCKED"], role="hybrid-odu", interfaces=[OSError("session gone")])
    with raises(OSError):
        RuProvisioner(SYNTHETIC_LAYOUT, sync_timeout_s=1, sync_poll_s=0.01, interface_timeout_s=1).provision(ru)


@mark.timeout(60)
def test_load_provision_config_validates_interface_name(tmp_path, o1_adapter_src):
    """processing.interface_name is optional; when present it is a non-empty string."""
    from ru_provisioner import load_provision_config

    _ = o1_adapter_src
    base = {key: value for key, value in SYNTHETIC_LAYOUT.items() if key != "activation"}
    path = tmp_path / "ru.yaml"
    path.write_text(
        yaml.safe_dump({**base, "processing": {**base["processing"], "interface_name": "fh0.31"}}), encoding="utf-8"
    )
    assert load_provision_config(str(path), yaml.safe_load)["processing"]["interface_name"] == "fh0.31"
    for bad in ("", "  ", 31):
        path.write_text(
            yaml.safe_dump({**base, "processing": {**base["processing"], "interface_name": bad}}), encoding="utf-8"
        )
        with raises(ValueError, match="interface_name"):
            load_provision_config(str(path), yaml.safe_load)


@mark.timeout(60)
def test_load_provision_config_validates_l2_mtu(tmp_path, o1_adapter_src):
    """interface.l2_mtu is optional; when present it is an integer in o-ran-interfaces' 64..65535."""
    from ru_provisioner import load_provision_config

    _ = o1_adapter_src
    base = {key: value for key, value in SYNTHETIC_LAYOUT.items() if key != "activation"}
    path = tmp_path / "ru.yaml"
    path.write_text(yaml.safe_dump({**base, "interface": {**base["interface"], "l2_mtu": 9000}}), encoding="utf-8")
    assert load_provision_config(str(path), yaml.safe_load)["interface"]["l2_mtu"] == 9000
    for bad in (100000, 10, "9000", True):
        path.write_text(yaml.safe_dump({**base, "interface": {**base["interface"], "l2_mtu": bad}}), encoding="utf-8")
        with raises(ValueError, match="l2_mtu"):
            load_provision_config(str(path), yaml.safe_load)


@mark.timeout(60)
def test_interface_l2_mtu_is_per_deployment(o1_adapter_src):
    """The interface template writes the profile's l2_mtu and keeps 9600 when the profile does not say."""
    from ru_config import RuConfig

    sent = []
    client = RuConfig(
        SimpleNamespace(edit_config=lambda **kwargs: sent.append(kwargs), server_capabilities=()), "running"
    )
    client.set_ietf_interfaces({"ru_mac_addr": "aa:bb:cc:dd:ee:01", "vlan": 31})
    client.set_ietf_interfaces({"ru_mac_addr": "aa:bb:cc:dd:ee:01", "vlan": 31, "l2_mtu": 9000})
    written = [re.search(r"<l2-mtu[^>]*>(\d+)</l2-mtu>", call["config"]).group(1) for call in sent]
    assert written == ["9600", "9000"]


@mark.timeout(60)
def test_load_provision_config_validates(tmp_path, o1_adapter_src):
    """Valid YAML loads with a defaulted activation block; malformed files fail fast."""
    from ru_provisioner import load_provision_config

    _ = o1_adapter_src
    good = tmp_path / "ru.yaml"
    config = {key: value for key, value in SYNTHETIC_LAYOUT.items() if key != "activation"}
    good.write_text(yaml.safe_dump(config), encoding="utf-8")
    loaded = load_provision_config(str(good), yaml.safe_load)
    assert loaded["activation"] == {"state": "ACTIVE", "tolerate_reply_timeout": True}
    assert loaded["carrier"]["dl_arfcn"] == 640000

    incomplete = tmp_path / "incomplete.yaml"
    incomplete.write_text(yaml.safe_dump({"interface": {}}), encoding="utf-8")
    with raises(ValueError, match="non-empty mapping"):
        load_provision_config(str(incomplete), yaml.safe_load)

    scalar = tmp_path / "scalar.yaml"
    scalar.write_text("just a string", encoding="utf-8")
    with raises(ValueError, match="must be a mapping"):
        load_provision_config(str(scalar), yaml.safe_load)

    empty_section = tmp_path / "empty_section.yaml"
    hollow = dict(config)
    hollow["endpoint"] = None
    empty_section.write_text(yaml.safe_dump(hollow), encoding="utf-8")
    with raises(ValueError, match="non-empty mapping"):
        load_provision_config(str(empty_section), yaml.safe_load)

    no_prb = tmp_path / "no_prb.yaml"
    partial = {**config, "endpoint": {"iq_bitwidth": 9}}
    no_prb.write_text(yaml.safe_dump(partial), encoding="utf-8")
    with raises(ValueError, match="num_prb"):
        load_provision_config(str(no_prb), yaml.safe_load)

    hollow_activation = tmp_path / "hollow_activation.yaml"
    hollow_activation.write_text(yaml.safe_dump({**config, "activation": {}}), encoding="utf-8")
    loaded = load_provision_config(str(hollow_activation), yaml.safe_load)
    assert loaded["activation"] == {
        "state": "ACTIVE",
        "tolerate_reply_timeout": True,
    }, "empty activation must default, not render empty"


@mark.timeout(60)
def test_load_provision_config_normalizes_declarations(tmp_path, o1_adapter_src):
    """Explicit endpoint/switching-point declarations are normalized at load
    (fail-fast), so a malformed declaration is a startup error rather than a
    rejection on every connect cycle."""
    from ru_provisioner import load_provision_config

    _ = o1_adapter_src
    base = {key: value for key, value in SYNTHETIC_LAYOUT.items() if key != "activation"}

    declared = tmp_path / "declared.yaml"
    config = dict(base)
    config["endpoint"] = {
        **base["endpoint"],
        "tx_endpoints": [{"name": "PORT-00", "eaxc_id": "0"}],
        "rx_endpoints": [{"name": "PORT-05", "eaxc_id": 0}],
        "prach_endpoints": [{"name": "PORT-25", "eaxc_id": 4}],
    }
    config["tdd"] = {"switching_points": [{"direction": "ul", "frame_offset": "4739840"}]}
    declared.write_text(yaml.safe_dump(config), encoding="utf-8")
    loaded = load_provision_config(str(declared), yaml.safe_load)
    assert loaded["endpoint"]["tx_endpoints"] == [{"name": "PORT-00", "eaxc_id": 0}], "string ids coerced"
    assert loaded["tdd"]["switching_points"] == [{"direction": "UL", "frame_offset": 4739840}]

    bad_endpoint = tmp_path / "bad_endpoint.yaml"
    broken = dict(base)
    broken["endpoint"] = {**base["endpoint"], "prach_endpoints": [{"name": "PORT-25"}]}
    bad_endpoint.write_text(yaml.safe_dump(broken), encoding="utf-8")
    with raises(ValueError, match="eaxc_id"):
        load_provision_config(str(bad_endpoint), yaml.safe_load)

    bad_points = tmp_path / "bad_points.yaml"
    bad_points.write_text(
        yaml.safe_dump({**base, "tdd": {"switching_points": [{"direction": "SIDEWAYS", "frame_offset": 1}]}}),
        encoding="utf-8",
    )
    with raises(ValueError, match="switching_points"):
        load_provision_config(str(bad_points), yaml.safe_load)

    partial_pattern = tmp_path / "partial_pattern.yaml"
    partial_pattern.write_text(yaml.safe_dump({**base, "tdd": {"nof_dl_slots": 8}}), encoding="utf-8")
    with raises(ValueError, match="scs_khz"):
        # pattern fields are a complete spec — a partial one fails at load,
        # not on every connect cycle
        load_provision_config(str(partial_pattern), yaml.safe_load)

    incoherent_pattern = tmp_path / "incoherent_pattern.yaml"
    incoherent_pattern.write_text(
        yaml.safe_dump(
            {**base, "tdd": {"scs_khz": 30, "dl_ul_tx_period": 10, "nof_dl_slots": 8, "nof_ul_slots": 5}}
        ),
        encoding="utf-8",
    )
    with raises(ValueError, match="dl_ul_tx_period"):
        load_provision_config(str(incoherent_pattern), yaml.safe_load)


def _sim_args():
    return SimpleNamespace(
        ru_netconf_host=os.getenv("MOCK_RU_HOST", "ocudu-mock-ru"),
        ru_netconf_port=int(os.getenv("MOCK_RU_SSH_PORT", "830")),
        ru_netconf_username=os.getenv("NETCONF_USERNAME", "root"),
        ru_netconf_password=os.getenv("NETCONF_PASSWORD", "root"),
        ru_supervision_interval=60,
        ru_supervision_guard=10,
        ru_datastore="running",
    )


class _NullAlarms:
    """Alarm-manager stand-in for wiring tests."""

    def set_alarm(self, *args, **kwargs):
        pass

    def clear_alarm(self, *args, **kwargs):
        pass


@mark.timeout(240)
def test_provision_on_connect_against_sim(o1_adapter_src, ru_config, mock_ru_ssh_manager):
    """Full flow on the mock RU: provisioning completes each cycle (tracked
    via a completion counter, not inferred from the session phase — the sim
    rejects every watchdog reset, so the session lawfully never claims
    SUPERVISED), leaves the layout in the datastore, keeps carriers INACTIVE
    (the sim exposes no sync), and a recycle genuinely RE-provisions: a leaf
    perturbed between cycles is restored by cycle 2, the only evidence specific
    to this provisioner."""
    from mplane_session import MplaneSession
    from ru_provisioner import RuProvisioner
    from state import AppState

    # Seed the layout first so the INACTIVE write below has carriers to land
    # on: the mock's ru profile seeds none, earlier suites may have left them
    # ACTIVE, and provisioning merges without flipping activation itself.
    ru_config.set_full_config(SYNTHETIC_LAYOUT, skip_activation=True)
    ru_config.set_oran_uplane_carrier_active({"state": "INACTIVE", "nof_tx_carriers": 2, "nof_rx_carriers": 2})

    provisioner = RuProvisioner(
        SYNTHETIC_LAYOUT, sync_timeout_s=1, sync_poll_s=0.5, supervision_interval=60, supervision_guard=10
    )
    provision_done = []
    session = MplaneSession(AppState(), _sim_args(), _NullAlarms(), retry_interval=0.5)
    session.poll_cap = 0.5
    session.register_cycle_handler(lambda rc: (provisioner.provision(rc), provision_done.append(1)))

    perturb_arfcn = """<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
     <user-plane-configuration xmlns="urn:o-ran:uplane-conf:1.0"><tx-array-carriers>
     <name>Tx-Array-Carrier-00</name><absolute-frequency-center>111111</absolute-frequency-center>
     </tx-array-carriers></user-plane-configuration></config>"""

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        try:
            deadline = time.monotonic() + 120
            while len(provision_done) < 1:
                assert time.monotonic() < deadline and not task.done()
                await asyncio.sleep(0.2)
            await asyncio.to_thread(_assert_provisioned_inactive, ru_config)
            # perturb a provisioner-owned leaf, then force a recycle: cycle 2
            # must restore it — proof that re-provisioning really ran
            await asyncio.to_thread(
                mock_ru_ssh_manager.edit_config, target="running", config=perturb_arfcn
            )
            await asyncio.to_thread(session.command_session.close_session)
            deadline = time.monotonic() + 120
            while len(provision_done) < 2:
                assert time.monotonic() < deadline
                assert not task.done(), f"session loop exited early: {task}"
                await asyncio.sleep(0.2)
            await asyncio.to_thread(_assert_provisioned_inactive, ru_config)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=15)

    asyncio.run(scenario())
    assert session.stats["connect_cycles"] >= 2


def _assert_provisioned_inactive(ru_config):
    """Provisioner-specific evidence is present and carriers are inactive."""
    uplane = ru_config.get_uplane_config().get("user-plane-configuration") or {}
    carriers = uplane.get("tx-array-carriers")
    carriers = {c["name"]: c for c in (carriers if isinstance(carriers, list) else [carriers]) if c}
    carrier = carriers["Tx-Array-Carrier-00"]
    assert carrier["absolute-frequency-center"] == "640000", "provisioned RF value must be in place"
    assert carrier["configurable-tdd-pattern"] == "1", "TDD binding provisioned"
    assert carrier["active"] == "INACTIVE", "no activation without sync LOCKED"
    # the interface and carrier leaves above are shared with other suites'
    # layouts; the proof that THIS provisioner ran is the perturbed
    # absolute-frequency-center being restored by the second cycle


HYBRID_LAYOUT = {
    **SYNTHETIC_LAYOUT,
    "interface": {**SYNTHETIC_LAYOUT["interface"], "vlan": 3031},
    "processing": {**SYNTHETIC_LAYOUT["processing"], "vlan": 3031},
}

# what the management plane would create: its own base port and its own VLAN
# interface, under its own names, on the profile's VLAN
_MANAGEMENT_PLANE_INTERFACE = """<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
<interfaces xmlns="urn:ietf:params:xml:ns:yang:ietf-interfaces">
  <interface>
    <name>smo-fronthaul1</name>
    <type xmlns:ianaift="urn:ietf:params:xml:ns:yang:iana-if-type">ianaift:ethernetCsmacd</type>
    <mac-address xmlns="urn:o-ran:interfaces:1.0">aa:bb:cc:dd:ee:01</mac-address>
  </interface>
  <interface>
    <name>smo-vlan3031</name>
    <type xmlns:ianaift="urn:ietf:params:xml:ns:yang:iana-if-type">ianaift:l2vlan</type>
    <enabled>true</enabled>
    <base-interface xmlns="urn:o-ran:interfaces:1.0">smo-fronthaul1</base-interface>
    <vlan-id xmlns="urn:o-ran:interfaces:1.0">3031</vlan-id>
    <mac-address xmlns="urn:o-ran:interfaces:1.0">aa:bb:cc:dd:ee:01</mac-address>
  </interface>
</interfaces>
</config>"""


# the low-level links reference processing-element01, so the element is
# re-pointed to the interface a sudo run would have created for this VLAN
# (fronthaul1 is the sudo template's base port, shared with other tests);
# only then can the management plane's own two interfaces go
_MANAGEMENT_PLANE_REPOINT = """<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
<interfaces xmlns="urn:ietf:params:xml:ns:yang:ietf-interfaces">
  <interface>
    <name>fronthaul1</name>
    <type xmlns:ianaift="urn:ietf:params:xml:ns:yang:iana-if-type">ianaift:ethernetCsmacd</type>
    <mac-address xmlns="urn:o-ran:interfaces:1.0">aa:bb:cc:dd:ee:01</mac-address>
  </interface>
  <interface>
    <name>uc-vlan3031</name>
    <type xmlns:ianaift="urn:ietf:params:xml:ns:yang:iana-if-type">ianaift:l2vlan</type>
    <enabled>true</enabled>
    <base-interface xmlns="urn:o-ran:interfaces:1.0">fronthaul1</base-interface>
    <vlan-id xmlns="urn:o-ran:interfaces:1.0">3031</vlan-id>
    <mac-address xmlns="urn:o-ran:interfaces:1.0">aa:bb:cc:dd:ee:01</mac-address>
  </interface>
</interfaces>
<processing-elements xmlns="urn:o-ran:processing-element:1.0">
  <ru-elements>
    <name>processing-element01</name>
    <transport-flow><interface-name>uc-vlan3031</interface-name></transport-flow>
  </ru-elements>
</processing-elements>
</config>"""
_MANAGEMENT_PLANE_CLEANUP = """<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"
        xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0">
<interfaces xmlns="urn:ietf:params:xml:ns:yang:ietf-interfaces">
  <interface nc:operation="delete"><name>smo-vlan3031</name></interface>
  <interface nc:operation="delete"><name>smo-fronthaul1</name></interface>
</interfaces>
</config>"""


@mark.timeout(240)
def test_hybrid_provisioning_waits_for_the_management_planes_interface_against_sim(
    o1_adapter_src, ru_config, mock_ru_ssh_manager, caplog
):
    """hybrid-odu on the mock RU: with no interface on the profile's VLAN the
    cycle waits instead of pushing a processing element the O-RU would reject
    (a leafref into ietf-interfaces); once a root session — the management
    plane's stand-in — creates one under its own name, provisioning completes
    and the element references that name."""
    from mplane_session import MplaneSession
    from ru_provisioner import RuProvisioner
    from state import AppState

    provisioner = RuProvisioner(
        HYBRID_LAYOUT,
        sync_timeout_s=1,
        sync_poll_s=0.5,
        supervision_interval=60,
        supervision_guard=10,
        interface_timeout_s=60,
    )
    provision_done = []
    args = _sim_args()
    args.ru_role = "hybrid-odu"
    session = MplaneSession(AppState(), args, _NullAlarms(), retry_interval=0.5)
    session.poll_cap = 0.5
    session.register_cycle_handler(lambda rc: (provisioner.provision(rc), provision_done.append(1)))

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        try:
            await asyncio.sleep(4)  # connected and waiting: nothing on VLAN 3031 yet
            assert not provision_done and not task.done()
            await asyncio.to_thread(
                mock_ru_ssh_manager.edit_config, target="running", config=_MANAGEMENT_PLANE_INTERFACE
            )
            deadline = time.monotonic() + 120
            while not provision_done:
                assert time.monotonic() < deadline
                assert not task.done(), f"session loop exited early: {task}"
                await asyncio.sleep(0.2)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=15)

    try:
        with caplog.at_level(logging.INFO):
            asyncio.run(scenario())

        processing = ru_config.get_processing_elements().get("processing-elements") or {}
        elements = processing.get("ru-elements")
        element = next(
            e for e in (elements if isinstance(elements, list) else [elements]) if e["name"] == "processing-element01"
        )
        assert element["transport-flow"]["interface-name"] == "smo-vlan3031"
        assert element["transport-flow"]["eth-flow"]["vlan-id"] == "3031"
        assert not any("rejected a provisioning edit" in r.getMessage() for r in caplog.records)
        assert any("bound to the management plane's interface smo-vlan3031" in r.getMessage() for r in caplog.records)
    finally:
        # leave the mock RU as a sudo run would: re-point the element, then drop
        # the management plane's two interfaces (see _MANAGEMENT_PLANE_REPOINT)
        mock_ru_ssh_manager.edit_config(target="running", config=_MANAGEMENT_PLANE_REPOINT)
        mock_ru_ssh_manager.edit_config(target="running", config=_MANAGEMENT_PLANE_CLEANUP)


@mark.timeout(60)
def test_provision_tolerates_rejected_edit_without_recycling(o1_adapter_src, caplog):
    """An rpc-error reply is alive-but-rejected: a provisioning edit the O-RU
    refuses (unsupported optional leaf, NACM deny, name mismatch) must be
    logged loudly and swallowed — never re-raised. Re-raising recycles the
    session, and the reconnect re-pushes the identical config and re-fails
    forever, starving supervision and the fault/PM bridges. (A transport
    failure still propagates — test_sync_wait_raises_on_dead_session.)"""
    from ru_provisioner import RuProvisioner

    _ = o1_adapter_src
    ru_config = _RecordingRuConfig(sync_states=["LOCKED"])
    ru_config.set_full_config = _raise(_StubRpcError("unknown-element: configurable-tdd-pattern"))

    with caplog.at_level(logging.ERROR):
        RuProvisioner(SYNTHETIC_LAYOUT, sync_timeout_s=7, sync_poll_s=0.01).provision(ru_config)  # must NOT raise

    assert "rejected a provisioning edit" in caplog.text
    assert "configurable-tdd-pattern" in caplog.text
    assert ru_config.calls == [], "rejected before sync/activation; the provisioner stopped, did not recycle"


@mark.timeout(60)
def test_provision_tolerates_rejected_activation_without_recycling(o1_adapter_src, caplog):
    """The same doctrine on the activation edit: a rejected carrier-activation
    is alive-but-rejected and must not recycle the session either."""
    from ru_provisioner import RuProvisioner

    _ = o1_adapter_src
    ru_config = _RecordingRuConfig(sync_states=["LOCKED"])
    ru_config.activate_full_config = _raise(_StubRpcError("access-denied: active"))

    with caplog.at_level(logging.ERROR):
        RuProvisioner(SYNTHETIC_LAYOUT, sync_timeout_s=7, sync_poll_s=0.01).provision(ru_config)  # must NOT raise

    assert "rejected a provisioning edit" in caplog.text
    # base config was applied and sync reached LOCKED before activation was rejected
    assert [name for name, *_ in ru_config.calls] == ["set_full_config"]


@mark.timeout(60)
def test_load_provision_config_rejects_bad_activation_state(tmp_path, o1_adapter_src):
    """activation.state must be the ACTIVE/INACTIVE enum (the CLI enforces it
    via argparse choices; the YAML path must too). Valid values normalise to
    upper-case; a bogus value fails fast at load, not on every connect cycle."""
    from ru_provisioner import load_provision_config

    _ = o1_adapter_src
    base = {key: value for key, value in SYNTHETIC_LAYOUT.items() if key != "activation"}

    lower = tmp_path / "lower.yaml"
    lower.write_text(yaml.safe_dump({**base, "activation": {"state": "inactive"}}), encoding="utf-8")
    assert load_provision_config(str(lower), yaml.safe_load)["activation"]["state"] == "INACTIVE"

    bogus = tmp_path / "bogus.yaml"
    bogus.write_text(yaml.safe_dump({**base, "activation": {"state": "on"}}), encoding="utf-8")
    with raises(ValueError, match="ACTIVE or INACTIVE"):
        load_provision_config(str(bogus), yaml.safe_load)


@mark.timeout(60)
def test_load_provision_config_tolerate_timeout_default_and_optout(tmp_path, o1_adapter_src):
    """Provisioning re-applies activation on every connect cycle and the
    array-carriers readback (not the edit reply) is the receipt, so a missing
    reply on an already-active carrier is tolerated by DEFAULT — otherwise a
    benign re-activation recycles the session. An explicit false opts out."""
    from ru_provisioner import load_provision_config

    _ = o1_adapter_src
    base = {key: value for key, value in SYNTHETIC_LAYOUT.items() if key != "activation"}

    default_yaml = tmp_path / "default.yaml"
    default_yaml.write_text(yaml.safe_dump({**base, "activation": {"state": "ACTIVE"}}), encoding="utf-8")
    assert load_provision_config(str(default_yaml), yaml.safe_load)["activation"]["tolerate_reply_timeout"] is True

    optout_yaml = tmp_path / "optout.yaml"
    optout_yaml.write_text(
        yaml.safe_dump({**base, "activation": {"state": "ACTIVE", "tolerate_reply_timeout": False}}),
        encoding="utf-8",
    )
    assert load_provision_config(str(optout_yaml), yaml.safe_load)["activation"]["tolerate_reply_timeout"] is False


@mark.timeout(60)
def test_load_provision_config_carries_pm_section(tmp_path, o1_adapter_src):
    """The optional pm section (an O-RU's supported measurement-object subset)
    is carried through the loader unchanged and must be a mapping when present;
    which objects an O-RU implements is vendor-specific, so it belongs in the
    profile rather than a generic default set."""
    from ru_provisioner import load_provision_config

    _ = o1_adapter_src
    base = {key: value for key, value in SYNTHETIC_LAYOUT.items() if key != "activation"}

    with_pm = tmp_path / "pm.yaml"
    with_pm.write_text(
        yaml.safe_dump({**base, "pm": {"rx_window_objects": ["RX_ON_TIME", "RX_LATE"]}}), encoding="utf-8"
    )
    assert load_provision_config(str(with_pm), yaml.safe_load)["pm"]["rx_window_objects"] == ["RX_ON_TIME", "RX_LATE"]

    bad_pm = tmp_path / "bad_pm.yaml"
    bad_pm.write_text(yaml.safe_dump({**base, "pm": ["RX_ON_TIME"]}), encoding="utf-8")
    with raises(ValueError, match="pm must be a mapping"):
        load_provision_config(str(bad_pm), yaml.safe_load)


@mark.timeout(60)
def test_load_provision_config_requires_the_leaves_the_templates_render(tmp_path, o1_adapter_src):
    """A missing carrier or interface leaf would render as an empty element the
    O-RU rejects on every cycle; the loader names every missing leaf at once,
    and interface and processing must agree on the VLAN and O-RU MAC address
    the processing element references."""
    from ru_provisioner import load_provision_config

    _ = o1_adapter_src
    base = {key: value for key, value in SYNTHETIC_LAYOUT.items() if key != "activation"}

    no_gain = tmp_path / "no_gain.yaml"
    carrier = {key: value for key, value in base["carrier"].items() if key not in ("tx_gain", "rf_bandwidth_hz")}
    no_gain.write_text(yaml.safe_dump({**base, "carrier": carrier}), encoding="utf-8")
    with raises(ValueError, match="tx_gain, rf_bandwidth_hz"):
        load_provision_config(str(no_gain), yaml.safe_load)

    vlan_mismatch = tmp_path / "vlan.yaml"
    vlan_mismatch.write_text(
        yaml.safe_dump({**base, "processing": {**base["processing"], "vlan": 32}}), encoding="utf-8"
    )
    with raises(ValueError, match="interface.vlan and processing.vlan"):
        load_provision_config(str(vlan_mismatch), yaml.safe_load)

    ports = tmp_path / "ports.yaml"
    ports.write_text(yaml.safe_dump({**base, "endpoint": {**base["endpoint"], "dl_port_id": "0,1"}}), encoding="utf-8")
    with raises(ValueError, match="dl_port_id must be a list of integers"):
        load_provision_config(str(ports), yaml.safe_load)

    index_base = tmp_path / "index_base.yaml"
    index_base.write_text(
        yaml.safe_dump({**base, "endpoint": {**base["endpoint"], "endpoint_index_base": "x"}}), encoding="utf-8"
    )
    with raises(ValueError, match="endpoint naming"):
        load_provision_config(str(index_base), yaml.safe_load)


@mark.timeout(60)
def test_load_provision_config_rejects_a_non_mapping_tdd_section(tmp_path, o1_adapter_src):
    """A tdd section that is not a mapping fails at load, not on every cycle."""
    from ru_provisioner import load_provision_config

    _ = o1_adapter_src
    base = {key: value for key, value in SYNTHETIC_LAYOUT.items() if key != "activation"}
    path = tmp_path / "tdd.yaml"
    path.write_text(yaml.safe_dump({**base, "tdd": "nope"}), encoding="utf-8")
    with raises(ValueError, match="tdd"):
        load_provision_config(str(path), yaml.safe_load)


@mark.timeout(60)
def test_load_provision_config_rejects_string_booleans(tmp_path, o1_adapter_src):
    """A quoted "false" is a truthy string: it would re-enable the carrier
    binding some firmware never answers, so the switches must be YAML booleans."""
    from ru_provisioner import load_provision_config

    _ = o1_adapter_src
    base = {key: value for key, value in SYNTHETIC_LAYOUT.items() if key != "activation"}

    binding = tmp_path / "binding.yaml"
    binding.write_text(yaml.safe_dump({**base, "tdd": {"carrier_binding": "false"}}), encoding="utf-8")
    with raises(ValueError, match="tdd.carrier_binding must be true or false"):
        load_provision_config(str(binding), yaml.safe_load)

    tolerate = tmp_path / "tolerate.yaml"
    tolerate.write_text(
        yaml.safe_dump({**base, "activation": {"state": "ACTIVE", "tolerate_reply_timeout": "false"}}),
        encoding="utf-8",
    )
    with raises(ValueError, match="tolerate_reply_timeout must be true or false"):
        load_provision_config(str(tolerate), yaml.safe_load)

    real_bool = tmp_path / "real_bool.yaml"
    real_bool.write_text(yaml.safe_dump({**base, "tdd": {"carrier_binding": False}}), encoding="utf-8")
    assert load_provision_config(str(real_bool), yaml.safe_load)["tdd"] == {"carrier_binding": False}


@mark.timeout(60)
def test_load_provision_config_tdd_rule_matches_set_full_config(tmp_path, o1_adapter_src):
    """Any tdd key that is not a provisioning switch is pattern content, the
    rule set_full_config applies — so a typo next to tdd_pattern_id fails at
    load instead of raising inside the handler on every cycle, and a quoted
    numeric is a load error rather than a TypeError traceback."""
    from ru_provisioner import load_provision_config

    _ = o1_adapter_src
    base = {key: value for key, value in SYNTHETIC_LAYOUT.items() if key != "activation"}

    typo = tmp_path / "typo.yaml"
    typo.write_text(yaml.safe_dump({**base, "tdd": {"tdd_pattern_id": 1, "scs": 30}}), encoding="utf-8")
    with raises(ValueError, match="scs_khz"):
        load_provision_config(str(typo), yaml.safe_load)

    quoted = tmp_path / "quoted.yaml"
    quoted.write_text(
        yaml.safe_dump({**base, "tdd": {"scs_khz": 30, "dl_ul_tx_period": "10", "nof_dl_slots": 7}}), encoding="utf-8"
    )
    with raises(ValueError, match="tdd"):
        load_provision_config(str(quoted), yaml.safe_load)

    switches_only = tmp_path / "switches.yaml"
    switches_only.write_text(yaml.safe_dump({**base, "tdd": {"tdd_pattern_id": 2, "carrier_binding": False}}), encoding="utf-8")
    assert load_provision_config(str(switches_only), yaml.safe_load)["tdd"] == {"tdd_pattern_id": 2, "carrier_binding": False}


@mark.timeout(60)
def test_load_provision_config_activation_state_defaults(tmp_path, o1_adapter_src):
    """An activation block that only opts out of the reply-timeout tolerance
    keeps the ACTIVE default instead of being rejected for a missing state."""
    from ru_provisioner import load_provision_config

    _ = o1_adapter_src
    base = {key: value for key, value in SYNTHETIC_LAYOUT.items() if key != "activation"}

    optout_only = tmp_path / "optout_only.yaml"
    optout_only.write_text(yaml.safe_dump({**base, "activation": {"tolerate_reply_timeout": False}}), encoding="utf-8")
    assert load_provision_config(str(optout_only), yaml.safe_load)["activation"] == {
        "state": "ACTIVE",
        "tolerate_reply_timeout": False,
    }
