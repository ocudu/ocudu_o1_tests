# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Fault-management bridge tests (adapter ru_fm_bridge.RuFaultBridge).

The bridge mirrors O-RU o-ran-fm faults into the adapter's AlarmManager
(adapter alarm id = 4000 + fault-id), from where the existing notifier fans
out to VES. Unit tests drive the bridge against a REAL AlarmManager with a
recording notifier — dynamic registration, severity mapping and event
emission are asserted on the true code path. Integration tests run the full
chain (mock RU -> MplaneSession -> bridge -> AlarmManager) with alarm-notifs
injected via sysrepocfg.
"""

import asyncio
import logging
import os
import time
from types import SimpleNamespace

from conftest import needs_ru_injection
from ncclient.operations import rpc as rpc_ops
from ncclient.transport import errors as transport_errors
from pytest import mark, raises

logger = logging.getLogger(__name__)

NOTIFICATION_WRAPPER = (
    '<notification xmlns="urn:ietf:params:xml:ns:netconf:notification:1.0">'
    "<eventTime>2026-01-01T00:00:00Z</eventTime>{payload}</notification>"
)
SUPERVISION_PAYLOAD = (
    '<supervision-notification xmlns="urn:o-ran:supervision:1.0">'
    "<session-id>{session_id}</session-id></supervision-notification>"
)


def _alarm_payload(fault_id, severity="MAJOR", cleared="false", text="unit under test", source="o-ru"):
    return (
        f'<alarm-notif xmlns="urn:o-ran:fm:1.0"><fault-id>{fault_id}</fault-id>'
        f"<fault-source>{source}</fault-source><fault-severity>{severity}</fault-severity>"
        f"<is-cleared>{cleared}</is-cleared><fault-text>{text}</fault-text>"
        f"<event-time>2026-01-01T00:00:00Z</event-time></alarm-notif>"
    )


def _wrapped(payload):
    return NOTIFICATION_WRAPPER.format(payload=payload)


def _make_bridge(o1_adapter_src):  # noqa: ARG001 - fixture puts src on sys.path
    """A bridge over a real AlarmManager with a recording notifier."""
    from alarm_manager import AlarmManager
    from ru_fm_bridge import RuFaultBridge

    events = []
    manager = AlarmManager(notifier=events.append)
    return RuFaultBridge(manager), manager, events


class _StubRpcError(rpc_ops.RPCError):
    """RPCError stand-in that skips the base class's rpc-reply XML parsing."""

    def __init__(self, message="access-denied"):  # pylint: disable=super-init-not-called
        Exception.__init__(self, message)


@mark.timeout(60)
def test_alarm_notif_raises_and_clears(o1_adapter_src):
    """An alarm-notif registers, raises and clears the mirrored adapter alarm,
    emitting notifier events (the VES seam) on both transitions."""
    from alarm_manager import AlarmSeverity

    bridge, manager, events = _make_bridge(o1_adapter_src)

    bridge.handle_notification(_wrapped(_alarm_payload(7, severity="MAJOR")))
    assert manager.is_active(4007)
    state = manager.get_state(4007)
    assert state.severity is AlarmSeverity.MAJOR
    assert "unit under test" in state.last_message and "o-ru" in state.last_message
    assert state.definition.name == "RU_FAULT_7"

    bridge.handle_notification(_wrapped(_alarm_payload(7, cleared="true")))
    assert not manager.is_active(4007)

    assert [event.became_active for event in events] == [True, False]
    assert events[0].new_severity is AlarmSeverity.MAJOR


@mark.timeout(60)
def test_severity_mapping_and_fallback(o1_adapter_src):
    """o-ran-fm literals map 1:1 onto AlarmSeverity; unknown literals fall
    back to INDETERMINATE instead of failing."""
    from alarm_manager import AlarmSeverity

    bridge, manager, _ = _make_bridge(o1_adapter_src)
    for fault_id, literal, expected in (
        (1, "CRITICAL", AlarmSeverity.CRITICAL),
        (2, "MAJOR", AlarmSeverity.MAJOR),
        (3, "MINOR", AlarmSeverity.MINOR),
        (4, "WARNING", AlarmSeverity.WARNING),
        (5, "BOGUS", AlarmSeverity.INDETERMINATE),
        (6, "CLEARED", AlarmSeverity.INDETERMINATE),  # an AlarmSeverity name, not an o-ran-fm literal
    ):
        bridge.handle_notification(_wrapped(_alarm_payload(fault_id, severity=literal)))
        assert manager.get_state(4000 + fault_id).severity is expected


@mark.timeout(60)
def test_noise_and_malformed_alarms_ignored(o1_adapter_src):
    """Non-alarm notifications and alarms without a fault-id never touch the manager."""
    bridge, manager, events = _make_bridge(o1_adapter_src)

    bridge.handle_notification(_wrapped(SUPERVISION_PAYLOAD.format(session_id=1)))
    no_fault_id = (
        '<alarm-notif xmlns="urn:o-ran:fm:1.0"><fault-source>ru</fault-source>'
        "<fault-severity>MAJOR</fault-severity><is-cleared>false</is-cleared>"
        "<event-time>2026-01-01T00:00:00Z</event-time></alarm-notif>"
    )
    bridge.handle_notification(_wrapped(no_fault_id))

    assert events == []
    assert manager.active_alarms() == []


@mark.timeout(60)
def test_reconcile_synchronises_the_mirror(o1_adapter_src):
    """Reconcile applies the RU's list and clears mirrored faults it no longer
    reports — the restart/reconnect catch-up path."""
    bridge, manager, events = _make_bridge(o1_adapter_src)
    bridge.handle_notification(_wrapped(_alarm_payload(1, severity="CRITICAL")))
    bridge.handle_notification(_wrapped(_alarm_payload(3, severity="MINOR")))
    assert manager.is_active(4001) and manager.is_active(4003)

    strict_calls = []

    def active_list(strict=False):
        strict_calls.append(strict)
        return [
            {"fault_id": 1, "fault_severity": "CRITICAL", "is_cleared": False, "fault_text": "unit under test"},
            {"fault_id": 7, "fault_severity": "MAJOR", "is_cleared": False, "fault_text": "new while away"},
            {"fault_id": 30, "fault_severity": "MAJOR", "is_cleared": True, "fault_text": "residual clear"},
            {"fault_id": None, "fault_severity": "MAJOR", "is_cleared": False},
        ]

    ru_config = SimpleNamespace(get_active_alarms=active_list)
    bridge.reconcile(ru_config)

    assert strict_calls == [True], "reconcile must read the list strictly"
    assert manager.is_active(4001), "fault still reported stays active"
    assert manager.is_active(4007), "fault raised while disconnected is picked up"
    assert not manager.is_active(4003), "fault gone from the list is cleared"
    assert "active-alarm-list" in manager.get_state(4003).last_message
    with raises(KeyError):
        manager.get_state(4030)  # never-seen residual clear registers nothing

    # a second identical reconcile is a full no-op northbound
    events_before = len(events)
    bridge.reconcile(ru_config)
    assert len(events) == events_before, "unchanged reconcile must emit no events"


@mark.timeout(60)
def test_multi_source_fault_aggregates_without_churn(o1_adapter_src):
    """Several sources reporting one fault-id are one adapter alarm at the most
    severe of their severities; an unchanged list or a repeated live report
    emits nothing northbound."""
    from alarm_manager import AlarmSeverity

    bridge, manager, events = _make_bridge(o1_adapter_src)
    listed = [
        {"fault_id": 5, "fault_source": "tx-array-0", "fault_severity": "MAJOR", "is_cleared": False, "fault_text": "t"},
        {"fault_id": 5, "fault_source": "tx-array-1", "fault_severity": "CRITICAL", "is_cleared": False, "fault_text": "t"},
    ]
    ru_config = SimpleNamespace(get_active_alarms=lambda strict=False: listed)

    bridge.reconcile(ru_config)
    assert len(events) == 1, "one fault-id, one event"
    assert manager.get_state(4005).severity is AlarmSeverity.CRITICAL
    assert "tx-array-0,tx-array-1" in manager.get_state(4005).last_message

    bridge.reconcile(ru_config)
    bridge.reconcile(ru_config)
    bridge.handle_notification(_wrapped(_alarm_payload(5, source="tx-array-0", severity="MAJOR", text="t")))
    assert len(events) == 1, "unchanged reconciles and a repeated live report emit nothing"

    bridge.handle_notification(_wrapped(_alarm_payload(5, source="tx-array-1", cleared="true", text="t")))
    assert manager.is_active(4005), "a source's clear re-aggregates: the other source still reports it"
    assert [event.new_severity for event in events] == [AlarmSeverity.CRITICAL, AlarmSeverity.MAJOR]

    bridge.handle_notification(_wrapped(_alarm_payload(5, source="tx-array-0", cleared="true", text="t")))
    assert not manager.is_active(4005) and events[-1].became_active is False


@mark.timeout(60)
def test_reconcile_read_failure_propagates_without_clearing(o1_adapter_src):
    """An unreadable active-alarm-list must raise (recycling the session), not
    masquerade as empty and falsely clear every mirrored fault."""
    bridge, manager, _ = _make_bridge(o1_adapter_src)
    bridge.handle_notification(_wrapped(_alarm_payload(2, severity="MAJOR")))

    def failing_read(strict=False):  # noqa: ARG001
        raise transport_errors.TransportError("read failed")

    with raises(transport_errors.TransportError):
        bridge.reconcile(SimpleNamespace(get_active_alarms=failing_read))
    assert manager.is_active(4002), "a failed read must not clear the mirror"


@mark.timeout(60)
def test_reconcile_rpc_error_keeps_the_mirror(o1_adapter_src):
    """An O-RU that refuses the list read (rpc-error: alive but rejected) must
    not recycle the session cycle after cycle; the mirror is kept as it is."""
    bridge, manager, events = _make_bridge(o1_adapter_src)
    bridge.handle_notification(_wrapped(_alarm_payload(2, severity="MAJOR")))

    def refused_read(strict=False):  # noqa: ARG001
        raise _StubRpcError("access-denied")

    bridge.reconcile(SimpleNamespace(get_active_alarms=refused_read))
    assert manager.is_active(4002) and len(events) == 1, "a refused read leaves the mirror untouched"


@mark.timeout(60)
def test_fault_id_outside_uint16_is_ignored(o1_adapter_src):
    """Only a uint16 fault-id maps into the reserved range; anything else is
    logged and ignored on both feeds, and the top of the range still retires."""
    bridge, manager, _ = _make_bridge(o1_adapter_src)
    bridge.handle_notification(_wrapped(_alarm_payload(70000)))
    bridge.handle_notification(_wrapped(_alarm_payload(-1)))
    listed = [
        {"fault_id": 65536, "fault_severity": "MAJOR", "is_cleared": False},
        {"fault_id": 65535, "fault_severity": "MAJOR", "is_cleared": False},
    ]
    bridge.reconcile(SimpleNamespace(get_active_alarms=lambda strict=False: listed))
    assert [state.definition.alarm_id for state in manager.active_alarms()] == [4000 + 65535]

    bridge.reconcile(SimpleNamespace(get_active_alarms=lambda strict=False: []))
    assert manager.active_alarms() == []


@mark.timeout(60)
def test_warning_fault_retired_only_by_reconcile(o1_adapter_src):
    """Per o-ran-fm, WARNING faults never get a clear notification; an empty
    reconcile is what retires them."""
    bridge, manager, _ = _make_bridge(o1_adapter_src)
    bridge.handle_notification(_wrapped(_alarm_payload(9, severity="WARNING")))
    assert manager.is_active(4009)

    bridge.reconcile(SimpleNamespace(get_active_alarms=lambda strict=False: []))
    assert not manager.is_active(4009)


@mark.timeout(60)
def test_recreated_bridge_ignores_unattributable_clear_until_reconcile(o1_adapter_src):
    """A bridge recreated over a live manager cannot attribute a clear to a
    source it never saw — the clear is ignored and the first reconcile heals
    the mirror from the manager's own active state."""
    from ru_fm_bridge import RuFaultBridge

    bridge, manager, _ = _make_bridge(o1_adapter_src)
    bridge.handle_notification(_wrapped(_alarm_payload(5)))

    second = RuFaultBridge(manager)
    second.handle_notification(_wrapped(_alarm_payload(5, cleared="true")))
    assert manager.is_active(4005), "an unattributable clear must not retire the alarm"

    second.reconcile(SimpleNamespace(get_active_alarms=lambda strict=False: []))
    assert not manager.is_active(4005), "reconcile heals from the manager's active state"


@mark.timeout(60)
def test_per_source_identity(o1_adapter_src):
    """(fault-id, fault-source) is the alarm identity: the mirror stays active
    until the LAST source clears."""
    bridge, manager, _ = _make_bridge(o1_adapter_src)
    bridge.handle_notification(_wrapped(_alarm_payload(9, source="tx-array-0")))
    bridge.handle_notification(_wrapped(_alarm_payload(9, source="tx-array-1")))
    assert manager.is_active(4009)

    bridge.handle_notification(_wrapped(_alarm_payload(9, cleared="true", source="tx-array-0")))
    assert manager.is_active(4009), "another source still reports the fault"

    bridge.handle_notification(_wrapped(_alarm_payload(9, cleared="true", source="tx-array-1")))
    assert not manager.is_active(4009), "last source's clear retires the alarm"


@mark.timeout(60)
def test_severity_escalation_reaches_northbound(o1_adapter_src):
    """Re-raising an active fault at a higher severity updates the mirror and
    emits a second event."""
    from alarm_manager import AlarmSeverity

    bridge, manager, events = _make_bridge(o1_adapter_src)
    bridge.handle_notification(_wrapped(_alarm_payload(8, severity="MINOR")))
    bridge.handle_notification(_wrapped(_alarm_payload(8, severity="CRITICAL")))

    assert manager.get_state(4008).severity is AlarmSeverity.CRITICAL
    assert [event.new_severity for event in events] == [AlarmSeverity.MINOR, AlarmSeverity.CRITICAL]


@mark.timeout(60)
def test_notifier_failure_does_not_desync_the_mirror(o1_adapter_src):
    """A notifier exception surfaces as RuntimeError from set_alarm, but the
    bridge's bookkeeping already happened — the next empty reconcile retires
    the alarm instead of leaking it forever."""
    from alarm_manager import AlarmManager
    from ru_fm_bridge import RuFaultBridge

    failures = [RuntimeError("VES down")]

    def flaky_notifier(event):  # noqa: ARG001
        if failures:
            raise failures.pop()

    manager = AlarmManager(notifier=flaky_notifier)
    bridge = RuFaultBridge(manager)
    with raises(RuntimeError):
        bridge.handle_notification(_wrapped(_alarm_payload(6)))
    assert manager.is_active(4006), "manager state mutates before the notifier runs"

    bridge.reconcile(SimpleNamespace(get_active_alarms=lambda strict=False: []))
    assert not manager.is_active(4006), "reconcile must retire the alarm despite the earlier notifier crash"


@mark.timeout(60)
def test_notifier_failure_during_reconcile_does_not_stop_the_sweep(o1_adapter_src):
    """One failing notifier call in the sweep is logged; the remaining list
    entries are still applied and the stale fault still retired."""
    from alarm_manager import AlarmManager
    from ru_fm_bridge import RuFaultBridge

    _ = o1_adapter_src
    failures = [RuntimeError("VES down")]

    def flaky_notifier(event):  # noqa: ARG001
        if failures:
            raise failures.pop()

    manager = AlarmManager(notifier=flaky_notifier)
    bridge = RuFaultBridge(manager)
    with raises(RuntimeError):
        bridge.handle_notification(_wrapped(_alarm_payload(9)))
    listed = [
        {"fault_id": 2, "fault_severity": "MAJOR", "is_cleared": False},
        {"fault_id": 3, "fault_severity": "MINOR", "is_cleared": False},
    ]
    failures.append(RuntimeError("VES down again"))
    bridge.reconcile(SimpleNamespace(get_active_alarms=lambda strict=False: listed))
    assert manager.is_active(4002) and manager.is_active(4003), "the sweep continues past a failing notifier"
    assert not manager.is_active(4009), "the stale fault is still retired"


class _FakeCommandSession:
    """Command-session double whose operational get returns an empty data reply."""

    server_capabilities = ()
    connected = True

    def __init__(self):
        self.dispatched = []

    def dispatch(self, element):
        self.dispatched.append(element)
        return SimpleNamespace(xml="<ok/>")

    def get(self, filter=None, with_defaults=None):  # noqa: A002 - ncclient signature
        return SimpleNamespace(
            xml='<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><data/></rpc-reply>'
        )

    def close_session(self):
        self.connected = False


class _FakeNotificationSession:
    """Notification-session double replaying scripted payloads."""

    connected = True

    def __init__(self, notifications):
        self.script = list(notifications)
        self.subscribed = False

    def create_subscription(self):
        self.subscribed = True

    def dispatch(self, element):  # noqa: ARG002 - watchdog resets ride the subscription session
        return SimpleNamespace(xml="<ok/>")

    def take_notification(self, block=True, timeout=None):  # noqa: ARG002 - ncclient signature
        if not self.script:
            time.sleep(min(timeout or 0.05, 0.05))
            return None
        entry = self.script.pop(0)
        if entry is None:
            time.sleep(min(timeout or 0.05, 0.05))
            return None
        return SimpleNamespace(notification_xml=_wrapped(entry))

    def close_session(self):
        self.connected = False


@mark.timeout(60)
def test_session_bridge_wiring_end_to_end(o1_adapter_src):
    """MplaneSession dispatches alarm-notifs into the bridge and runs the
    reconcile cycle handler with a live RuConfig — the orchestrator wiring,
    minus real sockets."""
    from alarm_defs import AlarmDefinitions
    from alarm_manager import AlarmManager
    from mplane_session import MplaneSession
    from ru_fm_bridge import RuFaultBridge
    from state import AppState

    events = []
    alarm_manager = AlarmManager(AlarmDefinitions.defs, notifier=events.append)
    bridge = RuFaultBridge(alarm_manager)
    reconciled = []

    args = SimpleNamespace(ru_supervision_interval=60, ru_supervision_guard=10, ru_datastore="running")
    session = MplaneSession(AppState(), args, alarm_manager, retry_interval=0.05)
    command = _FakeCommandSession()
    notification = _FakeNotificationSession(
        [_alarm_payload(11, severity="CRITICAL"), SUPERVISION_PAYLOAD.format(session_id=1)]
    )
    pairs = [command, notification]
    session.connect_factory = lambda: pairs.pop(0)
    session.poll_cap = 0.1
    session.register_notification_handler(bridge.handle_notification)
    session.register_cycle_handler(lambda ru_config: (reconciled.append(ru_config), bridge.reconcile(ru_config)))

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        try:
            deadline = time.monotonic() + 10
            while not alarm_manager.active_alarms():
                assert time.monotonic() < deadline and not task.done()
                await asyncio.sleep(0.02)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=10)

    asyncio.run(scenario())

    assert alarm_manager.is_active(4011)
    assert len(reconciled) == 1, "reconcile runs once per connect cycle"
    assert [event.became_active for event in events] == [True]


def _sim_args():
    return SimpleNamespace(
        ru_netconf_host=os.getenv("MOCK_RU_HOST", "ocudu-mock-ru"),
        ru_netconf_port=int(os.getenv("MOCK_RU_SSH_PORT", "830")),
        ru_netconf_username=os.getenv("NETCONF_USERNAME", "root"),
        ru_netconf_password=os.getenv("NETCONF_PASSWORD", "root"),
        ru_supervision_interval=60,
        ru_supervision_guard=10,
        ru_datastore="running",
    )


def _rich_payload(  # pylint: disable=too-many-arguments
    fault_id,
    severity="MAJOR",
    cleared="false",
    text="unit under test",
    source="o-ru",
    event_time="2026-01-01T00:00:00Z",
    affected=("Tx-Array-Carrier-00",),
):
    objects = "".join(f"<affected-objects><name>{name}</name></affected-objects>" for name in affected)
    return (
        f'<alarm-notif xmlns="urn:o-ran:fm:1.0"><fault-id>{fault_id}</fault-id>'
        f"<fault-source>{source}</fault-source>{objects}<fault-severity>{severity}</fault-severity>"
        f"<is-cleared>{cleared}</is-cleared><fault-text>{text}</fault-text>"
        f"<event-time>{event_time}</event-time></alarm-notif>"
    )


@mark.timeout(60)
def test_events_carry_the_fault_leaves_as_details(o1_adapter_src):
    """Raise, re-aggregation and clear each carry the o-ran-fm leaves — fault-id,
    the sources, the affected objects, the text, the event-time — so the
    notifier can put them into the VES notification instead of the log."""
    bridge, manager, events = _make_bridge(o1_adapter_src)

    bridge.handle_notification(
        _wrapped(
            _rich_payload(
                9,
                source="sync",
                affected=("Tx-Array-Carrier-00", "Rx-Array-Carrier-00"),
                event_time="2026-01-01T00:00:01Z",
            )
        )
    )
    assert events[-1].details == {
        "fault-id": "9",
        "fault-source": "sync",
        "affected-objects": "Rx-Array-Carrier-00,Tx-Array-Carrier-00",
        "fault-text": "unit under test",
        "event-time": "2026-01-01T00:00:01Z",
    }
    assert manager.get_state(4009).last_details == events[-1].details

    # a second source at a higher severity: its text and the latest time lead, the sources are joined
    bridge.handle_notification(
        _wrapped(
            _rich_payload(
                9,
                source="ptp",
                severity="CRITICAL",
                text="holdover expired",
                affected=(),
                event_time="2026-01-01T00:00:02Z",
            )
        )
    )
    assert events[-1].details["fault-source"] == "ptp,sync"
    assert events[-1].details["fault-text"] == "holdover expired"
    assert events[-1].details["event-time"] == "2026-01-01T00:00:02Z"
    assert events[-1].details["affected-objects"] == "Rx-Array-Carrier-00,Tx-Array-Carrier-00"

    bridge.handle_notification(
        _wrapped(
            _rich_payload(
                9, source="ptp", cleared="true", text="holdover expired", affected=(), event_time="2026-01-01T00:00:03Z"
            )
        )
    )
    bridge.handle_notification(
        _wrapped(
            _rich_payload(
                9, source="sync", cleared="true", affected=("Tx-Array-Carrier-00",), event_time="2026-01-01T00:00:04Z"
            )
        )
    )
    assert events[-1].became_active is False
    assert events[-1].details == {
        "fault-id": "9",
        "fault-source": "sync",
        "affected-objects": "Tx-Array-Carrier-00",
        "fault-text": "unit under test",
        "event-time": "2026-01-01T00:00:04Z",
    }


@mark.timeout(60)
def test_re_report_with_a_newer_event_time_does_not_churn(o1_adapter_src):
    """An O-RU re-announcing an unchanged fault (same severity, text and
    objects, fresher event-time) emits nothing; a changed text re-emits and
    carries the new time."""
    bridge, _, events = _make_bridge(o1_adapter_src)
    bridge.handle_notification(_wrapped(_rich_payload(11, event_time="2026-01-01T00:00:01Z")))
    bridge.handle_notification(_wrapped(_rich_payload(11, event_time="2026-01-01T00:00:09Z")))
    assert len(events) == 1 and events[0].details["event-time"] == "2026-01-01T00:00:01Z"

    bridge.handle_notification(_wrapped(_rich_payload(11, text="now worse", event_time="2026-01-01T00:00:10Z")))
    assert len(events) == 2
    assert events[1].details["fault-text"] == "now worse" and events[1].details["event-time"] == "2026-01-01T00:00:10Z"


@mark.timeout(60)
def test_reconcile_details_and_stale_retirement(o1_adapter_src):
    """Reconcile carries the listed leaves; a fault retired by reconcile clears with its id only."""
    bridge, manager, events = _make_bridge(o1_adapter_src)
    bridge.handle_notification(_wrapped(_rich_payload(2)))
    listed = [
        {
            "fault_id": 4,
            "fault_source": "fan",
            "fault_severity": "MINOR",
            "is_cleared": False,
            "fault_text": "fan slow",
            "event_time": "2026-01-01T00:00:05Z",
            "affected_objects": ["fan-1"],
        },
    ]
    bridge.reconcile(SimpleNamespace(get_active_alarms=lambda strict=False: listed))
    raised = next(event for event in events if event.alarm_id == 4004)
    assert raised.details == {
        "fault-id": "4",
        "fault-source": "fan",
        "affected-objects": "fan-1",
        "fault-text": "fan slow",
        "event-time": "2026-01-01T00:00:05Z",
    }
    retired = next(event for event in events if event.alarm_id == 4002 and not event.became_active)
    assert retired.details == {"fault-id": "2"}
    assert not manager.is_active(4002)


@mark.timeout(60)
def test_reported_alarm_type_and_leaves_reach_the_registry_and_details(o1_adapter_src):
    """An O-RU on o-ran-fm 2024-08-12 reports its alarm-type and the qualifying
    leaves: the mirrored alarm is registered with that type and the details
    carry probable-cause, specific-problem, proposed-repair-actions and the
    additional-information pairs; a 2022-shape report still types as
    EQUIPMENT; only a fresher event-time on the same leaves emits nothing, a
    changed probable-cause does."""
    from alarm_manager import AlarmType

    bridge, manager, events = _make_bridge(o1_adapter_src)
    rich = (
        '<alarm-notif xmlns="urn:o-ran:fm:1.0"><fault-id>21</fault-id><fault-source>fh0</fault-source>'
        "<fault-severity>MINOR</fault-severity><is-cleared>false</is-cleared><fault-text>Link degraded</fault-text>"
        "<event-time>{time}</event-time><probable-cause>{cause}</probable-cause>"
        "<specific-problem>CRC errors on port 0</specific-problem>"
        "<proposed-repair-actions>check the fibre</proposed-repair-actions>"
        "<additional-information><identifier>port</identifier><information>0</information></additional-information>"
        "<alarm-type>COMMUNICATIONS-ALARM</alarm-type></alarm-notif>"
    )
    bridge.handle_notification(_wrapped(rich.format(time="2026-01-01T00:00:01Z", cause="lossOfSignal")))
    assert manager.get_state(4021).definition.type is AlarmType.COMMUNICATIONS
    details = events[-1].details
    assert details["alarm-type"] == "COMMUNICATIONS-ALARM" and details["probable-cause"] == "lossOfSignal"
    assert details["specific-problem"] == "CRC errors on port 0"
    assert details["proposed-repair-actions"] == "check the fibre" and details["port"] == "0"
    assert details["fault-text"] == "Link degraded" and details["event-time"] == "2026-01-01T00:00:01Z"

    bridge.handle_notification(_wrapped(_rich_payload(9)))
    assert manager.get_state(4009).definition.type is AlarmType.EQUIPMENT
    assert "alarm-type" not in events[-1].details

    seen = len(events)
    bridge.handle_notification(_wrapped(rich.format(time="2026-01-01T00:00:09Z", cause="lossOfSignal")))
    assert len(events) == seen, "a fresher event-time on the same leaves must not emit"
    bridge.handle_notification(_wrapped(rich.format(time="2026-01-01T00:00:10Z", cause="lossOfFrame")))
    assert len(events) == seen + 1 and events[-1].details["probable-cause"] == "lossOfFrame"
    assert events[-1].details["event-time"] == "2026-01-01T00:00:10Z"


@needs_ru_injection
@mark.timeout(240)
def test_injected_fault_reaches_alarm_manager(o1_adapter_src, inject_ru_notification):
    """Full chain against the sim: an injected o-ran-fm alarm-notif lands in
    the adapter's AlarmManager as alarm 4000+fault-id, and its clear
    counterpart retires it — the reconcile on the next cycle would otherwise.

    The sim rejects every watchdog reset, so the session never grants
    SUPERVISED — readiness is observed via watchdog attempts, not phase
    (same idiom as the mplane_session sim tests)."""
    from alarm_defs import AlarmDefinitions
    from alarm_manager import AlarmManager
    from mplane_session import MplaneSession
    from ru_fm_bridge import RuFaultBridge
    from state import AppState

    events = []
    alarm_manager = AlarmManager(AlarmDefinitions.defs, notifier=events.append)
    bridge = RuFaultBridge(alarm_manager)
    session = MplaneSession(AppState(), _sim_args(), alarm_manager, retry_interval=0.5)
    session.poll_cap = 0.5
    session.register_notification_handler(bridge.handle_notification)
    session.register_cycle_handler(bridge.reconcile)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        try:
            deadline = time.monotonic() + 60
            while session.stats["watchdog_rpc_errors"] < 1:
                assert time.monotonic() < deadline and not task.done()
                await asyncio.sleep(0.2)
            await asyncio.to_thread(inject_ru_notification, _alarm_payload(14, severity="MAJOR"))
            deadline = time.monotonic() + 60
            while not (4014 in [state.definition.alarm_id for state in alarm_manager.active_alarms()]):
                assert time.monotonic() < deadline
                assert not task.done(), f"session loop exited early: {task}"
                await asyncio.sleep(0.2)
            await asyncio.to_thread(inject_ru_notification, _alarm_payload(14, cleared="true"))
            deadline = time.monotonic() + 60
            while alarm_manager.is_active(4014):
                assert time.monotonic() < deadline
                assert not task.done(), f"session loop exited early: {task}"
                await asyncio.sleep(0.2)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=15)

    asyncio.run(scenario())
    assert [event.became_active for event in events if event.alarm_id == 4014] == [True, False]


@needs_ru_injection
@mark.timeout(300)
def test_reconnect_reconcile_retires_stale_fault(o1_adapter_src, inject_ru_notification):
    """A fault mirrored before a session death is retired by the next cycle's
    reconcile when the RU no longer reports it (the sim's active-alarm-list
    is empty), closing the missed-clear window. Readiness is observed via
    watchdog attempts — the sim rejects every reset, so SUPERVISED is never
    granted."""
    from alarm_defs import AlarmDefinitions
    from alarm_manager import AlarmManager
    from mplane_session import MplaneSession
    from ru_fm_bridge import RuFaultBridge
    from state import AppState

    alarm_manager = AlarmManager(AlarmDefinitions.defs)
    bridge = RuFaultBridge(alarm_manager)
    session = MplaneSession(AppState(), _sim_args(), alarm_manager, retry_interval=0.5)
    session.poll_cap = 0.5
    session.register_notification_handler(bridge.handle_notification)
    session.register_cycle_handler(bridge.reconcile)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        try:
            deadline = time.monotonic() + 60
            while session.stats["watchdog_rpc_errors"] < 1:
                assert time.monotonic() < deadline and not task.done()
                await asyncio.sleep(0.2)
            await asyncio.to_thread(inject_ru_notification, _alarm_payload(21, severity="MINOR"))
            deadline = time.monotonic() + 60
            while 4021 not in [state.definition.alarm_id for state in alarm_manager.active_alarms()]:
                assert time.monotonic() < deadline and not task.done()
                await asyncio.sleep(0.2)
            # kill the command session: cycle dies, reconnects, reconciles
            await asyncio.to_thread(session.command_session.close_session)
            deadline = time.monotonic() + 90
            while alarm_manager.is_active(4021):
                assert time.monotonic() < deadline
                assert not task.done(), f"session loop exited early: {task}"
                await asyncio.sleep(0.2)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=15)

    asyncio.run(scenario())
    assert session.stats["connect_cycles"] >= 2
    assert "active-alarm-list" in alarm_manager.get_state(4021).last_message
