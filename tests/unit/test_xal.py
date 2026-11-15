"""pykci.xal: the address fragment stored on a building as plain fields."""

import json

from pykci.xal import FIELDS, first_address

XAL2 = (
    '<xAL:AddressDetails xmlns:xAL="urn:oasis:names:tc:ciq:xsdschema:xAL:2.0"><xAL:Country>'
    "<xAL:CountryName>Germany</xAL:CountryName>"
    '<xAL:Locality Type="Town"><xAL:LocalityName>Hamburg</xAL:LocalityName>'
    '<xAL:Thoroughfare Type="Street"><xAL:ThoroughfareNumber>12</xAL:ThoroughfareNumber>'
    "<xAL:ThoroughfareName>Jungfernstieg</xAL:ThoroughfareName></xAL:Thoroughfare>"
    "<xAL:PostalCode><xAL:PostalCodeNumber>20354</xAL:PostalCodeNumber></xAL:PostalCode>"
    "</xAL:Locality></xAL:Country></xAL:AddressDetails>"
)
ADDRESS = {"address_street": "Jungfernstieg", "address_house_number": "12",
           "address_postcode": "20354", "address_locality": "Hamburg"}


def test_fields_of_one_xal2_address():
    assert first_address(XAL2) == {"address_count": 1.0, **ADDRESS}


def test_several_addresses_are_counted_and_the_first_is_read():
    other = XAL2.replace("Jungfernstieg", "Neuer Wall").replace(">12<", ">7<")
    assert first_address(json.dumps([XAL2, other])) == {"address_count": 2.0, **ADDRESS}
    assert first_address(json.dumps([other, XAL2]))["address_street"] == "Neuer Wall"


def test_an_address_without_a_part_has_no_such_field():
    no_postcode = XAL2.replace("<xAL:PostalCode><xAL:PostalCodeNumber>20354</xAL:PostalCodeNumber></xAL:PostalCode>", "")
    address = first_address(no_postcode)
    assert "address_postcode" not in address and address["address_street"] == "Jungfernstieg"


def test_other_prefixes_and_surrounding_elements_do_not_matter():
    wrapped = ('<bldg:address xmlns:bldg="http://www.opengis.net/citygml/building/2.0"><core:Address '
               'xmlns:core="http://www.opengis.net/citygml/2.0"><core:xalAddress>'
               + XAL2.replace("xAL:", "x:").replace("xmlns:xAL", "xmlns:x")
               + "</core:xalAddress></core:Address></bldg:address>")
    assert first_address(wrapped) == {"address_count": 1.0, **ADDRESS}


def test_no_address():
    assert first_address(None) == {}
    assert first_address("") == {}


def test_what_is_not_xml_only_counts():
    assert first_address("not xml at all") == {"address_count": 1.0}
    assert first_address(json.dumps([{"street": "x"}])) == {"address_count": 1.0}


def test_the_xal2_elements_that_are_read():
    assert FIELDS == {"ThoroughfareName": "address_street", "ThoroughfareNumber": "address_house_number",
                      "PostalCodeNumber": "address_postcode", "LocalityName": "address_locality"}
