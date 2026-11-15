"""
Writing into the graph: what every writer needs, once.

The ingest steps (CityGML, OpenStreetMap, LoD3 facades) each add their own nodes, but they
share the plumbing around them: ids, indexes, the R-tree layers of the Neo4j Spatial
plugin, and the bounding boxes the R-tree works on.

  slug()                          an id-safe name
  create_indexes()                range indexes, named <label>_<property>_idx
  create_point_index()            the point index on <label>.location
  ensure_spatial_layer()          an R-tree layer on bbox_wkt
  backfill_bbox_from_geometry()   bbox_wkt / bbox_* / center_* of features that have none,
                                  from the geometry stored below them
"""

import re

from pykci.geometry import bbox_of_pos_lists


def slug(s):
    """Lower-case ASCII id: everything but a-z, 0-9 and _ becomes _."""
    return re.sub(r"[^a-z0-9_]", "_", s.lower())


def create_indexes(sess, pairs, *, tolerant=False):
    """One range index per (label, property), named <label>_<property>_idx. `tolerant`
    carries on when an index cannot be created (an equivalent one under another name)."""
    for label, prop in pairs:
        try:
            sess.run(f"CREATE INDEX {label.lower()}_{prop}_idx IF NOT EXISTS "
                     f"FOR (n:{label}) ON (n.{prop})")
        except Exception:
            if not tolerant:
                raise


def create_point_index(sess, label):
    """The point index <label>_location_idx on n.location; a server that cannot create
    it is left without."""
    try:
        sess.run(f"CREATE POINT INDEX {label.lower()}_location_idx IF NOT EXISTS "
                 f"FOR (n:{label}) ON (n.location)")
    except Exception:
        pass


def ensure_spatial_layer(sess, layer, *, report=False):
    """Make sure the R-tree layer `layer` (WKT in bbox_wkt) exists. False, with a
    warning, when the Neo4j Spatial plugin does not answer; `report` also says whether
    the layer was created or found."""
    try:
        existing = {r["name"] for r in sess.run("CALL spatial.layers() YIELD name RETURN name")}
        if layer not in existing:
            sess.run("CALL spatial.addWKTLayer($layer, 'bbox_wkt')", layer=layer)
            if report:
                print(f"  Spatial layer '{layer}' created.")
        elif report:
            print(f"  Spatial layer '{layer}' already exists.")
        return True
    except Exception as exc:
        print(f"  WARNING: spatial layer setup failed ({exc})")
        return False


def backfill_bbox_from_geometry(sess, ids, batch_size=2000):
    """Write bbox_wkt, bbox_min/max_x/y/z and center_x/y on the features `ids` from the
    geometry stored below them (GeometryRing / GeometryLineString pos_list, "x y z …",
    up to six edges down). Only non-degenerate boxes are written. Returns how many
    features got a box. Serial and idempotent (SET only)."""
    written = 0
    for i in range(0, len(ids), batch_size):
        rows = sess.run(
            "UNWIND $ids AS cid MATCH (c:Entity {id: cid}) "
            "OPTIONAL MATCH (c)-[*1..6]->(g) "
            "WHERE (g:GeometryRing OR g:GeometryLineString) AND g.pos_list IS NOT NULL "
            "RETURN cid AS id, collect(g.pos_list) AS pl", ids=ids[i:i + batch_size]).data()
        writes = []
        for r in rows:
            if not r["pl"]:
                continue
            bb = bbox_of_pos_lists(r["pl"])
            if bb is None:
                continue
            minx, miny, minz, maxx, maxy, maxz = bb
            writes.append({
                "id": r["id"],
                "wkt": (f"POLYGON(({minx} {miny}, {maxx} {miny}, {maxx} {maxy}, "
                        f"{minx} {maxy}, {minx} {miny}))"),
                "minx": minx, "miny": miny, "minz": minz,
                "maxx": maxx, "maxy": maxy, "maxz": maxz,
                "cx": (minx + maxx) / 2.0, "cy": (miny + maxy) / 2.0})
        if writes:
            sess.run(
                "UNWIND $rows AS row MATCH (n:Entity {id: row.id}) "
                "SET n.bbox_wkt=row.wkt, n.bbox_min_x=row.minx, n.bbox_min_y=row.miny, "
                "n.bbox_min_z=row.minz, n.bbox_max_x=row.maxx, n.bbox_max_y=row.maxy, "
                "n.bbox_max_z=row.maxz, n.center_x=row.cx, n.center_y=row.cy",
                rows=writes)
            written += len(writes)
    return written
