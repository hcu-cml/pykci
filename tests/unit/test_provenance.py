"""pykci.provenance: the one rule by which every value of the graph is labelled with its
source, shared by the exporters and by every program that shows the graph."""

import pytest

from pykci import provenance as p


def test_layers_and_stored_source_types_cover_each_other():
    """Six trust classes; each stored source_type is shown as exactly one layer and each
    layer has a description."""
    assert set(p.LAYERS) == {p.AUTHORITATIVE, p.CROWD_SOURCED, p.ML_PREDICTED, p.RECONSTRUCTED,
                             p.SENSED, p.DERIVED}
    assert set(p.SOURCE_TYPE_LAYER.values()) == set(p.LAYERS)
    assert p.SOURCE_TYPE_LAYER == {
        "authoritative": "authoritative", "crowdsourced": "crowd_sourced", "predicted": "ml_predicted",
        "reconstructed": "reconstructed", "sensed": "sensed", "derived": "derived"}


@pytest.mark.parametrize("key, layer", [
    ("measured_height", p.AUTHORITATIVE),
    ("function_code", p.AUTHORITATIVE),
    ("osm_name", p.CROWD_SOURCED),
    ("addr_street", p.CROWD_SOURCED),
    ("predicted_roof_material_0", p.ML_PREDICTED),
    ("pred_energy_class", p.ML_PREDICTED),
    ("predictedroofmaterial", p.ML_PREDICTED),
    ("PredictedRoofMaterial", p.ML_PREDICTED),          # keys are compared in lower case
    ("OSM_Height", p.CROWD_SOURCED),
    ("lod3_window_count", p.RECONSTRUCTED),
    ("live_result", p.SENSED),
    ("osmosis", p.AUTHORITATIVE),                       # a prefix needs its underscore
    ("liveliness", p.AUTHORITATIVE),
])
def test_layer_of_a_property_name(key, layer):
    assert p.property_provenance(key) == layer


@pytest.mark.parametrize("source_type, layer", [
    ("authoritative", p.AUTHORITATIVE), ("crowdsourced", p.CROWD_SOURCED), ("predicted", p.ML_PREDICTED),
    ("reconstructed", p.RECONSTRUCTED), ("sensed", p.SENSED), ("derived", p.DERIVED),
    ("Reconstructed", p.RECONSTRUCTED),                 # case does not matter
    (None, p.AUTHORITATIVE), ("", p.AUTHORITATIVE), ("something else", p.AUTHORITATIVE),
])
def test_layer_of_a_stored_source_type(source_type, layer):
    assert p.source_type_layer(source_type) == layer


def test_layer_of_a_value_uses_the_node():
    # 1. a key the node lists in derived_keys was computed
    assert p.layer_of("lon", {"derived_keys": ["lon", "lat"], "source_type": "authoritative"}) == p.DERIVED
    # 2. a namespaced key keeps its prefix layer, whatever the node is
    assert p.layer_of("osm_name", {"source_type": "reconstructed"}) == p.CROWD_SOURCED
    assert p.layer_of("live_result", {"source_type": "reconstructed"}) == p.SENSED
    # 3. any other key takes the layer of the node's source_type
    assert p.layer_of("height", {"source_type": "reconstructed"}) == p.RECONSTRUCTED
    assert p.layer_of("height", {"source_type": "sensed"}) == p.SENSED
    # a node without source_type (the CityGML features) is authoritative; so is no node at all
    assert p.layer_of("height", {}) == p.AUTHORITATIVE
    assert p.layer_of("height") == p.AUTHORITATIVE
    # derived_keys wins over the prefix
    assert p.layer_of("osm_name", {"derived_keys": ["osm_name"]}) == p.DERIVED


def test_provenance_keys_are_metadata_not_values():
    assert {"source", "source_type", "method", "method_version", "confidence", "derived_keys",
            "dataset_id"} == set(p.PROVENANCE_KEYS)
