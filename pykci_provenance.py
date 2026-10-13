"""
Provenance layer of a node property, from its name.

pykci is a multi-source graph (see GRAPH.md → Provenance & Multi-Source Design
Principle). Derived sources never overwrite authoritative values; they write
namespaced scalars instead, so the namespace alone tells a consumer where a
value came from:

    authoritative   CityGML / cadastral source (measured_height, function_code, …)
    crowd_sourced   OpenStreetMap fusion (osm_*, addr_*)        via ingest_osm.py
    ml_predicted    ML predictions (predicted_*, predictedroofmaterial)
                                                                via ingest_roof_materials.py
    reconstructed   LoD3 reconstruction (lod3_*)                via enrich_lod3_facades.py
    derived         computed by an exporter from the geometry (e.g. footprint
                    area in 3D Tiles), never stored in the graph

Exporters (build_3dtiles.py) and clients (the pykci web client) use this one
rule so a value is labelled the same way everywhere it is shown.
"""

AUTHORITATIVE = "authoritative"
CROWD_SOURCED = "crowd_sourced"
ML_PREDICTED = "ml_predicted"
RECONSTRUCTED = "reconstructed"
DERIVED = "derived"

LAYERS = {
    AUTHORITATIVE: "Authoritative city model (CityGML / cadastral)",
    CROWD_SOURCED: "Crowd-sourced (OpenStreetMap)",
    ML_PREDICTED: "Machine-learning prediction",
    RECONSTRUCTED: "Reconstructed geometry (LoD3)",
    DERIVED: "Derived by the exporter from the geometry",
}

_PREFIXES = (
    (("osm_", "addr_"), CROWD_SOURCED),
    (("predicted_", "pred_"), ML_PREDICTED),
    (("lod3_",), RECONSTRUCTED),
)
_EXACT = {
    "predictedroofmaterial": ML_PREDICTED,
}


def property_provenance(key: str) -> str:
    """Provenance layer of a Building/CityObject property name."""
    k = key.lower()
    if k in _EXACT:
        return _EXACT[k]
    for prefixes, layer in _PREFIXES:
        if k.startswith(prefixes):
            return layer
    return AUTHORITATIVE
