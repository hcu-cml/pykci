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
    sensed          live values of sensors and live feeds (live_*)
    derived         computed from other data: by an exporter from the geometry (e.g.
                    footprint area in 3D Tiles), or by a pipeline step that stores it
                    with source_type 'derived' (GRAPH.md)

Two vocabularies meet here. Nodes and edges STORE their trust class in
`source_type` (GRAPH.md): authoritative, crowdsourced, predicted, reconstructed,
sensed, derived. Exporters and clients SHOW the layers above. SOURCE_TYPE_LAYER
maps one to the other, and layer_of() labels one value of a node from its key, the
node's `derived_keys` and the node's `source_type`.

Exporters (build_3dtiles.py) and clients (the pykci web client) use this one
rule so a value is labelled the same way everywhere it is shown.
"""

AUTHORITATIVE = "authoritative"
CROWD_SOURCED = "crowd_sourced"
ML_PREDICTED = "ml_predicted"
RECONSTRUCTED = "reconstructed"
SENSED = "sensed"
DERIVED = "derived"

LAYERS = {
    AUTHORITATIVE: "Authoritative city model (CityGML / cadastral)",
    CROWD_SOURCED: "Crowd-sourced (OpenStreetMap)",
    ML_PREDICTED: "Machine-learning prediction",
    RECONSTRUCTED: "Reconstructed geometry (LoD3)",
    SENSED: "Sensed live (sensors, live feeds)",
    DERIVED: "Derived: computed from other data (by an exporter or a pipeline step)",
}

_PREFIXES = (
    (("osm_", "addr_"), CROWD_SOURCED),
    (("predicted_", "pred_"), ML_PREDICTED),
    (("lod3_",), RECONSTRUCTED),
    (("live_",), SENSED),
)
_EXACT = {
    "predictedroofmaterial": ML_PREDICTED,
}

# stored `source_type` (GRAPH.md vocabulary) → the layer it is shown as
SOURCE_TYPE_LAYER = {
    "authoritative": AUTHORITATIVE,
    "crowdsourced": CROWD_SOURCED,
    "predicted": ML_PREDICTED,
    "reconstructed": RECONSTRUCTED,
    "sensed": SENSED,
    "derived": DERIVED,
}

# provenance metadata of a node, not values of the feature: callers usually list them
# apart from (or hide them among) the feature's attributes
PROVENANCE_KEYS = frozenset({"source", "source_type", "method", "method_version", "confidence",
                             "derived_keys", "dataset_id"})


def property_provenance(key: str) -> str:
    """Provenance layer of a Building/CityObject property name."""
    k = key.lower()
    if k in _EXACT:
        return _EXACT[k]
    for prefixes, layer in _PREFIXES:
        if k.startswith(prefixes):
            return layer
    return AUTHORITATIVE


def source_type_layer(source_type: str | None) -> str:
    """Layer of a stored `source_type` value; a missing or unknown one is authoritative."""
    return SOURCE_TYPE_LAYER.get((source_type or "").lower(), AUTHORITATIVE)


def layer_of(key: str, node: dict | None = None) -> str:
    """Provenance layer of one value of a node, using the node as well as the key:

    1. a key the node lists in `derived_keys` was computed: DERIVED;
    2. a namespaced key (osm_*, predicted_*, lod3_*, live_*, …) keeps its prefix layer;
    3. any other key takes the layer of the node's `source_type` (authoritative when the
       node has none, as on the CityGML features).
    """
    node = node or {}
    if key in (node.get("derived_keys") or ()):
        return DERIVED
    layer = property_provenance(key)
    if layer != AUTHORITATIVE:
        return layer
    return source_type_layer(node.get("source_type"))
