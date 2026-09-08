# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""RU performance management tests for the M-plane client.

Requires the ru profile to load o-ran-performance-management; the pm_module
fixture asserts the mock RU advertises it, so a mock that drops the module
fails the suite rather than skipping it. The sim stores the activation config
but produces no measurement results — result reporting is exercised against
real hardware or a future PM state simulator.
"""

import logging
import re

from ncclient.operations import rpc as rpc_ops
from pytest import fixture, mark

logger = logging.getLogger(__name__)

_DELETE_PM = """<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
 <performance-measurement-objects xmlns="urn:o-ran:performance-management:1.0"
  xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0" nc:operation="delete"/></config>"""


def _as_list(node):
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


@fixture(scope="session")
def pm_module(ru_config, o1_adapter_src):
    """The RU's o-ran-performance-management capability entry (revision, features)."""
    from ofh_config_builder import parse_module_capabilities

    capabilities = parse_module_capabilities(ru_config.get_yang_library().get("yang-library", {}))
    assert "o-ran-performance-management" in capabilities, (
        "mock RU does not advertise o-ran-performance-management: the ru profile must load it"
    )
    return capabilities["o-ran-performance-management"]


@mark.timeout(90)
def test_pm_activation_roundtrip(ru_config, mock_ru_ssh_manager, pm_module):
    """Default activation lands the rx-window counters in the RU running config."""
    try:
        ru_config.set_oran_perf_measurement({"rx_window_interval": 30})
        pm = ru_config.get_perf_measurement_config().get("performance-measurement-objects", {})
        assert pm.get("rx-window-measurement-interval") == "30"
        assert pm.get("notification-interval") == "60"
        assert pm.get("enable-file-upload") == "false"

        # the client's own readback uses with-defaults report-all, so unwritten
        # defaults appear there; "never written" is asserted in explicit mode
        import xmltodict

        raw = mock_ru_ssh_manager.get_config(
            source="running",
            filter=("subtree", '<performance-measurement-objects xmlns="urn:o-ran:performance-management:1.0"/>'),
        )
        explicit = xmltodict.parse(
            raw.xml,
            process_namespaces=True,
            namespaces={"urn:o-ran:performance-management:1.0": None, "urn:ietf:params:xml:ns:netconf:base:1.0": None},
        )
        explicit_pm = (explicit.get("rpc-reply", {}).get("data") or {}).get("performance-measurement-objects") or {}
        assert "enable-SFTP-upload" not in explicit_pm, "deprecated leaves must no longer be written"

        objects = {entry["measurement-object"]: entry for entry in _as_list(pm.get("rx-window-measurement-objects"))}
        assert set(objects) == {
            "RX_ON_TIME", "RX_EARLY", "RX_LATE", "RX_CORRUPT", "RX_DUPL", "RX_TOTAL",
            "RX_ON_TIME_C", "RX_EARLY_C", "RX_LATE_C",
        }
        assert all(entry["active"] == "true" and entry["object-unit"] == "RU" for entry in objects.values())
    finally:
        mock_ru_ssh_manager.edit_config(target="running", config=_DELETE_PM)
    # after cleanup only YANG defaults may remain in the report-all view
    residue = ru_config.get_perf_measurement_config().get("performance-measurement-objects") or {}
    assert "rx-window-measurement-objects" not in residue
    assert "rx-window-measurement-interval" not in residue
    assert "notification-interval" not in residue


@mark.timeout(60)
def test_pm_custom_object_subset(ru_config, mock_ru_ssh_manager, pm_module):
    """A caller-provided object subset is honoured verbatim."""
    try:
        ru_config.set_oran_perf_measurement({"rx_window_objects": ["RX_TOTAL", "RX_LATE"]})
        pm = ru_config.get_perf_measurement_config().get("performance-measurement-objects", {})
        objects = {entry["measurement-object"] for entry in _as_list(pm.get("rx-window-measurement-objects"))}
        assert objects == {"RX_TOTAL", "RX_LATE"}
    finally:
        mock_ru_ssh_manager.edit_config(target="running", config=_DELETE_PM)


@mark.timeout(60)
def test_pm_invalid_object_rejected(ru_config, pm_module):
    """An unknown measurement-object enum is rejected by the RU's schema."""
    from ncclient.operations import RPCError
    from pytest import raises

    with raises(RPCError):
        ru_config.set_oran_perf_measurement({"rx_window_objects": ["RX_BANANAS"]})


@mark.timeout(60)
def test_pm_two_phase_sequencing(o1_adapter_src):
    """Measurement objects are configured while inactive, then activated in a
    separate edit-config, so parameter changes never coincide with an active
    measurement. Client-side sequencing check — no RU needed."""
    from ru_config import RuConfig

    controller = RuConfig(None, "running")
    sent = []
    controller.edit_config = lambda xml_request, description="": sent.append(xml_request)

    controller.set_oran_perf_measurement({"rx_window_objects": ["RX_TOTAL"], "rx_window_interval": 30})

    assert len(sent) == 2, "expected configure + activate edit-configs"
    configure, activate = sent
    assert "<active>false</active>" in configure
    assert "<rx-window-measurement-interval>30</rx-window-measurement-interval>" in configure
    assert "<report-info>COUNT</report-info>" in configure
    assert "<active>true</active>" in activate
    assert "<measurement-object>RX_TOTAL</measurement-object>" in activate
    assert "report-info" not in activate, "activation must not re-send object parameters"
    assert "measurement-interval" not in activate


class _StubRpcError(rpc_ops.RPCError):
    """RPCError stand-in that skips the base class's rpc-reply XML parsing."""

    def __init__(self, message):  # pylint: disable=super-init-not-called
        Exception.__init__(self, message)


def _reject_objects(unsupported):
    """An edit_config stub that rpc-errors any edit naming an unsupported object.

    Mirrors how a partial-support O-RU rejects the measurement edit — it names
    the offending object in the error ("RX_CORRUPT measurement object not
    supported"), one at a time.
    """

    def _edit(xml_request, description=""):  # noqa: ARG001 - matches edit_config's signature
        for obj in unsupported:
            if re.search(rf"\b{obj}\b", xml_request):
                raise _StubRpcError(f"{obj} measurement object not supported")

    return _edit


@mark.timeout(60)
def test_configure_perf_measurement_degrades_past_unsupported(o1_adapter_src, caplog):
    """Generic degrade path: drop whatever measurement object the O-RU rejects
    and retry, converging on its supported subset — no per-RU list needed."""
    from ru_config import RuConfig

    _ = o1_adapter_src
    controller = RuConfig(None, "running")
    controller.edit_config = _reject_objects(["RX_DUPL", "RX_CORRUPT"])

    with caplog.at_level(logging.INFO):
        active = controller.configure_perf_measurement()

    assert "RX_DUPL" not in active and "RX_CORRUPT" not in active
    assert "RX_ON_TIME" in active and "RX_TOTAL" in active
    assert "retrying without them" in caplog.text


@mark.timeout(60)
def test_configure_perf_measurement_word_boundary(o1_adapter_src):
    """Rejecting the _C variant must not also drop the base object — whole-word
    match, since '_' is a word character."""
    from ru_config import RuConfig

    _ = o1_adapter_src
    controller = RuConfig(None, "running")
    controller.edit_config = _reject_objects(["RX_ON_TIME_C"])

    active = controller.configure_perf_measurement({"rx_window_objects": ["RX_ON_TIME", "RX_ON_TIME_C"]})
    assert active == ["RX_ON_TIME"]


@mark.timeout(60)
def test_configure_perf_measurement_all_unsupported(o1_adapter_src):
    """If the O-RU supports none of the objects, PM ends inactive (empty list),
    not an infinite loop."""
    from ru_config import RuConfig

    _ = o1_adapter_src
    controller = RuConfig(None, "running")
    controller.edit_config = _reject_objects(["RX_ON_TIME", "RX_TOTAL"])

    assert controller.configure_perf_measurement({"rx_window_objects": ["RX_ON_TIME", "RX_TOTAL"]}) == []


@mark.timeout(60)
def test_configure_perf_measurement_propagates_non_object_error(o1_adapter_src):
    """An rpc-error that names no configured object is not an object-support
    problem — it propagates instead of spinning on object drops."""
    from ncclient.operations import RPCError
    from pytest import raises
    from ru_config import RuConfig

    _ = o1_adapter_src
    controller = RuConfig(None, "running")

    def _deny(xml_request, description=""):  # noqa: ARG001
        raise _StubRpcError("access-denied by NACM")

    controller.edit_config = _deny
    with raises(RPCError):
        controller.configure_perf_measurement({"rx_window_objects": ["RX_ON_TIME"]})
