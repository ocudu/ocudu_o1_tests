# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

from pathlib import Path

import pytest


@pytest.mark.timeout(180)
def test_initial_configuration_written(config_path: Path, load_config):
    """Ensure the adapter writes the initial CU configuration file."""
    config = load_config()
    assert "cu_cp" in config, "cu_cp section not found in config"
    assert "cu_up" in config, "cu_up section not found in config"
    assert "amf" in config["cu_cp"], "amf section missing under cu_cp"
    assert "f1ap" in config["cu_cp"], "f1ap section missing under cu_cp"


@pytest.mark.timeout(240)
def test_rendered_configuration_accepts_dryrun(dryrun_result):
    """Ensure the rendered config can be loaded by the component in dry-run mode."""
    assert dryrun_result["status_code"] == 0, (
        f"dry-run failed:\n{dryrun_result['logs']}"
    )
