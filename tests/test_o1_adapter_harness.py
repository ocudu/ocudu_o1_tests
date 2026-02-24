#
# Copyright 2021-2026 Software Radio Systems Limited
#
# By using this file, you agree to the terms and conditions set
# forth in the LICENSE file which can be found at the top level of
# the distribution.
#

import time
from pathlib import Path

import pytest
import requests

CONFIG_WAIT_TIMEOUT = 60


def _wait_for_condition(predicate, timeout=CONFIG_WAIT_TIMEOUT, interval=1.0):
    """Poll predicate until it returns a truthy value or timeout expires."""
    end_time = time.time() + timeout
    last_error = None
    while time.time() < end_time:
        try:
            result = predicate()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            result = None
        if result:
            return result
        time.sleep(interval)
    raise AssertionError(f"Timed out waiting for condition: {last_error}")


def _update_ssb_block_power(manager, value: int):
    """Send a NETCONF edit-config to update the SSB block power."""
    manager.edit_config(
        target="running",
        config=f"""
        <config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
          <ManagedElement xmlns="urn:3gpp:sa5:_3gpp-common-managed-element">
            <id>ran1</id>
            <GNBDUFunction xmlns="urn:3gpp:sa5:_3gpp-nr-nrm-gnbdufunction">
              <id>du1</id>
              <NRCellDU xmlns="urn:3gpp:sa5:_3gpp-nr-nrm-nrcelldu">
                <id>nrcelldu1</id>
                <attributes>
                  <srs_nrcelldu_extensions xmlns="urn:srsran-nrcelldu-extension:1.0">
                    <srs_nrcelldu_ssb_extensions>
                      <ssb_block_power_dbm>{value}</ssb_block_power_dbm>
                    </srs_nrcelldu_ssb_extensions>
                  </srs_nrcelldu_extensions>
                </attributes>
              </NRCellDU>
            </GNBDUFunction>
          </ManagedElement>
        </config>
        """,
    )


def _update_cell_pci(manager, value: int):
    """Send a NETCONF edit-config to update the NR PCI (nRPCI)."""
    manager.edit_config(
        target="running",
        config=f"""
        <config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
          <ManagedElement xmlns="urn:3gpp:sa5:_3gpp-common-managed-element">
            <id>ran1</id>
            <GNBDUFunction xmlns="urn:3gpp:sa5:_3gpp-nr-nrm-gnbdufunction">
              <id>du1</id>
              <NRCellDU xmlns="urn:3gpp:sa5:_3gpp-nr-nrm-nrcelldu">
                <id>nrcelldu1</id>
                <attributes>
                  <nRPCI>{value}</nRPCI>
                </attributes>
              </NRCellDU>
            </GNBDUFunction>
          </ManagedElement>
        </config>
        """,
    )


def _update_rrm_policy_min_ratio(manager, value: int):
    """Send a NETCONF edit-config to update the RRM policy min ratio."""
    manager.edit_config(
        target="running",
        config=f"""
        <config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
          <RRMPolicyRatio xmlns="urn:3gpp:sa5:_3gpp-nr-nrm-rrmpolicy">
            <id>rrm_policy1</id>
            <attributes>
              <rRMPolicyMinRatio>{value}</rRMPolicyMinRatio>
            </attributes>
          </RRMPolicyRatio>
        </config>
        """,
    )


@pytest.mark.timeout(180)
def test_initial_configuration_written(config_path: Path, load_config):
    """Ensure the adapter writes the initial configuration file."""
    config = load_config()
    assert "cells" in config, "cells section not found in config"
    assert config["cells"], "no cells defined in config"
    first_cell = config["cells"][0]
    assert "ssb" in first_cell, "ssb section missing in first cell"
    assert "ssb_block_power_dbm" in first_cell["ssb"], "ssb block power missing"


@pytest.mark.timeout(240)
def test_runtime_ssb_update_sends_ws_command(
    netconf_manager, load_config, o1_adapter_base_url
):
    """Runtime SSB power change should update config without requiring a restart."""
    session = requests.Session()
    config = load_config()
    initial_value = int(config["cells"][0]["ssb"]["ssb_block_power_dbm"])
    new_value = initial_value - 3 if initial_value > -20 else initial_value + 3

    try:
        _update_ssb_block_power(netconf_manager, new_value)

        def _ssb_updated():
            current = int(load_config()["cells"][0]["ssb"]["ssb_block_power_dbm"])
            return current == new_value

        _wait_for_condition(_ssb_updated, timeout=90)

        response = _wait_for_condition(
            lambda: session.get(f"{o1_adapter_base_url}/config-healthy", timeout=5),
            timeout=30,
        )
        assert response.status_code == 200, response.text
    finally:
        if new_value != initial_value:
            _update_ssb_block_power(netconf_manager, initial_value)
            _wait_for_condition(
                lambda: int(load_config()["cells"][0]["ssb"]["ssb_block_power_dbm"])
                == initial_value,
                timeout=90,
            )


@pytest.mark.timeout(240)
def test_non_runtime_change_triggers_restart_request(
    netconf_manager, load_config, o1_adapter_base_url
):
    """Changing non-runtime parameters should request a restart and update config."""
    session = requests.Session()
    config = load_config()
    initial_pci = int(config["cells"][0]["pci"])
    new_pci = (initial_pci + 1) % 504  # keep within valid PCI range

    try:
        _update_cell_pci(netconf_manager, new_pci)

        def _pci_updated():
            current = int(load_config()["cells"][0]["pci"])
            return current == new_pci

        _wait_for_condition(_pci_updated, timeout=90)

        def _restart_requested():
            resp = session.get(f"{o1_adapter_base_url}/config-healthy", timeout=5)
            if resp.status_code == 400:
                return {"response": resp}
            return None

        response_payload = _wait_for_condition(_restart_requested, timeout=90)
        response = response_payload["response"]
        assert response.status_code == 400
    finally:
        if new_pci != initial_pci:
            _update_cell_pci(netconf_manager, initial_pci)
            _wait_for_condition(
                lambda: int(load_config()["cells"][0]["pci"]) == initial_pci,
                timeout=90,
            )
        # Clear restart flag for subsequent tests
        session.post(f"{o1_adapter_base_url}/restarted", timeout=5)
        # Ensure adapter returns to healthy state
        _wait_for_condition(
            lambda: session.get(
                f"{o1_adapter_base_url}/config-healthy", timeout=5
            ).status_code
            == 200,
            timeout=60,
        )


@pytest.mark.timeout(240)
def test_rrm_policy_ratio_update_sends_ws_command(
    netconf_manager,
    load_config,
    o1_adapter_base_url,
    ws_event_log_path,
    read_ws_events,
):
    """Changing RRM policy ratios should emit a WS command without requiring a restart."""

    if ws_event_log_path.exists():
        ws_event_log_path.unlink()

    session = requests.Session()
    config = load_config()
    slicing_cfg = config["cell_cfg"]["slicing"][0]
    initial_ratio = int(slicing_cfg["sched_cfg"]["min_prb_policy_ratio"])
    new_ratio = initial_ratio - 5 if initial_ratio > 10 else initial_ratio + 5

    try:
        _update_rrm_policy_min_ratio(netconf_manager, new_ratio)

        def _rrm_event_received():
            events = [
                evt
                for evt in read_ws_events()
                if evt.get("cmd") == "rrm_policy_ratio_set"
            ]
            for event in reversed(events):
                policies = event.get("policies")
                candidate = None
                if isinstance(policies, dict):
                    candidate = policies.get("min_prb_policy_ratio")
                elif isinstance(policies, list) and policies:
                    first = policies[0]
                    if isinstance(first, dict):
                        candidate = first.get("min_prb_policy_ratio")
                try:
                    if int(candidate) == new_ratio:
                        return event
                except (TypeError, ValueError):
                    continue
            return None

        # validates that adapter sent a runtime WS command
        _wait_for_condition(_rrm_event_received, timeout=90)

        # validates config-file propagation
        _wait_for_condition(
            lambda: int(
                load_config()["cell_cfg"]["slicing"][0]["sched_cfg"][
                    "min_prb_policy_ratio"
                ]
            )
            == new_ratio,
            timeout=90,
        )

        health_resp = session.get(f"{o1_adapter_base_url}/config-healthy", timeout=5)
        assert health_resp.status_code == 200
    finally:
        if new_ratio != initial_ratio:
            _update_rrm_policy_min_ratio(netconf_manager, initial_ratio)
            _wait_for_condition(
                lambda: int(
                    load_config()["cell_cfg"]["slicing"][0]["sched_cfg"][
                        "min_prb_policy_ratio"
                    ]
                )
                == initial_ratio,
                timeout=90,
            )
        _wait_for_condition(
            lambda: session.get(
                f"{o1_adapter_base_url}/config-healthy", timeout=5
            ).status_code
            == 200,
            timeout=60,
        )
