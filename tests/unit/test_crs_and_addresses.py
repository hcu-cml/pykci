"""pykci.crs (srsName → EPSG, geoid undulation), pykci.xal (the address of a building) and the
municipality a building's key names (pykci.ingest_citygml)."""

import json

import pytest

from pykci import crs, xal


# ---- pykci.crs -----------------------------------------------------------------------------

@pytest.mark.parametrize("srs, epsg", [
    ("EPSG:25832", "EPSG:25832"),
    ("urn:ogc:def:crs:EPSG::25832", "EPSG:25832"),
    ("urn:ogc:def:crs,crs:EPSG::25832,crs:EPSG::5783", "EPSG:25832"),       # compound: the horizontal part
    ("http://www.opengis.net/def/crs/EPSG/0/6697", "EPSG:6697"),            # OGC HTTP form with a version
    ("urn:adv:crs:ETRS89_UTM32*DE_DHHN2016_NH", "EPSG:25832"),              # German ADV name
    ("urn:adv:crs:ETRS89_UTM33*DE_DHHN92_NH", "EPSG:25833"),
    ("CH1903+", "EPSG:2056"), ("LV95", "EPSG:2056"),                        # Swiss names without a code
    ("CH1903", "EPSG:21781"), ("LV03", "EPSG:21781"),
    ("epsg:2056", "EPSG:2056"),                                             # any letter case
    ("something pyproj may know", "something pyproj may know"),             # unknown: passed through
    ("", None), (None, None),
])
def test_epsg_of_a_source_file(srs, epsg):
    assert crs.epsg_from_srs(srs) == epsg


@pytest.mark.parametrize("srs, epsg", [
    ("EPSG:25832", "EPSG:25832"),
    ("urn:ogc:def:crs:EPSG::25832", "EPSG:25832"),
    ("urn:ogc:def:crs,crs:EPSG::3068,crs:EPSG::5783", "EPSG:3068"),
    ("urn:adv:crs:ETRS89_UTM32*DE_DHHN92_NH", "EPSG:25832"),
    ("urn:adv:crs:ETRS89_UTM33*DE_DHHN92_NH", "EPSG:25833"),
    ("urn:adv:crs:DE_DHDN_3GK2*DE_DHN92_NH", "EPSG:31466"),                 # Gauss-Krüger zones 2..5
    ("urn:adv:crs:DE_DHDN_3GK3*DE_DHN92_NH", "EPSG:31467"),
    ("urn:adv:crs:DE_DHDN_3GK5*DE_DHN92_NH", "EPSG:31469"),
    ("unknown", "unknown"),
])
def test_horizontal_epsg_of_a_dataset(srs, epsg):
    assert crs.horiz_epsg_from_srs(srs) == epsg


def test_every_adv_compound_crs_has_a_horizontal_code():
    """The names geoid_undulation() knows are names horiz_epsg_from_srs() resolves."""
    for name in crs.ADV_COMPOUND:
        assert crs.horiz_epsg_from_srs(f"urn:adv:crs:{name}*DE_DHHN92_NH").startswith("EPSG:")


def test_geoid_override_wins():
    assert crs.geoid_undulation("urn:adv:crs:ETRS89_UTM32*DE_DHHN92_NH", 0, 0, 0, override=39.5) == (
        39.5, "--geoid-offset")


def test_geoid_is_zero_and_says_so_without_a_compound_crs(capsys):
    assert crs.geoid_undulation("EPSG:2263", 0, 0, 0) == (0.0, "none")
    assert "no compound CRS mapping" in capsys.readouterr().out


# ---- pykci.xal -----------------------------------------------------------------------------

XAL2 = ('<xAL:AddressDetails xmlns:xAL="urn:oasis:names:tc:ciq:xsdschema:xAL:2.0"><xAL:Country>'
        '<xAL:CountryName>Germany</xAL:CountryName><xAL:Locality Type="Town">'
        '<xAL:LocalityName>Hamburg</xAL:LocalityName><xAL:Thoroughfare Type="Street">'
        '<xAL:ThoroughfareNumber>12</xAL:ThoroughfareNumber>'
        '<xAL:ThoroughfareName>Jungfernstieg</xAL:ThoroughfareName></xAL:Thoroughfare>'
        '<xAL:PostalCode><xAL:PostalCodeNumber>20354</xAL:PostalCodeNumber></xAL:PostalCode>'
        '</xAL:Locality></xAL:Country></xAL:AddressDetails>')
XAL3 = ('<xAL:Address xmlns:xAL="urn:oasis:names:tc:ciq:xal:3"><xAL:Country><xAL:NameElement>Germany'
        '</xAL:NameElement></xAL:Country><xAL:Locality xAL:Type="town"><xAL:NameElement>Hamburg'
        '</xAL:NameElement></xAL:Locality><xAL:Thoroughfare xAL:Type="street"><xAL:NameElement>'
        'Jungfernstieg</xAL:NameElement><xAL:Number>12</xAL:Number></xAL:Thoroughfare><xAL:PostCode>'
        '<xAL:Identifier>20354</xAL:Identifier></xAL:PostCode></xAL:Address>')
FIELDS = {"address_street": "Jungfernstieg", "address_house_number": "12", "address_postcode": "20354",
          "address_locality": "Hamburg"}


@pytest.mark.parametrize("fragment", [XAL2, XAL3], ids=["xAL 2 (CityGML 2.0)", "xAL 3 (CityGML 3.0)"])
def test_first_address(fragment):
    assert xal.first_address(fragment) == {"address_count": 1.0, **FIELDS}


def test_several_addresses_give_the_first_and_the_count():
    other = XAL2.replace("Jungfernstieg", "Neuer Wall")
    assert xal.first_address(json.dumps([XAL2, other])) == {"address_count": 2.0, **FIELDS}


def test_only_fields_the_address_has():
    partial = XAL2.replace("<xAL:PostalCode><xAL:PostalCodeNumber>20354</xAL:PostalCodeNumber></xAL:PostalCode>", "")
    assert "address_postcode" not in xal.first_address(partial)


@pytest.mark.parametrize("value", [None, ""])
def test_no_address(value):
    assert xal.first_address(value) == {}


def test_text_that_is_not_xml_counts_but_has_no_fields():
    assert xal.first_address("not xml at all") == {"address_count": 1.0}


# ---- the municipality of a key --------------------------------------------------------------

@pytest.mark.parametrize("key, expected", [
    ("02000000", ("Hamburg", "Hamburg")),           # LGV LoD2-DE 2026: the municipality key only
    ("02200227", ("Hamburg", "Hamburg")),           # a district key of the 2025 release
    ("02100104", ("HafenCity", "Hamburg")),         # a finer entry wins over the Land
    ("09161000", ("Ingolstadt", "Ingolstadt")),
    ("05315000", ("LoD2_tile", "LoD2_tile")),       # a key the table does not know: the file name
    ("", ("LoD2_tile", "LoD2_tile")),
])
def test_municipality_of_a_key(key, expected):
    """Hamburg is a city-state: every key of its Land (02) is Hamburg. Before, a tile of the 2026
    release named its own City and District after the file, 783 of each."""
    from pykci.ingest_citygml import _resolve_municipality
    assert _resolve_municipality(key, "LoD2_tile") == expected
