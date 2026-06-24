#!/usr/bin/env bash
# Run the 20 benchmark queries against pykci's Neo4j and time each.
# Output: results_pykci.txt
CS="docker exec neo4j cypher-shell -u neo4j -p neo4jneo4j --format plain"
OUT="$(dirname "$0")/results_pykci.txt"
: > "$OUT"
run(){ echo "==== $1 ====" | tee -a "$OUT"; echo "CYPHER: $2" | tee -a "$OUT"
  t0=$(date +%s.%N); res=$($CS "$2" 2>&1); t1=$(date +%s.%N)
  el=$(awk "BEGIN{printf \"%.3f\", $t1-$t0}")
  echo "-- exec(incl shell): ${el}s --" | tee -a "$OUT"
  echo "$res" | head -28 | tee -a "$OUT"; echo | tee -a "$OUT"; }

# ---------------- CITIZEN ----------------
run "C1 Hamburg at a glance" \
"MATCH (b:Building) RETURN count(b) AS buildings, round(avg(b.measured_height),1) AS avg_height_m, round(avg(b.storeys_above_ground),1) AS avg_storeys;"

run "C2 Most common roof material (+ pct)" \
"MATCH (b:Building) WHERE b.predictedroofmaterial IS NOT NULL WITH b.predictedroofmaterial AS m, count(*) AS n WITH collect({m:m,n:n}) AS rows, sum(n) AS tot UNWIND rows AS r RETURN r.m AS material, r.n AS n, round(100.0*r.n/tot,1) AS pct ORDER BY n DESC;"

run "C3 20 tallest buildings" \
"MATCH (b:Building) WHERE b.measured_height IS NOT NULL RETURN b.id AS id, b.measured_height AS height_m, b.function_code AS fn, b.predictedroofmaterial AS material ORDER BY b.measured_height DESC LIMIT 20;"

run "C4 Roof shapes popularity" \
"MATCH (b:Building) WHERE b.roof_type_code IS NOT NULL WITH b.roof_type_code AS code, count(*) AS n RETURN code, CASE code WHEN '1000' THEN 'Flat' WHEN '2100' THEN 'Mono-Pitch' WHEN '2200' THEN 'Offset Mono-Pitch' WHEN '3100' THEN 'Gable' WHEN '3200' THEN 'Hip' WHEN '3300' THEN 'Half-Hip' WHEN '3400' THEN 'Mansard' WHEN '3500' THEN 'Tent/Pyramid' WHEN '4000' THEN 'Tower' WHEN '5000' THEN 'Mixed' ELSE 'Other' END AS roof_shape, n ORDER BY n DESC;"

run "C5 Solar: flat roofs >5m (ranked)" \
"MATCH (b:Building) WHERE b.roof_type_code='1000' AND b.measured_height > 5 RETURN count(b) AS flat_over_5m;"
run "C5b Solar: top 15 flat roofs by height" \
"MATCH (b:Building) WHERE b.roof_type_code='1000' AND b.measured_height > 5 RETURN b.id AS id, b.measured_height AS height_m, b.predictedroofmaterial AS material ORDER BY b.measured_height DESC LIMIT 15;"

run "C6 Buildings per storey count" \
"MATCH (b:Building) WHERE b.storeys_above_ground IS NOT NULL RETURN b.storeys_above_ground AS storeys, count(*) AS n ORDER BY storeys;"

run "C7 Glass roofs (count + sample)" \
"MATCH (b:Building) WHERE b.predictedroofmaterial='glass' RETURN count(b) AS glass_roofs;"
run "C7b Glass roofs sample" \
"MATCH (b:Building) WHERE b.predictedroofmaterial='glass' RETURN b.id AS id, b.measured_height AS h, b.function_code AS fn ORDER BY b.measured_height DESC LIMIT 10;"

run "C8 High-rises > 50 m" \
"MATCH (b:Building) WHERE b.measured_height > 50 RETURN count(b) AS over_50m;"

run "C9 Mixed roof materials (FEASIBILITY CHECK)" \
"MATCH (b:Building) WHERE b.predictedroofmaterial IS NOT NULL AND (b.predictedroofmaterial CONTAINS ';' OR b.predictedroofmaterial CONTAINS ',') RETURN count(b) AS buildings_with_multiple_materials;"

run "C10 HafenCity bbox (≈565000-567000 E, 5933000-5934000 N)" \
"MATCH (b:Building) WHERE b.center_x>=565000 AND b.center_x<=567000 AND b.center_y>=5933000 AND b.center_y<=5934000 RETURN count(b) AS buildings, round(avg(b.measured_height),1) AS avg_h, count(b.predictedroofmaterial) AS with_material;"

# ---------------- EXPERT ----------------
run "E1 Typology: height/storey per use type" \
"MATCH (b:Building) WHERE b.description IS NOT NULL WITH split(b.description, ',')[0] AS use_type, b RETURN use_type, count(*) AS n, round(avg(b.measured_height),1) AS avg_h, round(avg(b.storeys_above_ground),1) AS avg_storeys ORDER BY n DESC LIMIT 15;"

run "E2 1km grid: density/height/dominant material (>=30)" \
"MATCH (b:Building) WHERE b.center_x IS NOT NULL WITH toInteger(floor(b.center_x/1000.0)) AS gx, toInteger(floor(b.center_y/1000.0)) AS gy, b WITH gx, gy, count(b) AS buildings, round(avg(b.measured_height),1) AS avg_h, [m IN collect(b.predictedroofmaterial) WHERE m IS NOT NULL] AS mats WHERE buildings>=30 WITH gx, gy, buildings, avg_h, apoc.coll.frequencies(mats) AS freq WITH gx, gy, buildings, avg_h, CASE WHEN size(freq)=0 THEN 'n/a' ELSE apoc.coll.sortMaps(freq,'count')[0].item END AS dominant_material RETURN gx AS x_km, gy AS y_km, buildings, avg_h, dominant_material ORDER BY buildings DESC LIMIT 15;"

run "E3 Solar potential score 0-7 (ranked)" \
"MATCH (b:Building) WITH b, (CASE b.roof_type_code WHEN '1000' THEN 3 WHEN '2100' THEN 1 ELSE 0 END) + (CASE b.predictedroofmaterial WHEN 'concrete' THEN 2 WHEN 'tar_paper' THEN 2 WHEN 'metal' THEN 2 WHEN 'roof_tiles' THEN 1 ELSE 0 END) + (CASE WHEN b.measured_height>10 THEN 1 ELSE 0 END) + (CASE WHEN b.storeys_above_ground>1 THEN 1 ELSE 0 END) AS score RETURN b.id AS id, score, b.roof_type_code AS roof, b.predictedroofmaterial AS material, b.measured_height AS h ORDER BY score DESC, h DESC LIMIT 15;"

run "E4 Floor-to-floor anomaly (<2 or >8 m)" \
"MATCH (b:Building) WHERE b.storeys_above_ground>0 AND b.measured_height IS NOT NULL WITH b, b.measured_height/b.storeys_above_ground AS ffh WHERE ffh<2 OR ffh>8 RETURN count(b) AS anomalies;"
run "E4b Floor-to-floor anomaly sample" \
"MATCH (b:Building) WHERE b.storeys_above_ground>0 AND b.measured_height IS NOT NULL WITH b, b.measured_height/b.storeys_above_ground AS ffh WHERE ffh<2 OR ffh>8 RETURN b.id AS id, b.measured_height AS h, b.storeys_above_ground AS storeys, round(ffh,2) AS floor_to_floor_m, b.function_code AS fn ORDER BY ffh DESC LIMIT 12;"

run "E5 Roof material x use type (>=500)" \
"MATCH (b:Building) WHERE b.predictedroofmaterial IS NOT NULL AND b.description IS NOT NULL WITH split(b.description,',')[0] AS use_type, b.predictedroofmaterial AS mat WITH use_type, count(*) AS type_total, collect(mat) AS mats WHERE type_total>=500 UNWIND ['roof_tiles','tar_paper','concrete','metal','glass'] AS m RETURN use_type, type_total, m AS material, round(100.0*size([x IN mats WHERE x=m])/type_total,1) AS pct ORDER BY use_type, pct DESC;"

run "E6 Heat-island: dark-roof pct per 2km cell (>=50)" \
"MATCH (b:Building) WHERE b.predictedroofmaterial IS NOT NULL AND b.center_x IS NOT NULL WITH toInteger(floor(b.center_x/2000.0)) AS gx, toInteger(floor(b.center_y/2000.0)) AS gy, b WITH gx, gy, count(*) AS withmat, sum(CASE WHEN b.predictedroofmaterial IN ['tar_paper','metal'] THEN 1 ELSE 0 END) AS dark WHERE withmat>=50 RETURN gx AS x_2km, gy AS y_2km, withmat, dark, round(100.0*dark/withmat,1) AS dark_pct ORDER BY dark_pct DESC LIMIT 15;"

run "E7 Material coverage completeness (FEASIBILITY CHECK)" \
"MATCH (b:Building) WHERE b.predictedroofmaterial IS NOT NULL WITH b LIMIT 1 RETURN [k IN keys(b) WHERE toLower(k) CONTAINS 'cov' OR toLower(k) CONTAINS 'material'] AS material_related_keys;"

run "E8 High-rise clusters: 500m cells, >30m, >=2" \
"MATCH (b:Building) WHERE b.measured_height>30 AND b.center_x IS NOT NULL WITH toInteger(floor(b.center_x/500.0)) AS gx, toInteger(floor(b.center_y/500.0)) AS gy, b WITH gx, gy, count(*) AS tall, round(avg(b.measured_height),1) AS avg_h, round(max(b.measured_height),1) AS max_h, collect(b.id)[0..3] AS sample WHERE tall>=2 RETURN gx AS x_500m, gy AS y_500m, tall, avg_h, max_h, sample ORDER BY tall DESC LIMIT 15;"

run "E9 FAR proxy per 1km cell (>=30)" \
"MATCH (b:Building) WHERE b.center_x IS NOT NULL AND b.storeys_above_ground IS NOT NULL WITH toInteger(floor(b.center_x/1000.0)) AS gx, toInteger(floor(b.center_y/1000.0)) AS gy, b, (b.bbox_max_x-b.bbox_min_x)*(b.bbox_max_y-b.bbox_min_y) AS fp WITH gx, gy, count(b) AS buildings, sum(fp*b.storeys_above_ground) AS gfa WHERE buildings>=30 RETURN gx AS x_km, gy AS y_km, buildings, round(gfa) AS gfa_m2, round(gfa/1000000.0,3) AS far ORDER BY far DESC LIMIT 15;"

run "E10 Ground elevation zones" \
"MATCH (b:Building) WHERE b.ground_z IS NOT NULL WITH b, CASE WHEN b.ground_z<0 THEN '1 below sea (<0m)' WHEN b.ground_z<5 THEN '2 low (0-5m)' WHEN b.ground_z<20 THEN '3 mid (5-20m)' WHEN b.ground_z<50 THEN '4 elevated (20-50m)' ELSE '5 high (>50m)' END AS zone RETURN zone, count(*) AS buildings, round(avg(b.measured_height),1) AS avg_building_h, round(min(b.ground_z),1) AS min_gz, round(max(b.ground_z),1) AS max_gz ORDER BY zone;"

echo "ALL DONE -> $OUT"
