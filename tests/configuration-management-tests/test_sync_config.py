# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""o-ran-sync configuration tests for the M-plane client.

The mock RU's ptp-config is real writable configuration (seeded with clock
classes 6/7/135), so sync writes are exercised as live roundtrips. The
config-false status trees are never populated by the sim, so the status read
is asserted as graceful absence.
"""

import logging

from ncclient.operations import RPCError
from pytest import mark, raises

logger = logging.getLogger(__name__)

_RESTORE_SYNC = """<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
 <sync xmlns="urn:o-ran:sync:1.0"><ptp-config xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0">
 <accepted-clock-classes nc:operation="delete"><clock-classes>141</clock-classes></accepted-clock-classes>
 <domain-number nc:operation="delete"/><ptp-profile nc:operation="delete"/></ptp-config></sync></config>"""


def _as_list(node):
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


@mark.timeout(60)
def test_sync_config_roundtrip(ru_config, mock_ru_ssh_manager):
    """Writable ptp-config fields land in the RU running config and restore cleanly."""
    try:
        ru_config.set_oran_sync_config(accepted_clock_classes=[141], domain_number=45, ptp_profile="G_8275_2")
        ptp = (ru_config.get_oran_sync().get("sync") or {}).get("ptp-config") or {}
        classes = {entry["clock-classes"] for entry in _as_list(ptp.get("accepted-clock-classes"))}
        assert "141" in classes and {"6", "7", "135"} <= classes
        assert ptp.get("domain-number") == "45"
        assert ptp.get("ptp-profile") == "G_8275_2"
    finally:
        mock_ru_ssh_manager.edit_config(target="running", config=_RESTORE_SYNC)

    ptp = (ru_config.get_oran_sync().get("sync") or {}).get("ptp-config") or {}
    assert {entry["clock-classes"] for entry in _as_list(ptp.get("accepted-clock-classes"))} == {"6", "7", "135"}


@mark.timeout(60)
def test_sync_config_validation(ru_config):
    """Bad ptp-profile fails client-side; out-of-range domain is rejected by the RU."""
    with raises(ValueError):
        ru_config.set_oran_sync_config(ptp_profile="G_8275_9")
    with raises(RPCError):
        ru_config.set_oran_sync_config(domain_number=300)  # uint8 overflow


@mark.timeout(60)
def test_sync_status_graceful_absence(ru_config):
    """The sim populates no sync-status: the read degrades to None fields."""
    assert ru_config.get_sync_status() == {
        "sync_state": None,
        "time_error": None,
        "frequency_error": None,
        "supported_reference_types": [],
    }
