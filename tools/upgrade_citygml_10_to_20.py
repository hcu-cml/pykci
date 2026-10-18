#!/usr/bin/env python3
"""
upgrade_citygml_10_to_20.py -- non-destructively rewrite CityGML 1.0 namespace
URIs to their 2.0 equivalents so files parse through the 2.0-bound ingest.

Only the namespace/schema-location version token changes
(``.../citygml[/module]/1.0`` -> ``.../citygml[/module]/2.0``); the element
structure of the CityGML *building* module is identical between 1.0 and 2.0 for
LoD0-2 building files (verified: same local-name tree, only the namespace URI
version differs), so this is a lossless upgrade for such files. Originals are
never modified; upgraded copies are written to --out with the same basenames.
Files already on 2.0 are copied verbatim (or hardlinked) so the output dir is a
complete, self-contained input set.

Usage:
    py tools/upgrade_citygml_10_to_20.py <file.gml | dir ...> --out <dir> [--link]
"""
import argparse
import os
import re
import shutil
import sys
from pathlib import Path

# opengis.net/citygml/1.0  and  opengis.net/citygml/<module>/1.0  ->  .../2.0
_NS_10 = re.compile(rb"(opengis\.net/citygml(?:/[a-z]+)?)/1\.0")


def _iter_inputs(paths):
    for p in paths:
        p = Path(p)
        if p.is_dir():
            yield from sorted(p.glob("*.gml"))
        elif p.is_file():
            yield p


def _is_citygml_10(head: bytes) -> bool:
    return b"opengis.net/citygml/1.0" in head or re.search(
        rb"opengis\.net/citygml/[a-z]+/1\.0", head
    ) is not None


def main():
    ap = argparse.ArgumentParser(description="Upgrade CityGML 1.0 namespaces to 2.0.")
    ap.add_argument("inputs", nargs="+", help="CityGML file(s) and/or directory(ies)")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--link", action="store_true",
                    help="hardlink already-2.0 files instead of copying")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    upgraded = copied = 0
    for src in _iter_inputs(args.inputs):
        dst = out / src.name
        with open(src, "rb") as fh:
            head = fh.read(65536)
        if _is_citygml_10(head):
            with open(src, "rb") as fh:
                data = fh.read()
            new = _NS_10.sub(rb"\1/2.0", data)
            with open(dst, "wb") as fh:
                fh.write(new)
            upgraded += 1
            print(f"  upgraded 1.0 -> 2.0: {src.name} ({len(data)/1e9:.2f} GB)")
        else:
            if dst.exists():
                dst.unlink()
            if args.link:
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copy2(src, dst)
            else:
                shutil.copy2(src, dst)
            copied += 1
            print(f"  kept 2.0 as-is:      {src.name}")
    print(f"Done. {upgraded} upgraded, {copied} kept -> {out}")


if __name__ == "__main__":
    main()
