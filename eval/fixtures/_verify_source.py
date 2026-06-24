"""Non-destructive census round-trip for ANY source GML, alongside the existing
graph: ingest as its own dataset, export only that dataset, census, then remove.
Usage: _verify_source.py <path-to.gml>"""
import subprocess, sys
from pathlib import Path
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
GML = Path(sys.argv[1]).resolve()
SRC = GML.name
EXP = ROOT / "output/citygml" / (GML.stem + "_export.gml")

subprocess.run([sys.executable, "ingest_citygml.py", str(GML)], cwd=ROOT, check=True,
               stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

import export_citygml as ex
ex.query_dataset = lambda sess: dict(
    sess.run("MATCH (d:Dataset {source:$s}) RETURN d LIMIT 1", s=SRC).single()["d"])
ex.export(EXP)

r = subprocess.run([sys.executable, "eval/roundtrip_census.py",
                    "--gml", str(GML), "--export", str(EXP), "--show", "60"], cwd=ROOT)

drv = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "neo4jneo4j"))
with drv.session() as s:
    s.run("MATCH (n {source:$s}) DETACH DELETE n", s=SRC)
drv.close()
print("\n[cleanup] fixture nodes removed; working graph untouched.")
sys.exit(r.returncode)
