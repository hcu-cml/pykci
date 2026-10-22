#!/usr/bin/env python3
"""
roundtrip_census.py
Semantic-completeness census for CityGML round-trip (ingest -> Neo4j -> export).

Unlike roundtrip_fidelity.py (which spot-checks specific features), this tool
proves that NOTHING is dropped: it compares the source GML against the exported
GML at four levels and reports every loss.

  1. Elements    — multiset of element local-names (source count must be <= export)
  2. Attributes  — multiset of (local-name, value) for source attributes
  3. Text        — multiset of (element local-name, normalised text) for leaf text
  4. Coordinates — multiset of coordinate tuples, gathered from BOTH gml:posList
                   and gml:pos, so the pos<->posList encoding change is not a loss

The bar is SEMANTIC COMPLETENESS, not byte-identity: encoding, element order, and
whitespace may differ. The appearance/material module is excluded unless
--include-appearance is given (the 3.0 codec models appearances).

Works for CityGML 2.0 and 3.0 alike (local names only). An OBJECTS table (every
CityGML object element — Road, TrafficSpace, WallSurface, X3DMaterial, … — counted
in source and export) is printed too; for a cross-version export (3.0 → 2.0) it shows
what was flattened, while the four multiset checks show what was lost.

Usage (from project root, after ingest + export of the SAME file):
    python eval/roundtrip_census.py --gml <source.gml> --export <export.gml>
        [--include-appearance] [--json OUT.json]

Exit code 0 when the export covers everything in the source (diff empty), else 1.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lxml import etree

_GML = "http://www.opengis.net/gml"

# Appearance/material module — deferred by decision, excluded from the census.
APPEARANCE_LOCALNAMES = {
    "Appearance", "appearanceMember", "appearance", "surfaceDataMember",
    "X3DMaterial", "Material", "ParameterizedTexture", "GeoreferencedTexture",
    "TexCoordList", "TexCoordGen", "textureCoordinates", "target",
    "diffuseColor", "emissiveColor", "specularColor", "transparency",
    "shininess", "ambientIntensity", "isFront", "isSmooth", "imageURI",
    "textureType", "wrapMode", "borderColor", "mimeType", "worldToTexture",
    "TextureAssociation", "surfaceDataMember",
}

# Coordinate carriers handled by the value-level coordinate check, so they are
# excluded from the element-name multiset (pos vs posList is an encoding choice).
COORD_LOCALNAMES = {"pos", "posList"}

# gml:id values may be legitimately synthesised on export for source elements
# that had none (optional in CityGML), so gml:id is excluded from attr checks.
IGNORE_ATTRS = {"id"}


def _ln(tag) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _is_appearance(el, include=False) -> bool:
    """True if el or any ancestor is in the appearance module."""
    if include:
        return False
    node = el
    while node is not None:
        if isinstance(node.tag, str) and _ln(node.tag) in APPEARANCE_LOCALNAMES:
            return True
        node = node.getparent()
    return False


def _norm_text(s: str) -> str:
    return " ".join(s.split())


def _coord_tuples(text: str):
    """Yield float-tuple coordinate groups from a posList/pos text (dim auto = 3)."""
    nums = text.split()
    # We don't know srsDimension here for certain; compare as a flat token multiset
    # of normalised number strings, which is dimension-agnostic and exact.
    for n in nums:
        try:
            yield repr(float(n))
        except ValueError:
            continue


def census(path: Path, include_appearance=False):
    root = etree.parse(str(path), etree.XMLParser(huge_tree=True)).getroot()
    elems: Counter = Counter()
    attrs: Counter = Counter()
    texts: Counter = Counter()
    coords: Counter = Counter()
    objects: Counter = Counter()

    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if _is_appearance(el, include_appearance):
            continue
        ln = _ln(el.tag)
        ns = etree.QName(el.tag).namespace or ""
        if "citygml" in ns and ln[:1].isupper():
            objects[ln] += 1

        if ln in COORD_LOCALNAMES:
            if el.text:
                coords.update(_coord_tuples(el.text))
            continue

        elems[ln] += 1

        for name, val in el.attrib.items():
            aln = _ln(name) if name.startswith("{") else name
            if aln in IGNORE_ATTRS:
                continue
            if val and val.strip():
                attrs[(aln, _norm_text(val))] += 1

        if el.text and el.text.strip():
            texts[(ln, _norm_text(el.text))] += 1

    return {"elements": elems, "attributes": attrs, "texts": texts, "coords": coords,
            "objects": objects}


def diff(src: Counter, exp: Counter):
    """Return {key: (src_count, exp_count)} for keys where exp < src."""
    out = {}
    for k, n in src.items():
        e = exp.get(k, 0)
        if e < n:
            out[k] = (n, e)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gml", required=True)
    ap.add_argument("--export", required=True)
    ap.add_argument("--show", type=int, default=40, help="max rows per section")
    ap.add_argument("--include-appearance", action="store_true",
                    help="include the appearance module (excluded by default)")
    ap.add_argument("--json", default=None, help="write the comparison as JSON")
    args = ap.parse_args()

    src = census(Path(args.gml), args.include_appearance)
    exp = census(Path(args.export), args.include_appearance)

    sep = "-" * 72
    print(sep)
    print(f"  CENSUS ROUND-TRIP  (appearance module "
          f"{'included' if args.include_appearance else 'excluded'})")
    print(f"  source: {args.gml}")
    print(f"  export: {args.export}")
    print(sep)

    print("  OBJECTS (source → export):")
    for k in sorted(set(src["objects"]) | set(exp["objects"])):
        a, b = src["objects"].get(k, 0), exp["objects"].get(k, 0)
        print(f"        {k:<32} {a:>7} → {b:<7}{'' if a == b else '  ≠'}")
    report = {"source": args.gml, "export": args.export,
              "objects": {k: [src["objects"].get(k, 0), exp["objects"].get(k, 0)]
                          for k in sorted(set(src["objects"]) | set(exp["objects"]))}}
    total_lost = 0
    for section, label in [("elements", "ELEMENTS (local-name)"),
                           ("attributes", "ATTRIBUTES (name, value)"),
                           ("texts", "TEXT (element, value)"),
                           ("coords", "COORDINATE tokens")]:
        d = diff(src[section], exp[section])
        lost = sum(s - e for s, e in d.values())
        total_lost += lost
        report[section] = {"keys_with_loss": len(d), "missing_instances": lost}
        status = "OK" if not d else "!!"
        print(f"  [{status}] {label}: "
              f"{len(d)} keys with loss, {lost} missing instances")
        for k, (s, e) in sorted(d.items(), key=lambda kv: kv[1][1] - kv[1][0])[:args.show]:
            print(f"        src={s:>6} exp={e:>6}  {k}")

    print(sep)
    if args.json:
        report["missing_total"] = total_lost
        Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if total_lost == 0:
        print("  RESULT: LOSSLESS — export covers every source element/attr/text/coord")
        print(sep)
        sys.exit(0)
    else:
        print(f"  RESULT: {total_lost} missing instances — NOT yet lossless")
        print(sep)
        sys.exit(1)


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):   # a cp1252 console must not crash on non-ASCII output
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(errors="replace")
    main()
