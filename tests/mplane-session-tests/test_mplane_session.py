# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Persistent M-plane session tests (adapter mplane_session.MplaneSession).

Two layers:

- unit: the state machine, watchdog semantics and alarm bookkeeping are driven
  with scripted fake sessions through a pluggable connect factory — no RU.
- integration: the session runs against the mock RU (``ocudu_netconf --config
  ru``). The sim has no application behind supervision-watchdog-reset (every
  reset answers rpc-error "no matching subscribers"), which deliberately
  exercises the alive-but-rejected path: the session must hold the connection
  WITHOUT claiming SUPERVISED — a rejected reset never fed the O-RU's
  watchdog (regression: an O-RU that rejected every reset was reported as
  SUPERVISED). Supervision-notification round trips are injected via
  sysrepocfg where docker can reach the container.
- profile gating: ``du_facing_loops_enabled`` decides which DU-facing
  orchestrator loops run next to the session — a pure function, no RU.
"""

import asyncio
import logging
import os
import time
from contextlib import suppress
from types import SimpleNamespace

from conftest import needs_ru_injection
from ncclient.operations import rpc as rpc_ops
from ncclient.operations.errors import TimeoutExpiredError
from ncclient.transport import errors as transport_errors
from pytest import mark

logger = logging.getLogger(__name__)

NOTIFICATION_WRAPPER = (
    '<notification xmlns="urn:ietf:params:xml:ns:netconf:notification:1.0">'
    "<eventTime>2026-01-01T00:00:00Z</eventTime>{payload}</notification>"
)
SUPERVISION_PAYLOAD = (
    '<supervision-notification xmlns="urn:o-ran:supervision:1.0">'
    "<session-id>{session_id}</session-id></supervision-notification>"
)
ALARM_PAYLOAD = (
    '<alarm-notif xmlns="urn:o-ran:fm:1.0"><fault-id>7</fault-id>'
    "<fault-source>ru</fault-source><fault-severity>MAJOR</fault-severity>"
    "<is-cleared>false</is-cleared><event-time>2026-01-01T00:00:00Z</event-time></alarm-notif>"
)

# an accepted reset whose reply says the O-RU kept its own timers (no next-update-at)
TIMERS_KEPT_REPLY = (
    '<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
    '<error-message xmlns="urn:o-ran:supervision:1.0">O-RU keeps its own timers</error-message>'
    "</rpc-reply>"
)

SESSION_ARGS = dict(  # noqa: C408 - readable defaults shared by all tests
    ru_supervision_interval=60,
    ru_supervision_guard=10,
    ru_datastore="running",
)


class _StubRpcError(rpc_ops.RPCError):
    """RPCError stand-in that skips the base class's rpc-reply XML parsing."""

    def __init__(self, message="no matching subscribers"):  # pylint: disable=super-init-not-called
        Exception.__init__(self, message)


class _RecorderAlarms:
    """Alarm manager stand-in recording (op, alarm_id) events."""

    def __init__(self):
        self.events = []

    def set_alarm(self, alarm_id, message=None):  # noqa: ARG002
        self.events.append(("set", alarm_id))

    def clear_alarm(self, alarm_id, message=None):  # noqa: ARG002
        self.events.append(("clear", alarm_id))


class _FakeSession:
    """Scripted NETCONF session double for both session roles.

    notifications: list of payload strings (wrapped on the fly), None (a
    take_notification timeout), exceptions (raised), the "disconnect"
    sentinel (flips connected off, as a transport death detected between
    polls) or a callable, invoked on the take_notification thread and
    yielding any of the former (a gate the test releases). When the script
    runs dry the session keeps timing out.
    """

    server_capabilities = ()

    def __init__(self, notifications=(), dispatch_error=None, dispatch_xml="<ok/>"):
        self.script = list(notifications)
        self.dispatch_error = dispatch_error
        self.dispatch_xml = dispatch_xml
        self.dispatched = []
        self.subscribed = False
        self.connected = True
        self.closed = False
        self.session_id = "42"
        self.get_calls = 0

    def create_subscription(self):
        self.subscribed = True

    def get(self, filter=None, with_defaults=None):  # noqa: A002 - ncclient's parameter name
        self.get_calls += 1
        return SimpleNamespace(xml='<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><data/></rpc-reply>')

    def take_notification(self, block=True, timeout=None):  # noqa: ARG002 - ncclient signature
        if not self.script:
            time.sleep(min(timeout or 0.05, 0.05))
            return None
        entry = self.script.pop(0)
        if callable(entry):
            entry = entry()
        if isinstance(entry, Exception):
            raise entry
        if entry == "disconnect":
            self.connected = False
            return None
        if entry is None:
            time.sleep(min(timeout or 0.05, 0.05))
            return None
        return SimpleNamespace(notification_xml=NOTIFICATION_WRAPPER.format(payload=entry))

    def dispatch(self, element):
        self.dispatched.append(element)
        if isinstance(self.dispatch_error, Exception):
            raise self.dispatch_error
        return SimpleNamespace(xml=self.dispatch_xml)

    def close_session(self):
        self.connected = False
        self.closed = True


class _DeadTransport:
    """Transport double under a manager whose peer stopped answering."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _DeadManager:
    """ncclient manager double for an O-RU that stopped answering.

    close_session is an RPC (<close-session/>): against a dead peer it waits
    out the manager's reply timeout and raises TimeoutExpiredError; the
    session only goes away when the transport underneath is closed
    (manager.session.close()), which is what flips connected off.
    """

    def __init__(self):
        self.timeout = 30
        self.session = _DeadTransport()
        self.close_session_calls = 0

    @property
    def connected(self):
        return not self.session.closed

    def close_session(self):
        self.close_session_calls += 1
        raise TimeoutExpiredError("ncclient timed out while waiting for an rpc reply")


def _make_session(o1_adapter_src, alarms, factory, **overrides):  # noqa: ARG001 - fixture puts src on sys.path
    from mplane_session import MplaneSession
    from state import AppState

    args = SimpleNamespace(**{**SESSION_ARGS, **overrides})
    app_state = AppState()
    session = MplaneSession(app_state, args, alarms, retry_interval=0.05)
    session.connect_factory = factory
    session.poll_cap = 0.1
    return session, app_state


async def _wait_until(task, until, timeout=15, what="condition", on_poll=None):
    """Poll until the predicate holds while the session task stays alive."""
    deadline = time.monotonic() + timeout
    while True:
        if on_poll is not None:
            on_poll()
        if until():
            return
        assert time.monotonic() < deadline, f"{what} not reached before timeout"
        assert not task.done(), f"session loop exited early: {task}"
        await asyncio.sleep(0.02)


async def _drive(session, until, timeout=15):
    """Run session.run() until the predicate holds, then stop it cleanly."""
    stop = asyncio.Event()
    task = asyncio.create_task(session.run(stop))
    try:
        await _wait_until(task, lambda: until(session), timeout)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)


def _pair_factory(pairs):
    """Connect factory yielding sessions from a list (command, notification, ...)."""
    queue = list(pairs)

    def factory():
        entry = queue.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry

    return factory


@mark.timeout(60)
def test_reaches_supervised_and_feeds_watchdog(o1_adapter_src):
    """Entry sends the initial reset, then one reset per supervision-notification."""
    alarms = _RecorderAlarms()
    command = _FakeSession()
    notification = _FakeSession(
        notifications=[SUPERVISION_PAYLOAD.format(session_id=1), SUPERVISION_PAYLOAD.format(session_id=1)]
    )
    session, app_state = _make_session(o1_adapter_src, alarms, _pair_factory([command, notification]))

    asyncio.run(_drive(session, lambda s: s.stats["supervision_notifications"] >= 2))

    from mplane_session import RuSessionState

    assert session.phase is RuSessionState.DISCONNECTED, "stop must land back in DISCONNECTED"
    assert notification.subscribed
    assert session.stats["watchdog_resets"] == 3, "initial reset + one per notification"
    # o-ran-supervision timers are per session, held by the session that
    # subscribed: resets dispatched anywhere else get rpc-error'd and the
    # O-RU's watchdog starves (observed against an O-RU).
    assert len(notification.dispatched) == 3, "all resets go through the subscription session"
    assert not command.dispatched, "the command session must not carry watchdog resets"
    assert app_state.session_state["ru_supervised"] is False, "teardown clears the supervised flag"
    assert alarms.events == [], "clean run raises no alarms"
    # the configured timers must be plumbed into the RPC payload verbatim
    import xml.etree.ElementTree as ET

    reset = notification.dispatched[0]
    namespace = "{urn:o-ran:supervision:1.0}"
    assert reset.tag == f"{namespace}supervision-watchdog-reset"
    assert reset.find(f"{namespace}supervision-notification-interval").text == "60"
    assert reset.find(f"{namespace}guard-timer-overhead").text == "10"


@mark.timeout(60)
def test_starvation_degrades_then_recovers(o1_adapter_src):
    """No supervision-notification within interval+guard -> DEGRADED + alarm 1004;
    the next supervision-notification restores SUPERVISED and clears it."""
    alarms = _RecorderAlarms()
    command = _FakeSession()
    # 8 x ~0.05s timeouts guarantee the 0.2s budget starves before the
    # recovering supervision-notification is reachable in the script.
    notification = _FakeSession(notifications=[None] * 8 + [SUPERVISION_PAYLOAD.format(session_id=1)])
    session, _ = _make_session(
        o1_adapter_src,
        alarms,
        _pair_factory([command, notification]),
        ru_supervision_interval=0.1,
        ru_supervision_guard=0.1,
    )

    asyncio.run(
        _drive(session, lambda s: s.stats["starvations"] >= 1 and s.stats["supervision_notifications"] >= 1)
    )

    assert ("set", 1004) in alarms.events and ("clear", 1004) in alarms.events
    assert alarms.events.index(("set", 1004)) < alarms.events.index(("clear", 1004))
    assert session.stats["watchdog_resets"] >= 2, "starvation triggers a recovery reset"


@mark.timeout(60)
def test_supervised_flag_follows_the_phase(o1_adapter_src):
    """session_state["ru_supervised"] is what the status endpoint reports and
    must be True in SUPERVISED only: False while CONNECTING, False in DEGRADED
    (a starved session was still reported as supervised) and False once
    DISCONNECTED."""
    import threading

    from mplane_session import RuSessionState

    alarms = _RecorderAlarms()
    connect_gate = threading.Event()
    degraded_gate = threading.Event()
    command = _FakeSession()
    notification = _FakeSession()

    def parked():
        # times out like an idle stream until the 0.2 s budget has starved
        # the session into DEGRADED, then holds the stream there until the
        # test has looked at the flag and releases it with a recovery
        if session.phase is not RuSessionState.DEGRADED and not degraded_gate.is_set():
            notification.script.insert(0, parked)
            time.sleep(0.05)
            return None
        degraded_gate.wait(10)
        return SUPERVISION_PAYLOAD.format(session_id=1)

    notification.script = [SUPERVISION_PAYLOAD.format(session_id=1), parked]
    pairs = _pair_factory([command, notification])

    def gated_connect():
        connect_gate.wait(10)
        return pairs()

    session, app_state = _make_session(
        o1_adapter_src, alarms, gated_connect, ru_supervision_interval=0.1, ru_supervision_guard=0.1
    )
    flag = app_state.session_state
    samples = {}

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        try:
            await _wait_until(task, lambda: session.phase is RuSessionState.CONNECTING, what="CONNECTING")
            samples["CONNECTING"] = flag["ru_supervised"]
            connect_gate.set()
            await _wait_until(task, lambda: session.phase is RuSessionState.SUPERVISED, what="SUPERVISED")
            samples["SUPERVISED"] = flag["ru_supervised"]
            await _wait_until(
                task,
                lambda: session.phase is RuSessionState.DEGRADED and not notification.script,
                what="DEGRADED with the stream parked",
            )
            samples["DEGRADED"] = flag["ru_supervised"]
            degraded_gate.set()
            await _wait_until(task, lambda: session.stats["supervision_notifications"] >= 2, what="recovery")
            samples["RESTORED"] = flag["ru_supervised"]
        finally:
            connect_gate.set()
            degraded_gate.set()
            stop.set()
            await asyncio.wait_for(task, timeout=10)

    asyncio.run(scenario())
    samples["DISCONNECTED"] = flag["ru_supervised"]

    assert session.phase is RuSessionState.DISCONNECTED
    assert session.stats["starvations"] >= 1
    assert samples == {
        "CONNECTING": False,
        "SUPERVISED": True,
        "DEGRADED": False,
        "RESTORED": True,
        "DISCONNECTED": False,
    }


@mark.timeout(60)
def test_rpc_error_reset_is_not_fatal_but_never_supervised(o1_adapter_src):
    """rpc-error on the watchdog reset means alive-but-rejected: the session
    stays up (the mock RU answers every reset this way) — but a rejected
    reset never fed the O-RU's watchdog, so the session must NOT report
    SUPERVISED on its strength. Regression test: an O-RU that rejected every
    reset was reported as SUPERVISED."""
    from mplane_session import RuSessionState

    alarms = _RecorderAlarms()
    command = _FakeSession()
    notification = _FakeSession(
        notifications=[SUPERVISION_PAYLOAD.format(session_id=1), SUPERVISION_PAYLOAD.format(session_id=1)],
        dispatch_error=_StubRpcError(),
    )
    session, _ = _make_session(o1_adapter_src, alarms, _pair_factory([command, notification]))

    phases_seen = set()

    def watched(state):
        phases_seen.add(state.phase)
        return state.stats["supervision_notifications"] >= 2

    asyncio.run(_drive(session, watched))

    assert session.stats["watchdog_rpc_errors"] == 3
    assert session.stats["watchdog_resets"] == 0
    assert session.stats["connect_cycles"] == 1, "rpc-errors must not tear the session down"
    assert RuSessionState.SUPERVISED not in phases_seen, "rejected resets must not claim SUPERVISED"


@mark.timeout(60)
def test_transport_death_reconnects(o1_adapter_src):
    """A transport error on the stream kills the cycle; the loop reconnects,
    raising 1003 for the outage and clearing it on recovery."""
    alarms = _RecorderAlarms()
    first_command = _FakeSession()
    first_notification = _FakeSession(
        notifications=[SUPERVISION_PAYLOAD.format(session_id=1), transport_errors.TransportError("stream died")]
    )
    second_command = _FakeSession()
    second_notification = _FakeSession(notifications=[SUPERVISION_PAYLOAD.format(session_id=2)])
    session, _ = _make_session(
        o1_adapter_src,
        alarms,
        _pair_factory([first_command, first_notification, second_command, second_notification]),
    )

    asyncio.run(_drive(session, lambda s: s.stats["connect_cycles"] >= 2))

    assert ("set", 1003) in alarms.events and ("clear", 1003) in alarms.events
    assert alarms.events.index(("set", 1003)) < alarms.events.index(("clear", 1003))
    assert not first_command.connected, "the dead cycle's sessions must be closed"


@mark.timeout(60)
def test_connect_failure_retries_with_alarm(o1_adapter_src):
    """A failed connect raises 1003 and backs off; the next attempt clears it."""
    alarms = _RecorderAlarms()
    command = _FakeSession()
    notification = _FakeSession(notifications=[SUPERVISION_PAYLOAD.format(session_id=1)])
    session, _ = _make_session(
        o1_adapter_src,
        alarms,
        _pair_factory([transport_errors.SSHError("connection refused"), command, notification]),
    )

    asyncio.run(_drive(session, lambda s: s.stats["supervision_notifications"] >= 1))

    assert alarms.events[0] == ("set", 1003)
    assert ("clear", 1003) in alarms.events
    assert session.stats["connect_cycles"] == 1


@mark.timeout(60)
def test_handshake_eof_is_a_connect_failure(o1_adapter_src, caplog):
    """A peer that closes the connection during the SSH handshake surfaces as
    paramiko's bare EOFError (ncclient wraps only SSHException). On the direct
    path and on the call-home path it is a connect failure like a refused
    socket — 1003, then a retry that clears it — and the log names the
    exception type, since the error itself carries no message."""
    for callhome in (False, True):
        alarms = _RecorderAlarms()
        ru_session = _FakeSession(notifications=[SUPERVISION_PAYLOAD.format(session_id=1)])
        ru_session.server_capabilities = (_INTERLEAVE,)
        pairs = [EOFError(), ru_session] if callhome else [EOFError(), _FakeSession(), ru_session]
        overrides = {"ru_callhome": True, "ru_callhome_port": 0} if callhome else {}
        session, _ = _make_session(o1_adapter_src, alarms, _pair_factory(pairs), **overrides)

        with caplog.at_level(logging.WARNING):
            asyncio.run(_drive(session, lambda s: s.stats["supervision_notifications"] >= 1))

        assert alarms.events[0] == ("set", 1003), f"callhome={callhome}: {alarms.events}"
        assert ("clear", 1003) in alarms.events, f"callhome={callhome}: {alarms.events}"
        assert session.stats["connect_cycles"] == 1
        assert any(
            "connect failed" in record.message and "EOFError" in record.message for record in caplog.records
        ), f"callhome={callhome}: {[r.message for r in caplog.records]}"
        caplog.clear()


@mark.timeout(60)
def test_callhome_peer_dropping_mid_handshake_keeps_the_session_task(o1_adapter_src):
    """An O-RU that dials in, sends its SSH banner and drops the connection
    (a reset during call-home) makes paramiko raise a bare EOFError out of the
    real accept path. The session task survives it — 1003 is raised, the
    listener stays bound for the re-dial — instead of ending the adapter."""
    import socket
    import threading

    alarms = _RecorderAlarms()
    # the real accept path builds the NETCONF session itself, so it needs the
    # credentials the scripted tests never touch
    session, _ = _make_session(
        o1_adapter_src,
        alarms,
        None,
        ru_callhome=True,
        ru_callhome_port=0,
        ru_callhome_bind="127.0.0.1",
        ru_netconf_username="mplane",
        ru_netconf_password="mplane",
    )

    async def scenario():
        listener = session._ensure_callhome_listener()  # pylint: disable=protected-access
        port = listener.getsockname()[1]

        def dial_and_drop():
            with socket.create_connection(("127.0.0.1", port)) as peer:
                peer.sendall(b"SSH-2.0-mock_ru\r\n")  # a server banner, then nothing more
                peer.settimeout(5)
                with suppress(OSError):
                    peer.recv(4096)  # the client's banner / KEXINIT, then close

        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        await asyncio.sleep(0.2)  # the loop is waiting in accept
        threading.Thread(target=dial_and_drop, daemon=True).start()
        await _wait_until(task, lambda: ("set", 1003) in alarms.events, timeout=30, what="1003 after the aborted handshake")
        assert session._ensure_callhome_listener() is listener  # pylint: disable=protected-access
        stop.set()
        await asyncio.wait_for(task, timeout=10)

    try:
        asyncio.run(scenario())
    finally:
        session._close_callhome_listener()  # pylint: disable=protected-access


@mark.timeout(60)
def test_outage_is_logged_once_then_at_debug(o1_adapter_src, caplog):
    """A long outage (the O-RU rebooting, a fibre out) must not print a
    WARNING and two phase transitions per retry: the first failed attempt is
    reported at WARNING/INFO, the retries that follow go to DEBUG."""
    alarms = _RecorderAlarms()
    attempts = []

    def refused():
        attempts.append(time.monotonic())
        raise transport_errors.SSHError("connection refused")

    session, _ = _make_session(o1_adapter_src, alarms, refused)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(_drive(session, lambda s: len(attempts) >= 3))

    failed = [r for r in caplog.records if "connect failed" in r.getMessage()]
    assert [r.levelno for r in failed if r.levelno >= logging.WARNING] == [logging.WARNING], "one WARNING per outage"
    retries_at_debug = [r for r in caplog.records if r.levelno == logging.DEBUG and "connect" in r.getMessage().lower()]
    assert len(retries_at_debug) >= 2, "the retries are logged at DEBUG"
    transitions = [r for r in caplog.records if "->" in r.getMessage() and "CONNECTING" in r.getMessage()]
    assert transitions, "phase transitions are logged"
    assert transitions[0].levelno == logging.INFO, "the first attempt's transition is INFO"
    assert len([r for r in transitions if r.levelno >= logging.INFO]) <= 2, "only the first attempt transitions at INFO"
    assert all(r.levelno == logging.DEBUG for r in transitions[2:]), "later attempts transition at DEBUG"


@mark.timeout(60)
def test_non_supervision_notifications_dispatch_to_handlers(o1_adapter_src):
    """Other notifications go to registered handlers; handler failures and the
    dispatch itself never feed the watchdog or kill the session."""
    alarms = _RecorderAlarms()
    received = []
    command = _FakeSession()
    notification = _FakeSession(notifications=[ALARM_PAYLOAD, SUPERVISION_PAYLOAD.format(session_id=1)])
    session, _ = _make_session(o1_adapter_src, alarms, _pair_factory([command, notification]))
    # the raising handler comes FIRST: later handlers must still be invoked
    session.register_notification_handler(lambda xml: (_ for _ in ()).throw(RuntimeError("handler bug")))
    session.register_notification_handler(received.append)

    asyncio.run(_drive(session, lambda s: s.stats["supervision_notifications"] >= 1))

    assert len(received) == 1 and "alarm-notif" in received[0]
    assert session.stats["other_notifications"] == 1
    assert session.stats["watchdog_resets"] == 2, "initial + supervision only; alarm-notif must not reset"


CARRIER_STATE_CHANGE_PAYLOAD = (
    '<tx-array-carriers-state-change xmlns="urn:o-ran:uplane-conf:1.0">'
    "<tx-array-carriers><name>Tx-Array-Carrier-00</name><state>READY</state></tx-array-carriers>"
    "<tx-array-carriers><name>Tx-Array-Carrier-01</name><state>BUSY</state></tx-array-carriers>"
    "</tx-array-carriers-state-change>"
)


@mark.timeout(60)
def test_carrier_state_changes_are_logged_as_receipts(o1_adapter_src, caplog):
    """array-carriers state-change notifications are the operator's activation
    receipt: each transition must surface at INFO in the session log (the
    wire shape is the o-ran-uplane-conf notification with a carriers list)."""
    alarms = _RecorderAlarms()
    command = _FakeSession()
    notification = _FakeSession(
        notifications=[CARRIER_STATE_CHANGE_PAYLOAD, SUPERVISION_PAYLOAD.format(session_id=1)]
    )
    session, _ = _make_session(o1_adapter_src, alarms, _pair_factory([command, notification]))

    with caplog.at_level(logging.INFO):
        asyncio.run(_drive(session, lambda s: s.stats["supervision_notifications"] >= 1))

    assert session.stats["other_notifications"] == 1
    assert "O-RU carrier state change: Tx-Array-Carrier-00 -> READY" in caplog.text
    assert "O-RU carrier state change: Tx-Array-Carrier-01 -> BUSY" in caplog.text


@mark.timeout(60)
def test_unanswered_rpc_recycles_session_not_process(o1_adapter_src):
    """An RPC reply timeout (ncclient TimeoutExpiredError — a direct
    NCClientError subclass, NOT an OperationError) on the initial watchdog
    reset must recycle the session, never enter SUPERVISED and never escape
    the run loop. Regression test for the adapter-killing escape."""
    alarms = _RecorderAlarms()
    first_command = _FakeSession()
    first_notification = _FakeSession(dispatch_error=TimeoutExpiredError("ncclient timed out while waiting"))
    second_command = _FakeSession()
    second_notification = _FakeSession(notifications=[SUPERVISION_PAYLOAD.format(session_id=1)])
    session, _ = _make_session(
        o1_adapter_src,
        alarms,
        _pair_factory([first_command, first_notification, second_command, second_notification]),
    )

    from mplane_session import RuSessionState

    asyncio.run(
        _drive(session, lambda s: s.stats["connect_cycles"] >= 2 and s.phase is RuSessionState.SUPERVISED)
    )

    assert len(first_notification.dispatched) == 1, "cycle 1 dies on the unanswered initial reset"
    assert first_command.closed and first_notification.closed, "the dead cycle's sessions must be closed"
    assert ("set", 1003) in alarms.events and ("clear", 1003) in alarms.events
    assert session.stats["watchdog_resets"] >= 1, "cycle 2 recovers and feeds the watchdog"


@mark.timeout(60)
@mark.parametrize("shared", [False, True], ids=["split-sessions", "call-home-shared"])
def test_close_sessions_against_a_dead_ru_is_bounded(o1_adapter_src, shared):
    """Teardown of a session whose peer stopped answering must not stall the
    reconnect: <close-session/> is an RPC and waits out the manager's reply
    timeout against a dead O-RU (a minute of every outage went into it on
    hardware). _close_sessions shortens that timeout to the module's
    _CLOSE_TIMEOUT_S and, when the RPC does not free the session, closes the
    transport underneath — on every distinct manager, once for a shared
    call-home session."""
    import mplane_session

    alarms = _RecorderAlarms()
    session, _ = _make_session(o1_adapter_src, alarms, _pair_factory([]))
    command = _DeadManager()
    notification = command if shared else _DeadManager()
    session.command_session = command
    session.notification_session = notification

    started = time.monotonic()
    session._close_sessions()  # pylint: disable=protected-access
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"teardown against a dead O-RU took {elapsed:.1f}s"
    for dead in {id(command): command, id(notification): notification}.values():
        assert dead.timeout == mplane_session._CLOSE_TIMEOUT_S  # pylint: disable=protected-access
        assert dead.session.closed, "the transport must be closed when close-session gets no answer"
        assert not dead.connected
    if shared:
        assert command.close_session_calls == 1, "a shared call-home session is closed once"
    assert session.command_session is None and session.notification_session is None


@mark.timeout(60)
def test_notification_session_death_recycles(o1_adapter_src):
    """The notification transport dying quietly (connected flips False between
    polls, nothing raises) must tear the cycle down and reconnect."""
    alarms = _RecorderAlarms()
    first_command = _FakeSession()
    first_notification = _FakeSession(notifications=[SUPERVISION_PAYLOAD.format(session_id=1), "disconnect"])
    second_command = _FakeSession()
    second_notification = _FakeSession(notifications=[SUPERVISION_PAYLOAD.format(session_id=2)])
    session, _ = _make_session(
        o1_adapter_src,
        alarms,
        _pair_factory([first_command, first_notification, second_command, second_notification]),
    )

    asyncio.run(_drive(session, lambda s: s.stats["connect_cycles"] >= 2))

    assert first_command.closed and first_notification.closed
    assert ("set", 1003) in alarms.events and ("clear", 1003) in alarms.events


@mark.timeout(60)
def test_connection_alarm_clears_on_reconnect_without_supervised(o1_adapter_src):
    """1003 is the connection alarm, not a supervision one: raised for the
    outage, it clears once the sessions are back up and subscribed even when
    the O-RU rejects every watchdog reset and SUPERVISED is never reached
    (1004 covers supervision; an O-RU without a supervision application
    otherwise kept 1003 raised for good after its first hiccup)."""
    from mplane_session import RuSessionState

    alarms = _RecorderAlarms()
    first_command = _FakeSession()
    first_notification = _FakeSession(
        notifications=[SUPERVISION_PAYLOAD.format(session_id=1)], dispatch_error=_StubRpcError()
    )
    second_command = _FakeSession()
    second_notification = _FakeSession(
        notifications=[SUPERVISION_PAYLOAD.format(session_id=2), SUPERVISION_PAYLOAD.format(session_id=2)],
        dispatch_error=_StubRpcError(),
    )
    session, app_state = _make_session(
        o1_adapter_src,
        alarms,
        _pair_factory([first_command, first_notification, second_command, second_notification]),
    )
    phases_seen = set()

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        observe = lambda: phases_seen.add(session.phase)  # noqa: E731 - poll hook
        try:
            # cycle 1 is up: the initial reset and the notification's were rejected
            await _wait_until(task, lambda: session.stats["watchdog_rpc_errors"] >= 2, on_poll=observe)
            first_command.connected = False  # the command session dies quietly
            # cycle 2 handled its second notification: anything the reconnect
            # clears has been cleared by now
            await _wait_until(
                task,
                lambda: session.stats["connect_cycles"] >= 2 and session.stats["supervision_notifications"] >= 3,
                what="reconnect",
                on_poll=observe,
            )
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=10)

    asyncio.run(scenario())

    assert [e for e in alarms.events if e[1] == 1003] == [("set", 1003), ("clear", 1003)]
    assert session.stats["watchdog_resets"] == 0, "no reset was ever accepted"
    assert RuSessionState.SUPERVISED not in phases_seen, "rejected resets must not claim SUPERVISED"
    assert app_state.session_state["ru_supervised"] is False


@mark.timeout(60)
def test_stop_during_backoff_wakes_early(o1_adapter_src):
    """Stopping while the loop backs off between failed connects must return
    promptly instead of sleeping out the full retry interval."""
    alarms = _RecorderAlarms()

    def always_refused():
        raise transport_errors.SSHError("connection refused")

    session, _ = _make_session(o1_adapter_src, alarms, always_refused)
    session.retry_interval = 30  # would dominate the test runtime if slept out

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        deadline = time.monotonic() + 10
        while ("set", 1003) not in alarms.events:
            assert time.monotonic() < deadline and not task.done()
            await asyncio.sleep(0.02)
        started = time.monotonic()
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        assert time.monotonic() - started < 2, "stop must wake the backoff sleep early"

    asyncio.run(scenario())


@mark.timeout(60)
def test_degraded_alarm_survives_reconnect_until_recovery(o1_adapter_src):
    """1004 raised for a starvation stays active across a session recycle and
    is cleared only when supervision actually recovers."""
    alarms = _RecorderAlarms()
    first_command = _FakeSession()
    first_notification = _FakeSession(notifications=[None] * 8 + [transport_errors.TransportError("stream died")])
    second_command = _FakeSession()
    second_notification = _FakeSession(notifications=[SUPERVISION_PAYLOAD.format(session_id=2)])
    session, _ = _make_session(
        o1_adapter_src,
        alarms,
        _pair_factory([first_command, first_notification, second_command, second_notification]),
        ru_supervision_interval=0.1,
        ru_supervision_guard=0.1,
    )

    asyncio.run(
        _drive(session, lambda s: s.stats["connect_cycles"] >= 2 and s.stats["supervision_notifications"] >= 1)
    )

    supervision_events = [e for e in alarms.events if e[1] == 1004]
    assert supervision_events == [("set", 1004), ("clear", 1004)], "one outage, cleared only on recovery"
    assert alarms.events.index(("clear", 1004)) > alarms.events.index(("set", 1003)), "1004 outlives the recycle"
    assert first_notification.closed


@mark.timeout(60)
def test_malformed_notification_is_isolated(o1_adapter_src):
    """Unparseable notification XML is counted, dispatched raw to handlers and
    never feeds the watchdog or kills the session."""
    alarms = _RecorderAlarms()
    received = []
    command = _FakeSession()
    notification = _FakeSession(notifications=["<broken<", SUPERVISION_PAYLOAD.format(session_id=1)])
    session, _ = _make_session(o1_adapter_src, alarms, _pair_factory([command, notification]))
    session.register_notification_handler(received.append)

    asyncio.run(_drive(session, lambda s: s.stats["supervision_notifications"] >= 1))

    assert session.stats["other_notifications"] == 1
    assert len(received) == 1 and "<broken<" in received[0]
    assert session.stats["watchdog_resets"] == 2, "initial + supervision only"


@mark.timeout(60)
def test_next_update_at_overrides_local_budget(o1_adapter_src):
    """A next-update-at in the watchdog reply overrides the locally computed
    starvation budget: the O-RU may lawfully keep its own timers."""
    from datetime import datetime, timedelta, timezone

    alarms = _RecorderAlarms()
    # a promise is honoured while promise + guard stays within twice the local
    # budget (1.0 + 1.0 s); beyond that the session treats the O-RU clock as
    # skewed and keeps the local budget (covered by the capping test below)
    promised = (datetime.now(timezone.utc) + timedelta(seconds=2.5)).isoformat()
    reply = (
        '<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
        f'<next-update-at xmlns="urn:o-ran:supervision:1.0">{promised}</next-update-at></rpc-reply>'
    )
    command = _FakeSession()
    notification = _FakeSession(notifications=[SUPERVISION_PAYLOAD.format(session_id=1)], dispatch_xml=reply)
    session, _ = _make_session(
        o1_adapter_src,
        alarms,
        _pair_factory([command, notification]),
        ru_supervision_interval=1.0,
        ru_supervision_guard=1.0,
    )

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        try:
            deadline = time.monotonic() + 10
            while session.stats["supervision_notifications"] < 1:
                assert time.monotonic() < deadline and not task.done()
                await asyncio.sleep(0.02)
            # with the local 2.0 s budget this window would starve once
            await asyncio.sleep(2.5)
            assert session.stats["starvations"] == 0, "next-update-at must extend the budget"
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=10)

    asyncio.run(scenario())
    assert not [e for e in alarms.events if e[1] == 1004]


@mark.timeout(60)
def test_next_update_at_parsing_edges(o1_adapter_src):
    """The deadline helper tolerates Z suffixes and rejects garbage/past/far values."""
    from datetime import datetime, timedelta, timezone

    alarms = _RecorderAlarms()
    session, _ = _make_session(o1_adapter_src, alarms, _pair_factory([]))

    future = datetime.now(timezone.utc) + timedelta(seconds=120)
    zulu = future.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert session._deadline_from_next_update(zulu) is not None  # pylint: disable=protected-access
    assert session._deadline_from_next_update(None) is None  # pylint: disable=protected-access
    assert session._deadline_from_next_update("not-a-date") is None  # pylint: disable=protected-access
    past = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    assert session._deadline_from_next_update(past) is None  # pylint: disable=protected-access
    far = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    assert session._deadline_from_next_update(far) is None  # pylint: disable=protected-access
    # next-update-at is stamped by the O-RU's (possibly skewed) clock — a
    # promise SHORTER than the local budget must never shrink patience
    # (an O-RU clock running ~36 s behind flapped DEGRADED every cycle)
    near = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    assert session._deadline_from_next_update(near) is None  # pylint: disable=protected-access


@mark.timeout(60)
def test_next_update_at_is_capped_and_clock_warned_once(o1_adapter_src, caplog):
    """A next-update-at far beyond the local budget (an O-RU clock running
    ahead, or a promise it will not keep) must not stretch patience without
    bound: the deadline is capped at twice the local budget, and the clock
    discrepancy is reported at WARNING once per connect cycle, not on every
    reset."""
    from datetime import datetime, timedelta, timezone

    alarms = _RecorderAlarms()
    # interval 60 + guard 10: a 70 s local budget, so the cap is 140 s
    session, _ = _make_session(o1_adapter_src, alarms, _pair_factory([]))
    an_hour_ahead = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    with caplog.at_level(logging.DEBUG):
        before = time.monotonic()
        deadlines = [
            session._deadline_from_next_update(an_hour_ahead),  # pylint: disable=protected-access
            session._deadline_from_next_update(an_hour_ahead),  # pylint: disable=protected-access
        ]
        after = time.monotonic()

    for deadline in deadlines:
        # a capped override, or None to fall back to the 70 s local budget
        stretch = None if deadline is None else deadline - before
        assert stretch is None or stretch <= 140 + (after - before), f"deadline stretched to {stretch:.0f}s"
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    message = warnings[0].getMessage().lower()
    assert "clock" in message or "next-update-at" in message, message


@mark.timeout(60)
def test_adjusted_timers_warning_once_per_connect_cycle(o1_adapter_src, caplog):
    """An accepted reset answered with error-message and no next-update-at
    means the O-RU kept its own timers: one WARNING per connect cycle, not
    one per reset (with a 60 s interval that was a warning a minute for the
    whole run); the repeats within a cycle go to DEBUG."""
    alarms = _RecorderAlarms()
    first_command = _FakeSession()
    first_notification = _FakeSession(
        notifications=[
            SUPERVISION_PAYLOAD.format(session_id=1),
            SUPERVISION_PAYLOAD.format(session_id=1),
            transport_errors.TransportError("stream died"),
        ],
        dispatch_xml=TIMERS_KEPT_REPLY,
    )
    second_command = _FakeSession()
    second_notification = _FakeSession(
        notifications=[SUPERVISION_PAYLOAD.format(session_id=2), SUPERVISION_PAYLOAD.format(session_id=2)],
        dispatch_xml=TIMERS_KEPT_REPLY,
    )
    session, _ = _make_session(
        o1_adapter_src,
        alarms,
        _pair_factory([first_command, first_notification, second_command, second_notification]),
    )

    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            _drive(session, lambda s: s.stats["connect_cycles"] >= 2 and s.stats["supervision_notifications"] >= 4)
        )

    assert session.stats["watchdog_resets"] == 6, "initial + two notifications, accepted, in each cycle"
    adjusted = [r for r in caplog.records if "adjusted the supervision timers" in r.getMessage()]
    assert [r.levelno for r in adjusted if r.levelno >= logging.WARNING] == [logging.WARNING] * 2, "one per cycle"
    repeats = [r for r in adjusted if r.levelno < logging.WARNING]
    assert repeats and all(r.levelno == logging.DEBUG for r in repeats), "the repeats are demoted to DEBUG"


@mark.timeout(60)
def test_command_session_keepalive_prevents_idle_reaping(o1_adapter_src):
    """The command session idles between cycle handlers and servers reap
    unsubscribed idle sessions (netopeer2: 180 s — an O-RU closed the session
    with EOF at exactly that age). The listener must issue a cheap read on
    the command session every interval/2, without recycling the session."""
    alarms = _RecorderAlarms()
    command = _FakeSession()
    notification = _FakeSession(
        notifications=[SUPERVISION_PAYLOAD.format(session_id=1), None, None, None, None, None, None, None]
    )
    session, _ = _make_session(
        o1_adapter_src,
        alarms,
        _pair_factory([command, notification]),
        ru_supervision_interval=0.2,
        ru_supervision_guard=0.2,
    )

    asyncio.run(_drive(session, lambda s: command.get_calls >= 2))

    assert session.stats["connect_cycles"] == 1, "keep-alives must not recycle the session"
    assert not command.dispatched, "keep-alive is a read on the command session, never a watchdog reset"


@mark.timeout(60)
def test_non_positive_budget_rejected(o1_adapter_src):
    """A non-positive supervision budget is a config error, not a tight loop."""
    from pytest import raises

    alarms = _RecorderAlarms()
    with raises(ValueError):
        _make_session(
            o1_adapter_src,
            alarms,
            _pair_factory([]),
            ru_supervision_interval=-20,
            ru_supervision_guard=10,
        )


_INTERLEAVE = "urn:ietf:params:netconf:capability:interleave:1.0"


@mark.timeout(60)
def test_callhome_single_session_runs_both_roles(o1_adapter_src):
    """Call-home mode: the O-RU's single dial-in carries both roles — the
    subscription, the watchdog resets and the command traffic all ride the
    accepted session (legal because it advertises :interleave), and stop
    closes that one session exactly once."""
    alarms = _RecorderAlarms()
    ru_session = _FakeSession(notifications=[SUPERVISION_PAYLOAD.format(session_id=1)])
    ru_session.server_capabilities = (_INTERLEAVE,)
    session, app_state = _make_session(
        o1_adapter_src, alarms, lambda: ru_session, ru_callhome=True, ru_callhome_port=0
    )

    seen = {}

    def snapshot(current):
        if current.stats["supervision_notifications"] >= 1:
            seen["command"] = current.command_session
            seen["notification"] = current.notification_session
            return True
        return False

    asyncio.run(_drive(session, snapshot))

    assert seen["command"] is ru_session and seen["notification"] is ru_session
    assert session.stats["callhome_accepts"] == 1
    assert ru_session.subscribed
    assert session.stats["watchdog_resets"] == 2, "initial reset + one per notification, on the shared session"
    assert len(ru_session.dispatched) == 2, "resets dispatched on the session that subscribed"
    assert app_state.session_state["ru_supervised"] is False, "stop lands unsupervised"
    assert ru_session.closed


@mark.timeout(60)
def test_callhome_rejects_peer_without_interleave(o1_adapter_src):
    """A call-home peer without :interleave cannot carry both roles on its
    single session: it is closed and the cycle retries (waiting out the RU's
    re-call-home) instead of half-driving it, with the connection alarm up."""
    alarms = _RecorderAlarms()
    rejected = []

    def factory():
        peer = _FakeSession()
        peer.server_capabilities = ("urn:ietf:params:netconf:base:1.1",)
        rejected.append(peer)
        return peer

    session, _ = _make_session(o1_adapter_src, alarms, factory, ru_callhome=True, ru_callhome_port=0)

    asyncio.run(_drive(session, lambda s: len(rejected) >= 2))

    assert all(peer.closed for peer in rejected[:2]), "an unusable peer must not be half-driven"
    assert session.stats["callhome_accepts"] == 0
    assert session.stats["connect_cycles"] == 0
    assert ("set", 1003) in alarms.events


@mark.timeout(60)
def test_callhome_listener_accepts_and_honors_stop(o1_adapter_src):
    """The real listener accepts a dial-in on the bound port, stays bound
    across accepts (reconnection is the O-RU re-dialing), a stop request
    interrupts a quiet accept promptly, and stopping the session while it
    waits for the O-RU's dial-in is not a connection loss (no 1003)."""
    import socket
    import threading

    alarms = _RecorderAlarms()
    session, _ = _make_session(
        o1_adapter_src, alarms, None, ru_callhome=True, ru_callhome_port=0, ru_callhome_bind="127.0.0.1"
    )

    async def scenario():
        listener = session._ensure_callhome_listener()  # pylint: disable=protected-access
        assert listener is not None
        port = listener.getsockname()[1]

        def dial():
            with socket.create_connection(("127.0.0.1", port)):
                time.sleep(0.2)

        dialer = threading.Thread(target=dial, daemon=True)
        dialer.start()
        accepted = await session._accept_call_home(None)  # pylint: disable=protected-access
        assert accepted is not None
        accepted.close()
        dialer.join()

        assert session._ensure_callhome_listener() is listener  # pylint: disable=protected-access

        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.3, stop.set)
        started = time.monotonic()
        assert await session._accept_call_home(stop) is None  # pylint: disable=protected-access
        assert time.monotonic() - started < 5, "stop must interrupt a quiet accept within the slice"

        # the whole loop, stopped while nobody has dialed in: shutting the
        # adapter down is not an outage of the O-RU connection
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, timeout=10)
        assert alarms.events == [], "stopping while waiting for the dial-in must not raise 1003"

    try:
        asyncio.run(scenario())
    finally:
        session._close_callhome_listener()  # pylint: disable=protected-access


def _sim_args():
    return dict(
        ru_netconf_host=os.getenv("MOCK_RU_HOST", "ocudu-mock-ru"),
        ru_netconf_port=int(os.getenv("MOCK_RU_SSH_PORT", "830")),
        ru_netconf_username=os.getenv("NETCONF_USERNAME", "root"),
        ru_netconf_password=os.getenv("NETCONF_PASSWORD", "root"),
    )


# The call-home integration needs a mock RU started with
# --enable-callhome <this host>:<port> (it dials us); set
# MOCK_RU_CALLHOME_PORT to the port that mock is dialing to enable it.
needs_callhome_mock = mark.skipif(
    not os.getenv("MOCK_RU_CALLHOME_PORT"),
    reason="needs a mock RU dialing this host (--enable-callhome; set MOCK_RU_CALLHOME_PORT)",
)


@needs_callhome_mock
@mark.timeout(240)  # two runs of a 90 s wait plus a 15 s stop each, worst case
def test_callhome_session_against_sim(o1_adapter_src):
    """The sim dials our listener (RFC 8071): the session accepts, runs both
    roles on the single connection (netopeer2 advertises :interleave), holds
    it unsupervised (the sim rejects every reset), and — after a stop — a
    fresh session is re-dialed by the sim's persistent call-home policy, the
    reconnect story with the roles inverted."""
    alarms = _RecorderAlarms()

    def run_once():
        session, _ = _make_session(
            o1_adapter_src,
            alarms,
            None,
            ru_netconf_username=os.getenv("NETCONF_USERNAME", "root"),
            ru_netconf_password=os.getenv("NETCONF_PASSWORD", "root"),
            ru_callhome=True,
            ru_callhome_port=int(os.environ["MOCK_RU_CALLHOME_PORT"]),
            ru_callhome_bind=os.getenv("MOCK_RU_CALLHOME_BIND", "0.0.0.0"),
        )
        session.connect_factory = None  # real accept
        session.poll_cap = 0.5
        session.retry_interval = 1
        # a listener that cannot bind (port taken, wrong bind address) would
        # otherwise surface as a 90 s wait for a dial-in that cannot come
        listener = session._ensure_callhome_listener()  # pylint: disable=protected-access
        assert listener is not None, "call-home listener failed to bind"
        cycles = []
        session.register_cycle_handler(lambda ru_config: cycles.append(bool(ru_config.get_uplane_config())))
        shared = {}

        async def scenario():
            stop = asyncio.Event()
            task = asyncio.create_task(session.run(stop))
            try:
                deadline = time.monotonic() + 90
                while not (cycles and session.stats["watchdog_rpc_errors"] >= 1):
                    assert time.monotonic() < deadline
                    assert not task.done(), f"session loop exited early: {task}"
                    await asyncio.sleep(0.2)
                shared["single"] = session.command_session is session.notification_session
            finally:
                stop.set()
                await asyncio.wait_for(task, timeout=15)

        asyncio.run(scenario())
        assert session.stats["callhome_accepts"] == 1
        assert shared["single"] is True, "both roles must ride the one dialed-in session"
        assert cycles and cycles[0] is True, "command traffic works on the shared session"

    run_once()
    # the sim's persistent call-home re-dials after the close: a fresh
    # session must be accepted again (reconnect with inverted roles)
    run_once()


@mark.timeout(120)
def test_holds_connection_unsupervised_against_sim(o1_adapter_src):
    """On the mock RU every watchdog reset is rejected (no supervision app):
    the session must hold both real sessions and the live subscription WITHOUT
    claiming SUPERVISED, and rejected resets must not drive reconnect churn."""
    from mplane_session import RuSessionState

    alarms = _RecorderAlarms()
    session, app_state = _make_session(o1_adapter_src, alarms, None, **_sim_args())
    session.connect_factory = None  # real NETCONF connects
    session.poll_cap = 0.5

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        try:
            deadline = time.monotonic() + 60
            while session.stats["watchdog_rpc_errors"] < 1:
                assert time.monotonic() < deadline
                assert not task.done(), f"session loop exited early: {task}"
                await asyncio.sleep(0.2)
            await asyncio.sleep(1.0)  # give a wrong implementation time to flip phase or recycle
            assert session.phase is not RuSessionState.SUPERVISED, "rejected resets must not claim SUPERVISED"
            assert app_state.session_state["ru_supervised"] is False
            assert session.stats["connect_cycles"] == 1, "rejected resets must not tear the session down"
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=15)

    asyncio.run(scenario())
    assert app_state.session_state["ru_supervised"] is False


@mark.timeout(180)
def test_command_session_death_reconnects_against_sim(o1_adapter_src):
    """Killing the command session mid-flight drives a full reconnect cycle
    back to a live pair of sessions (the sim never grants SUPERVISED, so the
    cycle is observed via watchdog attempts, not phase)."""
    alarms = _RecorderAlarms()
    session, _ = _make_session(o1_adapter_src, alarms, None, **_sim_args())
    session.connect_factory = None
    session.poll_cap = 0.5

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        try:
            deadline = time.monotonic() + 60
            while session.stats["watchdog_rpc_errors"] < 1:
                assert time.monotonic() < deadline and not task.done()
                await asyncio.sleep(0.2)
            await asyncio.to_thread(session.command_session.close_session)
            deadline = time.monotonic() + 60
            while not (session.stats["connect_cycles"] >= 2 and session.stats["watchdog_rpc_errors"] >= 2):
                assert time.monotonic() < deadline
                assert not task.done(), f"session loop exited early: {task}"
                await asyncio.sleep(0.2)
            # 1003 covers the outage: raised when the command session died and
            # cleared once the fresh pair is up, although the sim (rejecting
            # every reset) never grants SUPERVISED
            await _wait_until(task, lambda: ("clear", 1003) in alarms.events, timeout=10, what="1003 clear")
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=15)

    asyncio.run(scenario())
    assert [e for e in alarms.events if e[1] == 1003] == [("set", 1003), ("clear", 1003)]


@needs_ru_injection
@mark.timeout(180)
def test_injected_supervision_roundtrip_against_sim(o1_adapter_src, inject_ru_notification):
    """A sysrepocfg-injected supervision-notification reaches the session's
    listener and triggers a watchdog reset attempt on the subscription
    session — which the sim rejects, so the phase must still not read
    SUPERVISED."""
    from mplane_session import RuSessionState

    alarms = _RecorderAlarms()
    session, _ = _make_session(o1_adapter_src, alarms, None, **_sim_args())
    session.connect_factory = None
    session.poll_cap = 0.5

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        try:
            deadline = time.monotonic() + 60
            while session.stats["watchdog_rpc_errors"] < 1:
                assert time.monotonic() < deadline and not task.done()
                await asyncio.sleep(0.2)
            rpc_errors_before = session.stats["watchdog_rpc_errors"]
            await asyncio.to_thread(
                inject_ru_notification,
                SUPERVISION_PAYLOAD.format(session_id=session.notification_session.session_id),
            )
            deadline = time.monotonic() + 60
            while session.stats["supervision_notifications"] < 1:
                assert time.monotonic() < deadline
                assert not task.done(), f"session loop exited early: {task}"
                await asyncio.sleep(0.2)
            assert session.stats["watchdog_rpc_errors"] > rpc_errors_before, "notification must trigger a reset"
            assert session.phase is not RuSessionState.SUPERVISED, "a rejected reset must not claim SUPERVISED"
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=15)

    asyncio.run(scenario())


@mark.timeout(60)
def test_command_keepalive_period_beats_the_idle_reap(o1_adapter_src):
    """The command-session keepalive must fire inside the server's ~180 s idle
    reap regardless of the supervision interval: interval/2 normally, but a
    large interval whose interval/2 would miss the reap is clamped under it."""
    from mplane_session import MplaneSession

    _ = o1_adapter_src
    assert MplaneSession._command_keepalive_period(60) == 30.0  # interval/2
    assert MplaneSession._command_keepalive_period(0.1) == 0.05  # floor
    clamped = MplaneSession._command_keepalive_period(600)  # interval/2 == 300 would miss the ~180 s reap
    assert clamped == 150.0
    assert clamped < MplaneSession._COMMAND_SESSION_REAP_S


@mark.timeout(60)
def test_notification_dispatch_runs_off_the_event_loop(o1_adapter_src):
    """A blocking notification handler (the fault bridge's VES emit does a
    synchronous requests.post) must not freeze the event loop. Dispatch is
    offloaded to a thread, so a coroutine ON the loop can release the handler
    while it blocks — impossible if dispatch ran inline on the loop, where the
    releasing coroutine could never run and the handler would time out."""
    import threading

    alarms = _RecorderAlarms()
    in_handler = threading.Event()
    release = threading.Event()
    result = {}
    command = _FakeSession()
    notification = _FakeSession(notifications=[ALARM_PAYLOAD, SUPERVISION_PAYLOAD.format(session_id=1)])
    session, _ = _make_session(o1_adapter_src, alarms, _pair_factory([command, notification]))

    def blocking_handler(_xml):
        in_handler.set()
        result["released"] = release.wait(timeout=5)

    session.register_notification_handler(blocking_handler)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        try:
            deadline = time.monotonic() + 8
            while not in_handler.is_set():
                assert time.monotonic() < deadline and not task.done()
                await asyncio.sleep(0.02)
            release.set()  # runs ON the loop; only reachable while the handler blocks if dispatch is off-loop
            deadline = time.monotonic() + 8
            while session.stats["supervision_notifications"] < 1:
                assert time.monotonic() < deadline and not task.done()
                await asyncio.sleep(0.02)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=10)

    asyncio.run(scenario())
    assert result.get("released") is True, "the loop released the handler -> dispatch ran off the event loop"
    assert session.stats["other_notifications"] == 1


@mark.timeout(60)
@mark.parametrize(
    ("profile", "ru_forward", "expected"),
    [
        ("gnb", False, (True, True)),
        ("cu", False, (True, True)),
        ("cucp", False, (True, True)),
        ("cuup", False, (True, True)),
        ("du", False, (True, True)),
        ("ru", False, (False, False)),
        ("ru", True, (True, False)),
    ],
    ids=["gnb", "cu", "cucp", "cuup", "du", "ru", "ru-forward"],
)
def test_du_facing_loops_gating(o1_adapter_src, profile, ru_forward, expected):
    """netconf_main (northbound NETCONF server that configures a DU/gnb) and
    ws_handler (consumes a DU/gnb PM-telemetry websocket) are DU-facing. Every
    DU/gnb profile runs both. --profile ru manages an O-RU over the M-plane and
    has neither a DU to configure nor a telemetry websocket, so both loops stay
    off — otherwise they spin a permanent connect-retry / 1001-1002 alarm flap
    against nothing. --ru_forward is the one ru-profile consumer of the NETCONF
    loop (RU->DU state sync) and keeps it running; the websocket stays off.
    The helper returns (run_netconf_main, run_ws_handler)."""
    _ = o1_adapter_src
    from mplane_session import du_facing_loops_enabled

    assert du_facing_loops_enabled(profile, ru_forward) == expected
