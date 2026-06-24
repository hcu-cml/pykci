#!/usr/bin/env bash
# Run the 20 benchmark queries against 3DCityKG's embedded Neo4j (citygml4j schema).
# Query container 'citykg_query' must be running (no auth). Output: results_citykg.txt
OUT="$(dirname "$0")/results_citykg.txt"; : > "$OUT"
TMP="$(mktemp)"
runq(){ local label="$1"; cat > "$TMP"
  echo "==== $label ====" | tee -a "$OUT"
  sed 's/^/CYPHER: /' "$TMP" >> "$OUT"
  local t0 t1 res; t0=$(date +%s.%N)
  res=$(docker exec -i citykg_query cypher-shell --format plain < "$TMP" 2>&1); t1=$(date +%s.%N)
  awk "BEGIN{printf \"-- exec(incl shell): %.3fs --\n\", $t1-$t0}" | tee -a "$OUT"
  echo "$res" | head -26 | tee -a "$OUT"; echo | tee -a "$OUT"; }

# B=Building, SA=StringAttribute, Code labels (full citygml4j class names)
runq "C1 Hamburg at a glance" <<'EOF'
MATCH (b:`org.citygml4j.model.citygml.building.Building`)
OPTIONAL MATCH (b)-[:measuredHeight]->(mh)
RETURN count(b) AS buildings, round(avg(toFloat(mh.value)),1) AS avg_h,
       round(avg(toInteger(b.storeysAboveGround)),1) AS avg_storeys;
EOF

runq "C2 Most common roof material (+pct)" <<'EOF'
MATCH (b:`org.citygml4j.model.citygml.building.Building`)-[:genericAttribute]->()-[*1..3]->(sa:`org.citygml4j.model.citygml.generics.StringAttribute`)
WHERE sa.name='PredictedRoofMaterial'
WITH sa.value AS m, count(*) AS n WITH collect({m:m,n:n}) AS rows, sum(n) AS tot
UNWIND rows AS r RETURN r.m AS material, r.n AS n, round(100.0*r.n/tot,1) AS pct ORDER BY n DESC;
EOF

runq "C3 20 tallest buildings" <<'EOF'
MATCH (b:`org.citygml4j.model.citygml.building.Building`)-[:measuredHeight]->(mh)
WITH b, toFloat(mh.value) AS height ORDER BY height DESC LIMIT 20
OPTIONAL MATCH (b)-[:genericAttribute]->()-[*1..3]->(sa:`org.citygml4j.model.citygml.generics.StringAttribute`)
WHERE sa.name='PredictedRoofMaterial'
RETURN b.id AS id, height, sa.value AS material;
EOF

runq "C4 Roof shapes popularity" <<'EOF'
MATCH (b:`org.citygml4j.model.citygml.building.Building`)-[:roofType]->(c)
WITH c.value AS code, count(*) AS n
RETURN code, CASE code WHEN '1000' THEN 'Flat' WHEN '2100' THEN 'Mono-Pitch' WHEN '2200' THEN 'Offset Mono-Pitch'
  WHEN '3100' THEN 'Gable' WHEN '3200' THEN 'Hip' WHEN '3300' THEN 'Half-Hip' WHEN '3400' THEN 'Mansard'
  WHEN '3500' THEN 'Tent/Pyramid' WHEN '4000' THEN 'Tower' WHEN '5000' THEN 'Mixed' ELSE 'Other' END AS shape, n
ORDER BY n DESC;
EOF

runq "C5 Solar: flat roofs >5m (count)" <<'EOF'
MATCH (b:`org.citygml4j.model.citygml.building.Building`)-[:roofType]->(c) WHERE c.value='1000'
MATCH (b)-[:measuredHeight]->(mh) WHERE toFloat(mh.value)>5
RETURN count(DISTINCT b) AS flat_over_5m;
EOF

runq "C6 Buildings per storey count" <<'EOF'
MATCH (b:`org.citygml4j.model.citygml.building.Building`)
RETURN toInteger(b.storeysAboveGround) AS storeys, count(*) AS n ORDER BY storeys;
EOF

runq "C7 Glass roofs (count)" <<'EOF'
MATCH (b:`org.citygml4j.model.citygml.building.Building`)-[:genericAttribute]->()-[*1..3]->(sa:`org.citygml4j.model.citygml.generics.StringAttribute`)
WHERE sa.name='PredictedRoofMaterial' AND sa.value='glass'
RETURN count(DISTINCT b) AS glass_roofs;
EOF

runq "C8 High-rises > 50 m" <<'EOF'
MATCH (b:`org.citygml4j.model.citygml.building.Building`)-[:measuredHeight]->(mh)
WHERE toFloat(mh.value)>50 RETURN count(b) AS over_50m;
EOF

runq "C9 Mixed roof materials (FEASIBILITY)" <<'EOF'
MATCH (b:`org.citygml4j.model.citygml.building.Building`)-[:genericAttribute]->()-[*1..3]->(sa:`org.citygml4j.model.citygml.generics.StringAttribute`)
WHERE sa.name='PredictedRoofMaterial'
WITH b, count(sa) AS c RETURN max(c) AS max_materials_per_building, count(b) AS buildings_with_material;
EOF

runq "C10 HafenCity bbox (envelope centre)" <<'EOF'
MATCH (b:`org.citygml4j.model.citygml.building.Building`)-[:boundedBy]->()-[:envelope]->(env)
MATCH (env)-[:lowerCorner]->()-[:value]->()-[:elementData]->(lo)
MATCH (env)-[:upperCorner]->()-[:value]->()-[:elementData]->(hi)
WITH b, (toFloat(lo.`ARRAY_MEMBER[0]`)+toFloat(hi.`ARRAY_MEMBER[0]`))/2 AS cx,
        (toFloat(lo.`ARRAY_MEMBER[1]`)+toFloat(hi.`ARRAY_MEMBER[1]`))/2 AS cy
WHERE cx>=565000 AND cx<=567000 AND cy>=5933000 AND cy<=5934000
RETURN count(b) AS hafencity_buildings;
EOF

runq "E1 Typology by function code" <<'EOF'
MATCH (b:`org.citygml4j.model.citygml.building.Building`)-[:function]->()-[*1..2]->(c:`org.citygml4j.model.gml.basicTypes.Code`)
OPTIONAL MATCH (b)-[:measuredHeight]->(mh)
WITH c.value AS fn, toFloat(mh.value) AS h, toInteger(b.storeysAboveGround) AS st
RETURN fn, count(*) AS n, round(avg(h),1) AS avg_h, round(avg(st),1) AS avg_storeys ORDER BY n DESC LIMIT 15;
EOF

runq "E2 1km grid: density/height/dominant material (>=30)" <<'EOF'
MATCH (b:`org.citygml4j.model.citygml.building.Building`)-[:boundedBy]->()-[:envelope]->(env)
MATCH (env)-[:lowerCorner]->()-[:value]->()-[:elementData]->(lo)
MATCH (env)-[:upperCorner]->()-[:value]->()-[:elementData]->(hi)
OPTIONAL MATCH (b)-[:measuredHeight]->(mh)
OPTIONAL MATCH (b)-[:genericAttribute]->()-[*1..3]->(sa:`org.citygml4j.model.citygml.generics.StringAttribute`)
WHERE sa.name='PredictedRoofMaterial'
WITH toInteger(floor(((toFloat(lo.`ARRAY_MEMBER[0]`)+toFloat(hi.`ARRAY_MEMBER[0]`))/2)/1000.0)) AS gx,
     toInteger(floor(((toFloat(lo.`ARRAY_MEMBER[1]`)+toFloat(hi.`ARRAY_MEMBER[1]`))/2)/1000.0)) AS gy,
     b, toFloat(mh.value) AS h, sa.value AS mat
WITH gx, gy, count(b) AS buildings, round(avg(h),1) AS avg_h, [m IN collect(mat) WHERE m IS NOT NULL] AS mats
WHERE buildings>=30
WITH gx, gy, buildings, avg_h, apoc.coll.frequencies(mats) AS freq
RETURN gx AS x_km, gy AS y_km, buildings, avg_h,
       CASE WHEN size(freq)=0 THEN 'n/a' ELSE apoc.coll.sortMaps(freq,'count')[0].item END AS dominant_material
ORDER BY buildings DESC LIMIT 15;
EOF

runq "E3 Solar potential score 0-7 (ranked)" <<'EOF'
MATCH (b:`org.citygml4j.model.citygml.building.Building`)
OPTIONAL MATCH (b)-[:roofType]->(c)
OPTIONAL MATCH (b)-[:measuredHeight]->(mh)
OPTIONAL MATCH (b)-[:genericAttribute]->()-[*1..3]->(sa:`org.citygml4j.model.citygml.generics.StringAttribute`)
WHERE sa.name='PredictedRoofMaterial'
WITH b, c.value AS roof, toFloat(mh.value) AS h, toInteger(b.storeysAboveGround) AS st, sa.value AS mat
WITH b, roof, h, mat,
  (CASE roof WHEN '1000' THEN 3 WHEN '2100' THEN 1 ELSE 0 END) +
  (CASE mat WHEN 'concrete' THEN 2 WHEN 'tar_paper' THEN 2 WHEN 'metal' THEN 2 WHEN 'roof_tiles' THEN 1 ELSE 0 END) +
  (CASE WHEN h>10 THEN 1 ELSE 0 END) + (CASE WHEN st>1 THEN 1 ELSE 0 END) AS score
RETURN b.id AS id, score, roof, mat AS material, h ORDER BY score DESC, h DESC LIMIT 15;
EOF

runq "E4 Floor-to-floor anomaly (<2 or >8 m) count" <<'EOF'
MATCH (b:`org.citygml4j.model.citygml.building.Building`)-[:measuredHeight]->(mh)
WITH b, toFloat(mh.value) AS h, toInteger(b.storeysAboveGround) AS st
WHERE st>0 AND h IS NOT NULL
WITH b, h/st AS ffh WHERE ffh<2 OR ffh>8
RETURN count(b) AS anomalies;
EOF

runq "E5 Roof material x function code (>=500)" <<'EOF'
MATCH (b:`org.citygml4j.model.citygml.building.Building`)-[:function]->()-[*1..2]->(c:`org.citygml4j.model.gml.basicTypes.Code`)
MATCH (b)-[:genericAttribute]->()-[*1..3]->(sa:`org.citygml4j.model.citygml.generics.StringAttribute`)
WHERE sa.name='PredictedRoofMaterial'
WITH c.value AS fn, sa.value AS mat
WITH fn, count(*) AS type_total, collect(mat) AS mats WHERE type_total>=500
UNWIND ['roof_tiles','tar_paper','concrete','metal','glass'] AS m
RETURN fn, type_total, m AS material, round(100.0*size([x IN mats WHERE x=m])/type_total,1) AS pct
ORDER BY fn, pct DESC;
EOF

runq "E6 Heat-island: dark-roof pct per 2km cell (>=50)" <<'EOF'
MATCH (b:`org.citygml4j.model.citygml.building.Building`)-[:genericAttribute]->()-[*1..3]->(sa:`org.citygml4j.model.citygml.generics.StringAttribute`)
WHERE sa.name='PredictedRoofMaterial'
MATCH (b)-[:boundedBy]->()-[:envelope]->(env)
MATCH (env)-[:lowerCorner]->()-[:value]->()-[:elementData]->(lo)
MATCH (env)-[:upperCorner]->()-[:value]->()-[:elementData]->(hi)
WITH toInteger(floor(((toFloat(lo.`ARRAY_MEMBER[0]`)+toFloat(hi.`ARRAY_MEMBER[0]`))/2)/2000.0)) AS gx,
     toInteger(floor(((toFloat(lo.`ARRAY_MEMBER[1]`)+toFloat(hi.`ARRAY_MEMBER[1]`))/2)/2000.0)) AS gy,
     sa.value AS mat
WITH gx, gy, count(*) AS withmat, sum(CASE WHEN mat IN ['tar_paper','metal'] THEN 1 ELSE 0 END) AS dark
WHERE withmat>=50
RETURN gx AS x_2km, gy AS y_2km, withmat, dark, round(100.0*dark/withmat,1) AS dark_pct ORDER BY dark_pct DESC LIMIT 15;
EOF

runq "E7 Material coverage completeness (FEASIBILITY)" <<'EOF'
MATCH (b:`org.citygml4j.model.citygml.building.Building`)-[:genericAttribute]->()-[*1..3]->(sa:`org.citygml4j.model.citygml.generics.StringAttribute`)
WITH DISTINCT sa.name AS attr_name RETURN collect(attr_name) AS all_generic_attribute_names;
EOF

runq "E8 High-rise clusters: 500m cells, >30m, >=2" <<'EOF'
MATCH (b:`org.citygml4j.model.citygml.building.Building`)-[:measuredHeight]->(mh) WHERE toFloat(mh.value)>30
MATCH (b)-[:boundedBy]->()-[:envelope]->(env)
MATCH (env)-[:lowerCorner]->()-[:value]->()-[:elementData]->(lo)
MATCH (env)-[:upperCorner]->()-[:value]->()-[:elementData]->(hi)
WITH toInteger(floor(((toFloat(lo.`ARRAY_MEMBER[0]`)+toFloat(hi.`ARRAY_MEMBER[0]`))/2)/500.0)) AS gx,
     toInteger(floor(((toFloat(lo.`ARRAY_MEMBER[1]`)+toFloat(hi.`ARRAY_MEMBER[1]`))/2)/500.0)) AS gy,
     toFloat(mh.value) AS h, b
WITH gx, gy, count(*) AS tall, round(avg(h),1) AS avg_h, round(max(h),1) AS max_h, collect(b.id)[0..3] AS sample
WHERE tall>=2
RETURN gx AS x_500m, gy AS y_500m, tall, avg_h, max_h, sample ORDER BY tall DESC LIMIT 15;
EOF

runq "E9 FAR proxy per 1km cell (>=30)" <<'EOF'
MATCH (b:`org.citygml4j.model.citygml.building.Building`)-[:boundedBy]->()-[:envelope]->(env)
MATCH (env)-[:lowerCorner]->()-[:value]->()-[:elementData]->(lo)
MATCH (env)-[:upperCorner]->()-[:value]->()-[:elementData]->(hi)
WITH b, toFloat(lo.`ARRAY_MEMBER[0]`) AS minx, toFloat(lo.`ARRAY_MEMBER[1]`) AS miny,
        toFloat(hi.`ARRAY_MEMBER[0]`) AS maxx, toFloat(hi.`ARRAY_MEMBER[1]`) AS maxy,
        toInteger(b.storeysAboveGround) AS st
WITH toInteger(floor(((minx+maxx)/2)/1000.0)) AS gx, toInteger(floor(((miny+maxy)/2)/1000.0)) AS gy,
     (maxx-minx)*(maxy-miny)*st AS gfa
WITH gx, gy, count(*) AS buildings, sum(gfa) AS gfa_sum WHERE buildings>=30
RETURN gx AS x_km, gy AS y_km, buildings, round(gfa_sum) AS gfa_m2, round(gfa_sum/1000000.0,3) AS far
ORDER BY far DESC LIMIT 15;
EOF

runq "E10 Ground elevation zones (lowerCorner z)" <<'EOF'
MATCH (b:`org.citygml4j.model.citygml.building.Building`)-[:boundedBy]->()-[:envelope]->(env)
MATCH (env)-[:lowerCorner]->()-[:value]->()-[:elementData]->(lo)
OPTIONAL MATCH (b)-[:measuredHeight]->(mh)
WITH toFloat(lo.`ARRAY_MEMBER[2]`) AS gz, toFloat(mh.value) AS h
WITH CASE WHEN gz<0 THEN '1 below sea (<0m)' WHEN gz<5 THEN '2 low (0-5m)' WHEN gz<20 THEN '3 mid (5-20m)'
          WHEN gz<50 THEN '4 elevated (20-50m)' ELSE '5 high (>50m)' END AS zone, gz, h
RETURN zone, count(*) AS buildings, round(avg(h),1) AS avg_building_h, round(min(gz),1) AS min_gz, round(max(gz),1) AS max_gz
ORDER BY zone;
EOF

rm -f "$TMP"; echo "ALL DONE -> $OUT"
