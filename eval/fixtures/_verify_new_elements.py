"""Non-destructive round-trip check for the newly mapped Building elements.
Ingests eval/fixtures/new_elements_test.gml as its own dataset, exports ONLY that
dataset, runs the census gate, then removes the test nodes so the working graph
is left untouched."""
import subprocess, sys
from pathlib import Path
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
SRC = "new_elements_test.gml"
GML = ROOT / "eval/fixtures/new_elements_test.gml"
EXP = ROOT / "output/citygml/new_elements_test_export.gml"

# 1. Ingest the fixture (adds a separate Dataset; idempotent MERGE)
subprocess.run([sys.executable, "ingest_citygml.py", str(GML)], cwd=ROOT, check=True,
               stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

# 2. Export ONLY the fixture dataset (monkeypatch query_dataset → pick by source)
import export_citygml as ex

def _ds_by_source(sess):
    row = sess.run("MATCH (d:Dataset {source:$s}) RETURN d LIMIT 1", s=SRC).single()
    return dict(row["d"])

ex.query_dataset = _ds_by_source
ex.export(EXP)

# 3. Census gate
r = subprocess.run([sys.executable, "eval/roundtrip_census.py",
                    "--gml", str(GML), "--export", str(EXP), "--show", "80"],
                   cwd=ROOT)

# 4. Cleanup — remove every node sourced from the fixture
drv = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "neo4jneo4j"))
with drv.session() as s:
    s.run("MATCH (n {source:$s}) DETACH DELETE n", s=SRC)
    s.run("MATCH (n:GeometryRing {source:$s}) DETACH DELETE n", s=SRC)
drv.close()
print("\n[cleanup] fixture nodes removed; working graph untouched.")
sys.exit(r.returncode)
