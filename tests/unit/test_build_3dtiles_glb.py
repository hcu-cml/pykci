"""pykci.build_3dtiles: the bytes of a tile. A tile of a known small mesh is compared with
its recorded SHA-256, so any change to how tiles are written shows here, without a database.
The digests were recorded when the GLB writer was still part of build_3dtiles; pykci.gltf,
which holds it now, must give the same bytes."""

import hashlib
import struct

import numpy as np
import pytest

from pykci import build_3dtiles as b
from pykci import gltf
from pykci.testing import glb


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


QUAD = np.array([[0.0, 0.0, 0.0], [4.0, 0.0, 0.0], [4.0, 3.0, 0.0], [0.0, 3.0, 0.0]])
TRI2 = np.array([0, 1, 2, 0, 2, 3], dtype=np.int32)

# (feature index, feature id, surface type, vertices (east, north, up), normal, triangle indices)
MESH = [
    (0, "b1", "RoofSurface", QUAD + [0.0, 0.0, 9.5], (0.0, 0.0, 1.0), TRI2),
    (0, "b1", "WallSurface", QUAD[:3] @ np.array([[1.0, 0, 0], [0, 0, 1.0], [0, 1.0, 0]]), (0.0, -1.0, 0.0),
     np.array([0, 1, 2], dtype=np.int32)),
    (1, "b2", "RoofSurface", QUAD + [10.0, 2.0, 7.25], (0.0, 0.0, 1.0), TRI2),
]
MATERIALS = [{"name": "roof", "color": (0.8, 0.3, 0.2)},
             {"name": "wall", "color": (0.9, 0.9, 0.85, 0.5), "roughness": 0.6}]
SCHEMA = [
    {"id": "id", "name": "id", "type": "STRING", "provenance": "authoritative", "description": "gml:id"},
    {"id": "name", "name": "name", "type": "STRING", "provenance": "crowd_sourced"},
    {"id": "measured_height", "name": "measured_height", "type": "FLOAT64", "provenance": "authoritative"},
    {"id": "never_set", "name": "never_set", "type": "FLOAT64", "provenance": "derived"},
    {"id": "p_1a2b", "name": "用途", "type": "STRING", "provenance": "authoritative"},
]
ROWS = [{"id": "b1", "name": "Rathaus", "measured_height": 9.5, "p_1a2b": "業務施設"},
        {"id": "b2", "name": None, "measured_height": None, "p_1a2b": None}]


def material_of(fid, surface_type):
    return 0 if surface_type == "RoofSurface" else 1


# ---- tiled output (3D Tiles 1.1 with feature ids and a property table) ----------------------

def test_tile_with_attributes_is_byte_for_byte_the_recorded_one():
    data = b._pack_tile(MESH, MATERIALS, material_of, ROWS, SCHEMA)
    assert (len(data), sha(data)) == (4024, "e68e057051f65f3b910447e616e398eaed36e933e0cf4fe8a56dc1ca62f40f4d")


def test_tile_without_attributes_is_byte_for_byte_the_recorded_one():
    data = b._pack_tile(MESH, MATERIALS, material_of, ROWS, None)
    assert (len(data), sha(data)) == (2520, "32c6b6fb00ba54091d310bd59318ce17ab6097f4a6156c85489904242ca2a02f")


def test_tile_structure():
    data = b._pack_tile(MESH, MATERIALS, material_of, ROWS, SCHEMA)
    assert data[:4] == b"glTF" and struct.unpack_from("<II", data, 4) == (2, len(data))
    g = glb.chunks(data)[0]
    assert g["extensionsUsed"] == ["EXT_mesh_features", "EXT_structural_metadata"]
    assert [m["name"] for m in g["materials"]] == ["roof", "wall"]
    assert g["materials"][1]["alphaMode"] == "BLEND" and "alphaMode" not in g["materials"][0]
    roof, wall = g["meshes"][0]["primitives"]
    assert roof["material"] == 0 and wall["material"] == 1
    assert roof["extensions"]["EXT_mesh_features"]["featureIds"] == [
        {"featureCount": 2, "attribute": 0, "propertyTable": 0}]
    # east, north, up → glTF's east, up, −north
    pos = g["accessors"][roof["attributes"]["POSITION"]]
    assert pos["min"] == [0.0, 7.25, -5.0] and pos["max"] == [14.0, 9.5, 0.0]
    meta = g["extensions"]["EXT_structural_metadata"]
    table = meta["propertyTables"][0]
    assert table["name"] == "features" and table["class"] == "feature" and table["count"] == 2
    # a property no feature of the tile has is left out of the table and of the tile's schema
    assert list(table["properties"]) == ["id", "name", "measured_height", "p_1a2b"]
    props = meta["schema"]["classes"]["feature"]["properties"]
    assert list(props) == ["id", "name", "measured_height", "p_1a2b"]
    assert props["id"] == {"name": "id", "extras": {"provenance": "authoritative"}, "description": "gml:id",
                           "type": "STRING", "noData": "", "semantic": "ID"}
    assert props["name"]["semantic"] == "NAME"
    assert props["measured_height"] == {"name": "measured_height", "extras": {"provenance": "authoritative"},
                                        "type": "SCALAR", "componentType": "FLOAT64",
                                        "noData": -1.7976931348623157e308}
    assert props["p_1a2b"]["name"] == "用途"
    assert g["buffers"] == [{"byteLength": len(glb.chunks(data)[1])}]


# ---- the single GLB of build_3dtiles (no --tiled) -------------------------------------------

def test_single_glb_is_byte_for_byte_the_recorded_one():
    pos = [QUAD.tolist(), (QUAD + [0, 0, 5]).tolist()]
    nor = [[[0.0, 0.0, 1.0]] * 4] * 2
    idx = [TRI2.tolist(), TRI2.tolist()]
    fids = [[0, 0, 0, 0], [1, 1, 1, 1]]
    data = b.pack_glb(pos, nor, idx, mat_fids=fids, n_features=2, materials=MATERIALS)
    assert (len(data), sha(data)) == (2436, "42a9a4b1182552ecc9ef7c70e70a1d553834211f961e5e8533aa798ce8850026")
    plain = b.pack_glb(pos, nor, idx, materials=MATERIALS)
    assert (len(plain), sha(plain)) == (1844, "2bbd9301f462e1d4dbe831012fdf97495e2014a3c5a4318ba21823e952c1697c")


def test_single_glb_without_geometry_has_no_binary_chunk():
    data = b.pack_glb([[]], [[]], [[]], materials=MATERIALS)
    doc, binary = glb.chunks(data)
    assert binary == b"" and doc["buffers"] == [{"byteLength": 0}]
    assert len(data) == 20 + struct.unpack_from("<I", data, 12)[0]         # header + JSON chunk, nothing after


# ---- attribute ids and values ---------------------------------------------------------------

@pytest.mark.parametrize("key, pid", [
    ("measured_height", "measured_height"),
    ("roof-type", "roof_type"),
    ("3d_area", "_3d_area"),
    ("", "p_" + hashlib.sha1(b"").hexdigest()[:10]),
    ("用途", "p_" + hashlib.sha1("用途".encode("utf-8")).hexdigest()[:10]),      # mostly non-ASCII: hashed
    ("straße", "stra_e"),
])
def test_property_id(key, pid):
    assert b._meta_id(key, set()) == pid


def test_property_ids_are_unique():
    used = set()
    assert [b._meta_id(k, used) for k in ("a b", "a_b", "a-b")] == ["a_b", "a_b_2", "a_b_3"]
    assert used == {"a_b", "a_b_2", "a_b_3"}


@pytest.mark.parametrize("value, mtype, out", [
    (None, "FLOAT64", None), ("12.5", "FLOAT64", 12.5), ("x", "FLOAT64", None), (float("nan"), "FLOAT64", None),
    (True, "STRING", "true"), (False, "STRING", "false"), (3, "STRING", "3"),
    (["a", 1], "STRING", '["a", 1]'), ({"k": "ä"}, "STRING", '{"k": "ä"}'), (None, "STRING", None),
])
def test_attribute_value(value, mtype, out):
    assert b._coerce(value, mtype) == out


# ---- the tileset frame ---------------------------------------------------------------------

def test_ecef_of_a_point_in_hamburg():
    from pyproj import Transformer

    x, y, z = b.lonlat_to_ecef(9.9937, 53.5511, 25.0)
    geocentric = Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True).transform(9.9937, 53.5511, 25.0)
    assert (x, y, z) == pytest.approx(geocentric, abs=1e-6)
    lon, lat = b.ecef_to_lonlat(x, y, z)
    assert (lon, lat) == pytest.approx((9.9937, 53.5511), abs=1e-7)
    # the per-point function and the array function of pykci.gltf are the same formula
    assert gltf.ecef(9.9937, 53.5511, 25.0)[0] == pytest.approx([x, y, z], abs=1e-6)


def test_east_north_up_at_a_point():
    east, north, up = b.enu_basis(9.9937, 53.5511)
    frame = np.array([east, north, up])
    assert frame @ frame.T == pytest.approx(np.eye(3), abs=1e-12)            # orthonormal
    assert np.cross(east, north) == pytest.approx(up, abs=1e-12)             # right-handed
    assert east[2] == 0.0 and up[2] == pytest.approx(np.sin(np.radians(53.5511)))
    x, y, z = b.lonlat_to_ecef(9.9937, 53.5511, 0.0)
    assert up == pytest.approx(np.array([x, y, z * 1.0067394967]) / np.linalg.norm([x, y, z * 1.0067394967]), abs=1e-9)


# ---- quadtree and bounding regions ----------------------------------------------------------

def feature(i, cx, cy, z0=0.0, z1=10.0):
    return {"id": f"f{i}", "cx": cx, "cy": cy, "x0": cx - 1, "y0": cy - 1, "x1": cx + 1, "y1": cy + 1,
            "z0": z0, "z1": z1}


def test_quadtree_splits_until_leaves_are_small_enough():
    feats = [feature(i, x, y) for i, (x, y) in enumerate([(0, 0), (1, 1), (90, 0), (91, 1), (0, 90), (90, 90)])]
    tree = b._quadtree(feats, max_features=2)
    assert tree["path"] == "r" and tree["bounds"] == (-1, -1, 92, 91) and tree["z"] == (0.0, 10.0)
    assert [(c["path"], c["ids"]) for c in tree["children"]] == [
        ("r0", ["f0", "f1"]), ("r1", ["f2", "f3"]), ("r2", ["f4"]), ("r3", ["f5"])]
    whole = b._quadtree(feats, max_features=10)
    assert whole["ids"] == [f"f{i}" for i in range(6)] and "children" not in whole


def test_region_is_radians_and_ellipsoidal_heights():
    class Identity:
        def transform(self, xs, ys):
            return xs, ys

    region = b._region((10.0, 53.0, 10.5, 53.5), (2.0, 30.0), Identity(), geoid_m=40.0)
    assert region == pytest.approx([np.radians(10.0) - 1e-7, np.radians(53.0) - 1e-7,
                                    np.radians(10.5) + 1e-7, np.radians(53.5) + 1e-7, 41.0, 71.0])
