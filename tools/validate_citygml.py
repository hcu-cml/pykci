#!/usr/bin/env python3
"""Offline XSD validation of CityGML 2.0 / 3.0 instance documents.

Validates the FULL parsed tree (lxml ``etree.XMLSchema.validate``), not an
iterparse stream, so sequence-order errors (e.g. ``trafficSpace`` after
``auxiliaryTrafficSpace``) are reported.  No network access: every remote
``schemaLocation`` used by the OGC schemas (schemas.opengis.net, w3.org xlink /
xml.xsd, docs.oasis-open.org xAL 2.0) is redirected by an lxml resolver to the
local schema cache; any other http(s) URL aborts schema compilation instead of
being fetched.

Schema cache layout (default: $XDG_CACHE_HOME/pykci/citygml-schemas, i.e. usually
~/.cache/pykci/citygml-schemas; override with --schema-dir or $CITYGML_SCHEMA_DIR):

    SCHEMAS_OPENGIS_NET.zip     official OGC bundle (kept)
    SCHEMAS_OPENGIS_NET/        extracted bundle (mirror of schemas.opengis.net/)
    w3c/1999/xlink.xsd          W3C XLink schema (imported by GML 3.1.1 + 3.2.1)
    w3c/2001/xml.xsd            W3C xml: namespace schema (imported by xlink.xsd)
    master_citygml3.xsd         imports all CityGML 3.0 modules
    master_citygml2.xsd         imports all CityGML 2.0 modules + xAL 2.0
    catalog.xml                 OASIS catalog with the same rewrites (for xmllint)

Usage:
    validate_citygml.py FILE.gml [FILE2.gml ...] [--version 2|3]
                        [--max-lines N] [--group-by message|pattern]
                        [--no-refs] [--raw-names] [--json OUT.json]
    validate_citygml.py --setup        # one-time: download bundle + W3C xsds, build masters
    validate_citygml.py --write-masters  # (re)build master XSDs + catalog from the cache

Exit code: 0 = schema-valid (and no broken local xlinks / duplicate ids unless
--no-refs), 1 = invalid, 2 = usage / schema-compilation error.

Equivalent xmllint call (uses catalog.xml):
    XML_CATALOG_FILES=<schema-dir>/catalog.xml xmllint --nonet --noout \
        --schema <schema-dir>/master_citygml3.xsd FILE.gml
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

from lxml import etree

DEFAULT_SCHEMA_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "pykci" / "citygml-schemas"
BUNDLE = "SCHEMAS_OPENGIS_NET"
BUNDLE_URL = "https://schemas.opengis.net/SCHEMAS_OPENGIS_NET.zip"
W3C_FILES = {"w3c/1999/xlink.xsd": "https://www.w3.org/1999/xlink.xsd",
             "w3c/2001/xml.xsd": "https://www.w3.org/2001/xml.xsd"}

# remote prefix/URL -> path relative to the schema dir (prefixes end with "/")
URL_MAP = [
    ("http://schemas.opengis.net/", f"{BUNDLE}/"),
    ("https://schemas.opengis.net/", f"{BUNDLE}/"),
    ("http://www.w3.org/1999/xlink.xsd", "w3c/1999/xlink.xsd"),
    ("https://www.w3.org/1999/xlink.xsd", "w3c/1999/xlink.xsd"),
    ("http://www.w3.org/2001/xml.xsd", "w3c/2001/xml.xsd"),
    ("https://www.w3.org/2001/xml.xsd", "w3c/2001/xml.xsd"),
    ("http://docs.oasis-open.org/election/external/xAL.xsd", f"{BUNDLE}/citygml/xAL/xAL.xsd"),
]

# namespace -> schema path inside the bundle (import order matters only for readability)
MODULES = {
    3: [
        ("http://www.opengis.net/gml/3.2", "gml/3.2.1/gml.xsd"),
        ("urn:oasis:names:tc:ciq:xal:3", "citygml/xAL/3.0/xAL.xsd"),
        ("http://www.opengis.net/citygml/3.0", "citygml/3.0/core.xsd"),
        ("http://www.opengis.net/citygml/appearance/3.0", "citygml/appearance/3.0/appearance.xsd"),
        ("http://www.opengis.net/citygml/construction/3.0", "citygml/construction/3.0/construction.xsd"),
        ("http://www.opengis.net/citygml/building/3.0", "citygml/building/3.0/building.xsd"),
        ("http://www.opengis.net/citygml/bridge/3.0", "citygml/bridge/3.0/bridge.xsd"),
        ("http://www.opengis.net/citygml/tunnel/3.0", "citygml/tunnel/3.0/tunnel.xsd"),
        ("http://www.opengis.net/citygml/transportation/3.0", "citygml/transportation/3.0/transportation.xsd"),
        ("http://www.opengis.net/citygml/vegetation/3.0", "citygml/vegetation/3.0/vegetation.xsd"),
        ("http://www.opengis.net/citygml/cityfurniture/3.0", "citygml/cityfurniture/3.0/cityFurniture.xsd"),
        ("http://www.opengis.net/citygml/generics/3.0", "citygml/generics/3.0/generics.xsd"),
        ("http://www.opengis.net/citygml/relief/3.0", "citygml/relief/3.0/relief.xsd"),
        ("http://www.opengis.net/citygml/waterbody/3.0", "citygml/waterbody/3.0/waterBody.xsd"),
        ("http://www.opengis.net/citygml/landuse/3.0", "citygml/landuse/3.0/landUse.xsd"),
        ("http://www.opengis.net/citygml/cityobjectgroup/3.0", "citygml/cityobjectgroup/3.0/cityObjectGroup.xsd"),
        ("http://www.opengis.net/citygml/dynamizer/3.0", "citygml/dynamizer/3.0/dynamizer.xsd"),
        ("http://www.opengis.net/citygml/versioning/3.0", "citygml/versioning/3.0/versioning.xsd"),
        ("http://www.opengis.net/citygml/pointcloud/3.0", "citygml/pointcloud/3.0/pointCloud.xsd"),
    ],
    2: [
        ("http://www.opengis.net/gml", "gml/3.1.1/base/gml.xsd"),
        ("urn:oasis:names:tc:ciq:xsdschema:xAL:2.0", "citygml/xAL/xAL.xsd"),
        ("http://www.opengis.net/citygml/2.0", "citygml/2.0/cityGMLBase.xsd"),
        ("http://www.opengis.net/citygml/appearance/2.0", "citygml/appearance/2.0/appearance.xsd"),
        ("http://www.opengis.net/citygml/building/2.0", "citygml/building/2.0/building.xsd"),
        ("http://www.opengis.net/citygml/bridge/2.0", "citygml/bridge/2.0/bridge.xsd"),
        ("http://www.opengis.net/citygml/tunnel/2.0", "citygml/tunnel/2.0/tunnel.xsd"),
        ("http://www.opengis.net/citygml/transportation/2.0", "citygml/transportation/2.0/transportation.xsd"),
        ("http://www.opengis.net/citygml/vegetation/2.0", "citygml/vegetation/2.0/vegetation.xsd"),
        ("http://www.opengis.net/citygml/cityfurniture/2.0", "citygml/cityfurniture/2.0/cityFurniture.xsd"),
        ("http://www.opengis.net/citygml/generics/2.0", "citygml/generics/2.0/generics.xsd"),
        ("http://www.opengis.net/citygml/relief/2.0", "citygml/relief/2.0/relief.xsd"),
        ("http://www.opengis.net/citygml/waterbody/2.0", "citygml/waterbody/2.0/waterBody.xsd"),
        ("http://www.opengis.net/citygml/landuse/2.0", "citygml/landuse/2.0/landUse.xsd"),
        ("http://www.opengis.net/citygml/cityobjectgroup/2.0", "citygml/cityobjectgroup/2.0/cityObjectGroup.xsd"),
        ("http://www.opengis.net/citygml/texturedsurface/2.0", "citygml/texturedsurface/2.0/texturedSurface.xsd"),
    ],
}
MASTER_NAME = {3: "master_citygml3.xsd", 2: "master_citygml2.xsd"}

GML_IDS = ("{http://www.opengis.net/gml/3.2}id", "{http://www.opengis.net/gml}id")
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"


class OfflineResolutionError(RuntimeError):
    pass


# --------------------------------------------------------------------------- setup
def write_masters(schema_dir: Path) -> None:
    for ver, mods in MODULES.items():
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 f"<!-- Generated by validate_citygml.py: imports every CityGML {ver}.0 module from the",
                 f"     locally extracted OGC bundle ({BUNDLE}/). Remote imports inside the",
                 "     OGC schemas are redirected locally by the validator's resolver / catalog.xml. -->",
                 '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"',
                 f'           targetNamespace="urn:pykci:citygml{ver}-master" elementFormDefault="qualified">']
        for ns, rel in mods:
            p = schema_dir / BUNDLE / rel
            if not p.is_file():
                raise FileNotFoundError(f"missing schema {p} (run --setup)")
            lines.append(f'  <xs:import namespace="{ns}" schemaLocation="{BUNDLE}/{rel}"/>')
        lines.append("</xs:schema>\n")
        (schema_dir / MASTER_NAME[ver]).write_text("\n".join(lines), encoding="utf-8")
    cat = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<catalog xmlns="urn:oasis:names:tc:entity:xmlns:xml:catalog" prefer="system">']
    for prefix, rel in URL_MAP:
        target = (schema_dir / rel).resolve().as_uri() + ("/" if rel.endswith("/") else "")
        if prefix.endswith("/"):
            cat.append(f'  <rewriteURI uriStartString="{prefix}" rewritePrefix="{target}"/>')
            cat.append(f'  <rewriteSystem systemIdStartString="{prefix}" rewritePrefix="{target}"/>')
        else:
            cat.append(f'  <uri name="{prefix}" uri="{target}"/>')
            cat.append(f'  <system systemId="{prefix}" uri="{target}"/>')
    cat.append("</catalog>\n")
    (schema_dir / "catalog.xml").write_text("\n".join(cat), encoding="utf-8")


def setup(schema_dir: Path) -> None:
    """One-time download (the only function that touches the network)."""
    schema_dir.mkdir(parents=True, exist_ok=True)
    z = schema_dir / f"{BUNDLE}.zip"
    if not z.is_file():
        print(f"downloading {BUNDLE_URL} ...", file=sys.stderr)
        urllib.request.urlretrieve(BUNDLE_URL, z)
    out = schema_dir / BUNDLE
    if not (out / "citygml" / "3.0" / "core.xsd").is_file():
        with zipfile.ZipFile(z) as zf:
            for m in zf.namelist():
                if m.startswith("/") or ".." in Path(m).parts:
                    raise RuntimeError(f"unsafe path in zip: {m}")
            zf.extractall(out)
    for rel, url in W3C_FILES.items():
        p = schema_dir / rel
        if not p.is_file():
            p.parent.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(url, headers={"User-Agent": "pykci-schema-cache"})
            p.write_bytes(urllib.request.urlopen(req).read())
    write_masters(schema_dir)


# --------------------------------------------------------------------------- schema
class LocalResolver(etree.Resolver):
    def __init__(self, schema_dir: Path):
        super().__init__()
        self.schema_dir = schema_dir
        self.hits = collections.Counter()
        self.misses: list[str] = []

    def resolve(self, url, pubid, context):
        if not url:
            return None
        for prefix, rel in URL_MAP:
            if prefix.endswith("/"):
                if not url.startswith(prefix):
                    continue
                local = self.schema_dir / rel / url[len(prefix):]
            elif url == prefix:
                local = self.schema_dir / rel
            else:
                continue
            if not local.is_file():
                raise OfflineResolutionError(f"{url} maps to missing local file {local}")
            self.hits[prefix] += 1
            return self.resolve_filename(str(local), context)
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url) and not url.startswith("file:"):
            self.misses.append(url)
            raise OfflineResolutionError(f"refusing network access for unmapped URL {url}")
        return None  # plain relative/absolute file path -> libxml2 default handling


_SCHEMA_CACHE: dict[int, tuple[etree.XMLSchema, float]] = {}


def load_schema(version: int, schema_dir: Path) -> tuple[etree.XMLSchema, float]:
    if version in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[version]
    master = schema_dir / MASTER_NAME[version]
    if not master.is_file():
        write_masters(schema_dir)
    parser = etree.XMLParser(no_network=True, resolve_entities=False)
    resolver = LocalResolver(schema_dir)
    parser.resolvers.add(resolver)
    t0 = time.perf_counter()
    try:
        schema = etree.XMLSchema(etree.parse(str(master), parser))
    except etree.XMLSchemaParseError as e:
        extra = f"\nunmapped remote URLs (not fetched): {resolver.misses}" if resolver.misses else ""
        raise SystemExit(f"schema compilation failed for {master}:\n{e}{extra}")
    dt = time.perf_counter() - t0
    _SCHEMA_CACHE[version] = (schema, dt)
    return schema, dt


# --------------------------------------------------------------------------- helpers
def detect_version(root) -> int:
    seen = set()
    for el in root.iter():
        if isinstance(el.tag, str):
            ns = etree.QName(el).namespace or ""
            m = re.match(r"^http://www\.opengis\.net/citygml/(?:[a-z]+/)?(\d)\.0$", ns)
            if m:
                seen.add(int(m.group(1)))
    for ns in root.nsmap.values():
        m = re.match(r"^http://www\.opengis\.net/citygml/(?:[a-z]+/)?(\d)\.0$", ns or "")
        if m:
            seen.add(int(m.group(1)))
    if seen == {3}:
        return 3
    if seen == {2}:
        return 2
    if not seen:
        raise SystemExit("cannot auto-detect CityGML version (no CityGML namespace); use --version")
    if seen == {1}:
        raise SystemExit("CityGML 1.0 detected - not supported by this validator")
    raise SystemExit(f"mixed CityGML versions in one document: {sorted(seen)}; use --version")


WELL_KNOWN_PREFIX = {"http://www.opengis.net/citygml/3.0": "core", "http://www.opengis.net/citygml/2.0": "core",
                     "http://www.opengis.net/gml/3.2": "gml", "http://www.opengis.net/gml": "gml",
                     "http://www.w3.org/1999/xlink": "xlink"}


def prettify(msg: str, ns2pfx: dict[str, str]) -> str:
    def sub(m):
        ns = m.group(1)
        p = ns2pfx.get(ns) or WELL_KNOWN_PREFIX.get(ns)
        if p is None:
            mm = re.match(r"^http://www\.opengis\.net/citygml/([a-z]+)/\d\.0$", ns)
            p = mm.group(1) if mm else None
        return f"{p}:" if p else m.group(0)
    return re.sub(r"\{([^}]*)\}", sub, msg)


def pattern(msg: str) -> str:
    """Mask literal values so that e.g. 1000 'not a valid xs:ID' errors collapse into one group.
    Quoted tokens that look like names (contain ':' or '{', or are XSD type names) are kept."""
    def q(m):
        tok = m.group(1)
        if ":" in tok or "{" in tok or re.match(r"^[A-Za-z_]+Type$", tok):
            return m.group(0)
        return "'<v>'"
    msg = re.sub(r"'([^']*)'", q, msg)
    return re.sub(r"(?<![\w.:<])-?\d+(\.\d+)?(?![\w.])", "<n>", msg)


def check_refs(root) -> dict:
    ids = collections.Counter()
    for el in root.iter():
        if isinstance(el.tag, str):
            for a in GML_IDS:
                v = el.get(a)
                if v is not None:
                    ids[v] += 1
    dup = sorted(k for k, v in ids.items() if v > 1)
    local = external = 0
    broken = []
    for el in root.iter():
        if isinstance(el.tag, str):
            h = el.get(XLINK_HREF)
            if h is None:
                continue
            if h.startswith("#"):
                local += 1
                if h[1:] not in ids:
                    broken.append((el.sourceline, h))
            else:
                external += 1
    # Appearance targets are URIs in element text (2.0 + 3.0) or in @uri (2.0 ParameterizedTexture)
    app_targets = 0
    for el in root.iter():
        if isinstance(el.tag, str) and etree.QName(el).localname == "target" \
                and "/appearance/" in (etree.QName(el).namespace or ""):
            for v in (el.get("uri"), (el.text or "").strip()):
                if v and v.startswith("#"):
                    app_targets += 1
                    if v[1:] not in ids:
                        broken.append((el.sourceline, v))
    return {"gml_ids": sum(ids.values()), "duplicate_ids": dup, "local_hrefs": local,
            "appearance_targets": app_targets,
            "external_hrefs_not_checked": external, "broken_local_hrefs": broken}


# --------------------------------------------------------------------------- main
def validate_file(path: Path, args, schema_dir: Path) -> dict:
    res: dict = {"file": str(path)}
    t0 = time.perf_counter()
    parser = etree.XMLParser(huge_tree=True, no_network=True, resolve_entities=False, remove_blank_text=False)
    doc = etree.parse(str(path), parser)
    root = doc.getroot()
    res["parse_s"] = round(time.perf_counter() - t0, 3)
    res["n_elements"] = sum(1 for e in root.iter() if isinstance(e.tag, str))
    if args.version:
        version, how = int(args.version), "--version"
    else:
        version, how = detect_version(root), "auto-detected from namespaces"
    res["version"], res["version_source"] = version, how
    schema, compile_s = load_schema(version, schema_dir)
    res["schema"] = str(schema_dir / MASTER_NAME[version])
    res["compile_s"] = round(compile_s, 3)
    t1 = time.perf_counter()
    ok = schema.validate(doc)
    res["validate_s"] = round(time.perf_counter() - t1, 3)
    ns2pfx = {}
    for el in root.iter():
        if isinstance(el.tag, str):
            for p, u in el.nsmap.items():
                if p:
                    ns2pfx.setdefault(u, p)
    groups: dict[str, dict] = {}
    for e in schema.error_log:
        msg = e.message if args.raw_names else prettify(e.message, ns2pfx)
        key = pattern(msg) if args.group_by == "pattern" else msg
        g = groups.setdefault(key, {"message": key, "count": 0, "lines": [], "level": e.level_name})
        g["count"] += 1
        g["lines"].append(e.line)
    res["schema_valid"] = bool(ok)
    res["n_errors"] = sum(g["count"] for g in groups.values())
    res["groups"] = sorted(groups.values(), key=lambda g: (-g["count"], g["lines"][0]))
    if not args.no_refs:
        res["refs"] = check_refs(root)
    return res


def print_report(res: dict, max_lines: int) -> None:
    print(f"File      : {res['file']}")
    print(f"Elements  : {res['n_elements']:,}  (parsed in {res['parse_s']} s)")
    print(f"CityGML   : {res['version']}.0 ({res['version_source']})")
    print(f"Schema    : {res['schema']}  (compiled in {res['compile_s']} s; validation {res['validate_s']} s)")
    if res["schema_valid"]:
        print("XSD result: VALID")
    else:
        print(f"XSD result: INVALID - {res['n_errors']} error(s), {len(res['groups'])} distinct message(s)")
        for g in res["groups"]:
            lines = sorted(set(g["lines"]))
            shown = ", ".join(str(x) for x in lines[:max_lines]) + (" ..." if len(lines) > max_lines else "")
            print(f"  [{g['count']:>6}x] {g['message']}")
            print(f"            first line(s): {shown}")
    if "refs" in res:
        r = res["refs"]
        print(f"Refs      : {r['gml_ids']:,} gml:id, {len(r['duplicate_ids'])} duplicate(s); "
              f"{r['local_hrefs']:,} local xlink:href + {r['appearance_targets']:,} app:target, "
              f"{len(r['broken_local_hrefs'])} unresolved; "
              f"{r['external_hrefs_not_checked']} external xlink:href (not checked)")
        for d in r["duplicate_ids"][:max_lines]:
            print(f"    duplicate gml:id: {d}")
        for line, h in r["broken_local_hrefs"][:max_lines]:
            print(f"    unresolved reference {h} (line {line})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path, help="CityGML file(s) to validate")
    ap.add_argument("--version", choices=["2", "3"], help="force CityGML version (default: auto-detect)")
    ap.add_argument("--schema-dir", type=Path,
                    default=Path(os.environ.get("CITYGML_SCHEMA_DIR", DEFAULT_SCHEMA_DIR)))
    ap.add_argument("--max-lines", type=int, default=10, help="line numbers shown per error group (default 10)")
    ap.add_argument("--group-by", choices=["message", "pattern"], default="message",
                    help="group by exact message, or by message with literal values masked")
    ap.add_argument("--raw-names", action="store_true", help="keep {namespace}local names in messages")
    ap.add_argument("--no-refs", action="store_true", help="skip duplicate gml:id / unresolved #xlink check")
    ap.add_argument("--json", type=Path, help="also write the full result as JSON")
    ap.add_argument("--setup", action="store_true", help="download OGC bundle + W3C xsds (network!), build masters")
    ap.add_argument("--write-masters", action="store_true", help="(re)build master XSDs and catalog.xml")
    args = ap.parse_args(argv)
    sd = args.schema_dir.resolve()
    if args.setup:
        setup(sd)
        print(f"schema cache ready in {sd}")
    if args.write_masters:
        write_masters(sd)
        print(f"wrote {sd / MASTER_NAME[3]}, {sd / MASTER_NAME[2]}, {sd / 'catalog.xml'}")
    if not args.files:
        if args.setup or args.write_masters:
            return 0
        ap.error("no input files")
    results, rc = [], 0
    for f in args.files:
        try:
            res = validate_file(f, args, sd)
        except (etree.XMLSyntaxError, OSError) as e:
            print(f"File      : {f}\nERROR     : {e}")
            rc = max(rc, 2)
            continue
        print_report(res, args.max_lines)
        print()
        results.append(res)
        bad_refs = "refs" in res and (res["refs"]["duplicate_ids"] or res["refs"]["broken_local_hrefs"])
        if not res["schema_valid"] or bad_refs:
            rc = max(rc, 1)
    if args.json:
        args.json.write_text(json.dumps(results if len(results) != 1 else results[0], indent=1, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):   # a cp1252 console must not crash on non-ASCII output
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(errors="replace")
    sys.exit(main())
