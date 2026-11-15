"""
Coordinate reference systems of CityGML data: from an srsName to an EPSG code, and the
geoid undulation of a dataset.

CityGML files name their CRS in many ways (plain and URN EPSG codes, the OGC HTTP form, the
German ADV names, Swiss names without a code). Two readers exist because they answer two
different questions; every other module uses one of them instead of its own regex:

  epsg_from_srs()        the CRS of the *source file*, for reprojection at ingest (pyproj,
                         ogr2ogr): EPSG in every encoding, ADV UTM, Swiss CH1903 / LV95.
                         None for an empty name.
  horiz_epsg_from_srs()  the *horizontal* CRS of what is stored in the graph (Dataset.crs),
                         for placing it on the globe: EPSG, ADV UTM and Gauss-Krüger. A
                         compound name gives its horizontal part; heights go through
                         geoid_undulation().
"""

import re

# ADV srsName pattern → compound EPSG (horizontal + DHHN92 vertical ≈ DHHN2016).
# Used to derive the geoid undulation (orthometric → ellipsoidal height offset)
# via a pyproj compound-CRS → EPSG:4979 (WGS84 3D) transformation.
# Requires PROJ datum-grid files; falls back to 0.0 m with a warning if absent.
ADV_COMPOUND = {
    "ETRS89_UTM32": "EPSG:5555",   # ETRS89-UTM32N + DHHN92 height
    "ETRS89_UTM33": "EPSG:5556",   # ETRS89-UTM33N + DHHN92 height
    "DE_DHDN_GK2":  "EPSG:5675",   # Gauss-Krüger zone 2 + DHHN92
    "DE_DHDN_GK3":  "EPSG:5678",   # Gauss-Krüger zone 3 + DHHN92
    "DE_DHDN_GK4":  "EPSG:5679",   # Gauss-Krüger zone 4 + DHHN92
    "DE_DHDN_GK5":  "EPSG:5680",   # Gauss-Krüger zone 5 + DHHN92
}


def epsg_from_srs(srs):
    """Resolve a CityGML srsName to an 'EPSG:NNNN' string, for ogr2ogr / pyproj.

    Handles the plain/urn EPSG forms plus the two non-EPSG conventions seen in
    real data: adv/ETRS89-UTM compound urns (Hamburg → EPSG:258xx) and Swiss
    CH1903/LV95 names. Returns the original string if nothing matches (pyproj
    may still understand it), or None for empty input."""
    if not srs:
        return None
    # EPSG code in any common encoding: 'EPSG:6697', 'EPSG::6697' (URN),
    # or the OGC HTTP form '.../def/crs/EPSG/0/6697' (the '0/' is a version).
    m = re.search(r"EPSG[:/]+(?:\d+/)?(\d{4,6})", srs, re.IGNORECASE)
    if m:
        return f"EPSG:{m.group(1)}"
    u = srs.upper().replace(" ", "")
    # ADV UTM aliases (ETRS89_UTM32/33 → 25832/25833)
    m = re.search(r"UTM(\d{2})", u)
    if m:
        return f"EPSG:258{m.group(1)}"
    # Swiss national CRS sometimes given as a bare name with no EPSG code
    if "CH1903+" in u or "LV95" in u:
        return "EPSG:2056"
    if "CH1903" in u or "LV03" in u:
        return "EPSG:21781"
    return srs  # let pyproj try to resolve the raw string


def horiz_epsg_from_srs(srs_name: str) -> str:
    """Map a CityGML srsName to a horizontal EPSG CRS identifier for pyproj.

    Handles the most common patterns found in CityGML files:
      urn:ogc:def:crs:EPSG::25832   → EPSG:25832
      EPSG:25832                    → EPSG:25832
      urn:adv:crs:ETRS89_UTM32*…   → EPSG:25832
      urn:adv:crs:ETRS89_UTM33*…   → EPSG:25833
      urn:adv:crs:DE_DHDN_GK3*…    → EPSG:31467
    Falls back to the raw srsName string (pyproj may still accept it).
    """
    # OGC/EPSG URN or plain prefix: EPSG:NNNNN
    m = re.search(r"EPSG[:\s]+(\d+)", srs_name)
    if m:
        return f"EPSG:{m.group(1)}"
    # ADV UTM: ETRS89_UTM32 → EPSG:25832, ETRS89_UTM33 → EPSG:25833
    m = re.search(r"ETRS89_UTM(\d+)", srs_name, re.IGNORECASE)
    if m:
        return f"EPSG:{25800 + int(m.group(1))}"
    # ADV Gauss-Krüger: GK2 → EPSG:31466, GK3 → 31467, GK4 → 31468, GK5 → 31469
    m = re.search(r"GK(\d)", srs_name, re.IGNORECASE)
    if m:
        return f"EPSG:{31464 + int(m.group(1))}"
    return srs_name


def geoid_undulation(srs_name: str, sample_x: float, sample_y: float, sample_z: float,
                     override: float | None = None) -> tuple[float, str]:
    """Geoid undulation (m) at a sample point in the dataset CRS, and where it comes from.

    `override` (--geoid-offset) wins. Otherwise a compound-CRS → EPSG:4979 (WGS84 3D
    ellipsoidal) transformation via pyproj: the difference between the transformed
    ellipsoidal height and the input orthometric height is the undulation.

    Returns (0.0, 'none') and prints a warning when no compound CRS mapping is found for
    the srsName, the transformation fails, or PROJ has no geoid grid for it: PROJ then
    falls back to a 'ballpark' transformation that leaves the height unchanged, which
    would silently give 0 m instead of the ~40 m of Germany.
    """
    import math

    from pyproj import Transformer

    if override is not None:
        return float(override), "--geoid-offset"
    compound = None
    for pat, epsg in ADV_COMPOUND.items():
        if pat.lower() in srs_name.lower():
            compound = epsg
            break
    if compound is None:
        print(f"  Note: no compound CRS mapping for '{srs_name}'; geoid offset = 0.0 m "
              "(pass --geoid-offset to set it)")
        return 0.0, "none"
    try:
        t3d = Transformer.from_crs(compound, "EPSG:4979", always_xy=True)
        _, _, h_ell = t3d.transform(sample_x, sample_y, sample_z)
    except Exception as exc:
        print(f"  WARNING: geoid transform unavailable ({exc}); geoid offset = 0.0 m "
              "(pass --geoid-offset to set it)")
        return 0.0, "none"
    if "ballpark" in t3d.description.lower() or not math.isfinite(h_ell) or abs(h_ell - sample_z) < 1e-6:
        print(f"  WARNING: PROJ has no geoid grid for {compound} (ballpark transformation); geoid offset = "
              "0.0 m. Install the grid (projsync) or pass --geoid-offset")
        return 0.0, "none"
    return h_ell - sample_z, f"PROJ {compound} -> EPSG:4979 at the dataset centre"


def peek_srsname(path, limit=None, chunk_size=1_048_576):
    """The first srsName of a CityGML file, or None when it has none.

    With `limit`, only the first `limit` bytes are looked at (a bounded read for a
    possibly huge file whose envelope sits at the top). Without it the file is scanned in
    chunks up to the first match, so a populated file costs one chunk and a CRS declared
    deeper in the file is still found; a small tail is carried between chunks so a match
    split across a boundary is not missed."""
    pat = re.compile(rb'srsName="([^"]+)"')
    with open(path, "rb") as fh:
        if limit is not None:
            m = pat.search(fh.read(limit))
            return m.group(1).decode("utf-8", "ignore") if m else None
        tail = b""
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

    Reading normalises GML axis order: a *geographic* source stores (lat, lon), so the
    transformer is fed (lon, lat); a *projected* source stores (easting, northing) and
    passes straight through. With always_xy the output is (X, Y): (lon, lat) for a
    geographic target, (easting, northing) for a projected one.

    Z is scaled by the source's horizontal linear unit so heights come out in metres for
    projected sources measured in feet (e.g. NYC EPSG:2263); a geographic source's Z is
    already a metric height and passes through."""

    def __init__(self, src_crs, dst_crs):
        from pyproj import CRS, Transformer
        self.src = CRS.from_user_input(src_crs)
        self.dst = CRS.from_user_input(dst_crs)
        self.src_geographic = self.src.is_geographic
        self.dst_geographic = self.dst.is_geographic
        self.z_factor = 1.0
        if not self.src_geographic:
            try:
                f = self.src.axis_info[0].unit_conversion_factor
                if f:
                    self.z_factor = f
            except Exception:
                pass
        self.tf = Transformer.from_crs(self.src, self.dst, always_xy=True)

    def point(self, a, b, z=None):
        """One position in source axis order → (x, y, z); z stays None for 2D data."""
        x, y = self.tf.transform(b, a) if self.src_geographic else self.tf.transform(a, b)
        if z is None:
            return x, y, None
        return x, y, z * self.z_factor
