"""
build_tree_tiles.py — street trees in the Neo4j graph → instanced, textured 3D Tiles 1.1

Rebuilds the trees from the graph alone (populated by enrich_street_trees.py): every tree
is its implicit geometry, i.e. one of a few shared prototypes placed by the tree's
transformation matrix at its reference point. The tileset keeps that structure instead of
baking ~615 triangles per tree into the tiles:

  prototypes/<n>.bin  each prototype's triangles once (positions, normals, texture
                      coordinates), referenced by every tile that uses it
  textures/<n>.png    the prototype textures (CityGML ParameterizedTexture), downscaled
                      to --texture-size, colour-dilated into the transparent area so
                      mipmapped leaf edges do not darken
  tiles/<q>.glb       one leaf of a quadtree over the trees: one node per prototype with
                      EXT_mesh_gpu_instancing (TRANSLATION / ROTATION / SCALE per tree) and
                      EXT_instance_features + EXT_structural_metadata, so every tree is a
                      pickable feature carrying its attributes (same `feature` class and
                      provenance extras as build_3dtiles.py --tiled)

Internal tiles have no content: trees stream in when the camera is within roughly
--load-distance metres (a screen-space-error choice, see _geometric_error).

Placement: the reference point goes through the dataset CRS → WGS84 → ECEF → the
tileset's ENU frame; the matrix's 2x2 horizontal part is carried into ENU with the local
Jacobian of that projection (grid convergence + scale factor), then split into a rotation
about the vertical and a horizontal scale; the vertical scale stays as is. A tree whose
matrix has a zero horizontal scale is drawn with its vertical scale (counted in the log).

Usage:
    python build_tree_tiles.py --out ../pykci-client/data/tiles/hamburg/trees \\
        --texture-root /path/to/dir/the/imageURIs/are/relative/to \\
        --align ../pykci-client/data/tiles/hamburg/tileset.json

--align reads geoid_offset_m from the city tileset (build_3dtiles.py), so trees and
buildings share one vertical convention.

Neo4j connection (env overrides): NEO4J_URI (or NEO4J_URL) / NEO4J_USER / NEO4J_PASSWORD
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import struct
import sys
import time
from pathlib import Path

import numpy as np
from neo4j import GraphDatabase
from pyproj import Transformer

NEO4J_URL      = os.getenv("NEO4J_URI") or os.getenv("NEO4J_URL", "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "neo4jneo4j")

WGS84_A  = 6378137.0
WGS84_F  = 1.0 / 298.257223563
WGS84_E2 = 2 * WGS84_F - WGS84_F ** 2
F64_NODATA = -1.7976931348623157e308       # "absent" marker for FLOAT64 properties

# Node properties that are index plumbing or geometry, not tree attributes.
_EXCLUDE = re.compile(r"^(bbox.*|location|gtype|has_bbox_envelope|_gen_attrs_map|center_[xy]|"
                      r"ground_z|source|name|.*_xml)$")
# Untextured triangles (a ring without texture coordinates): plain bark.
_BARK = [0.36, 0.29, 0.22, 1.0]
_FALLBACK_FOLIAGE = [0.33, 0.45, 0.22, 1.0]


# ---- geodesy --------------------------------------------------------------------------

def _horiz_epsg(srs_name: str) -> str:
    """CityGML srsName → horizontal EPSG code (same rules as build_3dtiles.py)."""
    m = re.search(r"EPSG[:\s]+(\d+)", srs_name or "")
    if m:
        return f"EPSG:{m.group(1)}"
    m = re.search(r"ETRS89_UTM(\d+)", srs_name or "", re.IGNORECASE)
    if m:
        return f"EPSG:{25800 + int(m.group(1))}"
    m = re.search(r"GK(\d)", srs_name or "", re.IGNORECASE)
    if m:
        return f"EPSG:{31464 + int(m.group(1))}"
    return srs_name


def _ecef(lon_deg, lat_deg, h):
    lon, lat = np.radians(lon_deg), np.radians(lat_deg)
    n = WGS84_A / np.sqrt(1 - WGS84_E2 * np.sin(lat) ** 2)
    return np.column_stack([(n + h) * np.cos(lat) * np.cos(lon),
                            (n + h) * np.cos(lat) * np.sin(lon),
                            (n * (1 - WGS84_E2) + h) * np.sin(lat)])


def _quaternions(r: np.ndarray) -> np.ndarray:
    """(N, 3, 3) rotation matrices → (N, 4) unit quaternions (x, y, z, w)."""
    q = np.empty((len(r), 4))
    tr = r[:, 0, 0] + r[:, 1, 1] + r[:, 2, 2]
    for i in range(len(r)):
        m = r[i]
        if tr[i] > 0:
            k = 0.5 / math.sqrt(tr[i] + 1.0)
            q[i] = [(m[2, 1] - m[1, 2]) * k, (m[0, 2] - m[2, 0]) * k, (m[1, 0] - m[0, 1]) * k, 0.25 / k]
        elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
            k = 2.0 * math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2])
            q[i] = [0.25 * k, (m[0, 1] + m[1, 0]) / k, (m[0, 2] + m[2, 0]) / k, (m[2, 1] - m[1, 2]) / k]
        elif m[1, 1] > m[2, 2]:
            k = 2.0 * math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2])
            q[i] = [(m[0, 1] + m[1, 0]) / k, 0.25 * k, (m[1, 2] + m[2, 1]) / k, (m[0, 2] - m[2, 0]) / k]
        else:
            k = 2.0 * math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1])
            q[i] = [(m[0, 2] + m[2, 0]) / k, (m[1, 2] + m[2, 1]) / k, 0.25 * k, (m[1, 0] - m[0, 1]) / k]
    return q / np.linalg.norm(q, axis=1, keepdims=True)


def _enu_basis(lon_deg: float, lat_deg: float) -> np.ndarray:
    """Rows: east, north, up unit vectors (ECEF) at a WGS84 point."""
    lon, lat = math.radians(lon_deg), math.radians(lat_deg)
    return np.array([[-math.sin(lon), math.cos(lon), 0.0],
                     [-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat)],
                     [math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)]])


# ---- graph reads ---------------------------------------------------------------------

_Q_PROTOTYPES = """
MATCH (p:GeometryPrototype) WHERE $source IS NULL OR p.source = $source
MATCH (p)-[m:HAS_SURFACE_MEMBER]->(:GeometryPolygon)-[:HAS_EXTERIOR_RING]->(r:GeometryRing)
WITH p, m.order AS o, r ORDER BY o
RETURN p.id AS id, p.srs_dimension AS dim, p.texture_image AS image,
       p.x3d_diffuse_color AS diffuse, collect([r.pos_list, r.texture_coordinates]) AS rings
"""

_Q_TREES = """
MATCH (t:SolitaryVegetationObject)-[:HAS_LOD_SURFACE]->(ms:GeometryMultiSurface)
      -[:HAS_PROTOTYPE]->(p:GeometryPrototype)
WHERE $source IS NULL OR t.source = $source
RETURN t.id AS id, ms.transformation_matrix AS tm, ms.reference_point AS rp, p.id AS proto,
       t.bbox_min_z AS z0, t.bbox_max_z AS z1, t._gen_attrs_map AS gen, properties(t) AS props
"""


def _dataset_crs(sess, source: str | None) -> str:
    row = sess.run("""
        MATCH (t:SolitaryVegetationObject)-[:PART_OF]->(d:Dataset)
        WHERE $source IS NULL OR t.source = $source
        RETURN d.crs AS crs LIMIT 1""", source=source).single()
    if row is None or not row["crs"]:
        sys.exit("ERROR: no SolitaryVegetationObject with a Dataset CRS in the graph "
                 "(run enrich_street_trees.py first)")
    return row["crs"]


# ---- prototypes ------------------------------------------------------------------------

def _prototype_mesh(rings: list, dim: int) -> dict:
    """Triangles (closed 4-point rings) → non-indexed glTF arrays (y-up: x, z, −y),
    textured and untextured triangles apart. CityGML texture space has its origin at the
    bottom left, glTF at the top left: v → 1 − v."""
    parts = {"tex": ([], [], []), "plain": ([], [], [])}
    for pos, uv in rings:
        v = [float(t) for t in (pos or "").split()]
        if len(v) < 3 * dim:
            continue
        p = np.array(v[:3 * dim]).reshape(3, dim)[:, :3]
        p = np.column_stack([p[:, 0], p[:, 2], -p[:, 1]])
        n = np.cross(p[1] - p[0], p[2] - p[0])
        ln = np.linalg.norm(n)
        n = n / ln if ln > 0 else np.array([0.0, 1.0, 0.0])
        t = [float(x) for x in (uv or "").split()]
        key = "tex" if len(t) >= 6 else "plain"
        parts[key][0].append(p)
        parts[key][1].append(np.repeat(n[None, :], 3, axis=0))
        if key == "tex":
            parts[key][2].append(np.array([[t[0], 1 - t[1]], [t[2], 1 - t[3]], [t[4], 1 - t[5]]]))
    out = {}
    for key, (ps, ns, ts) in parts.items():
        if ps:
            out[key] = {"pos": np.vstack(ps).astype("<f4"), "nor": np.vstack(ns).astype("<f4"),
                        "uv": np.vstack(ts).astype("<f4") if ts else None}
    return out


def _write_prototype_bin(mesh: dict, path: Path) -> dict:
    """One external buffer per prototype; returns the layout the tiles reference."""
    buf, layout = bytearray(), {}
    for key, m in mesh.items():
        entry = {"count": len(m["pos"]), "min": m["pos"].min(axis=0).tolist(),
                 "max": m["pos"].max(axis=0).tolist()}
        for attr in ("pos", "nor", "uv"):
            if m[attr] is None:
                continue
            while len(buf) % 4:
                buf.append(0)
            entry[attr] = (len(buf), m[attr].nbytes)
            buf.extend(m[attr].tobytes())
        layout[key] = entry
    path.write_bytes(bytes(buf))
    return {"layout": layout, "byteLength": len(buf)}


def _prepare_texture(src: Path, dst: Path, size: int) -> None:
    """Downscale with premultiplied alpha, then fill the colour of transparent pixels from
    their surroundings (alpha-weighted blur), so filtered and mipmapped leaf edges keep the
    leaf colour instead of fading to the (arbitrary) colour under alpha = 0."""
    from PIL import Image, ImageFilter
    im = Image.open(src).convert("RGBA")
    if max(im.size) > size:
        im = im.convert("RGBa").resize((size, size), Image.LANCZOS).convert("RGBA")
    a = np.asarray(im, dtype=np.float32)
    alpha = a[:, :, 3:4] / 255.0
    pre = Image.fromarray(np.clip(a[:, :, :3] * alpha, 0, 255).astype(np.uint8))
    wgt = Image.fromarray((alpha[:, :, 0] * 255).astype(np.uint8))
    fill = np.zeros_like(a[:, :, :3])
    have = np.zeros(alpha.shape[:2], dtype=bool)
    for radius in (2, 8, 32, 128):
        num = np.asarray(pre.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32)
        den = np.asarray(wgt.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32)[:, :, None] / 255.0
        ok = (den[:, :, 0] > 1e-3) & ~have
        fill[ok] = (num / np.maximum(den, 1e-3))[ok]
        have |= ok
    out = a.copy()
    clear = alpha[:, :, 0] < 0.5
    out[clear, :3] = fill[clear]
    Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGBA").save(dst, optimize=True)


# ---- attributes ---------------------------------------------------------------------------

_TRANSLIT = str.maketrans({"ß": "ss", "ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"})


def _meta_id(key: str, used: set) -> str:
    """Property id valid for EXT_structural_metadata (^[a-zA-Z_][a-zA-Z0-9_]*$)."""
    base = re.sub(r"[^A-Za-z0-9_]", "_", key.translate(_TRANSLIT)) or "p"
    if base[0].isdigit():
        base = "_" + base
    out, i = base, 1
    while out in used:
        i += 1
        out = f"{base}_{i}"
    used.add(out)
    return out


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _schema(trees: list[dict]) -> list[dict]:
    """The tree attributes as metadata properties: every scalar node property (generic
    attributes typed by _gen_attrs_map, which pykci keeps as strings), plus the derived
    display fields the City Explorer reads (name, address_*) and the geometric height."""
    types: dict[str, str] = {}
    for t in trees:
        gen = {}
        try:
            gen = json.loads(t["gen"] or "{}")
        except ValueError:
            pass
        for k, v in t["props"].items():
            if _EXCLUDE.match(k) or isinstance(v, (list, dict)) or v is None:
                continue
            numeric = (gen.get(k, {}).get("type") in ("int", "double", "measure")
                       or isinstance(v, (int, float)) and not isinstance(v, bool))
            if types.get(k) != "STRING":
                types[k] = "FLOAT64" if numeric and _num(v) is not None else "STRING"
    used: set = set()
    schema = [{"id": _meta_id("id", used), "name": "id", "type": "STRING", "provenance": "authoritative",
               "description": "gml:id of the tree"},
              {"id": _meta_id("name", used), "name": "name", "type": "STRING", "provenance": "authoritative",
               "description": "Species (or genus) of the tree, for display"},
              {"id": _meta_id("address_street", used), "name": "address_street", "type": "STRING",
               "provenance": "authoritative", "description": "Street of the tree (cadastre)"},
              {"id": _meta_id("address_house_number", used), "name": "address_house_number",
               "type": "STRING", "provenance": "authoritative",
               "description": "House number the tree is listed at (cadastre)"}]
    for k in sorted(types):
        if k in ("id",):
            continue
        schema.append({"id": _meta_id(k, used), "name": k, "type": types[k],
                       "provenance": "authoritative"})
    schema += [{"id": _meta_id("geometric_height", used), "name": "geometric_height", "type": "FLOAT64",
                "provenance": "derived", "description": "Height of the placed prototype (m)"},
               {"id": _meta_id("prototype", used), "name": "prototype", "type": "STRING",
                "provenance": "authoritative", "description": "Implicit-geometry prototype (gml:id)"},
               {"id": _meta_id("longitude", used), "name": "longitude", "type": "FLOAT64",
                "provenance": "derived", "description": "WGS84 longitude of the trunk"},
               {"id": _meta_id("latitude", used), "name": "latitude", "type": "FLOAT64",
                "provenance": "derived", "description": "WGS84 latitude of the trunk"}]
    return schema


def _first(props: dict, *keys):
    for k in keys:
        v = props.get(k)
        if v not in (None, "", "0"):
            return str(v)
    return None


def _row(t: dict) -> dict:
    p = t["props"]
    row = {k: v for k, v in p.items() if not _EXCLUDE.match(k) and not isinstance(v, (list, dict))}
    row.update({"id": t["id"],
                "name": _first(p, "baumart", "gattung", "species", "name"),
                "address_street": _first(p, "straße", "strasse", "street"),
                "address_house_number": _first(p, "hausnummer", "house_number"),
                "geometric_height": (t["z1"] - t["z0"]) if t["z0"] is not None and t["z1"] is not None else None,
                "prototype": t["proto"], "longitude": t["lon"], "latitude": t["lat"]})
    return row


# ---- GLB --------------------------------------------------------------------------------

class _Glb:
    """glTF JSON + the BIN chunk (buffer 0); external buffers are appended by the caller."""

    def __init__(self):
        self.bin = bytearray()
        self.views: list = []
        self.accessors: list = []

    def view(self, data: bytes, buffer: int = 0, offset: int | None = None, target=None,
             align: int = 4) -> int:
        if offset is None:
            while len(self.bin) % align:
                self.bin.append(0)
            offset = len(self.bin)
            self.bin.extend(data)
        v = {"buffer": buffer, "byteOffset": offset, "byteLength": len(data) if data else 0}
        if target is not None:
            v["target"] = target
        self.views.append(v)
        return len(self.views) - 1

    def accessor(self, view: int, count: int, type_: str, ctype: int = 5126, **extra) -> int:
        self.accessors.append({"bufferView": view, "byteOffset": 0, "componentType": ctype,
                               "count": int(count), "type": type_, **extra})
        return len(self.accessors) - 1

    def external(self, buffer: int, offset: int, length: int, target=34962) -> int:
        v = {"buffer": buffer, "byteOffset": offset, "byteLength": length, "target": target}
        self.views.append(v)
        return len(self.views) - 1

    def glb(self, gltf: dict) -> bytes:
        gltf["bufferViews"] = self.views
        gltf["accessors"] = self.accessors
        while len(self.bin) % 4:
            self.bin.append(0)
        gltf["buffers"][0] = {"byteLength": len(self.bin)}
        js = json.dumps(gltf, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        js += b" " * (-len(js) % 4)
        total = 12 + 8 + len(js) + 8 + len(self.bin)
        return (struct.pack("<III", 0x46546C67, 2, total)
                + struct.pack("<II", len(js), 0x4E4F534A) + js
                + struct.pack("<II", len(self.bin), 0x004E4942) + bytes(self.bin))


def _schema_json(props: list[dict]) -> dict:
    out = {}
    for p in props:
        d = {"name": p["name"], "extras": {"provenance": p["provenance"]}}
        if p.get("description"):
            d["description"] = p["description"]
        if p["type"] == "FLOAT64":
            d.update({"type": "SCALAR", "componentType": "FLOAT64", "noData": F64_NODATA})
        else:
            d.update({"type": "STRING", "noData": ""})
        if p["name"] == "id":
            d["semantic"] = "ID"
        elif p["name"] == "name":
            d["semantic"] = "NAME"
        out[p["id"]] = d
    return {"id": "pykci_features", "classes": {"feature": {
        "name": "City feature",
        "description": "Top-level CityGML feature (here: SolitaryVegetationObject, a street tree)",
        "properties": out}}}


def _property_table(w: _Glb, schema: list[dict], rows: list[dict]) -> tuple[dict, list[dict]]:
    props, present = {}, []
    for p in schema:
        vals = [r.get(p["name"]) for r in rows]
        if all(v is None or v == "" for v in vals):
            continue
        present.append(p)
        if p["type"] == "FLOAT64":
            arr = np.array([F64_NODATA if _num(v) is None else _num(v) for v in vals], dtype="<f8")
            props[p["id"]] = {"values": w.view(arr.tobytes(), align=8)}
        else:
            enc = [("" if v is None else str(v)).encode("utf-8") for v in vals]
            offsets = np.zeros(len(enc) + 1, dtype="<u4")
            offsets[1:] = np.cumsum([len(e) for e in enc], dtype=np.uint64)
            props[p["id"]] = {"values": w.view(b"".join(enc) or b" ", align=8),
                              "stringOffsets": w.view(offsets.tobytes(), align=8),
                              "stringOffsetType": "UINT32"}
    return {"name": "trees", "class": "feature", "count": len(rows), "properties": props}, present


def _pack_leaf(idx: np.ndarray, inst: dict, protos: dict, schema: list[dict], rows: list[dict],
               tex_names: dict, alpha_cutoff: float) -> bytes:
    """One leaf: a node per prototype, its trees as GPU instances around the tile centre."""
    w = _Glb()
    gltf = {"asset": {"version": "2.0", "generator": "pykci build_tree_tiles.py"},
            "scene": 0, "scenes": [{"nodes": []}], "nodes": [], "meshes": [], "materials": [],
            "buffers": [None], "extensionsUsed": ["EXT_mesh_gpu_instancing", "EXT_instance_features",
                                                   "EXT_structural_metadata"],
            "extensionsRequired": ["EXT_mesh_gpu_instancing"]}
    order = idx[np.argsort(inst["proto_idx"][idx], kind="stable")]
    center = inst["gl"][order].mean(axis=0)
    images, textures, mat_of = [], [], {}

    def material(kind: str, pk: int) -> int:
        key = (kind, tex_names.get(pk) if kind == "tex" else None)
        if key in mat_of:
            return mat_of[key]
        pbr = {"metallicFactor": 0.0, "roughnessFactor": 0.9}
        mat = {"pbrMetallicRoughness": pbr, "doubleSided": True}
        if kind == "tex" and key[1]:
            images.append({"uri": f"../textures/{key[1]}"})
            textures.append({"sampler": 0, "source": len(images) - 1})
            pbr["baseColorTexture"] = {"index": len(textures) - 1}
            mat.update({"alphaMode": "MASK", "alphaCutoff": alpha_cutoff, "name": "foliage"})
        else:
            pbr["baseColorFactor"] = _FALLBACK_FOLIAGE if kind == "tex" else _BARK
            mat["name"] = "foliage" if kind == "tex" else "bark"
        gltf["materials"].append(mat)
        mat_of[key] = len(gltf["materials"]) - 1
        return mat_of[key]

    tile_rows = [rows[i] for i in order]
    fids = np.arange(len(order), dtype="<f4")
    start = 0
    pk_sorted = inst["proto_idx"][order]
    for pk in np.unique(pk_sorted):
        sel = slice(start, start + int((pk_sorted == pk).sum()))
        start = sel.stop
        ids = order[sel]
        proto = protos[pk]
        gltf["buffers"].append({"uri": f"../prototypes/{pk}.bin", "byteLength": proto["byteLength"]})
        bi = len(gltf["buffers"]) - 1
        prims = []
        for kind, lay in proto["layout"].items():
            attrs = {"POSITION": w.accessor(w.external(bi, *lay["pos"]), lay["count"], "VEC3",
                                            min=lay["min"], max=lay["max"]),
                     "NORMAL": w.accessor(w.external(bi, *lay["nor"]), lay["count"], "VEC3")}
            if "uv" in lay:
                attrs["TEXCOORD_0"] = w.accessor(w.external(bi, *lay["uv"]), lay["count"], "VEC2")
            prims.append({"attributes": attrs, "material": material(kind, pk), "mode": 4})
        gltf["meshes"].append({"name": proto["id"], "primitives": prims})
        n = len(ids)
        t = (inst["gl"][ids] - center).astype("<f4")
        r = inst["rot"][ids].astype("<f4")
        s = inst["scale"][ids].astype("<f4")
        gltf["nodes"].append({
            "name": proto["id"], "mesh": len(gltf["meshes"]) - 1,
            "translation": center.tolist(),
            "extensions": {
                "EXT_mesh_gpu_instancing": {"attributes": {
                    "TRANSLATION": w.accessor(w.view(t.tobytes()), n, "VEC3"),
                    "ROTATION": w.accessor(w.view(r.tobytes()), n, "VEC4"),
                    "SCALE": w.accessor(w.view(s.tobytes()), n, "VEC3"),
                    "_FEATURE_ID_0": w.accessor(w.view(fids[sel].tobytes()), n, "SCALAR")}},
                "EXT_instance_features": {"featureIds": [
                    {"featureCount": len(order), "attribute": 0, "propertyTable": 0}]}}})
        gltf["scenes"][0]["nodes"].append(len(gltf["nodes"]) - 1)
    if images:
        gltf.update({"images": images, "textures": textures,
                     "samplers": [{"magFilter": 9729, "minFilter": 9987,
                                   "wrapS": 33071, "wrapT": 33071}]})
    table, present = _property_table(w, schema, tile_rows)
    gltf["extensions"] = {"EXT_structural_metadata": {"schema": _schema_json(present),
                                                       "propertyTables": [table]}}
    return w.glb(gltf)


# ---- tiling --------------------------------------------------------------------------------

def _quadtree(idx: np.ndarray, en: np.ndarray, max_features: int, path: str = "r", depth: int = 0):
    if len(idx) <= max_features or depth >= 16:
        return {"path": path, "idx": idx}
    e, n = en[idx, 0], en[idx, 1]
    me, mn = (e.min() + e.max()) / 2, (n.min() + n.max()) / 2
    kids = []
    for q, mask in enumerate(((e < me) & (n < mn), (e >= me) & (n < mn),
                              (e < me) & (n >= mn), (e >= me) & (n >= mn))):
        if mask.any():
            kids.append(_quadtree(idx[mask], en, max_features, f"{path}{q}", depth + 1))
    return {"path": path, "children": kids}


def _geometric_error(load_distance: float, max_sse: float = 16.0, screen_px: float = 1000.0) -> float:
    """Geometric error of the content-less internal tiles such that their children (the
    leaves) load when the camera is about `load_distance` metres away:
    SSE = error * screen_px / (distance * 2 tan(fov/2)), Cesium's default fov 60°."""
    return round(load_distance * max_sse * 2 * math.tan(math.radians(30)) / screen_px, 2)


def build(args) -> None:
    t_start = time.perf_counter()
    out = Path(args.out)
    driver = GraphDatabase.driver(NEO4J_URL, auth=(NEO4J_USER, NEO4J_PASSWORD),
                                  notifications_disabled_classifications={"UNRECOGNIZED", "DEPRECATION"})
    with driver.session() as s:
        crs = _dataset_crs(s, args.source)
        proto_rows = s.run(_Q_PROTOTYPES, source=args.source).data()
        trees = s.run(_Q_TREES, source=args.source).data()
    driver.close()
    if not trees:
        sys.exit("ERROR: no trees with prototypes in the graph (run enrich_street_trees.py first)")
    horiz = _horiz_epsg(crs)
    print(f"  {len(trees):,} trees, {len(proto_rows)} prototypes, CRS {crs} → {horiz}", flush=True)

    geoid_m = 0.0
    if args.align:
        extras = json.loads(Path(args.align).read_text()).get("extras", {}).get("pykci", {})
        geoid_m = float(extras.get("geoid_offset_m") or 0.0)
        print(f"  aligned with {args.align}: geoid offset {geoid_m:+.2f} m", flush=True)

    # fresh output (only what this script writes)
    for sub in ("tiles", "prototypes", "textures"):
        shutil.rmtree(out / sub, ignore_errors=True)
        (out / sub).mkdir(parents=True, exist_ok=True)

    # prototypes: geometry + textures
    protos: dict[int, dict] = {}
    pk_of: dict[str, int] = {}
    tex_names: dict[int, str] = {}
    tex_done: dict[str, str] = {}
    missing_tex = 0
    root = Path(args.texture_root) if args.texture_root else None
    for k, p in enumerate(sorted(proto_rows, key=lambda r: r["id"])):
        mesh = _prototype_mesh(p["rings"], p["dim"] or 3)
        if not mesh:
            continue
        info = _write_prototype_bin(mesh, out / "prototypes" / f"{k}.bin")
        protos[k] = {"id": p["id"], **info}
        pk_of[p["id"]] = k
        img = p["image"]
        src = (root / img) if (root and img) else None
        if src is not None and src.is_file():
            if img not in tex_done:
                name = f"{len(tex_done)}.png"
                _prepare_texture(src, out / "textures" / name, args.texture_size)
                tex_done[img] = name
            tex_names[k] = tex_done[img]
        elif img:
            missing_tex += 1
    print(f"  prototypes written: {len(protos)}, textures: {len(tex_done)}"
          + (f" ({missing_tex} not found under --texture-root: plain foliage colour)"
             if missing_tex else ""), flush=True)

    # instances: placement in the tileset frame
    trees = [t for t in trees if t["proto"] in pk_of and t["rp"]]
    ref = np.array([[float(v) for v in t["rp"].split()[:3]] for t in trees])
    mat = np.array([[float(v) for v in (t["tm"] or "").split()] or list(np.eye(4).ravel())
                    for t in trees]).reshape(-1, 4, 4)
    ref = ref + mat[:, :3, 3]
    to_wgs = Transformer.from_crs(horiz, "EPSG:4326", always_xy=True)
    lon, lat = (np.asarray(v) for v in to_wgs.transform(ref[:, 0], ref[:, 1]))
    c_lon, c_lat = float((lon.min() + lon.max()) / 2), float((lat.min() + lat.max()) / 2)
    basis = _enu_basis(c_lon, c_lat)
    center = _ecef(np.array([c_lon]), np.array([c_lat]), np.array([0.0]))[0]

    def ecef(x, y, z):
        lo, la = to_wgs.transform(x, y)
        return _ecef(np.asarray(lo), np.asarray(la), z + geoid_m)

    e0 = ecef(ref[:, 0], ref[:, 1], ref[:, 2])
    dx = ecef(ref[:, 0] + 1.0, ref[:, 1], ref[:, 2]) - e0       # one grid metre east
    dy = ecef(ref[:, 0], ref[:, 1] + 1.0, ref[:, 2]) - e0       # one grid metre north
    p0 = (e0 - center) @ basis.T                                 # trunk in the tileset ENU frame
    # each tree's own ENU frame (its vertical, not the tileset centre's: Hamburg spans
    # ~40 km, i.e. up to ~0.2° of tilt at the edges)
    lo_r, la_r = np.radians(lon), np.radians(lat)
    e_l = np.column_stack([-np.sin(lo_r), np.cos(lo_r), np.zeros_like(lo_r)])
    n_l = np.column_stack([-np.sin(la_r) * np.cos(lo_r), -np.sin(la_r) * np.sin(lo_r), np.cos(la_r)])
    u_l = np.column_stack([np.cos(la_r) * np.cos(lo_r), np.cos(la_r) * np.sin(lo_r), np.sin(la_r)])
    # grid → local ENU Jacobian (meridian convergence + scale factor), applied to the
    # matrix's horizontal part; that stays a rotation about the vertical × uniform scale
    jac = np.stack([np.stack([(e_l * dx).sum(1), (e_l * dy).sum(1)], 1),
                    np.stack([(n_l * dx).sum(1), (n_l * dy).sum(1)], 1)], 1)
    m2 = jac @ mat[:, :2, :2]
    det = m2[:, 0, 0] * m2[:, 1, 1] - m2[:, 0, 1] * m2[:, 1, 0]
    s_xy = np.sqrt(np.abs(det))
    theta = np.arctan2(m2[:, 1, 0], m2[:, 0, 0])
    s_z = mat[:, 2, 2]
    zero = s_xy < 1e-6
    s_xy[zero] = np.abs(s_z[zero])
    # rotation: local ENU → tileset ENU, after the tree's own turn about its vertical
    local = np.stack([e_l @ basis.T, n_l @ basis.T, u_l @ basis.T], axis=2)   # columns
    c, s = np.cos(theta), np.sin(theta)
    rz = np.zeros((len(trees), 3, 3))
    rz[:, 0, 0], rz[:, 0, 1], rz[:, 1, 0], rz[:, 1, 1], rz[:, 2, 2] = c, -s, s, c, 1.0
    # into glTF y-up axes (east, up, −north): R_gl = C · R · Cᵀ
    cm = np.array([[1.0, 0, 0], [0, 0, 1.0], [0, -1.0, 0]])
    r_gl = cm @ (local @ rz) @ cm.T
    inst = {"gl": np.column_stack([p0[:, 0], p0[:, 2], -p0[:, 1]]),
            "rot": _quaternions(r_gl),
            "scale": np.column_stack([s_xy, s_z, s_xy]),
            "proto_idx": np.array([pk_of[t["proto"]] for t in trees])}
    if zero.any():
        print(f"  {int(zero.sum()):,} trees with a zero horizontal scale: drawn with their "
              f"vertical scale", flush=True)
    for t, a, b in zip(trees, lon, lat):
        t["lon"], t["lat"] = round(float(a), 7), round(float(b), 7)
    schema = _schema(trees)
    rows = [_row(t) for t in trees]

    # bounding regions: tree height from the graph's bbox_*, crown reach from the prototype
    z0 = np.array([t["z0"] if t["z0"] is not None else r[2] for t, r in zip(trees, ref)])
    z1 = np.array([t["z1"] if t["z1"] is not None else r[2] + 10 for t, r in zip(trees, ref)])
    reach = {k: max(abs(v) for lay in p["layout"].values()
                    for v in (lay["min"][0], lay["max"][0], lay["min"][2], lay["max"][2]))
             for k, p in protos.items()}
    half = s_xy * np.array([reach[k] for k in inst["proto_idx"]])

    tree = _quadtree(np.arange(len(trees)), p0[:, :2], args.max_features)
    g_internal = _geometric_error(args.load_distance)
    (out / "tiles").mkdir(parents=True, exist_ok=True)
    n_leaves, n_bytes = 0, 0
    eps = 1e-7

    def region(idx):
        r = np.maximum(half[idx], 1.0)
        dlat = np.degrees(r / 6_371_000.0)
        dlon = dlat / np.cos(np.radians(lat[idx]))
        return [math.radians(float((lon[idx] - dlon).min())) - eps,
                math.radians(float((lat[idx] - dlat).min())) - eps,
                math.radians(float((lon[idx] + dlon).max())) + eps,
                math.radians(float((lat[idx] + dlat).max())) + eps,
                float(z0[idx].min() + geoid_m) - 1.0, float(z1[idx].max() + geoid_m) + 1.0]

    def assemble(nd):
        nonlocal n_leaves, n_bytes
        if "idx" in nd:
            data = _pack_leaf(nd["idx"], inst, protos, schema, rows, tex_names, args.alpha_cutoff)
            (out / "tiles" / f"{nd['path']}.glb").write_bytes(data)
            n_leaves += 1
            n_bytes += len(data)
            if n_leaves % 25 == 0:
                print(f"  {n_leaves} leaf tiles …", flush=True)
            return {"boundingVolume": {"region": region(nd["idx"])}, "geometricError": 0.0,
                    "content": {"uri": f"tiles/{nd['path']}.glb"}}, nd["idx"]
        kids = [assemble(c) for c in nd["children"]]
        idx = np.concatenate([k[1] for k in kids])
        return {"boundingVolume": {"region": region(idx)}, "geometricError": g_internal,
                "children": [k[0] for k in kids]}, idx

    root_tile, _ = assemble(tree)
    e_v, n_v, u_v = basis
    root_tile["transform"] = [*e_v, 0.0, *n_v, 0.0, *u_v, 0.0, *center, 1.0]
    root_tile["transform"] = [float(v) for v in root_tile["transform"]]
    root_tile["refine"] = "ADD"
    tileset = {
        "asset": {"version": "1.1", "generator": "pykci build_tree_tiles.py"},
        "geometricError": g_internal,
        "root": root_tile,
        "extras": {"pykci": {
            "layer": "trees", "crs": crs, "horizontal_crs": horiz,
            "geoid_offset_m": round(geoid_m, 3),
            "center": {"longitude": c_lon, "latitude": c_lat},
            "features": len(trees), "prototypes": len(protos), "textures": len(tex_done),
            "texture_size": args.texture_size, "leaf_tiles": n_leaves,
            "load_distance_m": args.load_distance,
            "zero_horizontal_scale": int(zero.sum()),
            "feature_schema": "feature_schema.json",
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }},
    }
    (out / "tileset.json").write_text(json.dumps(tileset, indent=1), encoding="utf-8")
    (out / "feature_schema.json").write_text(json.dumps(
        {"class": "feature", "properties": schema}, indent=1, ensure_ascii=False), encoding="utf-8")
    proto_bytes = sum(p["byteLength"] for p in protos.values())
    tex_bytes = sum(f.stat().st_size for f in (out / "textures").iterdir())
    print(f"  {len(trees):,} trees in {n_leaves} leaf tiles ({n_bytes / 1e6:,.1f} MB) + "
          f"{len(protos)} prototypes ({proto_bytes / 1e6:.1f} MB) + {len(tex_done)} textures "
          f"({tex_bytes / 1e6:.1f} MB)  [{time.perf_counter() - t_start:.0f} s]", flush=True)
    print(f"  tileset.json : {out / 'tileset.json'}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="Output directory (tileset.json + tiles/ …)")
    ap.add_argument("--texture-root", default=None,
                    help="Directory the prototypes' texture_image URIs are relative to "
                         "(the directory of the source GML)")
    ap.add_argument("--align", default=None,
                    help="City tileset.json (build_3dtiles.py) to take the geoid offset from")
    ap.add_argument("--source", default=None,
                    help="Only trees/prototypes of this source file (default: all in the graph)")
    ap.add_argument("--max-features", type=int, default=2500, help="Trees per leaf tile (default 2500)")
    ap.add_argument("--texture-size", type=int, default=512,
                    help="Maximum texture edge in pixels (default 512)")
    ap.add_argument("--load-distance", type=float, default=1200.0,
                    help="Camera distance (m) from which trees stream in (default 1200)")
    ap.add_argument("--alpha-cutoff", type=float, default=0.4,
                    help="Alpha mask cut-off of the foliage textures (default 0.4)")
    build(ap.parse_args())


if __name__ == "__main__":
    main()
