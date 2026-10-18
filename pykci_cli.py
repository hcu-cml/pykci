"""
pykci command-line interface: one entry point to the pipeline scripts, for
people and for programs (the pykci web client, CI jobs, containers).

    pykci [--neo4j-uri URI] [--neo4j-user USER] [--neo4j-password PW] [--progress]
          <command> [command arguments …]

    (without installing: python pykci_cli.py <command> …)

Pipeline commands forward their arguments unchanged to the script, so
`pykci <command> --help` shows that script's own options:

    ingest-citygml   ingest_citygml.py           CityGML 2.0 → Neo4j
    ingest-osm       ingest_osm.py               OpenStreetMap enrichment (.osm.pbf)
    ingest-roofmats  ingest_roof_materials.py    ML roof-material predictions (GeoJSON)
    export-citygml   export_citygml.py           Neo4j → CityGML (dataset, --ids or --bbox)
    build-3dtiles    build_3dtiles.py            Neo4j → 3D Tiles (--tiled for whole cities)
    footprints       tools/extract_citygml_building_footprints.py   CityGML → GeoJSON (no Neo4j)
    upgrade-citygml  tools/upgrade_citygml_10_to_20.py              CityGML 1.0 → 2.0 (no Neo4j)
    inventory        eval/parse_citygml_inventory.py                feature inventory of a GML file
    stats            eval/stats_control.py                          precheck / postcheck / report
    osm-census       eval/osm_ingestion_census.py                   OSM completeness gate

Built-in commands print one JSON document on stdout:

    inspect FILE…    CityGML header + feature counts, streaming, no Neo4j (import preview)
    info             graph summary: datasets (CRS, bbox in WGS84), counts, provenance layers

Global options set the environment the scripts read (pykci_runtime):
--neo4j-uri/--neo4j-user/--neo4j-password → NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD,
--progress → PYKCI_PROGRESS=1 (lines 'PYKCI_PROGRESS {json}' on stdout).
The exit status is the command's own (non-zero on failure).
"""

import argparse
import json
import os
import runpy
import sys
from collections import Counter
from pathlib import Path

from pykci_runtime import neo4j_auth, neo4j_uri, progress

ROOT = Path(__file__).resolve().parent

# command → (module at the repo root | path relative to the repo root)
COMMANDS = {
    "ingest-citygml": ("module", "ingest_citygml"),
    "ingest-osm": ("module", "ingest_osm"),
    "ingest-roofmats": ("module", "ingest_roof_materials"),
    "export-citygml": ("module", "export_citygml"),
    "build-3dtiles": ("module", "build_3dtiles"),
    "footprints": ("path", "tools/extract_citygml_building_footprints.py"),
    "upgrade-citygml": ("path", "tools/upgrade_citygml_10_to_20.py"),
    "inventory": ("path", "eval/parse_citygml_inventory.py"),
    "stats": ("path", "eval/stats_control.py"),
    "osm-census": ("path", "eval/osm_ingestion_census.py"),
}
BUILTINS = ("inspect", "info")


def _run_script(command: str, argv: list[str]) -> int:
    kind, target = COMMANDS[command]
    if kind == "module":
        sys.argv = [f"{target}.py", *argv]
        runpy.run_module(target, run_name="__main__", alter_sys=True)
        return 0
    path = ROOT / target
    if not path.exists():
        print(f"ERROR: '{command}' needs {target}, which is not part of this installation; "
              f"run pykci from a source checkout (or `pip install -e .`).", file=sys.stderr)
        return 2
    sys.path.insert(0, str(path.parent))
    sys.argv = [str(path), *argv]
    runpy.run_path(str(path), run_name="__main__")
    return 0


# ---------------------------------------------------------------------------
# inspect: fast CityGML preview for an import dialog (no Neo4j)
# ---------------------------------------------------------------------------

_TOP_LEVEL_SKIP = {"Appearance"}      # app:Appearance members are not city objects


def inspect_citygml(path: Path) -> dict:
    """Stream one CityGML file and report version, CRS, envelope and counts of
    the top-level city objects (children of cityObjectMember), plus duplicate
    gml:ids among them (the MERGE-collapse risk ingest_citygml resolves)."""
    from lxml import etree

    size = path.stat().st_size
    info = {"file": path.name, "bytes": size, "citygml_version": None, "srs_name": None,
            "srs_dimension": None, "envelope": None, "lods": [], "features": {},
            "building_parts": 0, "duplicate_ids": 0, "errors": []}
    ids: Counter = Counter()
    lods: set = set()
    root = None
    depth_member = None
    last_emit = 0
    with open(path, "rb") as fh:
        try:
            for event, el in etree.iterparse(fh, events=("start", "end"), huge_tree=True):
                tag = el.tag if isinstance(el.tag, str) else ""
                ns, _, local = tag[1:].partition("}") if tag.startswith("{") else ("", "", tag)
                if event == "start":
                    if root is None:
                        root = el
                    if info["citygml_version"] is None and "citygml" in ns:
                        for v in ("3.0", "2.0", "1.0"):
                            if f"/citygml/{v}" in ns or ns.endswith(f"citygml/{v}"):
                                info["citygml_version"] = v
                                break
                    if local == "cityObjectMember":
                        depth_member = el
                    elif depth_member is not None and el.getparent() is depth_member:
                        if local not in _TOP_LEVEL_SKIP:
                            info["features"][local] = info["features"].get(local, 0) + 1
                            gid = el.get("{http://www.opengis.net/gml}id")
                            if gid:
                                ids[gid] += 1
                    elif local == "BuildingPart":
                        info["building_parts"] += 1
                    if local.startswith("lod") and local[3:4].isdigit():
                        lods.add(int(local[3]))
                    continue
                # end events: the CityModel envelope, not a member's own boundedBy
                if local == "Envelope" and info["envelope"] is None and depth_member is None:
                    info["srs_name"] = el.get("srsName")
                    dim = el.get("srsDimension")
                    info["srs_dimension"] = int(dim) if dim and dim.isdigit() else None
                    corners = {}
                    for c in el:
                        if isinstance(c.tag, str) and c.text:
                            corners[c.tag.rsplit("}", 1)[-1]] = [float(v) for v in c.text.split()]
                    if "lowerCorner" in corners and "upperCorner" in corners:
                        info["envelope"] = {"lower": corners["lowerCorner"],
                                            "upper": corners["upperCorner"]}
                elif info["srs_name"] is None and el.get("srsName"):
                    info["srs_name"] = el.get("srsName")
                if local == "cityObjectMember":
                    depth_member = None
                if el.getparent() is root:
                    # drop finished members (city objects, appearances) as we go
                    el.clear()
                    while el.getprevious() is not None:
                        del root[0]
                    pos = fh.tell()
                    if pos - last_emit > 50_000_000:
                        last_emit = pos
                        progress("inspect.read", done=pos, total=size, unit="bytes", file=path.name)
        except etree.XMLSyntaxError as exc:
            info["errors"].append(f"XML syntax error: {exc}")
    info["lods"] = sorted(lods)
    info["duplicate_ids"] = sum(1 for n in ids.values() if n > 1)
    info["buildings"] = info["features"].get("Building", 0)
    return info


def cmd_inspect(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pykci inspect",
                                 description="CityGML preview as JSON (no Neo4j).")
    ap.add_argument("paths", nargs="+", help=".gml files and/or directories")
    ap.add_argument("--recursive", action="store_true")
    args = ap.parse_args(argv)
    files = []
    for p in map(Path, args.paths):
        if p.is_dir():
            files += sorted(p.glob("**/*.gml" if args.recursive else "*.gml"))
        elif p.is_file():
            files.append(p)
    if not files:
        print(json.dumps({"error": "no .gml files found"}))
        return 1
    reports = []
    for i, f in enumerate(files):
        progress("inspect.file", done=i, total=len(files), unit="files", file=f.name)
        reports.append(inspect_citygml(f))
    totals = Counter()
    for r in reports:
        totals.update(r["features"])
    print(json.dumps({"files": reports, "totals": dict(totals),
                      "buildings": totals.get("Building", 0)}, indent=1))
    return 0 if all(not r["errors"] for r in reports) else 1


# ---------------------------------------------------------------------------
# info: what is in the graph (for a client to configure itself)
# ---------------------------------------------------------------------------

def cmd_info(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pykci info", description="Graph summary as JSON.")
    ap.parse_args(argv)
    from neo4j import GraphDatabase
    from pyproj import Transformer

    from build_3dtiles import _horiz_epsg_from_srs

    driver = GraphDatabase.driver(
        neo4j_uri(), auth=neo4j_auth(),
        notifications_disabled_classifications={"DEPRECATION", "UNRECOGNIZED"})
    driver.verify_connectivity()
    out: dict = {"neo4j": {"uri": neo4j_uri()}}
    with driver.session(database="neo4j") as sess:
        comp = sess.run("CALL dbms.components() YIELD name, versions, edition "
                        "WHERE name = 'Neo4j Kernel' "
                        "RETURN versions[0] AS version, edition").single()
        out["neo4j"].update({"version": comp["version"], "edition": comp["edition"]})

        def count(q):
            return sess.run(q).single()[0]

        out["counts"] = {
            "nodes": count("MATCH (n) RETURN count(n)"),
            "relationships": count("MATCH ()-[r]->() RETURN count(r)"),
            "buildings": count("MATCH (n:Building) RETURN count(n)"),
            "city_objects": count("MATCH (n:CityObject) RETURN count(n)"),
            "osm_features": count("MATCH (n:OsmFeature) RETURN count(n)"),
            "enriched_by": count("MATCH ()-[r:ENRICHED_BY]->() RETURN count(r)"),
            "has_poi": count("MATCH ()-[r:HAS_POI]->() RETURN count(r)"),
        }
        out["layers"] = {
            "authoritative": out["counts"]["buildings"] + out["counts"]["city_objects"] > 0,
            "crowd_sourced": out["counts"]["osm_features"] > 0,
            "ml_predicted": count("RETURN EXISTS { MATCH (b:Building) "
                                  "WHERE b.predicted_roof_material_source IS NOT NULL }"),
            "reconstructed": count("RETURN EXISTS { MATCH (b:Building) "
                                   "WHERE b.lod3_enriched = true }"),
        }
        try:
            out["spatial_layers"] = [r["name"] for r in sess.run(
                "CALL spatial.layers() YIELD name RETURN name ORDER BY name")]
        except Exception:
            out["spatial_layers"] = None
        datasets = []
        for r in sess.run("MATCH (d:Dataset) RETURN d ORDER BY d.id").data():
            d = r["d"]
            ds = {k: d.get(k) for k in ("id", "name", "source", "crs", "source_crs", "lod", "format")}
            # The extent is taken from the features, not the stored Dataset
            # envelope: some sources ship a degenerate one (Helsinki 2019).
            ext = sess.run(
                "MATCH (n)-[:PART_OF]->(:Dataset {id: $id}) WHERE n:Building OR n:CityObject "
                "RETURN count(n:Building) AS buildings, count(n) AS features, "
                "min(n.bbox_min_x) AS x0, min(n.bbox_min_y) AS y0, "
                "max(n.bbox_max_x) AS x1, max(n.bbox_max_y) AS y1, "
                "min(n.bbox_min_z) AS z0, max(n.bbox_max_z) AS z1",
                id=d["id"]).single()
            ds["buildings"], ds["features"] = ext["buildings"], ext["features"]
            if None not in (ext["x0"], ext["y0"], ext["x1"], ext["y1"]):
                ds["extent"] = [ext["x0"], ext["y0"], ext["x1"], ext["y1"]]
                ds["z_range"] = [ext["z0"], ext["z1"]]
                try:
                    t = Transformer.from_crs(_horiz_epsg_from_srs(d.get("crs") or ""),
                                             "EPSG:4326", always_xy=True)
                    xs = [ext["x0"], ext["x1"], ext["x1"], ext["x0"]]
                    ys = [ext["y0"], ext["y0"], ext["y1"], ext["y1"]]
                    lon, lat = t.transform(xs, ys)
                    ds["extent_wgs84"] = [min(lon), min(lat), max(lon), max(lat)]
                except Exception:
                    ds["extent_wgs84"] = None
            datasets.append(ds)
        out["datasets"] = datasets
    driver.close()
    print(json.dumps(out, indent=1, default=str))
    return 0


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    # The scripts print arrows/umlauts; on a Windows cp1252 console that raises
    # UnicodeEncodeError. Replace what the console cannot show instead of crashing
    # (machine-readable output, i.e. progress lines and inspect/info JSON, is ASCII).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    commands = "\n".join(f"  {c}" for c in [*COMMANDS, *BUILTINS])
    ap = argparse.ArgumentParser(
        prog="pykci", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="pykci: CityGML <-> Neo4j knowledge graph pipeline.",
        epilog=f"commands:\n{commands}\n\n`pykci <command> --help` shows the command's options.")
    ap.add_argument("--neo4j-uri", help="Bolt URI (env NEO4J_URI; default bolt://localhost:7687)")
    ap.add_argument("--neo4j-user", help="Neo4j user (env NEO4J_USER; default neo4j)")
    ap.add_argument("--neo4j-password", help="Neo4j password (env NEO4J_PASSWORD)")
    ap.add_argument("--progress", action="store_true",
                    help="emit machine-readable 'PYKCI_PROGRESS {json}' lines on stdout")
    ap.add_argument("command", choices=[*COMMANDS, *BUILTINS], metavar="command")
    ap.add_argument("args", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)

    for flag, var in (("neo4j_uri", "NEO4J_URI"), ("neo4j_user", "NEO4J_USER"),
                      ("neo4j_password", "NEO4J_PASSWORD")):
        if getattr(a, flag):
            os.environ[var] = getattr(a, flag)
    if a.progress:
        os.environ["PYKCI_PROGRESS"] = "1"
    # Scripts write their reports to output/… relative to the working directory
    # and import each other by module name, so the repo root must be importable.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    if a.command == "inspect":
        return cmd_inspect(a.args)
    if a.command == "info":
        return cmd_info(a.args)
    return _run_script(a.command, a.args)


if __name__ == "__main__":
    sys.exit(main())
