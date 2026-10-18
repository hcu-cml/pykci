"""
extract_citygml_building_footprints.py — CityGML building footprints → GeoJSON.

Extracts one ground-level footprint polygon per building from a CityGML file
(or a folder of them, recursively) and writes a single GeoJSON FeatureCollection
whose features carry the building's `gml:id` in `properties.gml_id`.

Design goals (see CITYGML_BUILDING_FOOTPRINT_EXTRACTION.md for the full rationale):

  * Version-agnostic — matches elements by *local name* ({*}Building, {*}posList,
    …) so CityGML 1.0 / 2.0 / 3.0 all work with one code path.
  * CRS-correct worldwide — resolves each file's srsName (Hamburg's compound
    `urn:adv:crs:ETRS89_UTM32*…`, Helsinki `EPSG::3879`, NYC EPSG:2263 in feet,
    …) and reprojects every footprint to a common target CRS (WGS84 lon/lat by
    default) so datasets from different cities line up in one file. Source and
    target CRS are recorded as top-level members of the FeatureCollection.
  * Streaming — uses lxml.iterparse and writes features incrementally, so multi-
    GB city files (e.g. Hamburg LoD2) parse in bounded memory.

Footprint selection, in priority order, per building (whole subtree, incl. parts):
  1. bldg:lod0FootPrint polygons  — the explicit 2D footprint, if authored.
  2. bldg:GroundSurface polygons  — the LoD2 ground boundary surface(s).
  3. Lowest (near-)horizontal polygon(s) — fallback for LoD1 extruded solids
     with no tagged ground surface (the bottom face of the block).

Usage:
    py tools/extract_citygml_building_footprints.py <file.gml | dir> [more …] [options]

Options:
    --output PATH          Output .geojson path (default output/geojson/<name>.geojson)
    --target-crs EPSG:N    Reproject footprints to this CRS (default EPSG:4326).
    --keep-source-crs      Do not reproject; emit coordinates in the source CRS
                           (axis-normalised to X=easting/lon, Y=northing/lat).
    --source-crs EPSG:N    Override/supply the source CRS (required for any file
                           that declares no srsName).
    --per-file             Write one GeoJSON per input file instead of combining.
    --precision N          Decimal places for X/Y (default: 9 for degrees, 3 for m).
    --horizontal-tol M     Flatness tolerance for the fallback (default 0.6, in
                           source vertical units).

Requires: lxml, pyproj (both already project dependencies).
"""

import argparse
import json
import re
import sys
import uuid
from pathlib import Path

from lxml import etree

GML_ID = "{http://www.opengis.net/gml}id"


# ---------------------------------------------------------------------------
# CRS resolution  (kept in sync with ingest_citygml._epsg_from_srs)
# ---------------------------------------------------------------------------

def resolve_epsg(srs):
    """Resolve a CityGML srsName to an 'EPSG:NNNN' string.

    Handles the plain/urn EPSG forms plus the two non-EPSG conventions seen in
    real data: adv/ETRS89-UTM compound urns (Hamburg → EPSG:258xx) and Swiss
    CH1903/LV95 names. Returns the original string if nothing matches (pyproj
    may still understand it), or None for empty input."""
    if not srs:
        return None
    m = re.search(r"EPSG[:/]+(?:\d+/)?(\d{4,6})", srs)
    if m:
        return f"EPSG:{m.group(1)}"
    u = srs.upper().replace(" ", "")
    m = re.search(r"UTM(\d{2})", u)          # ETRS89_UTM32 → EPSG:25832
    if m:
        return f"EPSG:258{m.group(1)}"
    if "CH1903+" in u or "LV95" in u:
        return "EPSG:2056"
    if "CH1903" in u or "LV03" in u:
        return "EPSG:21781"
    return srs


def peek_srsname(path, chunk_size=1_048_576):
    """Return the first srsName in a CityGML file, or None if it has none.
    Scans in chunks and stops at the first match, so it is cheap for populated
    files (srsName sits near the top) yet still finds a CRS declared deeper in
    the file; only a genuinely CRS-less file (e.g. an empty placeholder tile) is
    read to EOF — those are tiny. A small tail is carried between chunks so a
    match split across a boundary is not missed."""
    pat = re.compile(rb'srsName="([^"]+)"')
    tail = b""
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                return None
            m = pat.search(tail + chunk)
            if m:
                return m.group(1).decode("utf-8", "ignore")
            tail = chunk[-200:]


class Reprojector:
    """Transform CityGML coordinates from a source CRS to a target CRS.

    Reading normalises GML axis order: a *geographic* source stores (lat, lon),
    so we feed the transformer (lon, lat); a *projected* source stores
    (easting, northing) and passes straight through. With always_xy the output
    is (X, Y) = (lon, lat) for a geographic target or (easting, northing) for a
    projected one — exactly GeoJSON's expected coordinate order.

    Z is scaled by the source's horizontal linear unit so heights come out in
    metres for projected sources measured in feet (e.g. NYC EPSG:2263); a
    geographic source's Z is already a metric height and passes through."""

    def __init__(self, src_crs, dst_crs):
        from pyproj import CRS, Transformer
        src_obj = CRS.from_user_input(src_crs)
        dst_obj = CRS.from_user_input(dst_crs)
        self.src_epsg = _crs_label(src_obj, src_crs)
        self.dst_epsg = _crs_label(dst_obj, dst_crs)
        self.src_geographic = src_obj.is_geographic
        self.dst_geographic = dst_obj.is_geographic
        self.z_factor = 1.0
        if not self.src_geographic:
            try:
                f = src_obj.axis_info[0].unit_conversion_factor
                if f:
                    self.z_factor = f
            except Exception:
                pass
        self.tf = Transformer.from_crs(src_obj, dst_obj, always_xy=True)

    def point(self, a, b, z=None):
        x, y = self.tf.transform(b, a) if self.src_geographic else self.tf.transform(a, b)
        if z is None:
            return x, y, None
        return x, y, z * self.z_factor


def _crs_label(crs_obj, fallback):
    """Best-effort 'EPSG:NNNN' (or authority) label for a pyproj CRS."""
    try:
        code = crs_obj.to_epsg()
        if code:
            return f"EPSG:{code}"
    except Exception:
        pass
    return str(fallback)


# ---------------------------------------------------------------------------
# Coordinate parsing  (version-agnostic, all GML encodings)
# ---------------------------------------------------------------------------

def _ring_from_el(ring_el):
    """Return a list of (a, b, z) tuples for one gml:LinearRing / gml:Ring,
    supporting the three encodings found in the wild:
      * a single gml:posList (with optional srsDimension),
      * a sequence of gml:pos elements (IFC→CityGML converters, FZK-Haus),
      * legacy gml:coordinates (CityGML 1.0, comma-separated tuples).
    Values are parsed verbatim — never rounded — in source axis order. z is
    None when the data is 2D."""
    pl = ring_el.find("{*}posList")
    if pl is not None and pl.text and pl.text.split():
        dim = _srs_dim(pl)
        return _chunk(pl.text.split(), dim)

    pos_els = ring_el.findall("{*}pos")
    if pos_els:
        dim = _srs_dim(pos_els[0])
        toks = []
        for p in pos_els:
            if p.text:
                toks.extend(p.text.split())
        return _chunk(toks, dim)

    coords = ring_el.find("{*}coordinates")
    if coords is not None and coords.text:
        ts = coords.get("ts") or " "        # tuple separator  (default space)
        cs = coords.get("cs") or ","        # coordinate separator within a tuple
        out = []
        for tup in re.split(r"[%s\s]+" % re.escape(ts), coords.text.strip()):
            if not tup:
                continue
            parts = tup.split(cs)
            try:
                a = float(parts[0]); b = float(parts[1])
                z = float(parts[2]) if len(parts) > 2 else None
            except (ValueError, IndexError):
                continue
            out.append((a, b, z))
        return out
    return []


def _srs_dim(el):
    try:
        d = int(el.get("srsDimension", "3"))
        return d if d in (2, 3) else 3
    except (TypeError, ValueError):
        return 3


def _chunk(tokens, dim):
    """Split a flat token list into (a, b, z) tuples of stride `dim`. Falls back
    to inferring the stride from the token count if `dim` looks wrong."""
    n = len(tokens)
    if dim == 3 and n % 3 != 0 and n % 2 == 0:
        dim = 2
    elif dim == 2 and n % 2 != 0 and n % 3 == 0:
        dim = 3
    out = []
    for i in range(0, n - dim + 1, dim):
        try:
            a = float(tokens[i]); b = float(tokens[i + 1])
            z = float(tokens[i + 2]) if dim == 3 else None
        except ValueError:
            continue
        out.append((a, b, z))
    return out


def _polygon_rings(poly_el):
    """Return (exterior, [interiors]) for a gml:Polygon; each a list of (a,b,z).
    exterior is [] if the polygon has no readable ring."""
    ext_container = poly_el.find("{*}exterior")
    ext_ring = ext_container.find(".//{*}LinearRing") if ext_container is not None else None
    if ext_ring is None:
        ext_ring = poly_el.find(".//{*}LinearRing")
    exterior = _ring_from_el(ext_ring) if ext_ring is not None else []

    interiors = []
    for int_container in poly_el.findall("{*}interior"):
        ir = int_container.find(".//{*}LinearRing")
        if ir is not None:
            ring = _ring_from_el(ir)
            if ring:
                interiors.append(ring)
    return exterior, interiors


# ---------------------------------------------------------------------------
# Footprint selection
# ---------------------------------------------------------------------------

def _polys_under(bldg_el, xpath):
    """All gml:Polygon descendants beneath elements matched by `xpath`."""
    polys = []
    for host in bldg_el.iterfind(xpath):
        polys.extend(host.iterfind(".//{*}Polygon"))
    return polys


def select_footprint(bldg_el, horizontal_tol):
    """Return a list of polygons (each: (exterior, interiors)) representing the
    building's footprint, using the lod0FootPrint → GroundSurface → lowest-flat
    priority. Empty list if nothing usable is found."""
    # 1. explicit lod0FootPrint
    candidates = _polys_under(bldg_el, ".//{*}lod0FootPrint")
    # 2. GroundSurface boundary surfaces
    if not candidates:
        candidates = _polys_under(bldg_el, ".//{*}GroundSurface")
    if candidates:
        parsed = [_polygon_rings(p) for p in candidates]
        return [pr for pr in parsed if pr[0]]

    # 3. fallback: lowest near-horizontal polygon(s) of the solid/multisurface
    all_polys = [_polygon_rings(p) for p in bldg_el.iterfind(".//{*}Polygon")]
    all_polys = [pr for pr in all_polys if pr[0]]
    if not all_polys:
        return []

    has_z = any(any(pt[2] is not None for pt in ext) for ext, _ in all_polys)
    if not has_z:
        # 2D data: the polygons already are footprints.
        return all_polys

    flats = []
    for ext, interiors in all_polys:
        zs = [pt[2] for pt in ext if pt[2] is not None]
        if not zs:
            continue
        if max(zs) - min(zs) <= horizontal_tol:
            flats.append((sum(zs) / len(zs), ext, interiors))
    if not flats:
        return []
    z_min = min(f[0] for f in flats)
    return [(ext, interiors) for zmean, ext, interiors in flats
            if zmean <= z_min + horizontal_tol]


# ---------------------------------------------------------------------------
# GeoJSON assembly
# ---------------------------------------------------------------------------

def _emit_ring(ring, reproj, prec):
    """Reproject a ring to the target CRS and ensure it is closed.
    Returns a GeoJSON coordinate array."""
    out = []
    for a, b, z in ring:
        x, y, zz = reproj.point(a, b, z)
        x = round(x, prec); y = round(y, prec)
        if zz is None:
            out.append([x, y])
        else:
            out.append([x, y, round(zz, 3)])
    if len(out) >= 1 and out[0] != out[-1]:
        out.append(out[0])
    return out


def build_geometry(footprint, reproj, prec):
    """Turn the selected polygons into a GeoJSON Polygon (single) or
    MultiPolygon (several). Returns None if nothing valid remains."""
    polys = []
    for ext, interiors in footprint:
        ext_ring = _emit_ring(ext, reproj, prec)
        if len(ext_ring) < 4:            # need ≥3 distinct points + closure
            continue
        rings = [ext_ring] + [_emit_ring(ir, reproj, prec) for ir in interiors
                              if len(ir) >= 3]
        polys.append(rings)
    if not polys:
        return None
    if len(polys) == 1:
        return {"type": "Polygon", "coordinates": polys[0]}
    return {"type": "MultiPolygon", "coordinates": polys}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def gml_files(inputs, recursive=True):
    files = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            pattern = "**/*" if recursive else "*"
            for f in sorted(p.glob(pattern)):
                if f.suffix.lower() in (".gml", ".xml"):
                    files.append(f)
        elif p.is_file():
            files.append(p)
        else:
            print(f"  WARNING: input not found, skipped: {item}", file=sys.stderr)
    return files


def make_reprojector(src_srs, args):
    """Build a Reprojector for one file, honouring --source-crs / --target-crs /
    --keep-source-crs. Returns (reprojector, note) or (None, reason) if the
    source CRS is unknown and reprojection was requested."""
    src = args.source_crs or (resolve_epsg(src_srs) if src_srs else None)
    if src is None:
        if args.keep_source_crs:
            return _IdentityReprojector(), "unknown"
        return None, ("no srsName found and --source-crs not given "
                      "(cannot reproject; pass --source-crs EPSG:N or --keep-source-crs)")
    dst = src if args.keep_source_crs else (args.target_crs or "EPSG:4326")
    try:
        return Reprojector(src, dst), None
    except Exception as exc:
        return None, f"CRS setup failed ({exc})"


class _IdentityReprojector:
    """Used only for --keep-source-crs on a file with no declared CRS: emits
    coordinates verbatim (no datum/axis change possible without a known CRS)."""
    src_epsg = "unknown"
    dst_epsg = "unknown"
    dst_geographic = False

    def point(self, a, b, z=None):
        return a, b, z


def default_precision(reproj, override):
    if override is not None:
        return override
    return 9 if getattr(reproj, "dst_geographic", False) else 3


def process(args):
    files = gml_files(args.inputs, recursive=not args.no_recursive)
    if not files:
        print("No CityGML files found.", file=sys.stderr)
        return 1

    out_dir = Path("output/geojson")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Decide output grouping.
    if args.per_file:
        groups = [(f, [f]) for f in files]
    else:
        if args.output:
            out_path = Path(args.output)
        else:
            base = Path(args.inputs[0])
            stem = base.stem if base.is_file() else base.name
            out_path = out_dir / f"{stem or 'footprints'}.geojson"
        groups = [(out_path, files)]

    rc = 0
    for out_target, group_files in groups:
        if args.per_file:
            out_path = out_dir / f"{out_target.stem}.geojson"
        else:
            out_path = Path(out_target)
            out_path.parent.mkdir(parents=True, exist_ok=True)
        rc |= _write_group(out_path, group_files, args)
    return rc


def _write_group(out_path, files, args):
    src_labels, dst_labels = set(), set()
    total_feats = total_skipped = 0

    # Pre-resolve reprojectors so we can write correct CRS members in the header.
    # A file whose CRS can't be resolved (e.g. an empty placeholder tile with no
    # srsName) is skipped with a warning rather than aborting the whole batch;
    # we only error out if *no* input file is usable.
    resolved = []
    unresolved = 0
    for f in files:
        src_srs = peek_srsname(f)
        reproj, note = make_reprojector(src_srs, args)
        if reproj is None:
            print(f"  WARNING {f.name}: {note} -- file skipped", file=sys.stderr)
            unresolved += 1
            continue
        resolved.append((f, reproj))
        src_labels.add(reproj.src_epsg)
        dst_labels.add(reproj.dst_epsg)

    if not resolved:
        print("  ERROR: no input file had a resolvable CRS "
              "(pass --source-crs EPSG:N or --keep-source-crs)", file=sys.stderr)
        return 1

    source_crs = next(iter(src_labels)) if len(src_labels) == 1 else "mixed"
    target_crs = next(iter(dst_labels)) if len(dst_labels) == 1 else "mixed"

    with open(out_path, "w", encoding="utf-8") as out:
        out.write('{"type": "FeatureCollection", ')
        out.write(f'"source_crs": {json.dumps(source_crs)}, ')
        out.write(f'"target_crs": {json.dumps(target_crs)}, ')
        out.write('"features": [\n')
        first = True
        for f, reproj in resolved:
            prec = default_precision(reproj, args.precision)
            feats, skipped = _stream_file(f, reproj, prec, args.horizontal_tol,
                                          out, write_first=first)
            if feats:
                first = False
            total_feats += feats
            total_skipped += skipped
            print(f"  {f.name}: {feats} footprints"
                  + (f" ({skipped} skipped)" if skipped else "")
                  + f"  [{reproj.src_epsg} -> {reproj.dst_epsg}]")
        out.write("\n]}\n")

    print(f"-> {out_path}  ({total_feats} features"
          + (f", {total_skipped} buildings skipped" if total_skipped else "")
          + (f", {unresolved} files skipped (no CRS)" if unresolved else "")
          + f", source_crs={source_crs}, target_crs={target_crs})")
    return 0


def _stream_file(path, reproj, prec, horiz_tol, out, write_first):
    """Iterparse one file, emitting a GeoJSON Feature per building. Returns
    (n_written, n_skipped)."""
    written = skipped = 0
    context = etree.iterparse(str(path), events=("end",), tag="{*}Building",
                              huge_tree=True, recover=True)
    for _event, bldg in context:
        gml_id = bldg.get(GML_ID) or f"building_{uuid.uuid4().hex[:16]}"
        try:
            footprint = select_footprint(bldg, horiz_tol)
            geom = build_geometry(footprint, reproj, prec) if footprint else None
        except Exception as exc:
            geom = None
            print(f"    WARNING {path.name}/{gml_id}: {exc}", file=sys.stderr)
        if geom is None:
            skipped += 1
        else:
            feature = {"type": "Feature",
                       "properties": {"gml_id": gml_id},
                       "geometry": geom}
            if not (write_first and written == 0):
                out.write(",\n")
            out.write(json.dumps(feature, indent=4))
            written += 1
        # free memory: clear this building and its already-parsed siblings
        bldg.clear()
        while bldg.getprevious() is not None:
            parent = bldg.getparent()
            if parent is None:
                break
            del parent[0]
    del context
    return written, skipped


def main():
    ap = argparse.ArgumentParser(
        description="Extract CityGML building footprints to GeoJSON.")
    ap.add_argument("inputs", nargs="+",
                    help="CityGML file(s) and/or folder(s) (searched recursively)")
    ap.add_argument("--output", help="output .geojson path (combined mode)")
    ap.add_argument("--target-crs", default="EPSG:4326",
                    help="reproject footprints to this CRS (default EPSG:4326 WGS84)")
    ap.add_argument("--keep-source-crs", action="store_true",
                    help="emit coordinates in the source CRS (no reprojection)")
    ap.add_argument("--source-crs",
                    help="override/supply source CRS (needed if no srsName in file)")
    ap.add_argument("--per-file", action="store_true",
                    help="write one GeoJSON per input file instead of combining")
    ap.add_argument("--no-recursive", action="store_true",
                    help="do not descend into sub-folders")
    ap.add_argument("--precision", type=int, default=None,
                    help="decimal places for X/Y (default 9 for degrees, 3 for metres)")
    ap.add_argument("--horizontal-tol", type=float, default=0.6,
                    help="flatness tolerance for the lowest-polygon fallback")
    args = ap.parse_args()
    return process(args)


if __name__ == "__main__":
    sys.exit(main())
