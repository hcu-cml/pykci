"""Parts of the CityGML serialisers (pykci.citygml_writer) that the 2.0 and the 3.0 writer
share or that several features of one writer share, and the relationship table of
pykci.citygml_profiles that the export and the ingest (pykci.ingest_citygml) walk."""

import pytest
from lxml import etree

from pykci import citygml_writer, ingest_citygml
from pykci.citygml_profiles import FEATURE_RELS, PROFILE_V2, PROFILE_V3
from pykci.citygml_writer import BaseWriter, Writer2, Writer3

DATASET = {"crs": "EPSG:25832"}


def _children(el):
    return [(etree.QName(c).localname, c.text, c.get("codeSpace")) for c in el]


@pytest.mark.parametrize("writer_class, profile", [(Writer2, PROFILE_V2), (Writer3, PROFILE_V3)])
def test_class_function_usage_are_written_once_for_both_versions(writer_class, profile):
    assert writer_class.cfu is BaseWriter.cfu
    w = writer_class(DATASET, {})
    assert w.P is profile
    el = etree.Element("feature")
    w.cfu(el, "gen", {"class": "1000", "class_codespace": "http://example.org/class",
                      "function_list": ["a", "b"], "function_codespace": "http://example.org/fn",
                      "usage": "u"})
    assert _children(el) == [("class", "1000", "http://example.org/class"),
                             ("function", "a", "http://example.org/fn"),
                             ("function", "b", "http://example.org/fn"),
                             ("usage", "u", None)]
    assert etree.QName(el[0]).namespace == profile.ns["gen"]


def test_building_codes_use_their_own_keys():
    w = Writer2(DATASET, {})
    el = etree.Element("feature")
    w.cfu(el, "bldg", {"class_code": "31001", "class_codespace": "http://example.org/c",
                       "function_code": "1000", "usage_code": ""},
          "class_code", "function_code", "usage_code")
    assert _children(el) == [("class", "31001", "http://example.org/c"), ("function", "1000", None)]


def test_lod_geometry_is_written_in_schema_order_and_lod1_has_no_curves(monkeypatch):
    w = Writer2(DATASET, {})
    written = []
    monkeypatch.setattr(w, "solid", lambda el, prefix, local, solid: written.append(local))
    monkeypatch.setattr(w, "write_ms", lambda el, prefix, local, lst: written.append(local))
    monkeypatch.setattr(w, "multicurve", lambda el, prefix, local, lst, gid: written.append(local))
    monkeypatch.setattr(w, "write_tic", lambda el, prefix, tic, lod: written.append(f"lod{lod}TerrainIntersection"))
    ms = {"1": ["m"], "2": ["m"]}
    mcs = {"1": ["c"], "2": ["c"]}
    maps = (ms, {}, mcs, {}, {"1": "s", "2": "s"})
    g = {"tic": {"2": "t"}}
    el = etree.Element("feature")
    w.lod_block(el, "bldg", "1", g, maps)
    assert written == ["lod1Solid", "lod1MultiSurface"]
    written.clear()
    w.lod_block(el, "brid", "2", g, maps)
    assert written == ["lod2Solid", "lod2MultiSurface", "lod2MultiCurve", "lod2TerrainIntersection"]
    written.clear()
    w.lod_block(el, "bldg", "3", g, maps)                    # nothing stored for LoD3
    assert written == []


def test_export_and_ingest_walk_the_same_containment_edges():
    assert citygml_writer.FEATURE_RELS is FEATURE_RELS
    assert ingest_citygml.FEATURE_RELS is FEATURE_RELS
    assert len(FEATURE_RELS) == len(set(FEATURE_RELS)) == 18
    assert {"HAS_BOUNDARY", "HAS_OPENING", "HAS_BUILDING_PART", "HAS_SECTION", "HAS_POINT_CLOUD"} <= set(FEATURE_RELS)
