# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Delay-management derivation tests for the M-plane client.

The O-RU advertises its delay profile in o-ran-delay-management (nanoseconds,
config false); the client derives the DU-side ru_ofh t1a_*/ta4_* timing
windows (microseconds) from it. The mock RU implements the module's schema but
populates no delay data, so:

- parsing + derivation are unit-tested against mocked RU subtrees, with the
  golden targets taken from the n78 4x2 profiles' expected_du blocks;
- against the sim we prove graceful absence end-to-end (empty operational
  data -> no timing keys, no errors).
"""

import logging
from pathlib import Path

import yaml
from pytest import mark

logger = logging.getLogger(__name__)

PROFILE_DIR = Path(__file__).parent.parent / "configs" / "ru"

TIMING_KEYS = (
    "t1a_max_cp_dl",
    "t1a_min_cp_dl",
    "t1a_max_cp_ul",
    "t1a_min_cp_ul",
    "t1a_max_up",
    "t1a_min_up",
    "ta4_max",
    "ta4_min",
)

# derived ru_ofh key -> the ru-delay-profile leaf it comes from (zero transport)
PROFILE_LEAF_FOR_KEY = {
    "t1a_max_cp_dl": "t2a-max-cp-dl",
    "t1a_min_cp_dl": "t2a-min-cp-dl",
    "t1a_max_cp_ul": "t2a-max-cp-ul",
    "t1a_min_cp_ul": "t2a-min-cp-ul",
    "t1a_max_up": "t2a-max-up",
    "t1a_min_up": "t2a-min-up",
    "ta4_max": "ta3-max",
    "ta4_min": "ta3-min",
}


def _expected_du(profile_name: str) -> dict:
    with (PROFILE_DIR / f"{profile_name}.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)["expected_du"]


def _delay_subtree(profile_ns: dict) -> dict:
    """Shape a ru-delay-profile the way xmltodict returns it (string leaves)."""
    return {
        "bandwidth-scs-delay-state": {
            "bandwidth": "100000",  # kHz
            "subcarrier-spacing": "30000",  # Hz
            "ru-delay-profile": {leaf: str(value) for leaf, value in profile_ns.items()},
        }
    }


@mark.timeout(60)
@mark.parametrize("profile_name", ["n78_4x2_reference_timing", "n78_4x2_docs_timing"])
def test_derivation_matches_expected_du_golden(o1_adapter_src, profile_name):
    """An O-RU advertising t2a/ta3 values equal to a published O-RU timing set
    (in ns) must derive exactly the expected_du microsecond windows."""
    from ofh_config_builder import derive_du_timing

    expected = {key: _expected_du(profile_name)[key] for key in TIMING_KEYS}
    profile_ns = {PROFILE_LEAF_FOR_KEY[key]: expected[key] * 1000 for key in TIMING_KEYS}
    assert derive_du_timing(profile_ns) == expected


@mark.timeout(60)
def test_parse_delay_profile_shapes(o1_adapter_src):
    """xmltodict single-entry dicts, multi-entry lists and absent data all parse."""
    from ofh_config_builder import parse_delay_profile

    profile_ns = {leaf: (index + 1) * 1000 for index, leaf in enumerate(PROFILE_LEAF_FOR_KEY.values())}
    subtree = _delay_subtree(profile_ns)
    assert parse_delay_profile(subtree) == profile_ns

    # a first list entry without a ru-delay-profile must be skipped
    multi = {"bandwidth-scs-delay-state": [{"bandwidth": "20000"}, subtree["bandwidth-scs-delay-state"]]}
    assert parse_delay_profile(multi) == profile_ns

    assert parse_delay_profile({}) == {}
    assert parse_delay_profile(None) == {}


@mark.timeout(60)
def test_derivation_rounding_is_conservative(o1_adapter_src):
    """ns->us rounding: the DL transmit window tightens, the UL reception window widens."""
    from ofh_config_builder import derive_du_timing

    profile_ns = {"t2a-min-up": 100_500, "t2a-max-up": 100_500, "ta3-min": 100_500, "ta3-max": 100_500}
    assert derive_du_timing(profile_ns) == {
        "t1a_min_up": 101,  # ceil: DU must send earlier -> window tightens
        "t1a_max_up": 100,  # floor: DU must not send too early -> window tightens
        "ta4_min": 100,  # floor: DU starts listening earlier -> window widens
        "ta4_max": 101,  # ceil: DU listens longer -> window widens
    }


@mark.timeout(60)
def test_derivation_adds_transport_delay(o1_adapter_src):
    """T12 bounds shift the DL windows (min <- t12_max, max <- t12_min); T34 shifts UL."""
    from ofh_config_builder import derive_du_timing

    profile_ns = {"t2a-min-up": 80_000, "t2a-max-up": 390_000, "ta3-min": 25_000, "ta3-max": 500_000}
    derived = derive_du_timing(
        profile_ns, t12_min_ns=10_000, t12_max_ns=25_000, t34_min_ns=5_000, t34_max_ns=15_000
    )
    assert derived == {"t1a_min_up": 105, "t1a_max_up": 400, "ta4_min": 30, "ta4_max": 515}


@mark.timeout(60)
def test_print_layout_places_timing_at_ru_ofh_top_level(o1_adapter_src, capsys):
    """Derived timing keys land at ru_ofh top level before cells (gnb yaml layout)."""
    from ofh_config_builder import print_ofh_config

    print_ofh_config({"ru_mac_addr": "aa:bb:cc:dd:ee:ff"}, {"dl_arfcn": 1}, ru_ofh_extra={"t1a_max_up": 390})
    rendered = yaml.safe_load(capsys.readouterr().out)
    assert rendered["ru_ofh"]["t1a_max_up"] == 390
    assert rendered["ru_ofh"]["cells"][0]["ru_mac_addr"] == "aa:bb:cc:dd:ee:ff"
    assert list(rendered["ru_ofh"]) == ["t1a_max_up", "cells"]  # timing precedes cells
    assert rendered["cell_cfg"]["dl_arfcn"] == 1


@mark.timeout(60)
def test_sim_exposes_no_delay_data_and_degrades(ru_config, o1_adapter_src):
    """The mock RU implements o-ran-delay-management but advertises no profile:
    the operational read returns empty and no timing keys are derived."""
    from ofh_config_builder import build_ofh_timing

    delay_data = ru_config.get_oran_delay_management() or {}
    assert build_ofh_timing(delay_data.get("delay-management", {})) == {}


@mark.timeout(60)
def test_profile_entry_matched_by_numerology(o1_adapter_src):
    """With several bandwidth/SCS entries, the one keyed by the active
    carrier's numerology is used — never blindly the first."""
    from ofh_config_builder import parse_delay_profile

    subtree = {
        "bandwidth-scs-delay-state": [
            {
                "bandwidth": "20000",
                "subcarrier-spacing": "15000",
                "ru-delay-profile": {"t2a-min-up": "50000", "t2a-max-up": "200000"},
            },
            {
                "bandwidth": "100000",
                "subcarrier-spacing": "30000",
                "ru-delay-profile": {"t2a-min-up": "80000", "t2a-max-up": "390000"},
            },
        ]
    }
    matched = parse_delay_profile(subtree, bandwidth_khz=100000, scs_hz=30000)
    assert matched == {"t2a-min-up": 80000, "t2a-max-up": 390000}

    # no numerology given (or no match) -> first entry carrying a profile
    assert parse_delay_profile(subtree)["t2a-min-up"] == 50000
    assert parse_delay_profile(subtree, bandwidth_khz=40000, scs_hz=30000)["t2a-min-up"] == 50000


@mark.timeout(60)
def test_active_carrier_numerology_extraction(o1_adapter_src):
    """The carrier's bandwidth/SCS is read from the uplane subtree for selection."""
    from ofh_config_builder import active_carrier_numerology

    uplane = {
        "tx-array-carriers": {"name": "Tx-Array-Carrier-00", "channel-bandwidth": "100000000"},
        "low-level-tx-endpoints": {
            "name": "sep_txch1",
            "number-of-prb-per-scs": {"scs": "KHZ_30", "number-of-prb": "273"},
        },
    }
    assert active_carrier_numerology(uplane) == (100000, 30000)
    assert active_carrier_numerology({}) == (None, None)
    assert active_carrier_numerology(None) == (None, None)
