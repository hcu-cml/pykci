"""
Addresses of buildings: the xAL fragment stored on a node (`address_xml`) as plain fields.

`address_xml` holds the verbatim xAL subtree of the first bldg:address, or a JSON list of
them for a building with several addresses (xAL 2 in CityGML 2.0, xAL 3 in CityGML 3.0).
"""

import json

from lxml import etree

# local name of an xAL 2 element → the field it fills
FIELDS = {
    "ThoroughfareName": "address_street",
    "ThoroughfareNumber": "address_house_number",
    "PostalCodeNumber": "address_postcode",
    "LocalityName": "address_locality",
}


def first_address(address_xml) -> dict:
    """Street / number / postcode / locality from the first bldg:address
    (address_xml holds the verbatim xAL subtree(s), a JSON list for
    multi-address buildings): {address_count, address_street, address_house_number,
    address_postcode, address_locality}, each only where the address has it."""
    if not address_xml:
        return {}
    try:
        items = json.loads(address_xml)
        items = items if isinstance(items, list) else [items]
    except (ValueError, TypeError):
        items = [address_xml]
    out = {"address_count": float(len(items))}
    if not items or not isinstance(items[0], str):
        return out
    try:
        root = etree.fromstring(items[0].encode("utf-8"),
                                parser=etree.XMLParser(recover=True))
    except Exception:
        return out
    if root is None:
        return out
    from pykci.citygml_writer import _parse_address
    version, d = _parse_address(items[0].encode("utf-8"))
    if version == "3" and d:                     # CityGML 3.0: xAL 3
        for key, field in (("street", "address_street"), ("number", "address_house_number"),
                           ("postcode", "address_postcode"), ("locality", "address_locality")):
            if d.get(key):
                out[field] = d[key].strip()
        return out
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        local = el.tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]
        field = FIELDS.get(local)
        if field and field not in out and el.text and el.text.strip():
            out[field] = el.text.strip()
    return out
