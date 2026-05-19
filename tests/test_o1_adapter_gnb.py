# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

from pathlib import Path
from pytest import mark

import requests


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
                  <ocudu_nrcelldu_extensions xmlns="urn:ocudu-nrcelldu-extension:1.0">
                    <ocudu_nrcelldu_ssb_extensions>
                      <ssb_block_power_dbm>{value}</ssb_block_power_dbm>
                    </ocudu_nrcelldu_ssb_extensions>
                  </ocudu_nrcelldu_extensions>
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
          <ManagedElement xmlns="urn:3gpp:sa5:_3gpp-common-managed-element">
            <id>ran1</id>
            <GNBDUFunction xmlns="urn:3gpp:sa5:_3gpp-nr-nrm-gnbdufunction">
              <id>du1</id>
              <NRCellDU xmlns="urn:3gpp:sa5:_3gpp-nr-nrm-nrcelldu">
                <id>nrcelldu1</id>
                <RRMPolicyRatio xmlns="urn:3gpp:sa5:_3gpp-nr-nrm-rrmpolicy">
                  <id>rrm_policy1</id>
                  <attributes>
                    <rRMPolicyMinRatio>{value}</rRMPolicyMinRatio>
                  </attributes>
                </RRMPolicyRatio>
              </NRCellDU>
            </GNBDUFunction>
          </ManagedElement>
        </config>
        """,
    )


@mark.timeout(180)
def test_initial_configuration_written(config_path: Path, load_config):
    """Ensure the adapter writes the initial configuration file."""
    config = load_config()
    assert "cells" in config, "cells section not found in config"
    assert config["cells"], "no cells defined in config"
    first_cell = config["cells"][0]
    assert "ssb" in first_cell, "ssb section missing in first cell"
    assert "ssb_block_power_dbm" in first_cell["ssb"], "ssb block power missing"


@mark.timeout(240)
def test_rendered_configuration_accepts_dryrun(dryrun_result):
    """Ensure the rendered config can be loaded by the component in dry-run mode."""
    assert dryrun_result["status_code"] == 0, (
        f"dry-run failed:\n{dryrun_result['logs']}"
    )


@mark.timeout(60)
def test_netconf_over_tls_rfc_7589(tls_netconf_manager):
    """Connect over mutual TLS and verify the running config is fetchable."""
    reply = tls_netconf_manager.get_config(source="running")
    xml = reply.data_xml
    assert xml and "<" in xml


@mark.timeout(240)
def test_runtime_ssb_update_sends_ws_command(
    netconf_manager, load_config, o1_adapter_base_url, wait_for
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

        wait_for(_ssb_updated, timeout=90)

        response = wait_for(
            lambda: session.get(f"{o1_adapter_base_url}/config-healthy", timeout=5),
            timeout=30,
        )
        assert response.status_code == 200, response.text
    finally:
        if new_value != initial_value:
            _update_ssb_block_power(netconf_manager, initial_value)
            wait_for(
                lambda: int(load_config()["cells"][0]["ssb"]["ssb_block_power_dbm"])
                == initial_value,
                timeout=90,
            )


@mark.timeout(240)
def test_non_runtime_change_triggers_restart_request(
    netconf_manager, load_config, o1_adapter_base_url, wait_for
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

        wait_for(_pci_updated, timeout=90)

        def _restart_requested():
            resp = session.get(f"{o1_adapter_base_url}/config-healthy", timeout=5)
            if resp.status_code == 400:
                return {"response": resp}
            return None

        response_payload = wait_for(_restart_requested, timeout=90)
        response = response_payload["response"]
        assert response.status_code == 400
    finally:
        if new_pci != initial_pci:
            _update_cell_pci(netconf_manager, initial_pci)
            wait_for(
                lambda: int(load_config()["cells"][0]["pci"]) == initial_pci,
                timeout=90,
            )
        # Clear restart flag for subsequent tests
        session.post(f"{o1_adapter_base_url}/restarted", timeout=5)
        # Ensure adapter returns to healthy state
        wait_for(
            lambda: session.get(
                f"{o1_adapter_base_url}/config-healthy", timeout=5
            ).status_code
            == 200,
            timeout=60,
        )


@mark.timeout(240)
def test_rrm_policy_ratio_update_sends_ws_command(
    netconf_manager,
    load_config,
    o1_adapter_base_url,
    ws_event_log_path,
    read_ws_events,
    wait_for,
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
        wait_for(_rrm_event_received, timeout=90)

        # validates config-file propagation
        wait_for(
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
            wait_for(
                lambda: int(
                    load_config()["cell_cfg"]["slicing"][0]["sched_cfg"][
                        "min_prb_policy_ratio"
                    ]
                )
                == initial_ratio,
                timeout=90,
            )
        wait_for(
            lambda: session.get(
                f"{o1_adapter_base_url}/config-healthy", timeout=5
            ).status_code
            == 200,
            timeout=60,
        )


def _report_config_value(cfg: dict, report_cfg_id: int, leaf: str):
    """Return the value of `leaf` on the report_configs entry with id `report_cfg_id`, or None."""
    for rc in cfg.get("cu_cp", {}).get("mobility", {}).get("report_configs", []) or []:
        if rc.get("report_cfg_id") == report_cfg_id:
            return rc.get(leaf)
    return None


@getattr(mark, "MVP-FUNC-SMO-17-1-a")
@mark.timeout(240)
def test_smo_closed_loop_changes_ho_threshold(run_smo_closed_loop):
    """SMO closed-loop sets meas_trigger_quantity_threshold_db to -20 on
    report_configs[report_cfg_id=3]. Non-runtime parameter — adapter requests a full restart."""
    run_smo_closed_loop(
        payload_file="set_ho_threshold.xml",
        runtime=False,
        check_applied=lambda c: _report_config_value(c, 3, "meas_trigger_quantity_threshold_db") == -20,
    )


@getattr(mark, "MVP-FUNC-SMO-17-1-b")
@mark.timeout(240)
def test_smo_closed_loop_changes_pci(run_smo_closed_loop):
    """SMO closed-loop sets NRCellDU/nRPCI to 200. Non-runtime parameter — adapter requests a full restart."""
    run_smo_closed_loop(
        payload_file="set_nrpci.xml",
        runtime=False,
        check_applied=lambda c: int(c["cells"][0]["pci"]) == 200,
    )


@getattr(mark, "MVP-FUNC-SMO-17-1-c")
@mark.timeout(240)
def test_smo_closed_loop_changes_ho_hysteresis(run_smo_closed_loop):
    """SMO closed-loop sets hysteresis_db to 5 on report_configs[report_cfg_id=2].
    Non-runtime parameter — adapter requests a full restart."""
    run_smo_closed_loop(
        payload_file="set_hysteresis.xml",
        runtime=False,
        check_applied=lambda c: _report_config_value(c, 2, "hysteresis_db") == 5,
    )


@getattr(mark, "MVP-FUNC-SMO-17-1-d")
@mark.timeout(240)
def test_smo_closed_loop_changes_time_to_trigger(run_smo_closed_loop):
    """SMO closed-loop sets time_to_trigger_ms to 256 on report_configs[report_cfg_id=2].
    Non-runtime parameter — adapter requests a full restart."""
    run_smo_closed_loop(
        payload_file="set_time_to_trigger.xml",
        runtime=False,
        check_applied=lambda c: _report_config_value(c, 2, "time_to_trigger_ms") == 256,
    )


@getattr(mark, "MVP-FUNC-SMO-17-1-e")
@mark.timeout(240)
def test_smo_closed_loop_changes_ssb_tx_power(run_smo_closed_loop):
    """SMO closed-loop sets ssb_block_power_dbm to -10. Runtime-updatable leaf:
    adapter applies in place without restart."""
    run_smo_closed_loop(
        payload_file="set_ssb_block_power.xml",
        runtime=True,
        check_applied=lambda c: int(c["cells"][0]["ssb"]["ssb_block_power_dbm"]) == -10,
    )


@getattr(mark, "MVP-FUNC-SMO-17-1-f")
@mark.timeout(240)
def test_smo_closed_loop_changes_preamble_format(run_smo_closed_loop):
    """SMO closed-loop sets prach_config_index to 100. Non-runtime parameter — adapter requests a full restart."""
    run_smo_closed_loop(
        payload_file="set_prach_config_index.xml",
        runtime=False,
        check_applied=lambda c: int(c["cells"][0]["prach"]["prach_config_index"]) == 100,
    )