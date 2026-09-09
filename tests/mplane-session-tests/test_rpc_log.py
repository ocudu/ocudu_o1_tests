# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""NETCONF conversation capture tests (adapter rpc_log).

--rpc_log records the raw NETCONF exchange — every rpc, rpc-reply and
notification ncclient sees — to a file while keeping the console exactly as
it is without the capture (WARNING and up only). Unit tests pin the handler
routing; the sim integration test proves a real get-config round trip lands
in the file verbatim.
"""

import argparse
import logging

from pytest import mark


@mark.timeout(60)
def test_capture_routes_debug_to_file_not_console(o1_adapter_src, tmp_path, caplog):
    """ncclient DEBUG output goes to the capture file and stops propagating to root."""
    from rpc_log import disable_rpc_log, enable_rpc_log

    log_path = tmp_path / "rpc.log"
    handler = enable_rpc_log(str(log_path))
    try:
        with caplog.at_level(logging.DEBUG):
            logging.getLogger("ncclient.transport.session").debug("Sending:<rpc-marker/>")
        assert "Sending:<rpc-marker/>" in log_path.read_text()
        assert not [r for r in caplog.records if r.name.startswith("ncclient")]
    finally:
        disable_rpc_log(handler)


@mark.timeout(60)
def test_capture_keeps_warnings_on_console(o1_adapter_src, tmp_path, capsys):
    """WARNING and above still reach the terminal while the capture is active."""
    from rpc_log import disable_rpc_log, enable_rpc_log

    handler = enable_rpc_log(str(tmp_path / "rpc.log"))
    try:
        logging.getLogger("ncclient.operations.rpc").warning("watchdog-marker")
        assert "watchdog-marker" in capsys.readouterr().err
    finally:
        disable_rpc_log(handler)


@mark.timeout(60)
def test_disable_restores_default_verbosity(o1_adapter_src, tmp_path):
    """disable_rpc_log detaches both handlers and restores the WARNING pin."""
    from rpc_log import disable_rpc_log, enable_rpc_log

    ncclient_logger = logging.getLogger("ncclient")
    baseline_handlers = list(ncclient_logger.handlers)

    handler = enable_rpc_log(str(tmp_path / "rpc.log"))
    assert ncclient_logger.level == logging.DEBUG
    assert ncclient_logger.propagate is False

    disable_rpc_log(handler)
    assert ncclient_logger.level == logging.WARNING
    assert ncclient_logger.propagate is True
    assert list(ncclient_logger.handlers) == baseline_handlers


@mark.timeout(60)
def test_enable_twice_installs_one_capture(o1_adapter_src, tmp_path):
    """enable_rpc_log is idempotent: a second call in the same process (both
    entry points can reach it) hands back the capture already installed and
    adds nothing to the ncclient logger, and a single disable_rpc_log then
    restores the WARNING pin, propagation and the handler list."""
    from rpc_log import disable_rpc_log, enable_rpc_log

    ncclient_logger = logging.getLogger("ncclient")
    baseline_handlers = list(ncclient_logger.handlers)
    log_path = tmp_path / "rpc.log"

    first = enable_rpc_log(str(log_path))
    added_after_first = [h for h in ncclient_logger.handlers if h not in baseline_handlers]
    second = enable_rpc_log(str(log_path))
    added_after_second = [h for h in ncclient_logger.handlers if h not in baseline_handlers]
    try:
        assert second is first, "a second enable must hand back the capture already installed"
        assert added_after_second == added_after_first, "a second enable must not add handlers"
        assert [h for h in added_after_first if isinstance(h, logging.FileHandler)] == [first]
    finally:
        disable_rpc_log(first)
        if second is not first:  # leave the logger clean for the next test even when this one fails
            disable_rpc_log(second)

    assert ncclient_logger.level == logging.WARNING
    assert ncclient_logger.propagate is True
    assert list(ncclient_logger.handlers) == baseline_handlers


@mark.timeout(60)
def test_flag_registration(o1_adapter_src):
    """Both entry points register --rpc_log through the shared helper."""
    from rpc_log import add_rpc_log_argument

    parser = argparse.ArgumentParser()
    add_rpc_log_argument(parser)
    assert parser.parse_args([]).rpc_log is None
    assert parser.parse_args(["--rpc_log", "capture.log"]).rpc_log == "capture.log"


@mark.timeout(60)
def test_real_conversation_is_captured(o1_adapter_src, tmp_path, fresh_ru_manager):
    """A live get-config round trip against the RU lands in the capture file."""
    from rpc_log import disable_rpc_log, enable_rpc_log

    log_path = tmp_path / "rpc.log"
    handler = enable_rpc_log(str(log_path))
    try:
        fresh_ru_manager.get_config(source="running")
    finally:
        disable_rpc_log(handler)

    capture = log_path.read_text()
    assert "get-config" in capture  # the rpc we sent
    assert "rpc-reply" in capture  # the RU's answer
