# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Tests for xml_utils — the NETCONF error-info parsing helpers.

extract_bad_element pulls the <bad-element> name out of an RPCError's info
field so the operator sees which node the O-RU rejected. ncclient usually
hands it over as an xmltodict dict, but falls back to a raw XML string; both
shapes must yield the name.
"""

from pytest import mark


@mark.timeout(60)
def test_extract_bad_element_from_dict(o1_adapter_src):
    """The common path: ncclient parsed error-info into a dict."""
    from xml_utils import extract_bad_element

    assert extract_bad_element({"bad-element": "tx-array-carriers"}) == "tx-array-carriers"
    assert extract_bad_element({}) is None
    assert extract_bad_element(None) is None


@mark.timeout(60)
def test_extract_bad_element_from_namespaced_leaf_xml(o1_adapter_src):
    """Regression: a leaf <bad-element> has no child elements, so it is falsy;
    the old `find(...) or find(...)` discarded the namespaced match and fell
    through to the namespaceless one (which cannot match), returning None and
    dropping the diagnostic. The namespaced leaf's text must survive."""
    _ = o1_adapter_src
    from xml_utils import extract_bad_element

    info = (
        '<error-info xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
        "<bad-element>configurable-tdd-pattern</bad-element>"
        "</error-info>"
    )
    assert extract_bad_element(info) == "configurable-tdd-pattern"


@mark.timeout(60)
def test_extract_bad_element_xml_without_bad_element(o1_adapter_src):
    """XML error-info that carries no bad-element yields None, not a crash."""
    _ = o1_adapter_src
    from xml_utils import extract_bad_element

    assert extract_bad_element("<error-info><other-info>x</other-info></error-info>") is None


@mark.timeout(60)
def test_extract_bad_element_unparseable_is_none(o1_adapter_src):
    """A non-XML string is swallowed, not raised."""
    _ = o1_adapter_src
    from xml_utils import extract_bad_element

    assert extract_bad_element("not xml <<<") is None
