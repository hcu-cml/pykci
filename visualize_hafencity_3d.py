"""
Render ALL HafenCity buildings (with and without facade openings) + ALL HafenCity
street trees in one CesiumJS 3D viewer.

Reuses the rendering/HTML machinery of visualize_facade_openings_3d.py, but selects
features by a HafenCity bounding box (ETRS89 UTM32) instead of has_lod3_data:
  - every building in the bbox -> Wall + Roof surfaces, coloured by predictedroofmaterial
  - facade openings for any of those buildings that have them (the 1 enriched one)
  - every street tree in the bbox -> trunk + crown glyph
Camera opens on the whole area (initial_view='all').

Usage:
    python visualize_hafencity_3d.py
    python visualize_hafencity_3d.py --bbox 565800 5932400 567600 5933900
    python visualize_hafencity_3d.py --output output/html/hafencity_3d.html
"""
import argparse
from pathlib import Path

from neo4j import GraphDatabase
from pyproj import Transformer

import visualize_facade_openings_3d as V

# Default HafenCity extent (ETRS89 UTM32): minx miny maxx maxy
HAFENCITY_BBOX = (565800.0, 5932400.0, 567600.0, 5933900.0)
DEFAULT_OUTPUT = Path(__file__).parent / "output" / "html" / "hafencity_3d.html"


def _wkt(b):
    x0, y0, x1, y1 = b
    return (f"POLYGON(({x0} {y0}, {x1} {y0}, {x1} {y1}, "
            f"{x0} {y1}, {x0} {y0}))")


def building_ids_in_bbox(sess, bbox):
    return [r["id"] for r in sess.run(
        "CALL spatial.intersects('features',$w) YIELD node WHERE node:Building "
        "RETURN DISTINCT node.id AS id", w=_wkt(bbox))]


def fetch_buildings(sess, ids, t32_wgs, geoid):
    """Building dicts in the shape build_html expects (roof_mats from predictedroofmaterial)."""
    rows = sess.run("""
        MATCH (b:Building) WHERE b.id IN $ids
        OPTIONAL MATCH (b)-[:HAS_ROOF_TYPE]->(rt:RoofType)
        OPTIONAL MATCH (b)-[:HAS_BOUNDARY]->(gs:BoundarySurface {surface_type:'GroundSurface'})
                       -[:HAS_POLYGON]->(:GeometryPolygon)-[:HAS_EXTERIOR_RING]->(gr:GeometryRing)
        WITH b, head(collect(DISTINCT rt.name)) AS roof_type, collect(gr.pos_list)[0] AS pos_list
        RETURN b.id AS id,
               b.predictedroofmaterial AS material,
               toFloat(coalesce(b.measured_height, 8.0)) AS height,
               toFloat(coalesce(b.ground_z, 0.0)) AS ground_z,
               b.storeys_above_ground AS storeys,
               toInteger(coalesce(b.lod3_windows,0)) AS windows,
               toInteger(coalesce(b.lod3_doors,0)) AS doors,
               toFloat(b.center_x) AS cx, toFloat(b.center_y) AS cy,
               toFloat(b.bbox_min_x) AS bbox_min_x, toFloat(b.bbox_max_x) AS bbox_max_x,
               roof_type, pos_list
    """, ids=ids).data()
    out = []
    for r in rows:
        cx, cy = float(r["cx"] or 0), float(r["cy"] or 0)
        lon_c, lat_c = t32_wgs.transform(cx, cy)
        footprint = []
        if r["pos_list"]:
            footprint = [[p[0], p[1]] for p in
                         V._pos_list_to_wgs84(r["pos_list"], t32_wgs, geoid)]
        out.append({
            "id": r["id"], "lon": round(lon_c, 7), "lat": round(lat_c, 7),
            "height": round(float(r["height"]), 2), "ground_z": 0.0,
            "ground_z_raw": round(float(r["ground_z"]) + geoid, 2),
            "windows": int(r["windows"]), "doors": int(r["doors"]),
            "storeys": r["storeys"], "roof_type": r["roof_type"] or "—",
            "roof_mats": [r["material"]] if r["material"] else [],
            "source": "", "footprint": footprint,
            "bbox_min_x": float(r["bbox_min_x"] or 0), "bbox_max_x": float(r["bbox_max_x"] or 0),
            "cy_utm": cy, "tree_count": 0,
        })
    return out


def fetch_surfaces(sess, ids, t32_wgs, geoid, ground_z_by_bid):
    """Wall + Roof surfaces for the bbox buildings (build_html shape)."""
    rows = sess.run("""
        MATCH (b:Building) WHERE b.id IN $ids
        MATCH (b)-[:HAS_BOUNDARY]->(bs:BoundarySurface)
        WHERE bs.surface_type IN ['WallSurface','RoofSurface']
        MATCH (bs)-[:HAS_POLYGON]->(:GeometryPolygon)-[:HAS_EXTERIOR_RING]->(gr:GeometryRing)
        RETURN b.id AS bid, bs.surface_type AS stype, gr.pos_list AS pos_list
    """, ids=ids).data()
    surfaces = []
    for r in rows:
        if not r["pos_list"]:
            continue
        gz = ground_z_by_bid.get(r["bid"], 0.0)
        pts = V._pos_list_to_wgs84(r["pos_list"], t32_wgs, geoid, z_offset=-gz)
        if len(pts) >= 3:
            surfaces.append({"bid": r["bid"], "stype": r["stype"], "pts": pts})
    return surfaces


def fetch_trees(sess, bbox, t32_wgs):
    """All trees in the bbox (build_html tree-glyph shape)."""
    rows = sess.run("""
        CALL spatial.intersects('features',$w) YIELD node
        WHERE node:SolitaryVegetationObject
        RETURN DISTINCT node.id AS id, toFloat(node.center_x) AS x, toFloat(node.center_y) AS y,
               node.gattung AS gattung, node.baumart AS baumart, node.stra_e AS strasse,
               node.pflanzjahr AS pflanzjahr, toFloat(node.hoehe_aus_als) AS height,
               toFloat(node.kronendurchmesser) AS crown_d
    """, w=_wkt(bbox)).data()
    trees = []
    for r in rows:
        if r["x"] is None or r["y"] is None:
            continue
        lon, lat = t32_wgs.transform(r["x"], r["y"])
        h = max(2.0, min(float(r["height"]) if r["height"] else 8.0, 45.0))
        cd = max(1.5, min(float(r["crown_d"]) if r["crown_d"] else 5.0, 25.0))
        trunk_h = max(1.5, min(0.35 * h, h - 1.0))
        trees.append({
            "id": r["id"], "lon": round(lon, 7), "lat": round(lat, 7),
            "trunk_h": round(trunk_h, 2), "crown_r": round(cd / 2.0, 2),
            "crown_h": round(h - trunk_h, 2), "height": round(h, 1),
            "gattung": r["gattung"] or "—", "baumart": r["baumart"] or "—",
            "strasse": r["strasse"] or "", "planted": r["pflanzjahr"] or "—",
        })
    return trees


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", nargs=4, type=float, default=list(HAFENCITY_BBOX),
                    metavar=("MINX", "MINY", "MAXX", "MAXY"),
                    help="HafenCity extent in ETRS89 UTM32")
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = ap.parse_args()
    bbox = tuple(args.bbox)
    out_path = Path(args.output); out_path.parent.mkdir(parents=True, exist_ok=True)

    driver = GraphDatabase.driver(V.NEO4J_URL, auth=(V.NEO4J_USER, V.NEO4J_PASSWORD),
                                  notifications_disabled_classifications={"UNRECOGNIZED", "DEPRECATION"})
    crs, sx, sy, sz = V.fetch_dataset(driver)
    t32_wgs = Transformer.from_crs(V._horiz_epsg(crs), "EPSG:4326", always_xy=True)
    geoid = V._geoid_offset(crs, sx, sy, sz)

    with driver.session() as s:
        ids = building_ids_in_bbox(s, bbox)
        print(f"HafenCity buildings: {len(ids)}")
        buildings = fetch_buildings(s, ids, t32_wgs, geoid)
        ground_z_by_bid = {b["id"]: b["ground_z_raw"] for b in buildings}
        surfaces = fetch_surfaces(s, ids, t32_wgs, geoid, ground_z_by_bid)
        print(f"  wall+roof surfaces: {len(surfaces)}")
        wall_frames = V.find_best_wall_frame_per_building(driver)  # enriched only
        openings_3d = V.fetch_openings_3d(driver, buildings, t32_wgs, geoid, wall_frames)
        print(f"  facade openings: {len(openings_3d)}")
        trees = fetch_trees(s, bbox, t32_wgs)
        print(f"  trees: {len(trees)}")

    html = V.build_html(buildings, surfaces, openings_3d, {}, [], trees,
                        osm_buildings=False, initial_view="all")
    out_path.write_text(html, encoding="utf-8")
    driver.close()
    kb = out_path.stat().st_size // 1024
    print(f"\nSaved -> {out_path}  ({kb} KB)")
    print("Im Browser oeffnen (benoetigt Internet fuer CesiumJS-CDN + Basemap).")


if __name__ == "__main__":
    main()
