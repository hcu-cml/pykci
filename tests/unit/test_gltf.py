"""pykci.gltf: the frame of a tileset, the GLB container and the EXT_structural_metadata
encoding of build_3dtiles. The bytes of whole tiles are pinned in test_build_3dtiles_glb.py;
here each piece is read back (pykci.testing.glb reads a GLB)."""

import struct

import numpy as np
import pytest

from pykci import gltf
from pykci.testing import glb


# ---- the frame -------------------------------------------------------------------------------

def test_ecef_agrees_with_proj():
    from pyproj import Transformer

    lon, lat, h = np.array([9.9937, 10.2, -74.0]), np.array([53.5511, 53.4, 40.7]), np.array([25.0, -3.0, 300.0])
    expected = np.column_stack(Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True).transform(lon, lat, h))
    assert gltf.ecef(lon, lat, h) == pytest.approx(expected, abs=1e-6)


def test_ecef_of_one_point_is_one_row():
    assert gltf.ecef(0.0, 0.0, 0.0) == pytest.approx(np.array([[gltf.WGS84_A, 0.0, 0.0]]))
    north_pole = gltf.ecef(0.0, 90.0, 0.0)[0]
    assert north_pole[2] == pytest.approx(6356752.314245, abs=1e-5) and abs(north_pole[0]) < 1e-6


def test_east_north_up_are_the_rows_of_an_orthonormal_right_handed_basis():
    basis = gltf.enu_basis(9.9937, 53.5511)
    east, north, up = basis
    assert basis.shape == (3, 3) and basis @ basis.T == pytest.approx(np.eye(3), abs=1e-12)
    assert np.cross(east, north) == pytest.approx(up, abs=1e-12)
    assert east[2] == 0.0                                           # east is parallel to the equator
    # up is the ellipsoid normal: a point 1 m higher lies 1 m further along it
    step = gltf.ecef(9.9937, 53.5511, 1.0)[0] - gltf.ecef(9.9937, 53.5511, 0.0)[0]
    assert step == pytest.approx(up, abs=1e-9)


# ---- GLB --------------------------------------------------------------------------------------

def test_views_are_aligned_and_accessors_describe_them():
    w = gltf.GlbWriter()
    a = w.view(b"abc")                               # 3 bytes: the next view starts at 4
    b = w.view(b"12345678", target=34962)
    c = w.view(b"x", align=8)                        # at 16, the next multiple of 8 after 12
    assert (a, b, c) == (0, 1, 2)
    assert w.views == [{"buffer": 0, "byteOffset": 0, "byteLength": 3},
                       {"buffer": 0, "byteOffset": 4, "byteLength": 8, "target": 34962},
                       {"buffer": 0, "byteOffset": 16, "byteLength": 1}]
    assert w.accessor(b, 2, "SCALAR", ctype=5125, min=[1], max=[9]) == 0
    assert w.accessors == [{"bufferView": 1, "byteOffset": 0, "componentType": 5125, "count": 2,
                            "type": "SCALAR", "min": [1], "max": [9]}]


def test_array_makes_a_view_and_an_accessor_with_one_element_per_row():
    w = gltf.GlbWriter()
    pos = np.array([[0.0, 1.0, 2.0], [3.0, -1.0, 5.0]], dtype=np.float32)
    assert w.array(pos, 5126, "VEC3", 34962, minmax=True) == 0
    assert w.accessors[0] == {"bufferView": 0, "byteOffset": 0, "componentType": 5126, "count": 2, "type": "VEC3",
                              "min": [0.0, -1.0, 2.0], "max": [3.0, 1.0, 5.0]}
    assert w.views[0] == {"buffer": 0, "byteOffset": 0, "byteLength": 24, "target": 34962}
    w.array(np.array([0, 1, 1], dtype=np.uint32), 5125, "SCALAR", 34963)
    assert "min" not in w.accessors[1] and w.accessors[1]["count"] == 3


def test_glb_holds_the_json_and_the_binary_chunk():
    w = gltf.GlbWriter()
    w.accessor(w.view(b"\x01\x02\x03"), 3, "SCALAR", ctype=5121)
    data = w.glb({"asset": {"version": "2.0", "copyright": "© Stadt"}})
    assert data[:4] == b"glTF" and struct.unpack_from("<II", data, 4) == (2, len(data))
    json_length, json_kind = struct.unpack_from("<II", data, 12)
    assert json_kind == 0x4E4F534A and json_length % 4 == 0 and data[20 + json_length - 1:20 + json_length] in b" }"
    assert struct.unpack_from("<II", data, 20 + json_length) == (4, 0x004E4942)
    g, binary = glb.chunks(data)
    assert binary == b"\x01\x02\x03\x00"                             # padded to a multiple of 4
    assert g["buffers"] == [{"byteLength": 4}]
    assert g["asset"]["copyright"] == "© Stadt" and "©".encode("utf-8") in data      # UTF-8, not escaped
    assert list(g) == ["asset", "bufferViews", "accessors", "buffers"]


def test_glb_is_finish_then_pack():
    """glb() completes the glTF with its views, accessors and buffer and packs it; a key the
    glTF already has keeps its place."""
    w = gltf.GlbWriter()
    w.view(b"abcd")
    doc = {"asset": {"version": "2.0"}, "buffers": [], "scene": 0}
    data = w.glb(doc)
    assert list(doc) == ["asset", "buffers", "scene", "bufferViews", "accessors"]
    assert data == gltf.pack(doc, b"abcd")


def test_a_glb_without_data_and_what_is_no_glb():
    data = gltf.GlbWriter().glb({"asset": {"version": "2.0"}})
    assert glb.chunks(data) == ({"asset": {"version": "2.0"}, "bufferViews": [], "accessors": [],
                                 "buffers": [{"byteLength": 0}]}, b"")
    with pytest.raises(ValueError, match="not a GLB 2 file"):
        glb.chunks(data[:-4])
    with pytest.raises(ValueError, match="not a GLB 2 file"):
        glb.chunks(b"JSON" + data[4:])


# ---- metadata ---------------------------------------------------------------------------------

@pytest.mark.parametrize("value, out", [
    (12, 12.0), ("7.5", 7.5), (None, None), ("", None), ("abc", None), (float("inf"), None), (float("nan"), None),
    (True, 1.0), ([1], None)])
def test_finite_float(value, out):
    assert gltf.finite_float(value) == out


def test_unique_id_numbers_the_ids_that_are_taken():
    used = set()
    assert [gltf.unique_id(p, used) for p in ("a", "a", "b", "a", "a_2")] == ["a", "a_2", "b", "a_3", "a_2_2"]
    assert used == {"a", "a_2", "b", "a_3", "a_2_2"}


def test_class_property_of_a_number_and_of_a_text():
    assert gltf.class_property("height", "FLOAT64", "authoritative") == {
        "name": "height", "extras": {"provenance": "authoritative"},
        "type": "SCALAR", "componentType": "FLOAT64", "noData": gltf.F64_NODATA}
    assert gltf.class_property("osm_name", "STRING", "crowd_sourced", "Name in OpenStreetMap") == {
        "name": "osm_name", "extras": {"provenance": "crowd_sourced"}, "description": "Name in OpenStreetMap",
        "type": "STRING", "noData": ""}
    assert gltf.class_property("id", "STRING", "authoritative")["semantic"] == "ID"
    assert gltf.class_property("name", "STRING", "authoritative")["semantic"] == "NAME"


def test_schema_is_one_feature_class_keyed_by_property_id():
    props = [{"id": "id", "name": "id", "type": "STRING", "provenance": "authoritative"},
             {"id": "p_1", "name": "用途", "type": "STRING", "provenance": "authoritative", "description": "use"}]
    schema = gltf.schema_json(props)
    feature = schema["classes"]["feature"]
    assert schema["id"] == "pykci_features" and feature["name"] == "City feature"
    assert "Building" in feature["description"]
    assert list(feature["properties"]) == ["id", "p_1"]
    assert feature["properties"]["p_1"] == gltf.class_property("用途", "STRING", "authoritative", "use")


def test_float_column_marks_absent_values():
    w = gltf.GlbWriter()
    w.view(b"abc")                                                  # the column starts at a multiple of 8
    col = gltf.float_column(w, [1.5, None, -2.0])
    view = w.views[col["values"]]
    assert view["byteOffset"] == 8 and view["byteLength"] == 24
    assert np.frombuffer(bytes(w.bin[8:32]), dtype="<f8").tolist() == [1.5, gltf.F64_NODATA, -2.0]


def test_string_column_is_utf8_with_offsets():
    w = gltf.GlbWriter()
    col = gltf.string_column(w, ["Rathaus", None, "Straße"])
    assert col["stringOffsetType"] == "UINT32"
    values, offsets = w.views[col["values"]], w.views[col["stringOffsets"]]
    raw = bytes(w.bin[values["byteOffset"]:values["byteOffset"] + values["byteLength"]])
    offs = np.frombuffer(bytes(w.bin[offsets["byteOffset"]:offsets["byteOffset"] + offsets["byteLength"]]), "<u4")
    assert offs.tolist() == [0, 7, 7, 14]                           # ß is two bytes
    assert [raw[a:b].decode("utf-8") for a, b in zip(offs, offs[1:])] == ["Rathaus", "", "Straße"]
    assert offsets["byteOffset"] % 8 == 0


def test_a_column_of_empty_strings_still_has_data():
    w = gltf.GlbWriter()
    col = gltf.string_column(w, [None, None])
    assert w.views[col["values"]]["byteLength"] == 1                # a buffer view must not be empty
