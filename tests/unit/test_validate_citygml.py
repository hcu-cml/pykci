"""pykci.validate_citygml: the pieces of the offline XSD validator that need no schema
bundle (version detection, message grouping, reference check, the schema cache set-up with
a stand-in for the download), and one real validation when the bundle is in the cache."""

import io
import urllib.request
import zipfile

import pytest
from lxml import etree

from pykci import validate_citygml as v

CORE3 = "http://www.opengis.net/citygml/3.0"
CORE2 = "http://www.opengis.net/citygml/2.0"
GML32 = "http://www.opengis.net/gml/3.2"
XLINK = "http://www.w3.org/1999/xlink"


def root(xml: str):
    return etree.fromstring(xml.encode("utf-8"))


# ---- version -----------------------------------------------------------------------------

def test_version_from_the_namespaces():
    assert v.detect_version(root(f'<CityModel xmlns="{CORE3}"/>')) == 3
    assert v.detect_version(root(f'<CityModel xmlns="{CORE2}"/>')) == 2
    # a module namespace alone is enough
    assert v.detect_version(root('<b:Building xmlns:b="http://www.opengis.net/citygml/building/2.0"/>')) == 2


@pytest.mark.parametrize("xml, message", [
    ("<x/>", "cannot auto-detect"),
    ('<CityModel xmlns="http://www.opengis.net/citygml/1.0"/>', "CityGML 1.0"),
    (f'<a xmlns="{CORE3}"><b xmlns="{CORE2}"/></a>', "mixed CityGML versions"),
])
def test_version_that_cannot_be_validated(xml, message):
    with pytest.raises(SystemExit, match=message):
        v.detect_version(root(xml))


# ---- messages ---------------------------------------------------------------------------

def test_prettify_replaces_namespaces_by_prefixes():
    msg = f"Element '{{{CORE3}}}CityModel': '{{http://www.opengis.net/citygml/building/3.0}}Building' {{urn:x}}y"
    assert v.prettify(msg, {}) == "Element 'core:CityModel': 'building:Building' {urn:x}y"
    assert v.prettify("{urn:x}y", {"urn:x": "x"}) == "x:y"


def test_pattern_masks_values_and_keeps_names():
    assert v.pattern("'abc' is not a valid value of the atomic type 'xs:ID'") \
        == "'<v>' is not a valid value of the atomic type 'xs:ID'"
    assert v.pattern("value 12.5 at line 3 of 'FooType'") == "value <n> at line <n> of 'FooType'"


# ---- references --------------------------------------------------------------------------

def test_references_duplicates_and_unresolved_links():
    doc = root(f'''<m xmlns="{CORE3}" xmlns:gml="{GML32}" xmlns:xlink="{XLINK}"
        xmlns:app="http://www.opengis.net/citygml/appearance/3.0">
      <a gml:id="one"/><a gml:id="one"/><a gml:id="two"/>
      <r xlink:href="#two"/><r xlink:href="#missing"/><r xlink:href="http://example.org/x"/>
      <app:target>#two</app:target><app:target>#gone</app:target>
    </m>''')
    refs = v.check_refs(doc)
    assert refs["gml_ids"] == 3
    assert refs["duplicate_ids"] == ["one"]
    assert refs["local_hrefs"] == 2 and refs["external_hrefs_not_checked"] == 1
    assert refs["appearance_targets"] == 2
    assert [h for _, h in refs["broken_local_hrefs"]] == ["#missing", "#gone"]


# ---- the schema cache ----------------------------------------------------------------------

def bundle_zip(extra: dict | None = None) -> bytes:
    """A stand-in for the OGC bundle: every schema file the masters import, empty."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for mods in v.MODULES.values():
            for _, rel in mods:
                z.writestr(rel, "<x/>")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


class Downloads:
    """The two downloads of setup(), answered locally: `bundle` is what the bundle URL
    returns, `asked` the URLs requested so far."""

    def __init__(self):
        self.bundle = bundle_zip()
        self.asked = []

    def retrieve(self, url, target):
        self.asked.append(url)
        with open(target, "wb") as f:
            f.write(self.bundle)

    def urlopen(self, req):
        self.asked.append(req.full_url)
        return io.BytesIO(b"<xs:schema/>")


@pytest.fixture
def no_network(monkeypatch):
    downloads = Downloads()
    monkeypatch.setattr(urllib.request, "urlretrieve", downloads.retrieve)
    monkeypatch.setattr(urllib.request, "urlopen", downloads.urlopen)
    return downloads


def test_setup_builds_the_cache(tmp_path, no_network):
    v.setup(tmp_path)
    assert (tmp_path / "SCHEMAS_OPENGIS_NET.zip").is_file()
    assert (tmp_path / "SCHEMAS_OPENGIS_NET" / "citygml" / "3.0" / "core.xsd").is_file()
    assert (tmp_path / "w3c" / "1999" / "xlink.xsd").read_bytes() == b"<xs:schema/>"
    master = (tmp_path / "master_citygml3.xsd").read_text(encoding="utf-8")
    assert 'targetNamespace="urn:pykci:citygml3-master"' in master
    assert f'<xs:import namespace="{CORE3}" schemaLocation="SCHEMAS_OPENGIS_NET/citygml/3.0/core.xsd"/>' in master
    assert master.count("<xs:import") == len(v.MODULES[3])
    assert (tmp_path / "master_citygml2.xsd").read_text(encoding="utf-8").count("<xs:import") == len(v.MODULES[2])
    catalog = (tmp_path / "catalog.xml").read_text(encoding="utf-8")
    assert 'rewriteURI uriStartString="http://schemas.opengis.net/"' in catalog
    assert no_network.asked == [v.BUNDLE_URL, *v.W3C_FILES.values()]


def test_setup_downloads_nothing_twice(tmp_path, no_network):
    v.setup(tmp_path)
    del no_network.asked[:]
    v.setup(tmp_path)
    assert no_network.asked == []


def test_setup_refuses_a_zip_that_leaves_its_folder(tmp_path, no_network):
    no_network.bundle = bundle_zip({"../outside.xsd": "<x/>"})
    with pytest.raises(RuntimeError, match="unsafe path in zip"):
        v.setup(tmp_path)
    assert not (tmp_path.parent / "outside.xsd").exists()


def test_masters_need_the_bundle(tmp_path):
    with pytest.raises(FileNotFoundError, match="run --setup"):
        v.write_masters(tmp_path)


# ---- the command ---------------------------------------------------------------------------

def test_help_names_the_options(capsys):
    with pytest.raises(SystemExit) as stop:
        v.main(["--help"])
    assert stop.value.code == 0
    out = capsys.readouterr().out
    assert "validate_citygml.py --setup        # one-time: download bundle + W3C xsds, build masters" in out
    for option in ("--version {2,3}", "--schema-dir", "--max-lines", "--group-by {message,pattern}",
                   "--raw-names", "--no-refs", "--json", "--setup", "--write-masters"):
        assert option in out
    assert "--bundle-sha256" not in out


def test_setup_from_the_command_line(tmp_path, no_network, capsys):
    assert v.main(["--setup", "--schema-dir", str(tmp_path)]) == 0
    assert f"schema cache ready in {tmp_path.resolve()}" in capsys.readouterr().out
    assert (tmp_path / "master_citygml3.xsd").is_file()


def test_no_input_is_a_usage_error(tmp_path):
    with pytest.raises(SystemExit) as stop:
        v.main(["--schema-dir", str(tmp_path)])
    assert stop.value.code == 2


def test_a_file_that_is_not_xml_gives_exit_code_2(tmp_path, capsys):
    bad = tmp_path / "bad.gml"
    bad.write_text("<a><b></a>", encoding="utf-8")
    assert v.main([str(bad), "--schema-dir", str(tmp_path)]) == 2
    assert "ERROR" in capsys.readouterr().out


@pytest.mark.slow
def test_fzk_haus_is_valid_citygml_2(fzk_lod2, tmp_path, capsys):
    """Against the real OGC schemas, when `pykci validate-citygml --setup` has been run."""
    if not (v.DEFAULT_SCHEMA_DIR / "SCHEMAS_OPENGIS_NET" / "citygml" / "2.0" / "cityGMLBase.xsd").is_file():
        pytest.skip("no schema bundle in the cache (pykci validate-citygml --setup)")
    report = tmp_path / "report.json"
    assert v.main([str(fzk_lod2), "--schema-dir", str(v.DEFAULT_SCHEMA_DIR), "--json", str(report)]) == 0
    assert "XSD result: VALID" in capsys.readouterr().out
    import json
    res = json.loads(report.read_text(encoding="utf-8"))
    assert res["version"] == 2 and res["schema_valid"] and res["n_errors"] == 0
    assert res["refs"]["duplicate_ids"] == [] and res["refs"]["broken_local_hrefs"] == []
