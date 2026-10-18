#!/usr/bin/env bash
# Reset DB, ingest, export, and census-diff each FZK-Haus LoD file.
# Usage: bash eval/run_all_fzk.sh [LOD ...]   (default: 0 1 2 3 4)
set -uo pipefail
cd "$(dirname "$0")/.."

LODS=("$@"); [ ${#LODS[@]} -eq 0 ] && LODS=(0 1 2 3 4)

for L in "${LODS[@]}"; do
  F="input/citygml/FZK-Haus/FZK-Haus-LoD${L}-KIT-IAI-KHH-B36-V1.gml"
  E="output/citygml/FZK-Haus-LoD${L}-KIT-IAI-KHH-B36-V1_export.gml"
  echo "############################## LoD${L} ##############################"
  bash eval/reset_neo4j.sh >/dev/null 2>&1
  py ingest_citygml.py "$F" >/dev/null 2>&1
  py export_citygml.py >/dev/null 2>&1
  py eval/roundtrip_census.py --gml "$F" --export "$E" --show 60
done

# Synthetic fixture exercising the AbstractBuilding/_CityObject elements the FZK
# LoDs don't contain: recursive BuildingPart, interiorBuildingInstallation,
# lodN MultiCurve, storeyHeights*, terminationDate, metaDataProperty, location,
# generalizesTo, externalReference, and ADE content.
NF="eval/fixtures/new_elements_test.gml"
NE="output/citygml/new_elements_test_export.gml"
echo "############################## new-elements fixture ##############################"
bash eval/reset_neo4j.sh >/dev/null 2>&1
py ingest_citygml.py "$NF" >/dev/null 2>&1
py export_citygml.py >/dev/null 2>&1
py eval/roundtrip_census.py --gml "$NF" --export "$NE" --show 60
