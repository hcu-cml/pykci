#!/usr/bin/env bash
# Reset DB -> ingest (timed) -> export -> census-diff for each requested dataset.
# Validates the labelled-MATCH ingestion fix is lossless across all datasets and
# reports per-file ingest wall-clock so the speedup is visible.
set -uo pipefail
cd "$(dirname "$0")/.."

FILES=(
  "input/citygml/FZKHaus/FZK-Haus-LoD0-KIT-IAI-KHH-B36-V1.gml"
  "input/citygml/FZKHaus/FZK-Haus-LoD1-KIT-IAI-KHH-B36-V1.gml"
  "input/citygml/FZKHaus/FZK-Haus-LoD2-KIT-IAI-KHH-B36-V1.gml"
  "input/citygml/FZKHaus/FZK-Haus-LoD3-KIT-IAI-KHH-B36-V1.gml"
  "input/citygml/FZKHaus/FZK-Haus-LoD4-KIT-IAI-KHH-B36-V1.gml"
  "input/citygml/Railway_Scene_LoD3/Railway_Scene_LoD3.gml"
  "input/citygml/hamburg_hafencity.gml"
  "input/citygml/ingolstadt_citygml2_lod2_2026_roofmats_small.gml"
  "eval/fixtures/new_elements_test.gml"
)

declare -A INGEST_TIME
declare -A CENSUS_RESULT

for F in "${FILES[@]}"; do
  STEM="$(basename "$F" .gml)"
  E="output/citygml/${STEM}_export.gml"
  echo "############################## ${STEM} ##############################"

  bash eval/reset_neo4j.sh >/dev/null 2>&1

  T0=$(date +%s.%N)
  python ingest_citygml.py "$F" >/dev/null 2>&1
  ING_RC=$?
  T1=$(date +%s.%N)
  DT=$(awk "BEGIN{printf \"%.1f\", $T1-$T0}")
  INGEST_TIME["$STEM"]=$DT

  if [ $ING_RC -ne 0 ]; then
    echo "  INGEST FAILED (rc=$ING_RC)"
    CENSUS_RESULT["$STEM"]="INGEST-FAIL"
    continue
  fi
  echo "  ingest: ${DT}s"

  python export_citygml.py >/dev/null 2>&1
  if [ $? -ne 0 ]; then
    echo "  EXPORT FAILED"
    CENSUS_RESULT["$STEM"]="EXPORT-FAIL"
    continue
  fi

  python eval/roundtrip_census.py --gml "$F" --export "$E" --show 20
  if [ $? -eq 0 ]; then
    CENSUS_RESULT["$STEM"]="LOSSLESS"
  else
    CENSUS_RESULT["$STEM"]="LOSS"
  fi
done

echo ""
echo "================================ SUMMARY ================================"
for F in "${FILES[@]}"; do
  STEM="$(basename "$F" .gml)"
  printf "  %-50s ingest=%8ss  %s\n" "$STEM" "${INGEST_TIME[$STEM]:-?}" "${CENSUS_RESULT[$STEM]:-?}"
done
