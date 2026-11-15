"""pykci.crs: srsName → EPSG code, the first srsName of a file, reprojection, and the
classes built on the shared reprojector (pykci.ingest_citygml, the footprint tool)."""

import pytest

from pykci import crs
from pykci.crs import (ADV_COMPOUND, Reprojector, epsg_from_srs, geoid_undulation, horiz_epsg_from_srs,
                       peek_srsname)


# ---- epsg_from_srs: the CRS of a source file ------------------------------------------------------

@pytest.mark.parametrize("srs, expected", [
    ("EPSG:25832", "EPSG:25832"),
    ("urn:ogc:def:crs:EPSG::25832", "EPSG:25832"),
    ("urn:ogc:def:crs,crs:EPSG::25832,crs:EPSG::5783", "EPSG:25832"),       # compound: horizontal first
    ("http://www.opengis.net/def/crs/EPSG/0/6697", "EPSG:6697"),            # OGC HTTP form, '0/' is a version
    ("epsg:2056", "EPSG:2056"),                                             # any case
    ("urn:adv:crs:ETRS89_UTM32*DE_DHHN2016_NH", "EPSG:25832"),              # ADV names carry no code
    ("urn:adv:crs:ETRS89_UTM33*DE_DHHN92_NH", "EPSG:25833"),
    ("CH1903+ / LV95", "EPSG:2056"),
    ("CH1903 / LV03", "EPSG:21781"),
])
def test_epsg_of_a_source_srsname(srs, expected):
    assert epsg_from_srs(srs) == expected


def test_epsg_from_srs_returns_an_unknown_name_unchanged_and_none_for_nothing():
    assert epsg_from_srs("my local grid") == "my local grid"
    assert epsg_from_srs("") is None
    assert epsg_from_srs(None) is None


# ---- horiz_epsg_from_srs: the horizontal CRS of what is stored -------------------------------------

@pytest.mark.parametrize("srs, expected", [
    ("EPSG:25832", "EPSG:25832"),
    ("urn:ogc:def:crs:EPSG::25832", "EPSG:25832"),
    ("urn:ogc:def:crs,crs:EPSG::3068,crs:EPSG::5783", "EPSG:3068"),
    ("urn:adv:crs:ETRS89_UTM32*DE_DHHN2016_NH", "EPSG:25832"),
    ("urn:adv:crs:ETRS89_UTM33*DE_DHHN92_NH", "EPSG:25833"),
    ("urn:adv:crs:DE_DHDN_3GK3*DE_DHN92_NH", "EPSG:31467"),                 # Gauss-Krüger zone 3
    ("urn:adv:crs:DE_DHDN_3GK2*DE_DHN92_NH", "EPSG:31466"),
    ("something else", "something else"),
])
def test_horizontal_epsg_of_a_stored_srsname(srs, expected):
    assert horiz_epsg_from_srs(srs) == expected


# ---- geoid_undulation ------------------------------------------------------------------------------

def test_geoid_override_wins():
    assert geoid_undulation("urn:adv:crs:ETRS89_UTM32*DE_DHHN2016_NH", 0, 0, 0, override=39.5) == (
        39.5, "--geoid-offset")


def test_geoid_is_zero_with_a_note_for_a_crs_without_compound_mapping(capsys):
    assert geoid_undulation("EPSG:2056", 2600000, 1200000, 500) == (0.0, "none")
    assert "no compound CRS mapping" in capsys.readouterr().out


def test_adv_names_map_to_compound_crs_with_dhhn_heights():
    assert ADV_COMPOUND["ETRS89_UTM32"] == "EPSG:5555"
    assert set(ADV_COMPOUND) == {"ETRS89_UTM32", "ETRS89_UTM33", "DE_DHDN_GK2", "DE_DHDN_GK3",
                                 "DE_DHDN_GK4", "DE_DHDN_GK5"}


# ---- peek_srsname ----------------------------------------------------------------------------------

def test_first_srsname_of_the_repository_models(repo, fzk_lod2, citygml3_fixtures):
    assert peek_srsname(fzk_lod2) == "urn:adv:crs:ETRS89_UTM32*DE_DHHN92_NH"
    assert peek_srsname(citygml3_fixtures / "kitchen_sink.gml") == "urn:adv:crs:ETRS89_UTM32*DE_DHHN2016_NH"
    assert peek_srsname(repo / "eval" / "fixtures" / "new_elements_test.gml") == "urn:ogc:def:crs:EPSG::25832"
    # the bounded read finds the same name when it sits at the top of the file
    assert peek_srsname(fzk_lod2, limit=2_000_000) == "urn:adv:crs:ETRS89_UTM32*DE_DHHN92_NH"


def test_peek_scans_the_whole_file_unless_limited(tmp_path):
    late = tmp_path / "late.gml"
    late.write_bytes(b"<a>" + b" " * 5000 + b'<gml:Envelope srsName="EPSG:25832"/></a>')
    assert peek_srsname(late) == "EPSG:25832"
    assert peek_srsname(late, limit=1000) is None            # not within the first 1000 bytes
    assert peek_srsname(late, limit=6000) == "EPSG:25832"


def test_peek_finds_a_name_split_across_two_chunks(tmp_path):
    split = tmp_path / "split.gml"
    split.write_bytes(b"x" * 90 + b'<e srsName="EPSG:4326"/>')
    # a chunk boundary inside the attribute: the tail of the previous chunk is carried over
    assert peek_srsname(split, chunk_size=100) == "EPSG:4326"


def test_peek_of_a_file_without_crs_is_none(tmp_path):
    empty = tmp_path / "none.gml"
    empty.write_text("<CityModel/>", encoding="utf-8")
    assert peek_srsname(empty) is None
    assert peek_srsname(empty, limit=100) is None


# ---- Reprojector -----------------------------------------------------------------------------------

def test_geographic_source_is_read_as_lat_lon():
    r = Reprojector("EPSG:4326", "EPSG:25832")
    assert r.src_geographic and not r.dst_geographic and r.z_factor == 1.0
    x, y, z = r.point(53.55, 9.99, 12.5)                      # GML order of EPSG:4326: lat, lon
    assert (x, y) == pytest.approx((565590.9407, 5933912.1654), abs=1e-3)
    assert z == 12.5


def test_2d_positions_keep_no_height():
    x, y, z = Reprojector("EPSG:4326", "EPSG:25832").point(53.55, 9.99)
    assert (x, y) == pytest.approx((565590.9407, 5933912.1654), abs=1e-3)
    assert z is None


def test_heights_of_a_source_in_feet_come_out_in_metres():
    r = Reprojector("EPSG:2263", "EPSG:32618")                # New York Long Island, US survey feet
    assert not r.src_geographic
    assert r.z_factor == pytest.approx(0.3048006096, abs=1e-9)
    x, y, z = r.point(987000.0, 211000.0, 100.0)
    assert (x, y) == pytest.approx((585262.3163, 4511032.0531), abs=1e-3)
    assert z == pytest.approx(30.48006096, abs=1e-6)


def test_projected_source_to_geographic_target_gives_lon_lat():
    r = Reprojector("EPSG:2263", "EPSG:4326")
    assert r.dst_geographic
    lon, lat, _z = r.point(987000.0, 211000.0, 0.0)
    assert (lon, lat) == pytest.approx((-73.990075, 40.745822), abs=1e-6)


# ---- the reprojector of the CityGML ingest ----------------------------------------------------------

def test_ingest_reprojector_resolves_srsnames_and_rewrites_position_lists():
    from pykci.ingest_citygml import _Reprojector

    r = _Reprojector("urn:ogc:def:crs:EPSG::4326", "EPSG:25832")
    assert isinstance(r, crs.Reprojector)
    assert r.dst_epsg == "EPSG:25832"
    x1, y1, z1, x2, y2, z2 = (float(v) for v in r.poslist("53.55 9.99 12.5 53.56 10.0 1", 3).split())
    assert (x1, y1, z1) == pytest.approx((565590.9407, 5933912.1654, 12.5), abs=1e-3)
    assert (x2, y2, z2) == pytest.approx((566237.8352, 5935033.8929, 1.0), abs=1e-3)
    # 2D lists stay 2D
    assert len(r.poslist("53.55 9.99 53.56 10.0", 2).split()) == 4


def test_ingest_reprojector_leaves_what_it_cannot_read():
    from pykci.ingest_citygml import _Reprojector

    r = _Reprojector("EPSG:4326", "EPSG:25832")
    assert r.poslist("a b c", 3) == "a b c"
    assert r.poslist("", 3) == ""


def test_ingest_reprojector_takes_the_stride_from_the_length_without_dimension():
    from pykci.ingest_citygml import _Reprojector

    r = _Reprojector("EPSG:4326", "EPSG:25832")
    assert len(r.poslist("53.55 9.99 53.56 10.0", None).split()) == 4      # four values: 2D
    assert len(r.poslist("53.55 9.99 1 53.56 10.0 2", None).split()) == 6  # six values: 3D


# ---- the reprojector of the footprint tool ----------------------------------------------------------

def test_footprint_reprojector_labels_its_crs():
    from pykci.extract_citygml_building_footprints import Reprojector as FootprintReprojector

    r = FootprintReprojector("EPSG:2263", "EPSG:4326")
    assert isinstance(r, crs.Reprojector)
    assert (r.src_epsg, r.dst_epsg) == ("EPSG:2263", "EPSG:4326")
    lon, lat, z = r.point(987000.0, 211000.0, 100.0)
    assert (lon, lat) == pytest.approx((-73.990075, 40.745822), abs=1e-6)
    assert z == pytest.approx(30.48006096, abs=1e-6)
