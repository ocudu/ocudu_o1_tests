# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

from pathlib import Path
from pytest import mark


@mark.timeout(180)
def test_initial_configuration_written(config_path: Path, load_config):
    """Ensure the adapter writes the initial CU configuration file."""
    config = load_config()
    assert "cu_cp" in config, "cu_cp section not found in config"
    assert "cu_up" in config, "cu_up section not found in config"
    assert "amf" in config["cu_cp"], "amf section missing under cu_cp"
    assert "f1ap" in config["cu_cp"], "f1ap section missing under cu_cp"


@mark.timeout(240)
def test_rendered_configuration_accepts_dryrun(dryrun_result):
    """Ensure the rendered config can be loaded by the component in dry-run mode."""
    assert dryrun_result["status_code"] == 0, (
        f"dry-run failed:\n{dryrun_result['logs']}"
    )


@getattr(mark, "MVP-SEC-O-CU-05")
@getattr(mark, "MVP-SEC-O-CU-06")
@getattr(mark, "MVP-SEC-O-CU-07")
@getattr(mark, "MVP-SEC-O-CU-08")
@mark.timeout(60)
def test_netconf_over_tls_rfc_7589(tls_netconf_manager):
    """Connect to the O1 NETCONF endpoint over mutual TLS and verify the running config is fetchable.

    The combined CU runs CU-CP and CU-UP in the same component, so this single
    mTLS test covers the O1 security requirements for both halves.
    """
    reply = tls_netconf_manager.get_config(source="running")
    xml = reply.data_xml
    assert xml and "<" in xml
