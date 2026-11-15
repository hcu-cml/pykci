"""
Geometry stored in the graph, read back as coordinates.

Rings are stored as GML position lists (`pos_list`: "x y z x y z …", or "x y x y …" for
2D data, with `srs_dimension` on the ring node). Readers that only need the footprint use
ring_xy() instead of splitting the string themselves.
"""


def ring_xy(pos_list: str) -> list[tuple[float, float]]:
    """The (x, y) points of a position list. The stride is taken from the length: CityGML
    rings are usually x y z triplets, so a length divisible by 3 is read as 3D, anything else
    as 2D. Values that are not numbers are skipped."""
    v = pos_list.split()
    stride = 3 if len(v) % 3 == 0 else 2
    pts = []
    for i in range(0, len(v) - stride + 1, stride):
        try:
            pts.append((float(v[i]), float(v[i + 1])))
        except ValueError:
            pass
    return pts


def centroid_of_pos_lists(pos_lists):
    """Mean (x, y, z) over every point of a group of 3D position lists; (None, None, None)
    when there is no point. Empty and missing entries are skipped."""
    xs, ys, zs = [], [], []
    for pl in pos_lists:
        if not pl:
            continue
        coords = list(map(float, pl.split()))
        xs.extend(coords[0::3])
        ys.extend(coords[1::3])
        zs.extend(coords[2::3])
    if not xs:
        return None, None, None
    return sum(xs) / len(xs), sum(ys) / len(ys), sum(zs) / len(zs)


def bbox_of_pos_lists(pos_lists):
    """(minx, miny, minz, maxx, maxy, maxz) over a group of 3D position lists ("x y z …",
    stride 3), or None when the box is degenerate in x or y. Lists that are not 3D
    (fewer than three values, or a length not divisible by 3) are skipped."""
    minx = miny = minz = float("inf")
    maxx = maxy = maxz = float("-inf")
    ok = False
    for pl in pos_lists:
        v = pl.split()
        if len(v) < 3 or len(v) % 3 != 0:
            continue
        f = [float(t) for t in v]
        xs, ys, zs = f[0::3], f[1::3], f[2::3]
        minx, maxx = min(minx, min(xs)), max(maxx, max(xs))
        miny, maxy = min(miny, min(ys)), max(maxy, max(ys))
        minz, maxz = min(minz, min(zs)), max(maxz, max(zs))
        ok = True
    if not ok or not (maxx > minx and maxy > miny):
        return None
    return minx, miny, minz, maxx, maxy, maxz
