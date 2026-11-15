"""The tools that work on CityGML files without a database: building footprints
(pykci.extract_citygml_building_footprints), the CityGML 1.0 → 2.0 upgrade
(pykci.upgrade_citygml_10_to_20) and the offline XSD validation (pykci.validate_citygml).
Each is run as the command a user runs."""

import json
import sys

import pytest

from pykci.testing.plugin import run


def _module(name, *args, cwd):
    return run([sys.executable, "-m", name, *args], cwd=cwd)


# ---- footprints -----------------------------------------------------------------------------

def test_footprint_of_the_building_in_wgs84(fzk_lod2, tmp_path):
    out = tmp_path / "footprints.geojson"
    result = _module("pykci.extract_citygml_building_footprints", fzk_lod2, "--output", out, cwd=tmp_path)
    assert result.returncode == 0, result.stderr[-800:]
    collection = json.loads(out.read_text(encoding="utf-8"))
    assert collection["type"] == "FeatureCollection"
    (feature,) = collection["features"]
    assert feature["properties"]["gml_id"].startswith("UUID_")
    assert feature["geometry"]["type"] in ("Polygon", "MultiPolygon")
    ring = feature["geometry"]["coordinates"][0]
    ring = ring[0] if feature["geometry"]["type"] == "MultiPolygon" else ring
    assert ring[0] == ring[-1] and len(ring) >= 4                       # a closed ring
    assert all(8.4 < lon < 8.5 and 49.0 < lat < 49.2 for lon, lat, *_ in ring)   # reprojected to lon/lat


def test_footprint_in_the_source_crs(fzk_lod2, tmp_path):
    out = tmp_path / "footprints_utm.geojson"
    result = _module("pykci.extract_citygml_building_footprints", fzk_lod2, "--output", out,
                     "--keep-source-crs", cwd=tmp_path)
    assert result.returncode == 0, result.stderr[-800:]
    (feature,) = json.loads(out.read_text(encoding="utf-8"))["features"]
    ring = feature["geometry"]["coordinates"][0]
    ring = ring[0] if feature["geometry"]["type"] == "MultiPolygon" else ring
    assert all(x > 100_000 and y > 5_000_000 for x, y, *_ in ring)     # UTM 32N metres


# ---- CityGML 1.0 → 2.0 ----------------------------------------------------------------------

def test_upgrade_rewrites_only_the_namespace_version(fzk_lod2, tmp_path):
    v2 = fzk_lod2.read_bytes()
    v1 = v2.replace(b"opengis.net/citygml/2.0", b"opengis.net/citygml/1.0").replace(
        b"opengis.net/citygml/building/2.0", b"opengis.net/citygml/building/1.0")
    assert v1 != v2
    source = tmp_path / "in"
    source.mkdir()
    (source / "old.gml").write_bytes(v1)
    (source / "current.gml").write_bytes(v2)
    out = tmp_path / "out"
    result = _module("pykci.upgrade_citygml_10_to_20", source, "--out", out, cwd=tmp_path)
    assert result.returncode == 0, result.stderr[-800:]
    upgraded = (out / "old.gml").read_bytes()
    assert b"citygml/1.0" not in upgraded and b"citygml/building/1.0" not in upgraded
    assert upgraded == v2                                   # nothing else changed
    assert (out / "current.gml").read_bytes() == v2         # a 2.0 file is copied as it is
    assert (source / "old.gml").read_bytes() == v1          # the original is never modified


# ---- XSD validation -------------------------------------------------------------------------

@pytest.fixture(scope="module")
def schemas():
    """The validator's schema directory; the tests are skipped until `--setup` has fetched
    the OGC bundle once (the only step that needs the network)."""
    from pykci import validate_citygml

    directory = validate_citygml.DEFAULT_SCHEMA_DIR
    if not (directory / validate_citygml.BUNDLE).is_dir():
        pytest.skip("no XSD bundle: run `pykci validate-citygml --setup` once")
    return directory


def test_valid_file_validates(schemas, fzk_lod2, tmp_path):
    report = tmp_path / "report.json"
    result = _module("pykci.validate_citygml", fzk_lod2, "--json", report, cwd=tmp_path)
    assert result.returncode == 0, (result.stdout + result.stderr)[-800:]
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["schema_valid"] is True and data["version"] == 2
    assert "XSD result: VALID" in result.stdout


def test_invalid_file_is_reported(schemas, fzk_lod2, tmp_path):
    broken = tmp_path / "broken.gml"
    broken.write_bytes(fzk_lod2.read_bytes().replace(b"<bldg:measuredHeight", b"<bldg:notAnElement", 1).replace(
        b"</bldg:measuredHeight>", b"</bldg:notAnElement>", 1))
    report = tmp_path / "report.json"
    result = _module("pykci.validate_citygml", broken, "--json", report, cwd=tmp_path)
    assert result.returncode == 1
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["schema_valid"] is False and data["n_errors"] >= 1
    assert "notAnElement" in json.dumps(data["groups"])


def test_file_that_is_not_xml_is_an_error(schemas, tmp_path):
    bad = tmp_path / "not.gml"
    bad.write_text("this is not XML", encoding="utf-8")
    assert _module("pykci.validate_citygml", bad, cwd=tmp_path).returncode == 2
