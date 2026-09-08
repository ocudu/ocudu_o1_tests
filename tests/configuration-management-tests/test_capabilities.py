# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Capability discovery tests for the M-plane client.

The mock RU's ietf-yang-library is LIVE data (setup_ru.sh enables real YANG
features), so feature discovery is asserted end-to-end against the sim. The
o-ran-module-cap data nodes, by contrast, are config-false state the sim does
not populate — the client must degrade gracefully against it. The RFC 6243
with-defaults handling of the reads is proven against stub managers.
"""

import logging
from types import SimpleNamespace

from pytest import mark, raises

logger = logging.getLogger(__name__)


@mark.timeout(60)
def test_advertised_features_live(ru_config):
    """The sim advertises real YANG features through ietf-yang-library."""
    module_cap_features = ru_config.get_advertised_features("o-ran-module-cap")
    assert "CONFIGURABLE-TDD-PATTERN-SUPPORTED" in module_cap_features
    assert "PRACH-STATIC-CONFIGURATION-SUPPORTED" in module_cap_features
    assert ru_config.get_advertised_features("o-ran-wg4-features") >= {"SUPERVISION-WITH-SESSION-ID"}
    assert ru_config.get_advertised_features("no-such-module") == set()


@mark.timeout(60)
def test_parse_module_capabilities_layouts(o1_adapter_src):
    """Both yang-library layouts parse: RFC 8525 module-set and RFC 7895 modules-state."""
    from ofh_config_builder import parse_module_capabilities

    rfc8525 = {
        "module-set": {
            "name": "complete",
            "module": [
                {
                    "name": "o-ran-module-cap",
                    "revision": "2025-04-14",
                    "namespace": "urn:o-ran:module-cap:1.0",
                    "feature": ["A", "B"],
                },
                {"name": "solo", "feature": "ONLY"},  # single-value leaf-list arrives as a str
            ],
        }
    }
    capabilities = parse_module_capabilities(rfc8525)
    assert capabilities["o-ran-module-cap"]["features"] == {"A", "B"}
    assert capabilities["o-ran-module-cap"]["revision"] == "2025-04-14"
    assert capabilities["solo"]["features"] == {"ONLY"}

    rfc7895 = {"module": {"name": "m1", "feature": ["X"]}}
    assert parse_module_capabilities(rfc7895)["m1"]["features"] == {"X"}

    assert parse_module_capabilities({}) == {}
    assert parse_module_capabilities(None) == {}


@mark.timeout(60)
def test_module_cap_data_absent_on_sim(ru_config):
    """The sim implements the module-cap schema but advertises no data: the
    client must degrade to an empty readback, not a crash."""
    data = ru_config.get_oran_module_capabilities() or {}
    assert not (data.get("module-capability") or {}).get("ru-capabilities")


@mark.timeout(60)
def test_advertised_features_empty_data_returns_empty_set(o1_adapter_src):
    """An RU answering with empty <data/> must yield an empty feature set, not
    a crash (xmltodict maps <data/> to None)."""
    from types import SimpleNamespace

    from ru_config import RuConfig

    class _EmptyDataManager:
        @staticmethod
        def get(filter=None, with_defaults=None):  # noqa: A002 - ncclient signature
            return SimpleNamespace(
                xml='<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><data/></rpc-reply>'
            )

        @staticmethod
        def get_config(source=None, filter=None, with_defaults=None):  # noqa: A002
            return SimpleNamespace(
                xml='<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><data/></rpc-reply>'
            )

    ru = RuConfig(_EmptyDataManager(), "running")
    assert ru.get_advertised_features("o-ran-module-cap") == set()


_EMPTY_DATA_REPLY = '<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><data/></rpc-reply>'

_WITH_DEFAULTS_CAPABILITY = "urn:ietf:params:netconf:capability:with-defaults:1.0"


class _RecordingManager:
    """Stub manager that records the with_defaults mode each read asked for."""

    def __init__(self, capabilities):
        self.server_capabilities = capabilities
        self.requested = []

    def get(self, filter=None, with_defaults=None):  # noqa: A002 - ncclient signature
        self.requested.append(with_defaults)
        return SimpleNamespace(xml=_EMPTY_DATA_REPLY)

    def get_config(self, source=None, filter=None, with_defaults=None):  # noqa: A002
        self.requested.append(with_defaults)
        return SimpleNamespace(xml=_EMPTY_DATA_REPLY)


@mark.timeout(60)
def test_report_all_requested_only_when_the_capability_lists_it(o1_adapter_src):
    """RFC 6243: report-all is asked for only when the server's with-defaults
    capability URI lists it, as basic-mode or under also-supported. ncclient
    validates the mode against that capability before sending, so asking a
    basic-mode=explicit server for report-all raised WithDefaultsError and no
    request ever went out."""
    from ru_config import RuConfig

    _ = o1_adapter_src
    for capabilities, expected in (
        ((), None),
        ((f"{_WITH_DEFAULTS_CAPABILITY}?basic-mode=explicit",), None),
        ((f"{_WITH_DEFAULTS_CAPABILITY}?basic-mode=explicit&also-supported=trim",), None),
        ((f"{_WITH_DEFAULTS_CAPABILITY}?basic-mode=explicit&also-supported=report-all,trim",), "report-all"),
        ((f"{_WITH_DEFAULTS_CAPABILITY}?basic-mode=report-all",), "report-all"),
    ):
        manager = _RecordingManager(capabilities)
        client = RuConfig(manager, "running")
        client.get_uplane_config()  # <get-config>
        client.get_yang_library()  # operational <get>
        assert manager.requested == [expected, expected], capabilities


@mark.timeout(60)
def test_operation_error_on_read_degrades_like_other_read_failures(o1_adapter_src, caplog):
    """An ncclient OperationError raised while building or sending a read (the
    with-defaults validation error is one) is a read failure like any other:
    {} plus one ERROR line when non-strict, re-raised when strict."""
    from ncclient.operations.errors import OperationError
    from ru_config import RuConfig

    _ = o1_adapter_src

    class _RefusingManager:
        server_capabilities = ()

        @staticmethod
        def get(filter=None, with_defaults=None):  # noqa: A002 - ncclient signature
            raise OperationError("refused before sending")

        @staticmethod
        def get_config(source=None, filter=None, with_defaults=None):  # noqa: A002
            raise OperationError("refused before sending")

    client = RuConfig(_RefusingManager(), "running")
    with caplog.at_level(logging.ERROR):
        assert client.get_advertised_features("o-ran-module-cap") == set()
        assert client.get_uplane_config() == {}
    assert "Failed to retrieve" in caplog.text
    with raises(OperationError):
        client._get_and_print_config("<x/>", "strict probe", strict=True)
