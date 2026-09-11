# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""VES alarm mapping tests (adapter ves.VesMessages.send_alarm_event).

The notifier renders AlarmManager events as 3GPP TS 28.532 notifyNewAlarm /
notifyClearedAlarm VES events: the AlarmType as the TS 28.532 eventType
literal, the raiser's details as additionalInformation (their fault-text as
the specificProblem, their event-time as eventTime), the message as
additionalText. The rendered JSON is captured in place of the HTTP post.
"""

import ast
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pytest import mark

RU_DETAILS = {
    "fault-id": "17",
    "fault-source": "sync",
    "affected-objects": "Tx-Array-Carrier-00",
    "fault-text": "No external sync source",
    "event-time": "2026-01-01T00:00:01Z",
}


def _capture(o1_adapter_src, monkeypatch):
    """A VesMessages whose post is replaced by a JSON capture; templates resolve from the adapter root."""
    from ves import VesMessages

    monkeypatch.chdir(Path(o1_adapter_src).parent)
    sent = []
    ves = VesMessages("http", logging=logging)
    ves._send_ves_message = lambda msg: sent.append(json.loads(msg))  # pylint: disable=protected-access
    return ves, sent


def _event(o1_adapter_src, **overrides):  # noqa: ARG001 - fixture puts src on sys.path
    from alarm_manager import AlarmEvent, AlarmSeverity, AlarmTrend, AlarmType

    fields = {
        "alarm_id": 4017,
        "name": "RU_FAULT_17",
        "alarm_type": AlarmType.EQUIPMENT,
        "became_active": True,
        "old_severity": AlarmSeverity.CLEARED,
        "new_severity": AlarmSeverity.MAJOR,
        "trend": AlarmTrend.NO_CHANGE,
        "timestamp": datetime.now(timezone.utc),
        "message": "No external sync source; sources: sync",
        "details": dict(RU_DETAILS),
    }
    fields.update(overrides)
    return AlarmEvent(**fields)


def _data(sent):
    return sent[-1]["event"]["stndDefinedFields"]["data"]


@mark.timeout(60)
def test_ru_fault_event_maps_the_fault_leaves(o1_adapter_src, monkeypatch):
    """An O-RU fault arrives with its leaves in the notification's own fields."""
    ves, sent = _capture(o1_adapter_src, monkeypatch)
    ves.send_alarm_event(_event(o1_adapter_src))
    data = _data(sent)
    assert data["alarmId"] == "4017" and data["alarmType"] == "EQUIPMENT_ALARM"
    assert data["notificationType"] == "notifyNewAlarm" and data["perceivedSeverity"] == "MAJOR"
    assert data["eventTime"] == "2026-01-01T00:00:01Z"
    assert data["probableCause"] == "RU_FAULT_17" and data["specificProblem"] == "No external sync source"
    assert data["additionalText"] == "No external sync source; sources: sync"
    assert data["additionalInformation"] == RU_DETAILS
    assert "proposedRepairActions" not in data, "o-ran-fm has no repair-action leaf: no canned text for O-RU faults"
    assert data["trendIndication"] == "NO_CHANGE"
    header = sent[-1]["event"]["commonEventHeader"]
    assert header["sourceName"] == "ocududu", "the O-RU stays a component of the DU's managed element"


@mark.timeout(60)
def test_clear_event_is_a_cleared_alarm_notification(o1_adapter_src, monkeypatch):
    """A clear renders notifyClearedAlarm with CLEARED severity and the clear's time."""
    from alarm_manager import AlarmSeverity, AlarmTrend

    ves, sent = _capture(o1_adapter_src, monkeypatch)
    ves.send_alarm_event(
        _event(
            o1_adapter_src,
            became_active=False,
            old_severity=AlarmSeverity.MAJOR,
            new_severity=AlarmSeverity.CLEARED,
            trend=AlarmTrend.LESS_SEVERE,
            details={**RU_DETAILS, "event-time": "2026-01-01T00:05:00Z"},
        )
    )
    data = _data(sent)
    assert data["notificationType"] == "notifyClearedAlarm" and data["perceivedSeverity"] == "CLEARED"
    assert data["eventTime"] == "2026-01-01T00:05:00Z" and data["trendIndication"] == "LESS_SEVERE"


@mark.timeout(60)
def test_du_alarm_event_keeps_its_fields_with_the_3gpp_literal(o1_adapter_src, monkeypatch):
    """The DU's own alarms go through the same path: a real eventType literal, an
    ISO 8601 eventTime, no canned repair text, no additionalInformation."""
    from alarm_manager import AlarmType

    ves, sent = _capture(o1_adapter_src, monkeypatch)
    ves.send_alarm_event(
        _event(
            o1_adapter_src,
            alarm_id=1003,
            name="RU_NETCONF_CONNECTION_LOSS",
            alarm_type=AlarmType.COMMUNICATIONS,
            message=None,
            details=None,
        )
    )
    data = _data(sent)
    assert data["alarmId"] == "1003" and data["alarmType"] == "COMMUNICATIONS_ALARM"
    assert data["specificProblem"] == "RU_NETCONF_CONNECTION_LOSS" and data["additionalText"] == "ocudu"
    assert "proposedRepairActions" not in data and data["additionalInformation"] == {}
    assert data["eventTime"].endswith("Z") and not data["eventTime"].endswith("+00:00Z")
    assert datetime.fromisoformat(data["eventTime"].replace("Z", "+00:00")).tzinfo is not None


@mark.timeout(60)
def test_free_text_is_json_safe(o1_adapter_src, monkeypatch):
    """Quotes and backslashes in fault text survive the template as valid JSON."""
    ves, sent = _capture(o1_adapter_src, monkeypatch)
    text = 'PA "over-temperature" \\ limit'
    ves.send_alarm_event(_event(o1_adapter_src, message=text, details={"fault-text": text, "fault-source": "pa"}))
    assert _data(sent)["specificProblem"] == text and _data(sent)["additionalText"] == text


@mark.timeout(60)
def test_alarm_type_literals_cover_the_registry(o1_adapter_src, monkeypatch):
    """Every AlarmType renders a TS 28.532 eventType literal, never the enum's repr."""
    from alarm_manager import AlarmType

    ves, sent = _capture(o1_adapter_src, monkeypatch)
    for alarm_type in AlarmType:
        ves.send_alarm_event(_event(o1_adapter_src, alarm_type=alarm_type, details=None))
        assert _data(sent)["alarmType"].isupper() and "." not in _data(sent)["alarmType"], alarm_type


@mark.timeout(60)
def test_device_reported_leaves_win(o1_adapter_src, monkeypatch):
    """What the O-RU reported on o-ran-fm 2024-08-12 takes precedence: its
    alarm-type, probable-cause, specific-problem and proposed-repair-actions
    render as the matching fields, its additional-information pairs ride next
    to the leaf-name keys."""
    ves, sent = _capture(o1_adapter_src, monkeypatch)
    ves.send_alarm_event(
        _event(
            o1_adapter_src,
            details={
                **RU_DETAILS,
                "alarm-type": "COMMUNICATIONS-ALARM",
                "probable-cause": "lossOfSignal",
                "specific-problem": "CRC errors on port 0",
                "proposed-repair-actions": "check the fibre",
                "port": "0",
            },
        )
    )
    data = _data(sent)
    assert data["alarmType"] == "COMMUNICATIONS_ALARM", "the O-RU's type, not the registry's EQUIPMENT"
    assert data["probableCause"] == "lossOfSignal" and data["specificProblem"] == "CRC errors on port 0"
    assert data["proposedRepairActions"] == "check the fibre"
    assert data["additionalInformation"]["port"] == "0" and data["additionalInformation"]["fault-id"] == "17"


@mark.timeout(60)
def test_notification_id_advances_per_event_even_when_delivery_fails(o1_adapter_src, monkeypatch):
    """notificationId identifies the notification (TS 28.532): it is the sender's
    per-event sequence and advances whether or not the collector took the post."""
    import requests
    from ves import VesMessages

    monkeypatch.chdir(Path(o1_adapter_src).parent)
    posted = []

    def failing_post(url, data=None, **kwargs):  # noqa: ARG001 - requests.post signature
        posted.append(ast.literal_eval(data))
        raise requests.exceptions.ConnectionError("collector down")

    monkeypatch.setattr("ves.requests.post", failing_post)
    ves = VesMessages("http", logging=logging)
    for _ in range(3):
        ves.send_alarm_event(_event(o1_adapter_src))
    ids = [post["event"]["stndDefinedFields"]["data"]["notificationId"] for post in posted]
    sequences = [post["event"]["commonEventHeader"]["sequence"] for post in posted]
    assert ids == [0, 1, 2] and sequences == [0, 1, 2], (ids, sequences)
