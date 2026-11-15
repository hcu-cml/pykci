"""pykci.geometry: position lists of the graph read back as coordinates, and read from GML."""

from pykci.geometry import bbox_of_pos_lists, centroid_of_pos_lists, gml_position_list, ring_xy


# ---- ring_xy ----------------------------------------------------------------------------------

def test_ring_xy_reads_triplets_as_3d():
    assert ring_xy("1 2 3 4 5 6") == [(1.0, 2.0), (4.0, 5.0)]


def test_ring_xy_reads_a_length_not_divisible_by_three_as_2d():
    assert ring_xy("1 2 3 4 5 6 7 8") == [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0), (7.0, 8.0)]


def test_ring_xy_skips_what_is_not_a_number_and_ignores_a_trailing_rest():
    assert ring_xy("1 2 3 x 5 6 7 8 9") == [(1.0, 2.0), (7.0, 8.0)]
    assert ring_xy("") == []


# ---- centroid_of_pos_lists ----------------------------------------------------------------------

def test_centroid_is_the_mean_of_all_points_of_all_lists():
    assert centroid_of_pos_lists(["1 2 3 4 5 6", "10 20 30"]) == (5.0, 9.0, 13.0)


def test_centroid_skips_empty_and_missing_lists():
    assert centroid_of_pos_lists(["1 2 3 4 5 6", "", None, "10 20 30"]) == (5.0, 9.0, 13.0)


def test_centroid_of_nothing_is_none():
    assert centroid_of_pos_lists([]) == (None, None, None)
    assert centroid_of_pos_lists(["", None]) == (None, None, None)


# ---- bbox_of_pos_lists --------------------------------------------------------------------------

def test_bbox_spans_all_lists():
    assert bbox_of_pos_lists(["0 0 1 10 5 2", "-3 2 7 4 8 0"]) == (-3.0, 0.0, 0.0, 10.0, 8.0, 7.0)


def test_bbox_skips_lists_that_are_not_3d():
    # two values, and four values (not divisible by 3), are not "x y z …"
    assert bbox_of_pos_lists(["1 2", "1 2 3 4", "0 0 0 2 3 4"]) == (0.0, 0.0, 0.0, 2.0, 3.0, 4.0)


def test_bbox_is_none_when_flat_in_x_or_y_or_empty():
    assert bbox_of_pos_lists(["5 0 0 5 9 3"]) is None        # no extent in x
    assert bbox_of_pos_lists(["0 7 0 9 7 3"]) is None        # no extent in y
    assert bbox_of_pos_lists(["1 2 3"]) is None              # a single point
    assert bbox_of_pos_lists([]) is None
    assert bbox_of_pos_lists(["1 2"]) is None


def test_bbox_may_be_flat_in_z():
    assert bbox_of_pos_lists(["0 0 5 2 3 5"]) == (0.0, 0.0, 5.0, 2.0, 3.0, 5.0)


# ---- gml_position_list ------------------------------------------------------------------------

GML31, GML32 = "http://www.opengis.net/gml", "http://www.opengis.net/gml/3.2"


def ring(gml: str, inner: str):
    from lxml import etree
    return etree.fromstring(f'<gml:LinearRing xmlns:gml="{gml}">{inner}</gml:LinearRing>')


def test_gml_position_list_reads_a_pos_list_verbatim():
    el = ring(GML31, '<gml:posList srsDimension="3"> 1.50 2 3.000  4 5 6 </gml:posList>')
    text, dim_el = gml_position_list(el, GML31)
    assert text == "1.50 2 3.000  4 5 6" and dim_el.get("srsDimension") == "3"


def test_gml_position_list_joins_pos_elements_in_their_order():
    el = ring(GML32, "<gml:pos>1 2 3</gml:pos><gml:pos/><gml:pos> 4.0 5 6 </gml:pos>")
    text, dim_el = gml_position_list(el, GML32)
    assert text == "1 2 3 4.0 5 6" and dim_el.text == "1 2 3"


def test_gml_position_list_of_the_other_gml_version_or_without_positions_is_empty():
    assert gml_position_list(ring(GML32, "<gml:posList>1 2 3</gml:posList>"), GML31) == ("", None)
    assert gml_position_list(ring(GML31, "<gml:posList>  </gml:posList>"), GML31) == ("", None)
    assert gml_position_list(ring(GML31, "<gml:coordinates>1,2,3</gml:coordinates>"), GML31) == ("", None)
