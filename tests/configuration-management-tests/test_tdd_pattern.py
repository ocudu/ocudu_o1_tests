# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Computed TDD pattern tests for the M-plane client.

compute_tdd_switching_points derives o-ran-uplane-conf switching points
(1/1.2288 GHz ticks) from the OCUDU tdd_ul_dl_cfg field shape. The golden
check: offsets land on exact CP-inclusive TS 38.211 symbol boundaries — a
uniform slot/14 grid yields mid-symbol offsets that a boundary-validating
O-RU rejects. Explicit
tdd.switching_points from config bypass computation entirely (the vendor
escape hatch). The sim advertises CONFIGURABLE-TDD-PATTERN-SUPPORTED, so the
gated push runs live.
"""

import logging
import re

from pytest import mark, raises

logger = logging.getLogger(__name__)

# 7D 1S(6 DL/4 guard/4 UL) 2U at 30 kHz on CP-inclusive boundaries: a 30 kHz
# slot is one 44480-tick long-CP symbol + thirteen 43840-tick symbols, so the
# GP boundary after 6 DL symbols of slot 7 is 7*614400 + 44480 + 5*43840 =
# 4564480 (a uniform slot/14 grid puts it at 4564114, mid-symbol).
_GOLDEN_7D1S2U = [
    ("GP", 4564480),
    ("UL", 4739840),
    ("DL", 6144000),
    ("GP", 10708480),
    ("UL", 10883840),
    ("DL", 12288000),
]

_DELETE_PATTERN_2 = """<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
 <user-plane-configuration xmlns="urn:o-ran:uplane-conf:1.0">
  <configurable-tdd-patterns xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0" nc:operation="delete">
   <tdd-pattern-id>2</tdd-pattern-id></configurable-tdd-patterns></user-plane-configuration></config>"""


# A minimal full-config layout for the dry-run set_full_config tests; the
# tdd section is what each test varies.
_FULL_CONFIG_LAYOUT = {
    "interface": {"ru_mac_addr": "aa:bb:cc:dd:ee:01", "vlan": 31},
    "processing": {"ru_mac_addr": "aa:bb:cc:dd:ee:01", "du_mac_addr": "aa:bb:cc:dd:ee:02", "vlan": 31},
    "endpoint": {"iq_bitwidth": 9, "compression_type": "STATIC", "num_prb": 273, "frame_structure": 193},
    "carrier": {
        "dl_arfcn": 640000,
        "dl_freq": 3600000000,
        "ul_arfcn": 640000,
        "ul_freq": 3600000000,
        "tx_gain": 21.0,
        "rf_bandwidth_hz": 100000000,
    },
    "activation": {"state": "ACTIVE"},
}


def _as_list(node):
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


@mark.timeout(60)
def test_switching_points_golden(o1_adapter_src):
    """The canonical 7d1s2u pattern lands on CP-inclusive symbol boundaries."""
    from ofh_config_builder import compute_tdd_switching_points

    points = compute_tdd_switching_points(30, 10, 7, nof_dl_symbols=6, nof_ul_symbols=4)
    assert [(point["direction"], point["frame_offset"]) for point in points] == _GOLDEN_7D1S2U


@mark.timeout(60)
def test_long_cp_at_symbols_0_and_7_at_15khz(o1_adapter_src):
    """At 15 kHz both extended-CP symbols (l = 0 and l = 7) fall in the same
    slot, so a boundary after 8 symbols includes two long CPs: a 15 kHz
    symbol is 87680 ticks + 640 for each long CP."""
    from ofh_config_builder import compute_tdd_switching_points

    points = compute_tdd_switching_points(15, 10, 7, nof_dl_symbols=8, nof_ul_symbols=4)
    gp, ul, dl = points
    assert gp == {"direction": "GP", "frame_offset": 7 * 1228800 + 8 * 87680 + 2 * 640}  # 9304320
    assert ul == {"direction": "UL", "frame_offset": 7 * 1228800 + 10 * 87680 + 2 * 640}  # 9479680
    assert dl == {"direction": "DL", "frame_offset": 12288000}


@mark.timeout(60)
def test_slot_starts_leave_uniform_grid_at_60khz(o1_adapter_src):
    """At 60 kHz only slots 0 and 2 of a subframe start with the extended CP
    (l = 0 and l = 28), so slots are unequal and even slot starts leave the
    uniform grid: slot 5 begins at 1536320, not 5 * 307200 = 1536000. Each
    repetition must therefore be computed absolutely, not translated."""
    from ofh_config_builder import compute_tdd_switching_points

    points = compute_tdd_switching_points(60, 20, 5, nof_ul_slots=15)
    assert [(point["direction"], point["frame_offset"]) for point in points] == [
        ("UL", 1228800 + 14 * 21920 + 640),  # 1536320: subframe 1, slot 1 start
        ("DL", 6144000),
        ("UL", 7372800 + 14 * 21920 + 640),  # 7680320
        ("DL", 12288000),
    ]


@mark.timeout(60)
def test_switching_points_edge_cases(o1_adapter_src):
    """GP collapses without guard symbols; invalid SCS/period/symbol splits raise."""
    from ofh_config_builder import compute_tdd_switching_points

    no_guard = compute_tdd_switching_points(30, 10, 7, nof_dl_symbols=6, nof_ul_symbols=8)
    assert [point["direction"] for point in no_guard] == ["UL", "DL", "UL", "DL"]

    with raises(ValueError):
        compute_tdd_switching_points(25, 10, 7)  # unsupported SCS
    with raises(ValueError):
        compute_tdd_switching_points(15, 6, 4)  # 6 ms period does not divide the frame
    with raises(ValueError):
        compute_tdd_switching_points(30, 10, 7, nof_dl_symbols=8, nof_ul_symbols=8)  # > one slot


@mark.timeout(60)
def test_explicit_switching_points_bypass_computation(o1_adapter_src):
    """tdd.switching_points from config are pushed verbatim — the escape
    hatch for O-RUs that validate boundaries differently than TS 38.211
    CP-inclusive symbol edges (reported from a commercial O-RU that rejects
    the computed pattern)."""
    from ru_config import RuConfig

    _ = o1_adapter_src
    controller = RuConfig(None, "running")
    sent = []
    controller.edit_config = lambda xml_request, description="": sent.append(xml_request)

    raw_points = [
        {"direction": "gp", "frame_offset": 4500000},
        {"direction": "UL", "frame_offset": 4700000},
        {"direction": "DL", "frame_offset": 12288000},
    ]
    assert controller.set_oran_uplane_tdd_pattern({"switching_points": raw_points, "tdd_pattern_id": 2}) is True
    assert len(sent) == 1
    assert ">4500000<" in sent[0] and ">4700000<" in sent[0], "raw offsets pushed verbatim"
    assert ">GP<" in sent[0], "direction normalized to the o-ran-uplane-conf enum"
    assert "<o-ran-uplane-conf:tdd-pattern-id>2<" in sent[0]


@mark.timeout(60)
def test_explicit_switching_points_validated(o1_adapter_src):
    """Malformed switching-point lists raise instead of reaching the O-RU."""
    from ofh_config_builder import validate_switching_points

    _ = o1_adapter_src
    assert validate_switching_points([{"direction": "ul", "frame_offset": "777"}]) == [
        {"direction": "UL", "frame_offset": 777}
    ], "directions upper-cased, offsets coerced to int"
    for bad in (
        [],
        "not a list",
        [{"direction": "SIDEWAYS", "frame_offset": 1}],
        [{"direction": "DL"}],
        [{"direction": "DL", "frame_offset": 12288001}],
        [{"direction": "DL", "frame_offset": -1}],
        [{"direction": "DL", "frame_offset": 5}, {"direction": "UL", "frame_offset": 4}],
    ):
        with raises(ValueError):
            validate_switching_points(bad)


@mark.timeout(60)
def test_full_config_sources_tdd_pattern_from_config(o1_adapter_src):
    """set_full_config derives the pushed pattern from the tdd section: a
    section without pattern content falls back to the canonical 7d1s2u
    (6/4/4) shape, pattern fields are taken as a complete spec (a pattern is
    a coherent whole — no field-by-field merging with the default), raw
    switching_points bypass computation, and pattern_upload: false
    suppresses the push."""
    from ru_config import RuConfig

    _ = o1_adapter_src
    controller = RuConfig(None, "running")
    sent = []
    controller.edit_config = lambda xml_request, description="": sent.append((description, xml_request))
    layout = _FULL_CONFIG_LAYOUT

    def tdd_edits():
        return [xml for description, xml in sent if description == "ORAN Uplane TDD pattern"]

    controller.set_full_config(dict(layout), skip_activation=True)
    assert len(tdd_edits()) == 1 and ">4564480<" in tdd_edits()[0], "default = canonical 7d1s2u, CP-correct"

    sent.clear()
    full_spec = {"scs_khz": 30, "dl_ul_tx_period": 10, "nof_dl_slots": 8, "nof_dl_symbols": 10, "nof_ul_symbols": 2}
    controller.set_full_config({**layout, "tdd": full_spec}, skip_activation=True)
    assert len(tdd_edits()) == 1 and ">4564480<" not in tdd_edits()[0], "explicit fields recompute the pattern"

    sent.clear()
    with raises(ValueError, match="scs_khz"):
        # pattern fields are a complete spec: a partial one errors instead of
        # silently merging into an incoherent pattern
        controller.set_full_config({**layout, "tdd": {"nof_dl_slots": 8}}, skip_activation=True)

    sent.clear()
    controller.set_full_config(
        {**layout, "tdd": {"switching_points": [{"direction": "UL", "frame_offset": 777}]}}, skip_activation=True
    )
    assert len(tdd_edits()) == 1 and ">777<" in tdd_edits()[0], "raw switching_points flow through"

    sent.clear()
    controller.set_full_config({**layout, "tdd": {"pattern_upload": False}}, skip_activation=True)
    assert not tdd_edits(), "pattern_upload: false suppresses the push"


@mark.timeout(60)
def test_full_config_binds_carriers_to_configured_pattern_id(o1_adapter_src):
    """set_full_config binds the carriers to the tdd section's tdd_pattern_id.
    The O-RU validates and applies the pattern a carrier references at
    activation, so pushing pattern 2 while binding the carriers to pattern
    1 silently left the configured pattern unused (found in review of #14
    on a second vendor's O-RU)."""
    from ru_config import RuConfig

    _ = o1_adapter_src
    controller = RuConfig(None, "running")
    controller._is_configurable_tdd_supported = lambda: True
    sent = []
    controller.edit_config = lambda xml_request, description="": sent.append((description, xml_request))

    def edits(description):
        return [xml for sent_description, xml in sent if sent_description == description]

    tdd = {
        "tdd_pattern_id": 2,
        "scs_khz": 30,
        "dl_ul_tx_period": 10,
        "nof_dl_slots": 7,
        "nof_dl_symbols": 6,
        "nof_ul_symbols": 4,
    }
    controller.set_full_config({**_FULL_CONFIG_LAYOUT, "tdd": tdd}, skip_activation=True)
    pattern_edits = edits("ORAN Uplane TDD pattern")
    binding_edits = edits("ORAN Uplane TDD carrier binding")
    assert len(pattern_edits) == 1 and "<o-ran-uplane-conf:tdd-pattern-id>2<" in pattern_edits[0]
    assert len(binding_edits) == 1
    assert "<configurable-tdd-pattern>2</configurable-tdd-pattern>" in binding_edits[0], "carriers bound to pattern 2"
    assert "<configurable-tdd-pattern>1</configurable-tdd-pattern>" not in binding_edits[0]

    sent.clear()
    controller.set_full_config(dict(_FULL_CONFIG_LAYOUT), skip_activation=True)
    binding_edits = edits("ORAN Uplane TDD carrier binding")
    assert len(binding_edits) == 1 and "<configurable-tdd-pattern>1</configurable-tdd-pattern>" in binding_edits[0]


@mark.timeout(60)
def test_explicit_switching_point_ids_honored(o1_adapter_src):
    """A config-supplied switching_points list may name its switching-point-ids
    (like tdd_pattern_id, asked for in review of #14); omitted ids and
    computed patterns keep numbering the points by list position."""
    from ru_config import RuConfig

    _ = o1_adapter_src
    controller = RuConfig(None, "running")
    sent = []
    controller.edit_config = lambda xml_request, description="": sent.append(xml_request)

    def pushed_ids():
        return re.findall(r"<o-ran-uplane-conf:switching-point-id>(\d+)<", sent[-1])

    named = [
        {"direction": "GP", "frame_offset": 4500000, "switching_point_id": 10},
        {"direction": "UL", "frame_offset": 4700000, "switching_point_id": "20"},
        {"direction": "DL", "frame_offset": 12288000, "switching_point_id": 30},
    ]
    assert controller.set_oran_uplane_tdd_pattern({"switching_points": named, "tdd_pattern_id": 2}) is True
    assert pushed_ids() == ["10", "20", "30"], "configured ids pushed in list order, not renumbered"

    unnamed = [{key: value for key, value in point.items() if key != "switching_point_id"} for point in named]
    assert controller.set_oran_uplane_tdd_pattern({"switching_points": unnamed}) is True
    assert pushed_ids() == ["1", "2", "3"], "omitted ids stay positional"

    controller.set_oran_uplane_tdd_7d1s2u_slot_6_4_4()
    assert pushed_ids() == ["1", "2", "3", "4", "5", "6"], "computed patterns keep positional ids"


@mark.timeout(60)
def test_explicit_switching_point_ids_validated(o1_adapter_src):
    """switching_point_id is all-or-none, unique and uint16 (the type of the
    o-ran-uplane-conf switching-point-id leaf); id-less input keeps its
    output shape."""
    from ofh_config_builder import validate_switching_points

    _ = o1_adapter_src
    assert validate_switching_points([{"direction": "ul", "frame_offset": "777", "switching_point_id": "5"}]) == [
        {"direction": "UL", "frame_offset": 777, "switching_point_id": 5}
    ], "ids coerced to int alongside the offset"
    assert validate_switching_points([{"direction": "ul", "frame_offset": 777}]) == [
        {"direction": "UL", "frame_offset": 777}
    ], "id-less entries keep the {direction, frame_offset} shape"

    first = {"direction": "GP", "frame_offset": 100, "switching_point_id": 1}
    with raises(ValueError, match="every switching point or on none"):
        validate_switching_points([first, {"direction": "UL", "frame_offset": 200}])
    with raises(ValueError, match="unique"):
        validate_switching_points([first, {"direction": "UL", "frame_offset": 200, "switching_point_id": 1}])
    for out_of_range in (65536, -1, "first"):
        with raises(ValueError, match="uint16"):
            validate_switching_points([{**first, "switching_point_id": out_of_range}])


@mark.timeout(60)
def test_frame_parameters_golden(o1_adapter_src):
    """compute_num_prb/compute_frame_structure reproduce the fixed bandwidth tables."""
    from ofh_config_builder import compute_frame_structure, compute_num_prb

    assert [compute_num_prb(mhz) for mhz in (100, 80, 40, 20, 10)] == [273, 217, 106, 51, 24]
    assert [compute_frame_structure(compute_num_prb(mhz)) for mhz in (100, 40, 20, 10)] == [193, 177, 161, 145]
    # 217 PRB (80 MHz) needs FFT 4096 — a bandwidth the old table had no entry for
    assert compute_frame_structure(compute_num_prb(80)) == 193
    assert compute_num_prb(37) is None and compute_frame_structure(None) is None


@mark.timeout(90)
def test_tdd_pattern_gated_push_roundtrip(ru_config, mock_ru_ssh_manager, o1_adapter_src):
    """The gated push lands a computed pattern in the RU datastore verbatim."""
    from ofh_config_builder import compute_tdd_switching_points

    tdd_config = {
        "scs_khz": 30,
        "dl_ul_tx_period": 10,
        "nof_dl_slots": 8,
        "nof_dl_symbols": 10,
        "nof_ul_slots": 1,
        "nof_ul_symbols": 2,
        "tdd_pattern_id": 2,
    }
    try:
        assert ru_config.set_oran_uplane_tdd_pattern(tdd_config) is True

        uplane = ru_config.get_uplane_config().get("user-plane-configuration", {})
        pattern = next(
            entry for entry in _as_list(uplane.get("configurable-tdd-patterns")) if entry["tdd-pattern-id"] == "2"
        )
        pushed = [(point["direction"], int(point["frame-offset"])) for point in _as_list(pattern["switching-points"])]
        expected = compute_tdd_switching_points(30, 10, 8, nof_dl_symbols=10, nof_ul_symbols=2)
        assert pushed == [(point["direction"], point["frame_offset"]) for point in expected]
    finally:
        mock_ru_ssh_manager.edit_config(target="running", config=_DELETE_PATTERN_2)


@mark.timeout(60)
def test_slot_budget_validation(o1_adapter_src):
    """The slot budget must fit the period: nof_ul_slots is enforced when
    given, and DL + special slot can never exceed the period."""
    from ofh_config_builder import compute_tdd_switching_points

    # 7 DL + 1 special + 2 UL == 10 -> accepted
    assert compute_tdd_switching_points(30, 10, 7, nof_dl_symbols=6, nof_ul_symbols=4, nof_ul_slots=2)
    with raises(ValueError):  # 7 + 1 + 3 != 10
        compute_tdd_switching_points(30, 10, 7, nof_dl_symbols=6, nof_ul_symbols=4, nof_ul_slots=3)
    with raises(ValueError):  # DL slots + special slot beyond the period
        compute_tdd_switching_points(30, 10, 10, nof_dl_symbols=6, nof_ul_symbols=4)


@mark.timeout(60)
def test_pure_slot_pattern_has_no_special_slot(o1_adapter_src):
    """Without a symbol split there is no special slot: UL starts exactly at
    the DL/UL slot boundary and no GP point is emitted."""
    from ofh_config_builder import compute_tdd_switching_points

    points = compute_tdd_switching_points(30, 10, 8, nof_ul_slots=2)
    slot_ticks = 614400
    assert [(point["direction"], point["frame_offset"]) for point in points] == [
        ("UL", 8 * slot_ticks),
        ("DL", 10 * slot_ticks),
        ("UL", 10 * slot_ticks + 8 * slot_ticks),
        ("DL", 20 * slot_ticks),
    ]


_CARRIER_RF = {
    "dl_arfcn": 640000,
    "dl_freq": 3600000000,
    "ul_arfcn": 640000,
    "ul_freq": 3600000000,
    "tx_gain": 27.0,
    "rf_bandwidth_hz": 100000000,
    "nof_carriers": 2,
}

_REBIND_PATTERN_1 = """<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
 <user-plane-configuration xmlns="urn:o-ran:uplane-conf:1.0">
  <tx-array-carriers><name>Tx-Array-Carrier-00</name><configurable-tdd-pattern>1</configurable-tdd-pattern></tx-array-carriers>
  <tx-array-carriers><name>Tx-Array-Carrier-01</name><configurable-tdd-pattern>1</configurable-tdd-pattern></tx-array-carriers>
  <rx-array-carriers><name>Rx-Array-Carrier-00</name><configurable-tdd-pattern>1</configurable-tdd-pattern></rx-array-carriers>
  <rx-array-carriers><name>Rx-Array-Carrier-01</name><configurable-tdd-pattern>1</configurable-tdd-pattern></rx-array-carriers>
 </user-plane-configuration></config>"""


@mark.timeout(90)
def test_computed_pattern_binds_to_carriers(ru_config, mock_ru_ssh_manager):
    """A computed pattern is applied per carrier via the configurable-tdd-pattern
    leafref (o-ran-uplane-conf): pattern validation happens at carrier
    activation, so every carrier that will be activated must reference the
    pattern. The sim validates the leafref, proving the binding is real."""
    ru_config.set_oran_uplane_tx_array_carriers(dict(_CARRIER_RF))
    ru_config.set_oran_uplane_rx_array_carriers(dict(_CARRIER_RF))

    tdd_config = {
        "scs_khz": 30,
        "dl_ul_tx_period": 10,
        "nof_dl_slots": 7,
        "nof_dl_symbols": 6,
        "nof_ul_slots": 2,
        "nof_ul_symbols": 4,
        "tdd_pattern_id": 2,
        "nof_tx_carriers": 2,
        "nof_rx_carriers": 2,
    }
    try:
        assert ru_config.set_oran_uplane_tdd_pattern(tdd_config) is True

        uplane = ru_config.get_uplane_config().get("user-plane-configuration", {})
        tx_carriers = {entry["name"]: entry for entry in _as_list(uplane.get("tx-array-carriers"))}
        rx_carriers = {entry["name"]: entry for entry in _as_list(uplane.get("rx-array-carriers"))}
        for name in ("Tx-Array-Carrier-00", "Tx-Array-Carrier-01"):
            assert tx_carriers[name]["configurable-tdd-pattern"] == "2"
        for name in ("Rx-Array-Carrier-00", "Rx-Array-Carrier-01"):
            assert rx_carriers[name]["configurable-tdd-pattern"] == "2"
    finally:
        # rebind to the seeded pattern before deleting pattern 2, so the
        # leafref never dangles
        mock_ru_ssh_manager.edit_config(target="running", config=_REBIND_PATTERN_1)
        mock_ru_ssh_manager.edit_config(target="running", config=_DELETE_PATTERN_2)


@mark.timeout(60)
def test_binding_to_missing_pattern_rejected(ru_config, mock_ru_ssh_manager):
    """Binding to a pattern id that does not exist violates the leafref and
    must be rejected by a validating O-RU."""
    from ncclient.operations import RPCError

    ru_config.set_oran_uplane_tx_array_carriers(dict(_CARRIER_RF))
    with raises(RPCError):
        ru_config.bind_tdd_pattern_to_carriers(tdd_pattern_id=77, nof_tx_carriers=1, nof_rx_carriers=0)


@mark.timeout(60)
def test_bind_tdd_pattern_self_gates_on_unsupported_ru(o1_adapter_src, caplog):
    """The configurable-tdd-pattern leafref is if-feature-guarded, so it exists
    only when the O-RU advertises CONFIGURABLE-TDD-PATTERN-SUPPORTED.
    bind_tdd_pattern_to_carriers must self-gate on that feature like the
    pattern-upload step (set_full_config binds carriers by default) — otherwise,
    on an RU without the feature, the binding writes an unknown leaf, the edit
    is rpc-error'd, and the provision session recycles forever."""
    from ru_config import RuConfig

    _ = o1_adapter_src
    controller = RuConfig(None, "running")
    sent = []
    controller.edit_config = lambda xml_request, description="": sent.append(xml_request)

    controller._is_configurable_tdd_supported = lambda: False
    with caplog.at_level(logging.INFO):
        controller.bind_tdd_pattern_to_carriers(nof_tx_carriers=2, nof_rx_carriers=2)
    assert sent == [], "unsupported RU: no carrier-binding edit may be sent"
    assert "skipping TDD carrier binding" in caplog.text

    controller._is_configurable_tdd_supported = lambda: True
    controller.bind_tdd_pattern_to_carriers(nof_tx_carriers=2, nof_rx_carriers=2)
    assert len(sent) == 1 and "configurable-tdd-pattern" in sent[0], "supported RU still binds"
