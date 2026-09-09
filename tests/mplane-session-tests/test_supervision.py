# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Supervision command/response tests for the M-plane client.

The mock RU implements o-ran-supervision (feature SUPERVISION-WITH-SESSION-ID)
but has no application behind the supervision-watchdog-reset RPC, and netopeer2
interleaves its own rpc-execution/config-change notifications. Therefore:

- the watchdog RPC is proven schema-valid via the server's distinguishable
  errors: a well-formed RPC reaches dispatch ("no matching subscribers"), a
  malformed one is rejected by libyang ("not found as a child");
- the ru_controller.supervise() loop logic (noise filtering, timeout
  handling, bounded stop) is unit-tested with a scripted fake manager;
- full notification round-trips inject supervision-notifications into the sim
  via `sysrepocfg --notification` and run where docker can reach the mock RU
  container: the compose du job (daemon socket mounted, MOCK_RU_CONTAINER set)
  or a local run with MOCK_RU_CONTAINER pointing at it; elsewhere they skip.
"""

import logging
import threading
from types import SimpleNamespace
from xml.etree import ElementTree as ET

from conftest import needs_ru_injection
from ncclient.operations import RPCError
from pytest import mark, raises

logger = logging.getLogger(__name__)

SUPERVISION_TAG = "{urn:o-ran:supervision:1.0}supervision-notification"
NOTIFICATION_WRAPPER = (
    '<notification xmlns="urn:ietf:params:xml:ns:netconf:notification:1.0">'
    "<eventTime>2026-01-01T00:00:00Z</eventTime>{payload}</notification>"
)
SUPERVISION_PAYLOAD = (
    '<supervision-notification xmlns="urn:o-ran:supervision:1.0">'
    "<session-id>{session_id}</session-id></supervision-notification>"
)
NOISE_PAYLOAD = (
    '<netconf-config-change xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-notifications">'
    "<changed-by><username>root</username><session-id>1</session-id></changed-by>"
    "</netconf-config-change>"
)


@mark.timeout(60)
def test_watchdog_rpc_reaches_dispatch(ru_config):
    """A well-formed supervision-watchdog-reset passes libyang validation; the
    sim's only complaint is that no application implements the RPC."""
    with raises(RPCError, match="no matching subscribers"):
        ru_config.reset_supervision_watchdog(60, 10)


@mark.timeout(60)
def test_watchdog_rpc_schema_enforced(fresh_ru_manager):
    """A malformed variant of the same RPC is rejected by schema validation —
    proving the well-formed one above really was validated."""
    from ncclient.xml_ import to_ele

    bogus = (
        '<supervision-watchdog-reset xmlns="urn:o-ran:supervision:1.0">'
        "<bogus-child-element>1</bogus-child-element></supervision-watchdog-reset>"
    )
    with raises(RPCError, match="not found as a child"):
        fresh_ru_manager.dispatch(to_ele(bogus))


class _FakeManager:
    """Scripted stand-in for an ncclient manager (RuConfig duck-types it)."""

    def __init__(self, script, dispatch_xml="<ok/>"):
        self.script = list(script)
        self.dispatch_xml = dispatch_xml
        self.dispatched = []
        self.subscribed = False

    def create_subscription(self):
        self.subscribed = True

    def take_notification(self, block=True, timeout=None):  # noqa: ARG002 - ncclient signature
        return self.script.pop(0)

    def dispatch(self, element):
        self.dispatched.append(element)
        return SimpleNamespace(xml=self.dispatch_xml)


@mark.timeout(60)
def test_supervise_loop_filters_and_stops_bounded(o1_adapter_src):
    """supervise() ignores noise and timeouts, resets the watchdog once per
    supervision-notification, and honours max_notifications."""
    from ru_config import RuConfig
    from ru_controller import supervise

    supervision = SimpleNamespace(
        notification_xml=NOTIFICATION_WRAPPER.format(payload=SUPERVISION_PAYLOAD.format(session_id=1))
    )
    noise = SimpleNamespace(notification_xml=NOTIFICATION_WRAPPER.format(payload=NOISE_PAYLOAD))
    fake = _FakeManager([noise, None, supervision, supervision])

    supervise(RuConfig(fake, "running"), 2, 1, max_notifications=2)

    assert fake.subscribed
    assert not fake.script, "loop must consume exactly the scripted notifications"
    assert len(fake.dispatched) == 2, "one watchdog reset per supervision-notification"
    reset = fake.dispatched[0]
    assert reset.tag == "{urn:o-ran:supervision:1.0}supervision-watchdog-reset"
    interval = reset.find("{urn:o-ran:supervision:1.0}supervision-notification-interval")
    assert interval is not None and interval.text == "2"


TIMERS_KEPT_REPLY = (
    '<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
    '<error-message xmlns="urn:o-ran:supervision:1.0">O-RU keeps its own timers</error-message>'
    "</rpc-reply>"
)


@mark.timeout(60)
def test_supervise_split_config_stays_on_the_supervision_session(o1_adapter_src, caplog):
    """o-ran-supervision timers belong to the session that subscribed, so with
    a split RuConfig (command session plus a dedicated supervision session)
    supervise() must subscribe, read the notification stream and dispatch the
    watchdog resets on supervision_manager; nothing supervision-related may
    touch the command session. An accepted reset whose reply carries
    error-message (the O-RU kept its own timers) is a deviation the operator
    must see, so it is logged at WARNING."""
    from ru_config import RuConfig
    from ru_controller import supervise

    supervision = SimpleNamespace(
        notification_xml=NOTIFICATION_WRAPPER.format(payload=SUPERVISION_PAYLOAD.format(session_id=7))
    )
    command = _FakeManager([])
    supervision_session = _FakeManager([supervision], dispatch_xml=TIMERS_KEPT_REPLY)

    with caplog.at_level(logging.INFO):
        supervise(RuConfig(command, "running", supervision_manager=supervision_session), 2, 1, max_notifications=1)

    assert supervision_session.subscribed and not command.subscribed, "the subscription is on the supervision session"
    assert not supervision_session.script, "the notification stream is read on the supervision session"
    assert len(supervision_session.dispatched) == 1 and not command.dispatched, "the reset goes where the timers are"
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert any("O-RU keeps its own timers" in message for message in warnings), warnings


@needs_ru_injection
@mark.timeout(90)
def test_injected_supervision_notification_received(fresh_ru_manager, inject_ru_notification):
    """Round-trip: a sysrepocfg-injected supervision-notification reaches a
    subscribed NETCONF client, with the session-id leafref validated by the sim."""
    fresh_ru_manager.create_subscription()
    inject_ru_notification(SUPERVISION_PAYLOAD.format(session_id=fresh_ru_manager.session_id))

    for _ in range(20):  # drain netopeer's rpc-execution noise
        notification = fresh_ru_manager.take_notification(block=True, timeout=3)
        if notification is None:
            continue
        found = ET.fromstring(notification.notification_xml).find(".//" + SUPERVISION_TAG)
        if found is not None:
            session_id = found.find("{urn:o-ran:supervision:1.0}session-id")
            assert session_id is not None and session_id.text == str(fresh_ru_manager.session_id)
            return
    raise AssertionError("injected supervision-notification never arrived")


@needs_ru_injection
@mark.timeout(120)
def test_supervise_loop_end_to_end_against_sim(o1_adapter_src, fresh_ru_manager, inject_ru_notification):
    """Drive the real supervise() loop: an injected supervision-notification must
    be received, filtered, and answered with a watchdog reset. The sim has no
    RPC application, so a correct loop observably surfaces the reset attempt as
    the 'no matching subscribers' RPCError."""
    from ru_config import RuConfig
    from ru_controller import supervise

    ru = RuConfig(fresh_ru_manager, "running")
    outcome = {}

    def _run():
        try:
            supervise(ru, 2, 1, max_notifications=1)
        except Exception as exc:  # noqa: BLE001 - captured for assertion
            outcome["exc"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    payload = SUPERVISION_PAYLOAD.format(session_id=fresh_ru_manager.session_id)
    for _ in range(10):
        if not thread.is_alive():
            break
        inject_ru_notification(payload)
        thread.join(timeout=4)
    assert not thread.is_alive(), "supervise() did not react to injected supervision-notifications"

    exc = outcome.get("exc")
    assert isinstance(exc, RPCError), f"expected the watchdog reset to surface an RPCError, got {exc!r}"
    assert "no matching subscribers" in str(exc)
