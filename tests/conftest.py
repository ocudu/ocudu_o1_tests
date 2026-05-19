# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import json
import os
import ssl
import time
from pathlib import Path
from typing import Callable, Optional

import pytest
import requests
import yaml
from ncclient import manager
from ncclient.transport.errors import TransportError


def pytest_collection_modifyitems(items):
    """Record all markers as JUnit XML properties."""
    for item in items:
        markers = []
        for marker in item.iter_markers():
            if marker.name in ("parametrize", "skip", "skipif", "xfail", "usefixtures", "filterwarnings", "timeout"):
                continue
            markers.append(marker.name)
        item.user_properties.append(("markers", ";".join(markers)))


def _wait_for(condition: Callable[[], Optional[object]], timeout: int = 120, interval: float = 1.0):
    """Wait until condition returns a truthy value or timeout expires."""
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            result = condition()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            result = None
        if result:
            return result
        time.sleep(interval)
    raise TimeoutError(f"Condition not met within {timeout} seconds: {last_error}")


@pytest.fixture
def wait_for():
    """Expose the polling helper to test modules."""
    return _wait_for


@pytest.fixture(scope="session")
def config_path() -> Path:
    """Return the path to the generated O1 adapter config file, waiting for it to appear."""
    path = Path(os.getenv("CONFIG_PATH", "/tmp/config.yaml"))
    _wait_for(lambda: path.exists() and path.stat().st_size > 0)
    return path


@pytest.fixture(scope="session")
def load_config(config_path: Path):
    """Return a callable to load the current adapter configuration."""

    def _loader() -> dict:
        with config_path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)

    return _loader


@pytest.fixture(scope="session")
def o1_adapter_base_url():
    """Return the base URL for the O1 adapter REST API."""
    host = os.getenv("O1_ADAPTER_HOST", "ocudu-o1-adapter")
    port = int(os.getenv("O1_ADAPTER_PORT", "5000"))
    base_url = f"http://{host}:{port}"
    session = requests.Session()
    _wait_for(lambda: session.get(f"{base_url}/status", timeout=5).ok)
    return base_url


@pytest.fixture(scope="session")
def netconf_manager():
    """Create a NETCONF manager connection with retries."""
    host = os.getenv("NETCONF_HOST", "ocudu-netconf")
    port = int(os.getenv("NETCONF_PORT", "830"))
    username = os.getenv("NETCONF_USERNAME", "root")
    password = os.getenv("NETCONF_PASSWORD", "root")

    def _connect():
        return manager.connect(
            host=host,
            port=port,
            username=username,
            password=password,
            hostkey_verify=False,
            allow_agent=False,
            look_for_keys=False,
            timeout=10,
        )

    conn = _wait_for(_connect, timeout=120)
    yield conn
    try:
        conn.close_session()
    except TransportError:
        pass


@pytest.fixture(scope="session")
def tls_netconf_manager():
    """Connect to the netopeer2 TLS endpoint with the test client cert."""
    cert_dir = Path(os.getenv("NETCONF_TLS_CERT_DIR", "/etc/netconf-tls-client"))
    host = os.getenv("NETCONF_HOST", "ocudu-netconf")
    port = int(os.getenv("NETCONF_TLS_PORT", "6513"))

    def _connect():
        return manager.connect_tls(
            host=host,
            port=port,
            keyfile=str(cert_dir / "client.key"),
            certfile=str(cert_dir / "client.crt"),
            ca_certs=str(cert_dir / "ca.crt"),
            protocol=ssl.PROTOCOL_TLS_CLIENT,
            check_hostname=False,
        )

    conn = _wait_for(_connect, timeout=120)
    yield conn
    try:
        conn.close_session()
    except TransportError:
        pass


@pytest.fixture(scope="session")
def mock_ru_ssh_manager():
    """Connect to the mock RU's NETCONF-over-SSH endpoint."""
    host = os.getenv("MOCK_RU_HOST", "ocudu-mock-ru")
    port = int(os.getenv("MOCK_RU_SSH_PORT", "830"))
    username = os.getenv("NETCONF_USERNAME", "root")
    password = os.getenv("NETCONF_PASSWORD", "root")

    def _connect():
        return manager.connect(
            host=host,
            port=port,
            username=username,
            password=password,
            hostkey_verify=False,
            allow_agent=False,
            look_for_keys=False,
            timeout=10,
        )

    conn = _wait_for(_connect, timeout=120)
    yield conn
    conn.close_session()


@pytest.fixture(scope="session")
def mock_ru_tls_manager():
    """Connect to the mock RU's NETCONF-over-TLS endpoint with mTLS."""
    cert_dir = Path(os.getenv("MOCK_RU_TLS_CERT_DIR", "/etc/mock-ru-tls-client"))
    host = os.getenv("MOCK_RU_HOST", "ocudu-mock-ru")
    port = int(os.getenv("MOCK_RU_TLS_PORT", "6513"))

    def _connect():
        return manager.connect_tls(
            host=host,
            port=port,
            keyfile=str(cert_dir / "client.key"),
            certfile=str(cert_dir / "client.crt"),
            ca_certs=str(cert_dir / "ca.crt"),
            protocol=ssl.PROTOCOL_TLS_CLIENT,
            check_hostname=False,
        )

    conn = _wait_for(_connect, timeout=120)
    yield conn
    conn.close_session()


@pytest.fixture(scope="session")
def ws_event_log_path() -> Path:
    """Return the path used by the mock gNB to persist WebSocket events."""
    return Path(os.getenv("MOCK_GNB_EVENT_LOG", "/tmp/mock_gnb_events.jsonl"))


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


@pytest.fixture()
def read_ws_events(ws_event_log_path: Path):
    """Provide a callable that loads all WebSocket events emitted so far."""
    return lambda: _read_jsonl(ws_event_log_path)


@pytest.fixture(scope="session")
def mock_smo_event_log_path() -> Path:
    """Path used by the mock SMO to persist received PM envelopes + closed-loop actions."""
    return Path(os.getenv("MOCK_SMO_EVENT_LOG", "/tmp/mock_smo_events.jsonl"))


@pytest.fixture(scope="session")
def mock_smo_base_url() -> str:
    """Base URL for the mock SMO HTTP control surface."""
    host = os.getenv("MOCK_SMO_HOST", "ocudu-mock-smo")
    port = int(os.getenv("MOCK_SMO_PORT", "9560"))
    return f"http://{host}:{port}"


@pytest.fixture()
def configure_mock_smo(mock_smo_base_url):
    """Return a callable that swaps the SMO's trigger payload and (by default) clears its event log."""

    def _configure(payload_file: str, clear_log: bool = True) -> None:
        resp = requests.post(
            f"{mock_smo_base_url}/configure",
            json={"payload_file": payload_file, "clear_log": clear_log},
            timeout=5,
        )
        resp.raise_for_status()

    return _configure


@pytest.fixture()
def read_smo_events(mock_smo_event_log_path: Path):
    """Provide a callable that loads all mock SMO event log entries written so far."""
    return lambda: _read_jsonl(mock_smo_event_log_path)


_NF_FUNCTION_NS = {
    "GNBCUCPFunction": "urn:3gpp:sa5:_3gpp-nr-nrm-gnbcucpfunction",
    "GNBDUFunction": "urn:3gpp:sa5:_3gpp-nr-nrm-gnbdufunction",
}


@pytest.fixture()
def set_pm_admin_state():
    """Return a callable that flips PerfMetricJob[id=defaulttrace] administrativeState via NETCONF."""

    def _set(manager, nf_function: str, function_id: str, state: str):
        manager.edit_config(
            target="running",
            config=f"""
            <config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
              <ManagedElement xmlns="urn:3gpp:sa5:_3gpp-common-managed-element">
                <id>ran1</id>
                <{nf_function} xmlns="{_NF_FUNCTION_NS[nf_function]}">
                  <id>{function_id}</id>
                  <PerfMetricJob>
                    <id>defaulttrace</id>
                    <attributes>
                      <administrativeState>{state}</administrativeState>
                    </attributes>
                  </PerfMetricJob>
                </{nf_function}>
              </ManagedElement>
            </config>
            """,
        )

    return _set


@pytest.fixture()
def wait_for_metric_envelopes(read_smo_events, wait_for):
    """Return a callable that polls the mock SMO log until `min_count` metric envelopes arrive."""

    def _wait(min_count: int = 3, timeout: int = 90) -> list[dict]:
        def _have_enough():
            evts = [e for e in read_smo_events() if isinstance(e, dict) and "metrics" in e]
            return evts if len(evts) >= min_count else None

        return wait_for(_have_enough, timeout=timeout)

    return _wait


@pytest.fixture()
def pm_envelope_shape():
    """Assert a PM JSON envelope conforms to the agreed schema."""

    def _assert(envelope: dict, expected_nf_type: str):
        assert envelope.get("nfType") == expected_nf_type, (
            f"nfType {envelope.get('nfType')!r} != {expected_nf_type!r}"
        )
        assert envelope.get("nfInstanceId"), "nfInstanceId missing"
        assert envelope.get("timestamp"), "timestamp missing"
        assert isinstance(envelope.get("granularityPeriod"), int), "granularityPeriod must be int"
        measured = envelope.get("measuredObject") or {}
        assert measured.get("objectType"), "measuredObject.objectType missing"
        assert measured.get("objectId"), "measuredObject.objectId missing"
        assert measured.get("plmnId"), "measuredObject.plmnId missing"
        metrics = envelope.get("metrics")
        assert isinstance(metrics, list) and metrics, "metrics must be non-empty list"
        for m in metrics:
            assert "name" in m and "value" in m, f"metric entry missing name/value: {m}"

    return _assert


_SMO_PAYLOAD_ROOT = "/opt/mock-smo/payloads"


@pytest.fixture()
def run_smo_closed_loop(
    netconf_manager,
    o1_adapter_base_url,
    load_config,
    read_smo_events,
    read_ws_events,
    ws_event_log_path,
    wait_for,
    wait_for_metric_envelopes,
    pm_envelope_shape,
    set_pm_admin_state,
    configure_mock_smo,
):
    """Drive the SMO closed-loop pattern shared by MVP-FUNC-SMO-17-1-a..f tests."""

    def _run(*, payload_file: str, runtime: bool, check_applied: Callable[[dict], bool]):
        session = requests.Session()
        configure_mock_smo(f"{_SMO_PAYLOAD_ROOT}/{payload_file}")
        if not runtime:
            ws_event_log_path.unlink(missing_ok=True)

        try:
            set_pm_admin_state(netconf_manager, "GNBCUCPFunction", "cucp1", "UNLOCKED")
            envelopes = wait_for_metric_envelopes()
            for env in envelopes[:3]:
                pm_envelope_shape(env, "gnb")

            wait_for(
                lambda: any(e.get("event") == "netconf_edit_sent" for e in read_smo_events()),
                timeout=90,
            )

            if runtime:
                wait_for(lambda: check_applied(load_config()), timeout=90)
                health_resp = session.get(f"{o1_adapter_base_url}/config-healthy", timeout=5)
                assert health_resp.status_code == 200, health_resp.text
            else:
                wait_for(
                    lambda: session.get(f"{o1_adapter_base_url}/config-healthy", timeout=5).status_code == 400,
                    timeout=90,
                )
                wait_for(lambda: check_applied(load_config()), timeout=90)
                wait_for(lambda: any(e.get("cmd") == "quit" for e in read_ws_events()), timeout=90)
        finally:
            set_pm_admin_state(netconf_manager, "GNBCUCPFunction", "cucp1", "LOCKED")
            if not runtime:
                session.post(f"{o1_adapter_base_url}/restarted", timeout=5)
                wait_for(
                    lambda: session.get(f"{o1_adapter_base_url}/config-healthy", timeout=5).status_code == 200,
                    timeout=60,
                )

    return _run


@pytest.fixture(scope="session")
def dryrun_result():
    """Return the result of the YAML validator sidecar dry-run check."""
    result_path = Path("/tmp/dryrun.result")
    _wait_for(
        lambda: result_path.exists() and result_path.stat().st_size > 0,
        timeout=240,
    )

    lines = result_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return {
        "status_code": int(lines[0]),
        "logs": "\n".join(lines[1:]),
    }
