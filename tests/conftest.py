import json
import os
import time
from pathlib import Path
from typing import Callable, Optional

import pytest
import requests
import yaml
from ncclient import manager


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
    host = os.getenv("O1_ADAPTER_HOST", "srsran-o1_adapter")
    port = int(os.getenv("O1_ADAPTER_PORT", "5000"))
    base_url = f"http://{host}:{port}"
    session = requests.Session()
    _wait_for(lambda: session.get(f"{base_url}/status", timeout=5).ok)
    return base_url


@pytest.fixture(scope="session")
def netconf_manager():
    """Create a NETCONF manager connection with retries."""
    host = os.getenv("NETCONF_HOST", "srsran-netconf")
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
    conn.close_session()


@pytest.fixture(scope="session")
def ws_event_log_path() -> Path:
    """Return the path used by the mock gNB to persist WebSocket events."""
    return Path(os.getenv("MOCK_GNB_EVENT_LOG", "/tmp/mock_gnb_events.jsonl"))


@pytest.fixture()
def read_ws_events(ws_event_log_path: Path):
    """Provide a callable that loads all WebSocket events emitted so far."""

    def _reader():
        if not ws_event_log_path.exists():
            return []
        with ws_event_log_path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    return _reader
