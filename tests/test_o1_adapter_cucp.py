# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import logging
from pathlib import Path
from pytest import mark

logger = logging.getLogger(__name__)

@getattr(mark, "MVP-ARCH-INTF-8")
@mark.timeout(180)
def test_initial_configuration_written(config_path: Path, load_config):
    logger.info("Ensure the adapter writes the initial CU-CP configuration file.")
    config = load_config()
    assert "cu_cp" in config, "cu_cp section not found in config"
    assert "amf" in config["cu_cp"], "amf section missing under cu_cp"
    assert "e1ap" in config["cu_cp"], "e1ap section missing under cu_cp"
    assert "f1ap" in config["cu_cp"], "f1ap section missing under cu_cp"

@getattr(mark, "MVP-ARCH-INTF-8")
@mark.timeout(240)
def test_rendered_configuration_accepts_dryrun(dryrun_result):
    logger.info("Ensure the rendered config can be loaded by the component in dry-run mode.")
    assert dryrun_result["status_code"] == 0, (
        f"dry-run failed:\n{dryrun_result['logs']}"
    )

@getattr(mark, "MVP-ARCH-INTF-8")
@getattr(mark, "MVP-SEC-O-CU-03")
@getattr(mark, "MVP-SEC-O-CU-05")
@getattr(mark, "MVP-SEC-O-CU-06")
@mark.timeout(60)
def test_netconf_over_tls_rfc_7589(tls_netconf_manager):
    logger.info("Connect to the O1 NETCONF endpoint over mutual TLS and verify the running config is fetchable.")
    reply = tls_netconf_manager.get_config(source="running")
    xml = reply.data_xml
    assert xml and "<" in xml
