# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""The adapter's entry point around the M-plane session: the /ru/status and
/ru/ready routes a probe reads, and passwords read from files instead of the
command line."""

import argparse
import subprocess
import sys

from pytest import fixture, mark, raises


@fixture(scope="module")
def ru_routes(o1_adapter_src):
    """The adapter's Flask app with the /ru routes of a provisioning session.
    The routes register on the module-level app, so once per test module."""
    import o1_adapter
    from state import AppState

    _ = o1_adapter_src
    app_state = AppState()
    o1_adapter.configure_app(app_state, ru_supervised=True, ru_provisioning=True)
    return app_state, o1_adapter.app.test_client()


@mark.timeout(60)
def test_ru_ready_follows_the_session_and_the_cycle(ru_routes):
    """503 until the session is SUPERVISED and the cycle is ACTIVE, 200 then;
    /ru/status answers 200 with the same body throughout."""
    from ru_provisioner import PHASE_ACTIVE, PHASE_SYNC_NOT_LOCKED

    app_state, client = ru_routes
    app_state.session_state["ru_mplane_state"] = "CONNECTING"
    response = client.get("/ru/ready")
    assert response.status_code == 503
    assert response.get_json() == {
        "session": "CONNECTING",
        "provisioning": {"phase": "NOT_PROVISIONED", "sync_state": None, "carriers": {}, "updated_at": None},
        "ready": False,
    }

    app_state.session_state["ru_mplane_state"] = "SUPERVISED"
    app_state.ru_provision = {"phase": PHASE_SYNC_NOT_LOCKED, "sync_state": "FREERUN", "carriers": {}, "updated_at": "t"}
    assert client.get("/ru/ready").status_code == 503

    app_state.ru_provision = {"phase": PHASE_ACTIVE, "sync_state": "LOCKED", "carriers": {"c": "READY"}, "updated_at": "t"}
    response = client.get("/ru/ready")
    assert response.status_code == 200 and response.get_json()["ready"] is True
    status = client.get("/ru/status")
    assert status.status_code == 200 and status.get_json() == response.get_json()

    app_state.session_state["ru_mplane_state"] = "DEGRADED"
    assert client.get("/ru/ready").status_code == 503
    assert client.get("/ru/status").status_code == 200
    assert client.get("/status").get_json() == {"success": "OK"}


def _parser():
    parser = argparse.ArgumentParser()
    for option, default in (("netconf_password", "root"), ("ru_netconf_password", ""), ("ves_password", "sample1")):
        parser.add_argument(f"--{option}", default=default)
        parser.add_argument(f"--{option}_file", default=None)
    return parser


@mark.timeout(60)
def test_password_files_replace_the_options(o1_adapter_src, tmp_path):
    """Each *_password_file holds its password; trailing line breaks (a file
    written by echo ends in one) are not part of it, other whitespace is."""
    from o1_adapter import read_password_files

    _ = o1_adapter_src
    ru_secret = tmp_path / "ru"
    ru_secret.write_text("s3cret pass \n", encoding="utf-8")
    ves_secret = tmp_path / "ves"
    ves_secret.write_text("v", encoding="utf-8")
    parser = _parser()
    args = parser.parse_args(["--ru_netconf_password_file", str(ru_secret), "--ves_password_file", str(ves_secret)])
    read_password_files(parser, args)
    assert (args.ru_netconf_password, args.ves_password, args.netconf_password) == ("s3cret pass ", "v", "root")


@mark.timeout(60)
def test_password_file_usage_errors(o1_adapter_src, tmp_path):
    """Both forms of one password, a missing file or an empty one are usage
    errors at startup, in-process and from the command line."""
    from o1_adapter import read_password_files

    empty = tmp_path / "empty"
    empty.write_text("\n", encoding="utf-8")
    secret = tmp_path / "secret"
    secret.write_text("p", encoding="utf-8")
    cases = [
        (["--ru_netconf_password", "p", "--ru_netconf_password_file", str(secret)], "cannot be combined"),
        (["--ru_netconf_password_file", str(tmp_path / "absent")], "No such file"),
        (["--ru_netconf_password_file", str(empty)], "is empty"),
    ]
    for argv, needle in cases:
        parser = _parser()
        with raises(SystemExit):
            read_password_files(parser, parser.parse_args(argv))

        result = subprocess.run(
            [sys.executable, "o1_adapter.py", "--profile", "ru", "--ru_supervise", "--ru_netconf_username", "u", *argv],
            cwd=o1_adapter_src,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 2, (argv, result.returncode, result.stderr[-400:])
        assert needle in result.stderr, (argv, result.stderr[-400:])


@mark.timeout(60)
def test_http_port_outside_the_tcp_range_is_refused(o1_adapter_src):
    """--http_port moves the HTTP endpoints off 5000 (two adapters on one host
    network); a value outside 1..65535 is a usage error at startup."""
    for port in ("0", "65536"):
        result = subprocess.run(
            [sys.executable, "o1_adapter.py", "--profile", "ru", "--http_port", port],
            cwd=o1_adapter_src,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 2, (port, result.returncode, result.stderr[-400:])
        assert "--http_port must be 1..65535" in result.stderr
