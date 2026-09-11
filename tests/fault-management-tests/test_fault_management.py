# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Fault management command/response tests for the M-plane client.

The mock RU implements o-ran-fm but raises no alarms of its own (its
active-alarm-list stays empty; alarm-notifs occur only when injected), so:

- the alarm parsers are unit-tested against mocked payloads;
- the o-ran-fm severity literals are locked to the adapter's AlarmSeverity
  members (the mapping ru_fm_bridge relies on);
- reading the active-alarm-list runs live against the sim (graceful empty);
- full alarm round-trips (injected alarm-notifs through the resident session)
  live in test_fm_bridge.py and only run where docker can reach the mock RU
  container (set MOCK_RU_CONTAINER; skipped in compose/CI).
"""

import logging
from types import SimpleNamespace

from ncclient.operations.errors import OperationError
from pytest import mark, raises

logger = logging.getLogger(__name__)

NOTIFICATION_WRAPPER = (
    '<notification xmlns="urn:ietf:params:xml:ns:netconf:notification:1.0">'
    "<eventTime>2026-01-01T00:00:00Z</eventTime>{payload}</notification>"
)
ALARM_NOTIF_PAYLOAD = (
    '<alarm-notif xmlns="urn:o-ran:fm:1.0">'
    "<fault-id>{fault_id}</fault-id><fault-source>{source}</fault-source>"
    "<fault-severity>{severity}</fault-severity><is-cleared>{cleared}</is-cleared>"
    "<event-time>2026-07-15T12:00:00Z</event-time></alarm-notif>"
)


@mark.timeout(60)
def test_parse_alarm_notification_payloads(o1_adapter_src):
    """Full and minimal alarm-notifs parse; non-FM notifications return None."""
    from fm_parsers import parse_alarm_notification

    full = (
        '<alarm-notif xmlns="urn:o-ran:fm:1.0"><fault-id>42</fault-id>'
        "<fault-source>radio</fault-source>"
        "<affected-objects><name>tx0</name></affected-objects>"
        "<affected-objects><name>tx1</name></affected-objects>"
        "<fault-severity>MAJOR</fault-severity><is-cleared>false</is-cleared>"
        "<fault-text>PA overtemp</fault-text>"
        "<event-time>2026-07-15T00:59:58Z</event-time></alarm-notif>"
    )
    alarm = parse_alarm_notification(NOTIFICATION_WRAPPER.format(payload=full))
    assert alarm == {
        "fault_id": 42,
        "fault_source": "radio",
        "fault_severity": "MAJOR",
        "is_cleared": False,
        "fault_text": "PA overtemp",
        "event_time": "2026-07-15T00:59:58Z",
        "affected_objects": ["tx0", "tx1"],
        "probable_cause": None,
        "specific_problem": None,
        "proposed_repair_actions": None,
        "alarm_type": None,
        "additional_information": {},
    }

    minimal = ALARM_NOTIF_PAYLOAD.format(fault_id=7, source="fan", severity="WARNING", cleared="true")
    parsed = parse_alarm_notification(NOTIFICATION_WRAPPER.format(payload=minimal))
    assert parsed["fault_id"] == 7 and parsed["is_cleared"] is True
    assert parsed["fault_text"] is None and parsed["affected_objects"] == []

    noise = (
        '<netconf-config-change xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-notifications">'
        "<changed-by><username>root</username></changed-by></netconf-config-change>"
    )
    assert parse_alarm_notification(NOTIFICATION_WRAPPER.format(payload=noise)) is None


@mark.timeout(60)
def test_parsers_carry_the_2024_leaves(o1_adapter_src):
    """o-ran-fm 2024-08-12 added probable-cause, specific-problem,
    proposed-repair-actions, alarm-type and the additional-information pairs;
    both parsers carry them, and an O-RU on the 2022 shape yields None / {}."""
    from fm_parsers import parse_active_alarms, parse_alarm_notification

    payload = (
        '<alarm-notif xmlns="urn:o-ran:fm:1.0"><fault-id>21</fault-id><fault-source>fh0</fault-source>'
        "<fault-severity>MINOR</fault-severity><is-cleared>false</is-cleared><fault-text>Link degraded</fault-text>"
        "<event-time>2026-07-15T01:00:00Z</event-time><probable-cause>lossOfSignal</probable-cause>"
        "<specific-problem>CRC errors on port 0</specific-problem>"
        "<proposed-repair-actions>check the fibre</proposed-repair-actions>"
        "<additional-information><identifier>port</identifier><information>0</information></additional-information>"
        "<additional-information><identifier> crc-errors </identifier><information>412</information></additional-information>"
        "<additional-information><identifier></identifier><information>dropped</information></additional-information>"
        "<alarm-type>COMMUNICATIONS-ALARM</alarm-type></alarm-notif>"
    )
    alarm = parse_alarm_notification(NOTIFICATION_WRAPPER.format(payload=payload))
    assert alarm["alarm_type"] == "COMMUNICATIONS-ALARM"
    assert alarm["probable_cause"] == "lossOfSignal" and alarm["specific_problem"] == "CRC errors on port 0"
    assert alarm["proposed_repair_actions"] == "check the fibre"
    assert alarm["additional_information"] == {"port": "0", "crc-errors": "412"}

    listed = parse_active_alarms(
        {
            "active-alarm-list": {
                "active-alarms": [
                    {
                        "fault-id": "21",
                        "fault-severity": "MINOR",
                        "is-cleared": "false",
                        "alarm-type": {"#text": "PHYSICAL-VIOLATION"},
                        "probable-cause": "lossOfSignal",
                        "additional-information": {"identifier": "port", "information": "0"},
                    },
                    {"fault-id": "22", "fault-severity": "MAJOR", "is-cleared": "false"},
                ]
            }
        }
    )
    assert listed[0]["alarm_type"] == "PHYSICAL-VIOLATION" and listed[0]["probable_cause"] == "lossOfSignal"
    assert listed[0]["additional_information"] == {"port": "0"}
    assert listed[1]["alarm_type"] is None and listed[1]["additional_information"] == {}


@mark.timeout(60)
def test_parse_active_alarms_shapes(o1_adapter_src):
    """xmltodict single-entry dicts, multi-entry lists and absent data all parse."""
    from fm_parsers import parse_active_alarms

    single = {
        "active-alarm-list": {
            "active-alarms": {
                "fault-id": "7",
                "fault-source": "fan",
                "fault-severity": "WARNING",
                "is-cleared": "false",
                "event-time": "t",
                "affected-objects": {"name": "fan0"},
            }
        }
    }
    alarms = parse_active_alarms(single)
    assert len(alarms) == 1
    assert alarms[0]["fault_id"] == 7 and alarms[0]["affected_objects"] == ["fan0"]

    multi = {
        "active-alarm-list": {
            "active-alarms": [
                {"fault-id": "1", "fault-severity": "MINOR", "is-cleared": "false"},
                {"fault-id": "2", "fault-severity": "CRITICAL", "is-cleared": "true"},
            ]
        }
    }
    assert [alarm["fault_id"] for alarm in parse_active_alarms(multi)] == [1, 2]

    assert parse_active_alarms({}) == []
    assert parse_active_alarms(None) == []

    # an empty <active-alarms/> element deserializes to None inside the list,
    # affected-objects may arrive as a bare name rather than a mapping, and a
    # leaf carrying an XML attribute deserializes to a mapping with "#text"
    odd_shapes = {
        "active-alarm-list": {
            "active-alarms": [
                None,
                {"fault-id": "3", "fault-severity": "MAJOR", "is-cleared": "false", "affected-objects": "tx0"},
                {
                    "fault-id": {"@nc:operation": "none", "#text": "4"},
                    "fault-source": {"@xml:lang": "en", "#text": "fan"},
                    "fault-severity": "MINOR",
                    "is-cleared": {"#text": "false"},
                    "affected-objects": {"name": {"#text": "fan0"}},
                },
            ]
        }
    }
    parsed = parse_active_alarms(odd_shapes)
    assert [alarm["fault_id"] for alarm in parsed] == [3, 4]
    assert parsed[0]["affected_objects"] == ["tx0"]
    assert parsed[1]["fault_source"] == "fan" and parsed[1]["is_cleared"] is False
    assert parsed[1]["affected_objects"] == ["fan0"]


@mark.timeout(60)
def test_severity_literals_map_to_alarm_manager(o1_adapter_src):
    """o-ran-fm fault-severity literals map 1:1 onto AlarmSeverity member names
    (the mapping ru_fm_bridge relies on)."""
    from alarm_manager import AlarmSeverity

    for literal in ("CRITICAL", "MAJOR", "MINOR", "WARNING"):
        assert AlarmSeverity[literal].name == literal


@mark.timeout(60)
def test_strict_read_does_not_degrade_an_unparsable_reply(o1_adapter_src):
    """A reply the client cannot parse is {} (no alarms) only when tolerated;
    a strict read raises, so a reconciling caller never mass-clears on it."""
    from ru_config import RuConfig

    _ = o1_adapter_src
    manager = SimpleNamespace(get=lambda **kwargs: SimpleNamespace(xml="<rpc-reply><data><active-alarm-list"))
    ru_config = RuConfig(manager, "running")
    assert ru_config.get_active_alarms() == []
    with raises(OperationError):
        ru_config.get_active_alarms(strict=True)


@mark.timeout(60)
def test_active_alarm_list_empty_on_sim(ru_config):
    """The sim raises no alarms of its own: a strict read (the one the bridge
    makes) returns a clean empty list."""
    assert ru_config.get_active_alarms(strict=True) == []
