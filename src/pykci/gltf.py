"""
glTF for the 3D Tiles of build_3dtiles: the parts that do not depend on what a tile shows.

  the frame      WGS84 → ECEF and the east/north/up basis a tileset is anchored in
  GlbWriter      a binary glTF (GLB): the JSON chunk and the BIN chunk with its buffer views
                 and accessors (pack() writes the container)
  metadata       EXT_structural_metadata: a property of the feature class, the schema, the
                 columns of a property table (FLOAT64, STRING), property ids

build_3dtiles keeps what is its own: which properties a tile has, how raw values become
numbers or text, its materials and meshes.
"""

import json
import math
import struct

import numpy as np

WGS84_A  = 6378137.0
WGS84_F  = 1.0 / 298.257223563
WGS84_E2 = 2 * WGS84_F - WGS84_F ** 2


# ---- the frame of a tileset ------------------------------------------------------------------

def ecef(lon_deg, lat_deg, h):
    """WGS84 geographic (degrees) + ellipsoidal height (m) → ECEF (m), for (N,) arrays or
    scalars → (N, 3)."""
    lon = np.radians(lon_deg)
    lat = np.radians(lat_deg)
    sin_lat = np.sin(lat)
    n = WGS84_A / np.sqrt(1.0 - WGS84_E2 * sin_lat ** 2)
    return np.column_stack([
        (n + h) * np.cos(lat) * np.cos(lon),
        (n + h) * np.cos(lat) * np.sin(lon),
        (n * (1.0 - WGS84_E2) + h) * sin_lat,
    ])


def enu_basis(lon_deg: float, lat_deg: float) -> np.ndarray:
    """East / North / Up unit vectors (ECEF) at a WGS84 geographic point, as the rows of a
    (3, 3) array: `east, north, up = enu_basis(lon, lat)`."""
    lon = math.radians(lon_deg)
    lat = math.radians(lat_deg)
    return np.array([[-math.sin(lon), math.cos(lon), 0.0],
                     [-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat)],
                     [math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)]])


# ---- GLB -----------------------------------------------------------------------------------

class GlbWriter:
    """A binary glTF being written. Data goes into the BIN chunk (buffer 0) through view()
    or array(); glb() completes the glTF (finish()) and returns the file (pack())."""

    def __init__(self):
        self.bin = bytearray()
        self.views: list = []
        self.accessors: list = []

    def view(self, data: bytes, target=None, align: int = 4) -> int:
        """A buffer view of `data`, appended to the BIN chunk at a multiple of `align`."""
        while len(self.bin) % align:
            self.bin.append(0)
        v = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data)}
        if target is not None:
            v["target"] = target
        self.bin.extend(data)
        self.views.append(v)
        return len(self.views) - 1

    def accessor(self, view: int, count: int, type_: str, ctype: int = 5126, **extra) -> int:
        """An accessor on a buffer view; `extra` adds properties such as min and max."""
        self.accessors.append({"bufferView": view, "byteOffset": 0, "componentType": ctype,
                               "count": int(count), "type": type_, **extra})
        return len(self.accessors) - 1

    def array(self, arr: np.ndarray, ctype: int, type_: str, target, minmax: bool = False) -> int:
        """An accessor on a new buffer view of the array (one element per row)."""
        extra = {"min": arr.min(axis=0).tolist(), "max": arr.max(axis=0).tolist()} if minmax else {}
        return self.accessor(self.view(arr.tobytes(), target), len(arr), type_, ctype, **extra)

    def finish(self, gltf: dict) -> None:
        """Put the buffer views, the accessors and the BIN buffer (padded to 4 bytes) into
        the glTF."""
        gltf["bufferViews"] = self.views
        gltf["accessors"] = self.accessors
        while len(self.bin) % 4:
            self.bin.append(0)
        gltf["buffers"] = [{"byteLength": len(self.bin)}]

    def glb(self, gltf: dict) -> bytes:
        self.finish(gltf)
        return pack(gltf, bytes(self.bin))


def pack(gltf: dict, binary: bytes) -> bytes:
    """The GLB container: the glTF as its JSON chunk (UTF-8, padded with spaces) and `binary`
    (a multiple of 4 bytes) as its BIN chunk."""
    js = json.dumps(gltf, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    js += b" " * (-len(js) % 4)
    total = 12 + 8 + len(js) + 8 + len(binary)
    return (struct.pack("<III", 0x46546C67, 2, total)
            + struct.pack("<II", len(js), 0x4E4F534A) + js
            + struct.pack("<II", len(binary), 0x004E4942) + binary)


# ---- EXT_structural_metadata -------------------------------------------------------------------

F64_NODATA = -1.7976931348623157e308   # "absent" marker for FLOAT64 properties


def finite_float(value) -> float | None:
    """The value as a float; None for what is no number, or not a finite one."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def unique_id(pid: str, used: set) -> str:
    """`pid`, or `pid_2`, `pid_3`, … when it is taken; the result is added to `used`."""
    base, i = pid, 2
    while pid in used:
        pid, i = f"{base}_{i}", i + 1
    used.add(pid)
    return pid


def class_property(name: str, type_: str, provenance: str, description: str | None = None) -> dict:
    """One property of the metadata class: `type_` FLOAT64 (absent values hold F64_NODATA) or
    STRING (absent values are empty). The properties named id and name carry the semantics
    viewers look for; the provenance layer goes into extras."""
    d = {"name": name, "extras": {"provenance": provenance}}
    if description:
        d["description"] = description
    if type_ == "FLOAT64":
        d.update({"type": "SCALAR", "componentType": "FLOAT64", "noData": F64_NODATA})
    else:
        d.update({"type": "STRING", "noData": ""})
    if name == "id":
        d["semantic"] = "ID"
    elif name == "name":
        d["semantic"] = "NAME"
    return d


def schema_json(props: list[dict]) -> dict:
    """EXT_structural_metadata schema (embedded in every tile) of the properties
    [{id, name, type, provenance[, description]}]."""
    out = {p["id"]: class_property(p["name"], p["type"], p["provenance"], p.get("description")) for p in props}
    return {"id": "pykci_features", "classes": {"feature": {
        "name": "City feature",
        "description": "Top-level CityGML feature (Building incl. BuildingParts, or CityObject)",
        "properties": out}}}


def float_column(w: GlbWriter, values: list) -> dict:
    """A FLOAT64 property of a property table: one float per feature, None → F64_NODATA."""
    arr = np.array([F64_NODATA if v is None else v for v in values], dtype="<f8")
    return {"values": w.view(arr.tobytes(), align=8)}


def string_column(w: GlbWriter, values: list) -> dict:
    """A STRING property of a property table: UTF-8 values and their offsets, None → ''."""
    enc = [("" if v is None else v).encode("utf-8") for v in values]
    offsets = np.zeros(len(enc) + 1, dtype="<u4")
    offsets[1:] = np.cumsum([len(e) for e in enc], dtype=np.uint64)
    data = b"".join(enc) or b" "      # a bufferView must not be empty
    return {"values": w.view(data, align=8),
            "stringOffsets": w.view(offsets.tobytes(), align=8),
            "stringOffsetType": "UINT32"}
