# Footprint extraction — `extract_citygml_building_footprints.py`

Extracts one **ground-level footprint polygon per building** from CityGML and
writes a single GeoJSON `FeatureCollection`, carrying each building's `gml:id`
in `properties.gml_id`. This document explains *why* the code is shaped the way
it is — the interesting decisions are all in CityGML's messiness and in getting
the coordinate systems right for datasets from cities around the world.

```bash
# single file  → output/geojson/<stem>.geojson  (default target CRS = WGS84)
py tools/extract_citygml_building_footprints.py input/citygml/FZK-Haus/FZK-Haus-LoD2-KIT-IAI-KHH-B36-V1.gml

# whole folder, recursively  → one combined output/geojson/<folder>.geojson
py tools/extract_citygml_building_footprints.py input/citygml/nyc

# keep the source CRS instead of reprojecting (raw easting/northing)
py tools/extract_citygml_building_footprints.py input/citygml/hamburg --keep-source-crs

# force a specific target CRS, or supply a source CRS for files without srsName
py tools/extract_citygml_building_footprints.py somewhere/ --target-crs EPSG:3857
py tools/extract_citygml_building_footprints.py nocrs.gml --source-crs EPSG:2950
```

---

## Output shape

```json
{"type": "FeatureCollection", "source_crs": "EPSG:2263", "target_crs": "EPSG:4326", "features": [
  { "type": "Feature",
    "properties": { "gml_id": "DEHHALKAP0000o0J" },
    "geometry": { "type": "Polygon", "coordinates": [ [ [lon, lat, h], … ] ] } },
  …
]}
```

`source_crs` and `target_crs` are **top-level members of the FeatureCollection**
so the CRS of the coordinates is unambiguous at a glance (see *Why CRS members…*
below). Every feature's `properties.gml_id` is the CityGML `gml:id` of the
building — the whole point of the export, so downstream results can be joined
back to the graph / source data.

---

## The decisions, and why

### 1. What *is* a "footprint"? — a three-level priority

CityGML has no single canonical "footprint". Different producers and LoDs encode
the ground outline differently, so the extractor tries three sources in order and
takes the first that yields polygons (`select_footprint`):

1. **`bldg:lod0FootPrint`** — the explicit 2-D footprint, when the author
   provided one. This is exactly a footprint, so it wins.
2. **`bldg:GroundSurface`** — the LoD2 ground boundary surface(s) reached via
   `boundedBy`. This is the common case for LoD2 city models (Hamburg, Helsinki,
   FZK-Haus LoD2–4). A building may have several ground surfaces.
3. **Lowest near-horizontal polygon(s)** — a geometric fallback for LoD1
   *extruded blocks* that carry only a `lod1Solid` with no tagged ground surface.
   Every polygon's exterior ring is checked for flatness (`max(z) − min(z) ≤
   --horizontal-tol`, default 0.6 in source vertical units); among the flat ones,
   those at the lowest mean elevation are the base face = the footprint.

The whole building **subtree** is searched, so `bldg:BuildingPart` geometry is
included under the parent building's id — a building split into parts still
produces one footprint feature.

If a building yields multiple ground polygons the geometry is emitted as a
**`MultiPolygon`**; a single polygon becomes a **`Polygon`** (matching the example
format). Polygon holes (`gml:interior` rings, e.g. courtyards) are preserved as
GeoJSON interior rings.

### 2. Coordinate systems — the part that has to be right for "cities worldwide"

The datasets ship in wildly different CRSs, and several don't use plain EPSG
codes:

| Dataset  | `srsName` in the file                       | Resolved   | Units |
| -------- | ------------------------------------------- | ---------- | ----- |
| Hamburg  | `urn:adv:crs:ETRS89_UTM32*DE_DHHN2016_NH`   | EPSG:25832 | m     |
| Helsinki | `urn:ogc:def:crs:EPSG::3879`                | EPSG:3879  | m     |
| NYC      | `EPSG:2263`                                 | EPSG:2263  | **US survey feet** |

`resolve_epsg()` normalises all of these (it is deliberately kept in sync with
`ingest_citygml._epsg_from_srs`, so the two tools agree on what a `srsName`
means). It understands the plain/urn EPSG forms, the *adv* ETRS89-UTM compound
urns (→ `EPSG:258xx`), and the Swiss CH1903/LV95 names.

**Reprojection to a common target (default WGS84) is the default**, because that
is what makes footprints from different cities overlay in one file/viewer. The
`Reprojector` handles the two things that quietly corrupt reprojected geometry:

- **GML axis order.** A *geographic* source CRS stores coordinates as
  `(lat, lon)`, a *projected* one as `(easting, northing)`. The reprojector feeds
  pyproj the right order and uses `always_xy=True`, so the output is always
  `(X, Y)` = `(lon, lat)` for a geographic target or `(easting, northing)` for a
  projected one — i.e. GeoJSON's expected order. Get this wrong and the whole
  city lands in the ocean off Africa.
- **Vertical units.** For a projected source measured in feet (NYC), the Z
  values are scaled by the source's horizontal linear-unit factor so heights come
  out in **metres** (verified: NYC `15.413 m`, not `50.57 ft`). A geographic
  source's Z is already a metric height and passes through untouched.

Escape hatches: `--keep-source-crs` emits the raw source coordinates (still
axis-normalised — reproduces the classic easting/northing look, e.g. Hamburg
`549024.281 5935686.371 18.048`); `--target-crs EPSG:N` picks any other target;
`--source-crs EPSG:N` supplies a CRS for a file that declares no `srsName` (the
script errors out with a clear message rather than guessing).

Coordinate **precision** defaults to 9 decimals for degrees (≈ 0.1 mm) and 3 for
metres, overridable with `--precision`. XY values that are *not* reprojected are
still passed through `round()` only at these safe precisions; parsing itself is
verbatim (`float()` of the source tokens, never truncated during parse).

### 3. Why `source_crs` / `target_crs` are custom members

RFC 7946 (the GeoJSON standard) **removed the `crs` member** and declares all
GeoJSON to be WGS84 unless parties agree otherwise. Rather than resurrect the
deprecated `crs` object (which many parsers ignore), the file records the CRS as
two plain foreign members — allowed by the spec and trivially readable — so a
human or script can see exactly what frame the numbers are in, including when
`--keep-source-crs` or `--target-crs` produces non-WGS84 output. In a combined
run over files with differing CRSs the value is `"mixed"` (each file is still
reprojected to the common target, so this only appears if you mix targets).

### 4. Version-agnostic parsing — one code path for CityGML 1.0 / 2.0 / 3.0

CityGML 1.0, 2.0 and 3.0 differ only by a version number in their namespace URIs
(`…/citygml/1.0` vs `…/2.0`). Hard-coding one namespace would silently skip files
in another version. The extractor therefore matches every element by **local
name** using lxml's `{*}` namespace wildcard (`{*}Building`, `{*}posList`,
`{*}GroundSurface`, …), so all versions work without branching. `gml:id` uses the
one namespace that is stable across all versions.

Coordinate rings are read from all three GML encodings seen in real data
(`_ring_from_el`):
- a single **`gml:posList`** (honouring `srsDimension`),
- a sequence of **`gml:pos`** elements (as emitted by IFC→CityGML converters such
  as the FZK-Haus files),
- legacy **`gml:coordinates`** (comma-separated tuples, CityGML 1.0).

`_chunk` even repairs a wrong/absent `srsDimension` by inferring 2-D vs 3-D from
the token count, so malformed-but-parseable geometry still survives.

### 5. Streaming, for multi-GB city files

Hamburg LoD2 and the NYC tiles are large (the single NYC tile tested holds ~50 000
buildings). The script uses `lxml.iterparse` on `end` events for `{*}Building`
and writes each GeoJSON `Feature` to disk immediately, then **clears the parsed
element and its already-emitted siblings** (`bldg.clear()` + deleting prior
siblings). Memory stays bounded regardless of file size, and features never all
live in RAM at once. `huge_tree=True` and `recover=True` let it push through very
large trees and minor XML defects.

---

## Behaviour notes & limitations

- **Buildings only.** By design this exports building footprints (Building +
  BuildingPart). Roads, water, vegetation, etc. are not emitted.
- **Buildings with no recoverable ground geometry are skipped** and counted in
  the run summary (`… skipped`), rather than emitting empty features.
- **Ring winding is not normalised.** Coordinates are emitted in source order
  (rings are closed if the source left them open). RFC 7946 recommends
  right-hand-rule winding; most consumers tolerate either, so no reordering is
  imposed. Add a rewind step if a strict consumer requires it.
- **`--keep-source-crs` with a geographic source** still reorders axes to
  `(lon, lat)` so the output is valid GeoJSON, but does not change the datum.
- Requires `lxml` and `pyproj`, both already project dependencies. Nothing here
  touches Neo4j — it reads CityGML and writes GeoJSON, standalone.

## Tested against

| Input | Result |
| ----- | ------ |
| FZK-Haus LoD0–LoD4 (5 files, combined) | 5 footprints, EPSG:25832 → EPSG:4326 |
| NYC tile `DA10` (EPSG:2263, feet) | 49 847 footprints, feet → WGS84, heights in m |
| `--keep-source-crs` on Hamburg-style UTM | raw easting/northing preserved |

All emitted rings verified closed; every feature carries a `gml_id`.
