# OSM ingestion summary

```json
{
  "dataset": "hamburg_hafencity_roofmats.gml",
  "source": "osm:hamburg-260629.osm",
  "osm_features": 3602,
  "by_kind": {
    "other": 418,
    "road": 958,
    "water": 7,
    "landuse": 238,
    "building": 145,
    "poi": 624,
    "furniture": 1197,
    "vegetation": 15
  },
  "match_cases": {
    "1:1": 28,
    "n:1": 9,
    "1:n": 8,
    "n:m": 3
  },
  "matched": 189,
  "orphans": 3413,
  "orphans_by_reason": {
    "no_pykci_type": 2833,
    "no_overlap": 69,
    "no_containing_building": 511
  },
  "osm_buildings": 145,
  "pykci_buildings_enriched": 64,
  "building_enrich_rate": 0.441,
  "pois_attached": 112,
  "note": "orphans are mostly net-new types (roads/furniture/landuse/vegetation) with no CityObject counterpart in this LoD2 tile — they are kept as spatially-indexed standalone nodes, not discarded."
}
```
