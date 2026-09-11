# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Performance bridge tests (adapter ru_pm_bridge.RuPmBridge).

The bridge flattens o-ran-performance-management measurement-result-stats
notifications into metric samples and streams them through PmMetrics — the
same PM-job filter, envelope schema and push queue the gNB WS metrics use.
Unit tests drive the real PmMetrics/AppState; a fake-session test covers the
session dispatch chain; the sim integration test round-trips the PM
configuration. Notification injection is not sim-testable here: sysrepo
validates the measurement-object leafref against the operational datastore,
which only a resident subscriber application could populate (CLI operational
edits are unsupported) — the streaming path is exercised with scripted
sessions instead, and against real hardware before merge.
"""

import asyncio
import logging
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

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
PM_PAYLOAD = (
    '<measurement-result-stats xmlns="urn:o-ran:performance-management:1.0">'
    "<rx-window-stats><measurement-object>RX_ON_TIME</measurement-object>"
    "<start-time>2026-01-01T00:00:00Z</start-time><end-time>2026-01-01T00:00:01Z</end-time>"
    "<count>52</count></rx-window-stats>"
    "<rx-window-stats><measurement-object>RX_LATE</measurement-object>"
    "<start-time>2026-01-01T00:00:00Z</start-time><end-time>2026-01-01T00:00:01Z</end-time>"
    "<count>3</count></rx-window-stats>"
    "</measurement-result-stats>"
)
# Real o-ran-pm-epe-statistics shape: results nested per object-unit
EPE_PAYLOAD = (
    '<measurement-result-stats xmlns="urn:o-ran:performance-management:1.0">'
    "<epe-statistics><measurement-object>TEMPERATURE</measurement-object>"
    "<start-time>2026-01-01T00:00:00Z</start-time><end-time>2026-01-01T00:00:01Z</end-time>"
    "<epe-measurement-resultv2><object-unit-id>PA1</object-unit-id>"
    "<min>25</min><max>44</max><average>33.5</average></epe-measurement-resultv2>"
    "</epe-statistics></measurement-result-stats>"
)


def _wrapped(payload):
    return NOTIFICATION_WRAPPER.format(payload=payload)


def _pm_setup(o1_adapter_src, jobs=None):  # noqa: ARG001 - fixture puts src on sys.path
    """A bridge over real PmMetrics/AppState with optionally-seeded PM jobs."""
    from pm_metrics import PmMetrics
    from ru_pm_bridge import RuPmBridge
    from state import AppState

    state = AppState()
    state.pm_metrics_queue = asyncio.Queue()  # fresh per test: AppState fields are class-shared
    state.pm_jobs = dict(jobs or {})
    pm_metrics = PmMetrics(state, "du")
    return RuPmBridge(pm_metrics), state


_STREAMING_JOB = {
    "job1": {"administrativeState": "UNLOCKED", "streamTarget": "http://smo/pm", "performanceMetrics": []}
}


@mark.timeout(60)
def test_counts_parse_and_stream(o1_adapter_src):
    """rx-window counts become ru.<group>.<object> samples inside a standard
    PM envelope on the push queue."""
    bridge, state = _pm_setup(o1_adapter_src, jobs=_STREAMING_JOB)
    bridge.handle_notification(_wrapped(PM_PAYLOAD))

    target, envelope = state.pm_metrics_queue.get_nowait()
    assert target == "http://smo/pm"
    assert {m["name"]: m["value"] for m in envelope["metrics"]} == {
        "ru.rx-window-stats.RX_ON_TIME": 52,
        "ru.rx-window-stats.RX_LATE": 3,
    }
    assert envelope["jobId"] == "job1"
    assert state.pm_metrics_queue.empty()


@mark.timeout(60)
def test_epe_nested_results_parse(o1_adapter_src):
    """EPE results nest inside epe-measurement-resultv2 per the real YANG:
    the parser must reach them, name the unit from object-unit-id, keep leaf
    names as suffixes and skip the timestamps."""
    from ru_pm_bridge import parse_measurement_stats

    _ = o1_adapter_src
    metrics = {m["name"]: m["value"] for m in parse_measurement_stats(_wrapped(EPE_PAYLOAD))}
    assert metrics == {
        "ru.epe-statistics.TEMPERATURE.PA1.min": 25,
        "ru.epe-statistics.TEMPERATURE.PA1.max": 44,
        "ru.epe-statistics.TEMPERATURE.PA1.average": 33.5,
    }


@mark.timeout(60)
def test_multiple_nested_results_yield_distinct_samples(o1_adapter_src):
    """Several per-object-unit results in one entry each become a sample, kept
    apart by the unit identifier in the name (the same name twice would be
    indistinguishable in an envelope)."""
    from ru_pm_bridge import parse_measurement_stats

    _ = o1_adapter_src
    payload = (
        '<measurement-result-stats xmlns="urn:o-ran:performance-management:1.0">'
        "<epe-statistics><measurement-object>POWER</measurement-object>"
        "<epe-measurement-resultv2><object-unit-id>PA1</object-unit-id><average>17</average>"
        "</epe-measurement-resultv2>"
        "<epe-measurement-resultv2><object-unit-id>PA2</object-unit-id><average>19</average>"
        "</epe-measurement-resultv2>"
        "</epe-statistics></measurement-result-stats>"
    )
    samples = parse_measurement_stats(_wrapped(payload))
    assert [(s["name"], s["value"]) for s in samples] == [
        ("ru.epe-statistics.POWER.PA1.average", 17),
        ("ru.epe-statistics.POWER.PA2.average", 19),
    ]


@mark.timeout(60)
def test_result_lists_are_named_by_their_keys_in_document_order(o1_adapter_src):
    """A result list entry is named by its keys as serialised (schema order);
    a plain count under a keyed entry keeps the keys; the O-RU-level name leaf
    of an RU-unit result is not a key and stays out of the name."""
    from ru_pm_bridge import parse_measurement_stats

    _ = o1_adapter_src
    payload = (
        '<measurement-result-stats xmlns="urn:o-ran:performance-management:1.0">'
        "<rx-window-stats><measurement-object>RX_ON_TIME</measurement-object>"
        "<start-time>2026-01-01T00:00:00Z</start-time><end-time>2026-01-01T00:01:00Z</end-time>"
        "<name>o-ru</name><count>69</count>"
        "<eaxc-measured-result><eaxc-id>3</eaxc-id><transport-name>eth0</transport-name><count>8</count>"
        "</eaxc-measured-result>"
        "<tr-measured-result><name>fh-1</name><count>5</count></tr-measured-result>"
        "</rx-window-stats></measurement-result-stats>"
    )
    assert parse_measurement_stats(_wrapped(payload)) == [
        {"name": "ru.rx-window-stats.RX_ON_TIME", "value": 69},
        {"name": "ru.rx-window-stats.RX_ON_TIME.3", "value": 8},
        {"name": "ru.rx-window-stats.RX_ON_TIME.fh-1", "value": 5},
    ]


@mark.timeout(60)
def test_result_containers_are_named_by_their_tag(o1_adapter_src):
    """transceiver results nest min/max/first/latest containers with a value and
    a time under a keyed entry: the container tag becomes a segment, the
    timestamp is skipped, and a multiple-* batch of earlier intervals is not
    streamed (the latest result is reported alongside it)."""
    from ru_pm_bridge import parse_measurement_stats

    _ = o1_adapter_src
    payload = (
        '<measurement-result-stats xmlns="urn:o-ran:performance-management:1.0">'
        "<transceiver-stats><measurement-object>RX_POWER</measurement-object>"
        "<transceiver-measurement-result><object-unit-id>1</object-unit-id>"
        "<min><value>-3.5</value><time>2026-01-01T00:00:10Z</time></min>"
        "<max><value>2.25</value><time>2026-01-01T00:00:40Z</time></max>"
        "</transceiver-measurement-result>"
        "<multiple-transceiver-measurement-result><start-time>t0</start-time>"
        "<transceiver-measurement-result><object-unit-id>1</object-unit-id><min><value>-9</value></min>"
        "</transceiver-measurement-result></multiple-transceiver-measurement-result>"
        "</transceiver-stats></measurement-result-stats>"
    )
    assert parse_measurement_stats(_wrapped(payload)) == [
        {"name": "ru.transceiver-stats.RX_POWER.1.min.value", "value": -3.5},
        {"name": "ru.transceiver-stats.RX_POWER.1.max.value", "value": 2.25},
    ]


@mark.timeout(60)
def test_parser_rejects_what_is_not_a_finite_module_number(o1_adapter_src):
    """Values that YANG numeric types cannot produce (NaN, infinity, digit
    separators, spaces inside) yield no sample; surrounding whitespace is
    tolerated; leaves outside the module's namespace are not results; every
    measurement-result-stats element in a notification is parsed; a result
    nested beyond reason is dropped, not raised."""
    from ru_pm_bridge import parse_measurement_stats

    _ = o1_adapter_src

    def entry(value, extra=""):
        return (
            "<rx-window-stats><measurement-object>RX_LATE</measurement-object>"
            f"<count>{value}</count>{extra}</rx-window-stats>"
        )

    ns = 'xmlns="urn:o-ran:performance-management:1.0"'
    for bad in ("NaN", "inf", "-Infinity", "1_000", "1 000", "0x10", ""):
        assert parse_measurement_stats(_wrapped(f"<measurement-result-stats {ns}>{entry(bad)}</measurement-result-stats>")) == []
    ok = parse_measurement_stats(_wrapped(f"<measurement-result-stats {ns}>{entry(' 7 ')}</measurement-result-stats>"))
    assert ok == [{"name": "ru.rx-window-stats.RX_LATE", "value": 7}]

    foreign = entry(1, '<v:serial xmlns:v="urn:vendor">12345</v:serial>')
    assert parse_measurement_stats(_wrapped(f"<measurement-result-stats {ns}>{foreign}</measurement-result-stats>")) == [
        {"name": "ru.rx-window-stats.RX_LATE", "value": 1}
    ]

    two = f"<measurement-result-stats {ns}>{entry(1)}</measurement-result-stats>" * 2
    assert [s["value"] for s in parse_measurement_stats(_wrapped(two))] == [1, 1]

    deep = "<x>" * 3000 + "<count>1</count>" + "</x>" * 3000
    nested = f"<measurement-result-stats {ns}><rx-window-stats><measurement-object>RX_LATE</measurement-object>{deep}</rx-window-stats></measurement-result-stats>"
    assert parse_measurement_stats(_wrapped(nested)) == []


@mark.timeout(60)
def test_noise_and_malformed_ignored(o1_adapter_src):
    """Non-PM notifications, PM entries without a measurement-object and
    unparseable XML all produce no samples."""
    from ru_pm_bridge import parse_measurement_stats

    _ = o1_adapter_src
    assert parse_measurement_stats(_wrapped(SUPERVISION_PAYLOAD.format(session_id=1))) == []
    assert parse_measurement_stats("<broken<") == []
    no_object = (
        '<measurement-result-stats xmlns="urn:o-ran:performance-management:1.0">'
        "<rx-window-stats><count>5</count></rx-window-stats></measurement-result-stats>"
    )
    assert parse_measurement_stats(_wrapped(no_object)) == []


@mark.timeout(60)
def test_entry_without_measurement_object_is_logged(o1_adapter_src, caplog):
    """An entry the parser cannot name does not vanish silently."""
    from ru_pm_bridge import parse_measurement_stats

    _ = o1_adapter_src
    no_object = (
        '<measurement-result-stats xmlns="urn:o-ran:performance-management:1.0">'
        "<tx-stats><count>5</count></tx-stats></measurement-result-stats>"
    )
    with caplog.at_level(logging.DEBUG):
        parse_measurement_stats(_wrapped(no_object))
    assert any("tx-stats" in record.getMessage() and "measurement-object" in record.getMessage() for record in caplog.records)


@mark.timeout(60)
def test_job_allowlist_filters_ru_metrics(o1_adapter_src):
    """PM jobs allowlist RU metrics like any other metric; jobs without a
    match get no envelope."""
    jobs = {
        "rx_only": {
            "administrativeState": "UNLOCKED",
            "streamTarget": "http://smo/rx",
            "performanceMetrics": ["ru.rx-window-stats.RX_ON_TIME"],
        },
        "unrelated": {
            "administrativeState": "UNLOCKED",
            "streamTarget": "http://smo/other",
            "performanceMetrics": ["DLDelay_gNBCUUP"],
        },
        "locked": {"administrativeState": "LOCKED", "streamTarget": "http://smo/locked"},
    }
    bridge, state = _pm_setup(o1_adapter_src, jobs=jobs)
    bridge.handle_notification(_wrapped(PM_PAYLOAD))

    target, envelope = state.pm_metrics_queue.get_nowait()
    assert target == "http://smo/rx"
    assert [m["name"] for m in envelope["metrics"]] == ["ru.rx-window-stats.RX_ON_TIME"]
    assert state.pm_metrics_queue.empty(), "non-matching and locked jobs must get nothing"


@mark.timeout(60)
def test_no_jobs_no_queue_growth(o1_adapter_src):
    """Without active PM jobs the bridge is a no-op — no queue growth — and the
    gNB metrics path does not even flatten its payload."""
    import pm_metrics as pm_module

    bridge, state = _pm_setup(o1_adapter_src)
    bridge.handle_notification(_wrapped(PM_PAYLOAD))
    assert state.pm_metrics_queue.empty()

    flattened = []
    original = pm_module._flatten_metrics  # pylint: disable=protected-access
    pm_module._flatten_metrics = lambda data: flattened.append(data) or original(data)  # pylint: disable=protected-access
    try:
        asyncio.run(bridge.pm_metrics.handle_ws_message('{"du": {"x": 1}}'))
    finally:
        pm_module._flatten_metrics = original  # pylint: disable=protected-access
    assert flattened == [], "no job: the WS payload must not be flattened"
    assert state.pm_metrics_queue.empty()


@mark.timeout(60)
def test_ru_samples_from_a_worker_thread_reach_the_loop(o1_adapter_src):
    """The session dispatches notifications on a worker thread while the push
    queue belongs to the event loop: with the loop bound (run_pusher does it)
    a sample handed over from another thread must wake a consumer awaiting the
    queue on the loop."""
    bridge, state = _pm_setup(o1_adapter_src, jobs=_STREAMING_JOB)
    put_threads = []
    original_put = state.pm_metrics_queue.put_nowait
    state.pm_metrics_queue.put_nowait = lambda item: put_threads.append(threading.get_ident()) or original_put(item)

    async def scenario():
        bridge.pm_metrics.bind_loop()
        consumer = asyncio.ensure_future(state.pm_metrics_queue.get())
        await asyncio.sleep(0)  # the consumer is parked on the queue before the producer runs
        worker = threading.Thread(target=bridge.handle_notification, args=(_wrapped(PM_PAYLOAD),))
        worker.start()
        target, envelope = await asyncio.wait_for(consumer, timeout=5)
        worker.join(timeout=5)
        assert not worker.is_alive()
        return target, envelope, threading.get_ident()

    target, envelope, loop_thread = asyncio.run(scenario())
    assert target == "http://smo/pm"
    assert {m["name"] for m in envelope["metrics"]} == {"ru.rx-window-stats.RX_ON_TIME", "ru.rx-window-stats.RX_LATE"}
    assert put_threads == [loop_thread], "the push queue must only be touched on the loop thread"


@mark.timeout(60)
def test_ru_samples_stream_inline_without_a_bound_loop(o1_adapter_src):
    """Before run_pusher binds the loop (or in a process without one) the
    samples are streamed inline rather than dropped."""
    bridge, state = _pm_setup(o1_adapter_src, jobs=_STREAMING_JOB)
    bridge.pm_metrics.handle_ru_metrics([{"name": "ru.rx-window-stats.RX_LATE", "value": 1}])
    _, envelope = state.pm_metrics_queue.get_nowait()
    assert envelope["metrics"] == [{"name": "ru.rx-window-stats.RX_LATE", "value": 1}]


@mark.timeout(60)
def test_configure_gated_on_module_capabilities(o1_adapter_src):
    """configure() writes measurement objects only when the O-RU advertises
    o-ran-performance-management (yang-library or legacy modules-state, via
    the client's capabilities map)."""
    bridge, _ = _pm_setup(o1_adapter_src)
    calls = []

    with_pm = SimpleNamespace(
        get_perf_measurement_config=lambda operational=False: {},
        get_module_capabilities_map=lambda strict=False: {"o-ran-supervision": {}, "o-ran-performance-management": {}},
        configure_perf_measurement=lambda config=None: calls.append(config) or [],
    )
    bridge.configure(with_pm)
    assert calls == [None], "PM present: objects configured with defaults"

    without_pm = SimpleNamespace(
        get_perf_measurement_config=lambda operational=False: {},
        get_module_capabilities_map=lambda strict=False: {"o-ran-supervision": {}},
        configure_perf_measurement=lambda config=None: calls.append(config) or [],
    )
    bridge.configure(without_pm)
    assert calls == [None], "PM absent: no configuration attempted"


@mark.timeout(60)
def test_capability_read_is_strict(o1_adapter_src, caplog):
    """The gate read must not turn a failed read into "does not advertise":
    a transport failure propagates (the session recycles), an rpc-error is
    logged as a rejected read, and only an answered read without the module
    skips as not advertised."""
    from ncclient.transport import errors as transport_errors

    bridge, _ = _pm_setup(o1_adapter_src)
    strict_seen = []
    calls = []

    def capabilities(strict=False):
        strict_seen.append(strict)
        raise transport_errors.TransportError("gone")

    with raises(transport_errors.TransportError):
        bridge.configure(SimpleNamespace(get_module_capabilities_map=capabilities))
    assert strict_seen == [True], "the capability read must be strict"

    class _StubRpcError(rpc_ops_module().RPCError):
        def __init__(self):  # pylint: disable=super-init-not-called
            Exception.__init__(self, "access-denied")

    def refused(strict=False):  # noqa: ARG001
        raise _StubRpcError()

    with caplog.at_level(logging.WARNING):
        bridge.configure(SimpleNamespace(get_module_capabilities_map=refused, configure_perf_measurement=calls.append))
    assert calls == [] and any("rejected the yang-library read" in r.getMessage() for r in caplog.records)


def rpc_ops_module():
    from ncclient.operations import rpc as rpc_ops

    return rpc_ops


@mark.timeout(60)
def test_configure_remembers_the_converged_object_set(o1_adapter_src, caplog):
    """The client drops the objects an O-RU rejects; the bridge then requests
    exactly the accepted set from the next cycle on, so the rejections happen
    once per process. A write that activated nothing (rejected everything, or
    the role skipped it) leaves the request as it was."""
    bridge, _ = _pm_setup(o1_adapter_src)
    requests_seen = []

    def configure_perf_measurement(config=None):
        requests_seen.append(None if config is None else list(config.get("rx_window_objects", [])))
        return ["RX_ON_TIME", "RX_LATE"] if len(requests_seen) < 3 else []

    ru_config = SimpleNamespace(
        get_perf_measurement_config=lambda operational=False: {},
        get_module_capabilities_map=lambda strict=False: {"o-ran-performance-management": {}},
        configure_perf_measurement=configure_perf_measurement,
    )
    with caplog.at_level(logging.INFO):
        bridge.configure(ru_config)
    assert requests_seen == [None], "first cycle: the default set"
    assert bridge.pm_config == {"rx_window_objects": ["RX_ON_TIME", "RX_LATE"]}
    assert any("converged on" in r.getMessage() for r in caplog.records)

    bridge.configure(ru_config)
    bridge.configure(ru_config)  # activates nothing this time
    assert requests_seen[1:] == [["RX_ON_TIME", "RX_LATE"], ["RX_ON_TIME", "RX_LATE"]]
    assert bridge.pm_config == {"rx_window_objects": ["RX_ON_TIME", "RX_LATE"]}, "an empty result changes nothing"


@mark.timeout(60)
def test_cycle_hook_relogs_the_stream_and_configures_only_when_enabled(o1_adapter_src, caplog):
    """Each connect cycle logs the first notification at INFO again; the
    configure step runs from the hook only with --ru_pm."""
    from ru_pm_bridge import RuPmBridge

    bridge, _ = _pm_setup(o1_adapter_src, jobs=_STREAMING_JOB)
    calls = []
    ru_config = SimpleNamespace(
        get_perf_measurement_config=lambda operational=False: {},
        get_module_capabilities_map=lambda strict=False: {"o-ran-performance-management": {}},
        configure_perf_measurement=lambda config=None: calls.append(config) or [],
    )
    with caplog.at_level(logging.INFO):
        bridge.handle_notification(_wrapped(PM_PAYLOAD))
        bridge.handle_notification(_wrapped(PM_PAYLOAD))
        bridge.on_cycle(ru_config)
        bridge.handle_notification(_wrapped(PM_PAYLOAD))
    assert sum("performance stream started" in r.getMessage() for r in caplog.records) == 2
    assert calls == [], "configure is off by default"

    configuring = RuPmBridge(bridge.pm_metrics, configure=True)
    configuring.on_cycle(ru_config)
    assert calls == [None]


@mark.timeout(60)
def test_configure_under_hybrid_odu_skips_the_write(o1_adapter_src, caplog):
    """Table 6.5-1 gives hybrid-odu read access to performance management but
    no write: the client skips the measurement edit with one log line and the
    bridge treats that as a normal cycle."""
    from ru_config import ROLE_HYBRID_ODU, RuConfig

    bridge, _ = _pm_setup(o1_adapter_src)
    sent = []
    manager = SimpleNamespace(
        edit_config=lambda **kwargs: sent.append(kwargs),
        server_capabilities=(),
        get=lambda **kwargs: SimpleNamespace(
            xml='<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><data/></rpc-reply>'
        ),
    )
    ru_config = RuConfig(manager, "running", role=ROLE_HYBRID_ODU)
    ru_config.get_module_capabilities_map = lambda strict=False: {"o-ran-performance-management": {}}
    with caplog.at_level(logging.INFO):
        bridge.configure(ru_config)
    assert sent == [], "hybrid-odu must not write the measurement objects"
    assert any("not writable for role hybrid-odu" in record.getMessage() for record in caplog.records)


@mark.timeout(60)
def test_ru_pm_flag_requires_the_session(o1_adapter_src):
    """--ru_pm without --ru_supervise is a usage error, not a silent no-op
    (runs where the adapter's own dependencies are installed)."""
    from pytest import importorskip

    for module in ("flask", "websockets", "deepdiff"):  # the adapter's imports the test image does not carry
        importorskip(module)
    result = subprocess.run(
        [sys.executable, "o1_adapter.py", "--ru_pm"],
        cwd=o1_adapter_src,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 2, (result.returncode, result.stderr[-400:])
    assert "--ru_pm requires --ru_supervise" in result.stderr

    result = subprocess.run(
        [sys.executable, "o1_adapter.py", "--ru_pm", "--ru_supervise", "--ru_forward", "--ru_netconf_username", "u",
         "--ru_netconf_password", "p"],
        cwd=o1_adapter_src, capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode == 2 and "cannot be combined with --ru_forward" in result.stderr


@mark.timeout(60)
def test_configure_failure_semantics(o1_adapter_src, caplog):
    """A rejected PM write (rpc-error) is alive-but-rejected: logged, session
    lives. A transport failure propagates so the session recycles."""
    from ncclient.operations import rpc as rpc_ops
    from ncclient.transport import errors as transport_errors

    bridge, _ = _pm_setup(o1_adapter_src)

    class _StubRpcError(rpc_ops.RPCError):
        def __init__(self):  # pylint: disable=super-init-not-called
            Exception.__init__(self, "access denied")

    def raiser(error):
        def _raise(config=None):  # noqa: ARG001
            raise error

        return _raise

    rejecting = SimpleNamespace(
        get_perf_measurement_config=lambda operational=False: {},
        get_module_capabilities_map=lambda strict=False: {"o-ran-performance-management": {}},
        configure_perf_measurement=raiser(_StubRpcError()),
    )
    with caplog.at_level(logging.WARNING):
        bridge.configure(rejecting)  # must not raise
    assert any("rejected the measurement configuration" in record.getMessage() for record in caplog.records)

    dead = SimpleNamespace(
        get_perf_measurement_config=lambda operational=False: {},
        get_module_capabilities_map=lambda strict=False: {"o-ran-performance-management": {}},
        configure_perf_measurement=raiser(transport_errors.TransportError("gone")),
    )
    with raises(transport_errors.TransportError):
        bridge.configure(dead)


CAPABILITIES = {
    "performance-measurement-objects": {
        "measurement-capabilitites": {
            "rx-window-objects": [
                {"measurement-object": "RX_ON_TIME", "object-unit": ["RU", "TRANSPORT"]},
                {"measurement-object": "RX_EARLY", "object-unit": "RU"},
                {"measurement-object": "RX_LATE", "object-unit": {"#text": "RU", "@xmlns:x": "urn:x"}},
                {"measurement-object": "RX_TOTAL", "object-unit": ["TRANSPORT"]},
                {"measurement-object": "RX_CORRUPT"},
            ],
            "tx-stats-objects": {"measurement-object": "TX_TOTAL", "object-unit": ["RU"]},
            "transceiver-objects": [{"measurement-object": "RX_POWER", "object-unit": "PORT_NUMBER"}],
        }
    }
}


def _capable_ru(data, requests_seen):
    """A client double advertising the PM module and the given PM tree; records the configuration requests."""

    def configure_perf_measurement(config=None):
        requests_seen.append(config)
        return list((config or {}).get("rx_window_objects") or [])

    return SimpleNamespace(
        get_module_capabilities_map=lambda strict=False: {"o-ran-performance-management": {}},
        get_perf_measurement_config=lambda operational=False: data if operational else {},
        configure_perf_measurement=configure_perf_measurement,
    )


@mark.timeout(60)
def test_parse_measurement_capabilities_shapes(o1_adapter_src):
    """The config-false capability lists (the module's own spelling) parse per
    group whether an entry comes as a list, a single mapping or an attributed
    leaf; an O-RU without them yields {}."""
    from ru_pm_bridge import parse_measurement_capabilities

    assert parse_measurement_capabilities(CAPABILITIES) == {
        "rx-window": {
            "RX_ON_TIME": ["RU", "TRANSPORT"],
            "RX_EARLY": ["RU"],
            "RX_LATE": ["RU"],
            "RX_TOTAL": ["TRANSPORT"],
            "RX_CORRUPT": [],
        },
        "tx-stats": {"TX_TOTAL": ["RU"]},
        "transceiver": {"RX_POWER": ["PORT_NUMBER"]},
    }
    assert parse_measurement_capabilities({}) == {}
    assert parse_measurement_capabilities({"performance-measurement-objects": {"enable-file-upload": "false"}}) == {}


@mark.timeout(60)
def test_advertised_rx_window_objects_are_requested_up_front(o1_adapter_src, caplog):
    """With capabilities and no profile set, every advertised rx-window object
    the template's unit (RU) applies to is requested — no rejected first edit."""
    bridge, _ = _pm_setup(o1_adapter_src)
    seen = []
    with caplog.at_level(logging.INFO):
        bridge.configure(_capable_ru(CAPABILITIES, seen))
    assert seen == [{"rx_window_objects": ["RX_ON_TIME", "RX_EARLY", "RX_LATE", "RX_CORRUPT"]}]
    assert any(
        "advertises measurement objects: rx-window (5), tx-stats (1), transceiver (1)" in r.getMessage()
        for r in caplog.records
    )


@mark.timeout(60)
def test_profile_objects_are_narrowed_to_the_advertised(o1_adapter_src, caplog):
    """A profile naming an unadvertised object: it is dropped (logged), the rest
    requested, and the profile's own request is kept for the next cycle."""
    bridge, _ = _pm_setup(o1_adapter_src)
    bridge.pm_config = {"rx_window_objects": ["RX_ON_TIME", "RX_DUPL", "RX_LATE"], "notification_interval": 30}
    seen = []
    with caplog.at_level(logging.INFO):
        bridge.configure(_capable_ru(CAPABILITIES, seen))
    assert seen == [{"rx_window_objects": ["RX_ON_TIME", "RX_LATE"], "notification_interval": 30}]
    assert bridge.pm_config["rx_window_objects"] == ["RX_ON_TIME", "RX_DUPL", "RX_LATE"]
    assert any("not advertised by the O-RU" in r.getMessage() and "RX_DUPL" in r.getMessage() for r in caplog.records)


@mark.timeout(60)
def test_nothing_advertised_skips_the_write(o1_adapter_src, caplog):
    """Capabilities without rx-window objects, or a profile naming only
    unadvertised ones, skip the edit with a warning."""
    tx_only = {
        "performance-measurement-objects": {
            "measurement-capabilitites": {"tx-stats-objects": [{"measurement-object": "TX_TOTAL"}]}
        }
    }
    seen = []
    with caplog.at_level(logging.WARNING):
        bridge, _ = _pm_setup(o1_adapter_src)
        bridge.configure(_capable_ru(tx_only, seen))
        bridge, _ = _pm_setup(o1_adapter_src)
        bridge.pm_config = {"rx_window_objects": ["RX_DUPL"]}
        bridge.configure(_capable_ru(CAPABILITIES, seen))
    assert seen == []
    assert sum("advertises none of the rx-window measurement objects" in r.getMessage() for r in caplog.records) == 2


@mark.timeout(60)
def test_no_capabilities_keeps_the_configured_set(o1_adapter_src, caplog):
    """An O-RU exposing no capabilities gets the configured set as before; the
    client's reject-and-retry covers it."""
    bridge, _ = _pm_setup(o1_adapter_src)
    bridge.pm_config = {"rx_window_objects": ["RX_ON_TIME", "RX_DUPL"]}
    seen = []
    with caplog.at_level(logging.INFO):
        bridge.configure(_capable_ru({}, seen))
    assert seen == [{"rx_window_objects": ["RX_ON_TIME", "RX_DUPL"]}]
    assert any("exposes no measurement-capabilitites" in r.getMessage() for r in caplog.records)


class _FakeCommandSession:
    """Command-session double: empty operational gets, recording dispatch."""

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

    def create_subscription(self):
        pass

    def dispatch(self, element):  # noqa: ARG002 - watchdog resets ride the subscription session
        return SimpleNamespace(xml="<ok/>")

    def take_notification(self, block=True, timeout=None):  # noqa: ARG002 - ncclient signature
        if not self.script:
            time.sleep(min(timeout or 0.05, 0.05))
            return None
        return SimpleNamespace(notification_xml=_wrapped(self.script.pop(0)))

    def close_session(self):
        self.connected = False


class _NullAlarms:
    """Alarm-manager stand-in for wiring tests."""

    def set_alarm(self, *args, **kwargs):
        pass

    def clear_alarm(self, *args, **kwargs):
        pass


@mark.timeout(60)
def test_session_dispatch_streams_pm_notifications(o1_adapter_src):
    """MplaneSession dispatches measurement-result-stats into the bridge and
    the envelope lands on the PM queue — the orchestrator wiring, minus real
    sockets."""
    from mplane_session import MplaneSession
    from state import AppState

    bridge, state = _pm_setup(o1_adapter_src, jobs=_STREAMING_JOB)
    args = SimpleNamespace(ru_supervision_interval=60, ru_supervision_guard=10, ru_datastore="running")
    session = MplaneSession(AppState(), args, _NullAlarms(), retry_interval=0.05)
    pairs = [_FakeCommandSession(), _FakeNotificationSession([PM_PAYLOAD, SUPERVISION_PAYLOAD.format(session_id=1)])]
    session.connect_factory = lambda: pairs.pop(0)
    session.poll_cap = 0.1
    session.register_notification_handler(bridge.handle_notification)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        try:
            deadline = time.monotonic() + 10
            while state.pm_metrics_queue.empty():
                assert time.monotonic() < deadline and not task.done()
                await asyncio.sleep(0.02)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=10)

    asyncio.run(scenario())
    _, envelope = state.pm_metrics_queue.get_nowait()
    assert {m["name"] for m in envelope["metrics"]} == {
        "ru.rx-window-stats.RX_ON_TIME",
        "ru.rx-window-stats.RX_LATE",
    }


_DELETE_PM = """<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
 <performance-measurement-objects xmlns="urn:o-ran:performance-management:1.0"
  xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0" nc:operation="remove"/></config>"""


@mark.timeout(90)
def test_configure_roundtrip_against_sim(o1_adapter_src, ru_config, mock_ru_ssh_manager, caplog):
    """bridge.configure against the mock RU: the yang-library gate passes (the
    sim implements the module) and the schema-validated measurement objects
    land in the running datastore."""
    from pytest import skip
    from ofh_config_builder import parse_module_capabilities

    capabilities = parse_module_capabilities(ru_config.get_yang_library().get("yang-library", {}))
    if "o-ran-performance-management" not in capabilities:
        skip("mock RU lacks o-ran-performance-management")

    bridge, _ = _pm_setup(o1_adapter_src)
    try:
        with caplog.at_level(logging.INFO):
            bridge.configure(ru_config)
        # the mock exposes no measurement-capabilitites: the configured set is
        # requested and the client learns from rejections, as before
        assert any("exposes no measurement-capabilitites" in r.getMessage() for r in caplog.records)
        pm = ru_config.get_perf_measurement_config().get("performance-measurement-objects", {})
        objects = pm.get("rx-window-measurement-objects")
        objects = objects if isinstance(objects, list) else [objects]
        assert {entry["measurement-object"] for entry in objects} >= {"RX_ON_TIME", "RX_LATE", "RX_TOTAL"}
        assert all(entry["active"] == "true" for entry in objects)
    finally:
        mock_ru_ssh_manager.edit_config(target="running", config=_DELETE_PM)
