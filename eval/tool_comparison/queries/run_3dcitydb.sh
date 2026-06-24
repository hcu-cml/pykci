#!/usr/bin/env bash
# Run the 20 benchmark queries against 3DCityDB v5 (PostgreSQL/PostGIS). Container 'cdb'.
# A reusable `bld` CTE pivots building attributes from the property table + envelope geometry.
# The CTE is inlined per query (NO precomputed view) so each query pays the real cost.
# Output: results_3dcitydb.txt
OUT="$(dirname "$0")/results_3dcitydb.txt"; : > "$OUT"
PG(){ docker exec cdb psql -U postgres -d postgres -P pager=off -t -c "$1" 2>&1; }

BLD="WITH bld AS (
 SELECT f.id, f.objectid AS gmlid,
   max(p.val_double) FILTER (WHERE p.name='value' AND p.val_uom IS NOT NULL) AS height,
   max(p.val_int)    FILTER (WHERE p.name='storeysAboveGround') AS storeys,
   max(p.val_string) FILTER (WHERE p.name='roofType') AS rooftype,
   max(p.val_string) FILTER (WHERE p.name='function') AS function,
   max(p.val_string) FILTER (WHERE p.name='PredictedRoofMaterial') AS material,
   ST_X(ST_Centroid(f.envelope)) AS cx, ST_Y(ST_Centroid(f.envelope)) AS cy,
   ST_ZMin(f.envelope) AS ground_z,
   (ST_XMax(f.envelope)-ST_XMin(f.envelope)) AS dx, (ST_YMax(f.envelope)-ST_YMin(f.envelope)) AS dy
 FROM citydb.feature f JOIN citydb.property p ON p.feature_id=f.id
 WHERE f.objectclass_id=901 GROUP BY f.id, f.objectid, f.envelope)"

run(){ echo "==== $1 ====" | tee -a "$OUT"; echo "SQL: $2" | tee -a "$OUT"
  local t0 t1 res; t0=$(date +%s.%N); res=$(PG "$BLD $2"); t1=$(date +%s.%N)
  awk "BEGIN{printf \"-- exec(incl psql): %.3fs --\n\", $t1-$t0}" | tee -a "$OUT"
  echo "$res" | grep -v '^$' | head -26 | tee -a "$OUT"; echo | tee -a "$OUT"; }

# ---------------- CITIZEN ----------------
run "C1 Hamburg at a glance" \
"SELECT count(*) AS buildings, round(avg(height)::numeric,1) AS avg_h, round(avg(storeys)::numeric,1) AS avg_storeys FROM bld;"

run "C2 Most common roof material (+pct)" \
"SELECT material, count(*) AS n, round(100.0*count(*)/sum(count(*)) OVER (),1) AS pct FROM bld WHERE material IS NOT NULL GROUP BY material ORDER BY n DESC;"

run "C3 20 tallest buildings" \
"SELECT gmlid, round(height::numeric,3) AS height_m, function, material FROM bld WHERE height IS NOT NULL ORDER BY height DESC LIMIT 20;"

run "C4 Roof shapes popularity" \
"SELECT rooftype, CASE rooftype WHEN '1000' THEN 'Flat' WHEN '2100' THEN 'Mono-Pitch' WHEN '2200' THEN 'Offset Mono-Pitch' WHEN '3100' THEN 'Gable' WHEN '3200' THEN 'Hip' WHEN '3300' THEN 'Half-Hip' WHEN '3400' THEN 'Mansard' WHEN '3500' THEN 'Tent/Pyramid' WHEN '4000' THEN 'Tower' WHEN '5000' THEN 'Mixed' ELSE 'Other' END AS shape, count(*) AS n FROM bld WHERE rooftype IS NOT NULL GROUP BY rooftype ORDER BY n DESC;"

run "C5 Solar: flat roofs >5m (count)" \
"SELECT count(*) AS flat_over_5m FROM bld WHERE rooftype='1000' AND height>5;"

run "C6 Buildings per storey count" \
"SELECT storeys, count(*) AS n FROM bld WHERE storeys IS NOT NULL GROUP BY storeys ORDER BY storeys;"

run "C7 Glass roofs (count)" \
"SELECT count(*) AS glass_roofs FROM bld WHERE material='glass';"

run "C8 High-rises > 50 m" \
"SELECT count(*) AS over_50m FROM bld WHERE height>50;"

run "C9 Mixed roof materials (FEASIBILITY)" \
"SELECT max(c) AS max_materials_per_building FROM (SELECT feature_id, count(*) AS c FROM citydb.property WHERE name='PredictedRoofMaterial' GROUP BY feature_id) t;"

run "C10 HafenCity bbox (envelope centroid)" \
"SELECT count(*) AS buildings, round(avg(height)::numeric,1) AS avg_h, count(material) AS with_material FROM bld WHERE cx BETWEEN 565000 AND 567000 AND cy BETWEEN 5933000 AND 5934000;"

# ---------------- EXPERT ----------------
run "E1 Typology by function code" \
"SELECT function, count(*) AS n, round(avg(height)::numeric,1) AS avg_h, round(avg(storeys)::numeric,1) AS avg_storeys FROM bld WHERE function IS NOT NULL GROUP BY function ORDER BY n DESC LIMIT 15;"

run "E2 1km grid: density/height/dominant material (>=30)" \
", grid AS (SELECT floor(cx/1000)::int AS gx, floor(cy/1000)::int AS gy, count(*) AS buildings, round(avg(height)::numeric,1) AS avg_h, mode() WITHIN GROUP (ORDER BY material) FILTER (WHERE material IS NOT NULL) AS dominant_material FROM bld GROUP BY 1,2) SELECT gx AS x_km, gy AS y_km, buildings, avg_h, dominant_material FROM grid WHERE buildings>=30 ORDER BY buildings DESC LIMIT 15;"

run "E3 Solar potential score 0-7 (ranked)" \
"SELECT gmlid, (CASE rooftype WHEN '1000' THEN 3 WHEN '2100' THEN 1 ELSE 0 END + CASE material WHEN 'concrete' THEN 2 WHEN 'tar_paper' THEN 2 WHEN 'metal' THEN 2 WHEN 'roof_tiles' THEN 1 ELSE 0 END + CASE WHEN height>10 THEN 1 ELSE 0 END + CASE WHEN storeys>1 THEN 1 ELSE 0 END) AS score, rooftype, material, round(height::numeric,2) AS h FROM bld ORDER BY score DESC, height DESC LIMIT 15;"

run "E4 Floor-to-floor anomaly (<2 or >8 m) count" \
"SELECT count(*) AS anomalies FROM bld WHERE storeys>0 AND height IS NOT NULL AND (height/storeys<2 OR height/storeys>8);"

run "E5 Roof material x function code (>=500)" \
", ft AS (SELECT function, count(*) AS type_total FROM bld WHERE material IS NOT NULL AND function IS NOT NULL GROUP BY function HAVING count(*)>=500) SELECT b.function, ft.type_total, b.material, round(100.0*count(*)/ft.type_total,1) AS pct FROM bld b JOIN ft ON ft.function=b.function WHERE b.material IS NOT NULL GROUP BY b.function, ft.type_total, b.material ORDER BY b.function, pct DESC LIMIT 30;"

run "E6 Heat-island: dark-roof pct per 2km cell (>=50)" \
", grid AS (SELECT floor(cx/2000)::int AS gx, floor(cy/2000)::int AS gy, count(*) AS withmat, count(*) FILTER (WHERE material IN ('tar_paper','metal')) AS dark FROM bld WHERE material IS NOT NULL GROUP BY 1,2) SELECT gx AS x_2km, gy AS y_2km, withmat, dark, round(100.0*dark/withmat,1) AS dark_pct FROM grid WHERE withmat>=50 ORDER BY dark_pct DESC LIMIT 15;"

run "E7 Material coverage completeness (FEASIBILITY)" \
"SELECT count(*) AS coverage_attribute_rows FROM citydb.property WHERE name ILIKE '%cov%' OR name ILIKE '%coverage%';"

run "E8 High-rise clusters: 500m cells, >30m, >=2" \
", grid AS (SELECT floor(cx/500)::int AS gx, floor(cy/500)::int AS gy, count(*) AS tall, round(avg(height)::numeric,1) AS avg_h, round(max(height)::numeric,1) AS max_h, (array_agg(gmlid ORDER BY height DESC))[1:3] AS sample FROM bld WHERE height>30 GROUP BY 1,2) SELECT gx AS x_500m, gy AS y_500m, tall, avg_h, max_h, sample FROM grid WHERE tall>=2 ORDER BY tall DESC LIMIT 15;"

run "E9 FAR proxy per 1km cell (>=30)" \
", grid AS (SELECT floor(cx/1000)::int AS gx, floor(cy/1000)::int AS gy, count(*) AS buildings, sum(dx*dy*storeys) AS gfa FROM bld WHERE storeys IS NOT NULL GROUP BY 1,2) SELECT gx AS x_km, gy AS y_km, buildings, round(gfa::numeric) AS gfa_m2, round((gfa/1000000.0)::numeric,3) AS far FROM grid WHERE buildings>=30 ORDER BY far DESC LIMIT 15;"

run "E10 Ground elevation zones (envelope ZMin)" \
"SELECT CASE WHEN ground_z<0 THEN '1 below sea (<0m)' WHEN ground_z<5 THEN '2 low (0-5m)' WHEN ground_z<20 THEN '3 mid (5-20m)' WHEN ground_z<50 THEN '4 elevated (20-50m)' ELSE '5 high (>50m)' END AS zone, count(*) AS buildings, round(avg(height)::numeric,1) AS avg_h, round(min(ground_z)::numeric,1) AS min_gz, round(max(ground_z)::numeric,1) AS max_gz FROM bld GROUP BY zone ORDER BY zone;"

echo "ALL DONE -> $OUT"
