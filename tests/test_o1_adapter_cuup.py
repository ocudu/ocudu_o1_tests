# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

from pathlib import Path
from pytest import mark


@mark.timeout(180)
def test_initial_configuration_written(config_path: Path, load_config):
    """Ensure the adapter writes the initial CU-UP configuration file."""
    config = load_config()
    assert "cu_up" in config, "cu_up section not found in config"
    assert "e1ap" in config["cu_up"], "e1ap section missing under cu_up"
    assert "ngu" in config["cu_up"], "ngu section missing under cu_up"
    assert "f1u" in config["cu_up"], "f1u section missing under cu_up"


@mark.timeout(240)
def test_rendered_configuration_accepts_dryrun(dryrun_result):
    """Ensure the rendered config can be loaded by the component in dry-run mode."""
    assert dryrun_result["status_code"] == 0, (
        f"dry-run failed:\n{dryrun_result['logs']}"
    )


@getattr(mark, "MVP-SEC-O-CU-07")
@mark.timeout(60)
def test_netconf_over_tls_rfc_7589(tls_netconf_manager):
    """Connect over mutual TLS and verify the running config is fetchable."""
    reply = tls_netconf_manager.get_config(source="running")
    xml = reply.data_xml
    assert xml and "<" in xml
