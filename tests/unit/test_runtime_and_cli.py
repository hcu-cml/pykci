"""pykci.runtime (progress events for programs that drive pykci) and the parts of pykci.cli
that need no database: the CityGML preview (`inspect`) and the handling of global options."""

import json

import pytest

from pykci import cli, runtime


# ---- pykci.runtime: progress events ---------------------------------------------------------

def test_progress_is_silent_unless_enabled(monkeypatch, capsys):
    monkeypatch.delenv("PYKCI_PROGRESS", raising=False)
    assert runtime.progress_enabled() is False
    runtime.progress("ingest.parse", done=10, total=20, unit="features")
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("value", ["1", "true", "yes", "jsonl", "TRUE"])
def test_progress_line_is_prefixed_json(monkeypatch, capsys, value):
    monkeypatch.setenv("PYKCI_PROGRESS", value)
    runtime.progress("ingest.parse", done=10, total=20, unit="features", message="läuft", file="a.gml")
    line = capsys.readouterr().out
    assert line.startswith(runtime.PROGRESS_PREFIX) and line.endswith("\n")
    assert line.isascii()                               # survives any console encoding
    event = json.loads(line[len(runtime.PROGRESS_PREFIX):])
    assert {k: event[k] for k in ("stage", "done", "total", "unit", "message", "file")} == {
        "stage": "ingest.parse", "done": 10, "total": 20, "unit": "features", "message": "läuft",
        "file": "a.gml"}
    assert isinstance(event["t"], float)


def test_progress_leaves_out_what_is_not_given(monkeypatch, capsys):
    monkeypatch.setenv("PYKCI_PROGRESS", "1")
    runtime.progress("tiles.done")
    event = json.loads(capsys.readouterr().out[len(runtime.PROGRESS_PREFIX):])
    assert set(event) == {"stage", "t"}


# ---- pykci.cli: inspect ---------------------------------------------------------------------

def test_inspect_reports_version_crs_and_counts(fzk_lod2):
    report = cli.inspect_citygml(fzk_lod2)
    assert report["file"] == fzk_lod2.name and report["bytes"] == fzk_lod2.stat().st_size
    assert report["citygml_version"] == "2.0"
    assert report["srs_name"] == "urn:adv:crs:ETRS89_UTM32*DE_DHHN92_NH" and report["srs_dimension"] == 3
    assert report["features"] == {"Building": 1} and report["buildings"] == 1
    assert report["lods"] == [2] and report["duplicate_ids"] == 0 and report["errors"] == []
    lower, upper = report["envelope"]["lower"], report["envelope"]["upper"]
    assert len(lower) == len(upper) == 3 and all(a <= b for a, b in zip(lower, upper))


def test_inspect_recognises_citygml_3(citygml3_fixtures):
    report = cli.inspect_citygml(citygml3_fixtures / "kitchen_sink.gml")
    assert report["citygml_version"] == "3.0" and report["errors"] == []
    assert sum(report["features"].values()) > 5


def test_inspect_counts_duplicate_ids(fzk_lod2, tmp_path):
    text = fzk_lod2.read_text(encoding="utf-8")
    start, end = text.index("<core:cityObjectMember>"), text.index("</core:cityObjectMember>") + len(
        "</core:cityObjectMember>")
    doubled = tmp_path / "doubled.gml"
    doubled.write_text(text[:end] + text[start:end] + text[end:], encoding="utf-8")
    report = cli.inspect_citygml(doubled)
    assert report["features"] == {"Building": 2} and report["duplicate_ids"] == 1


def test_inspect_reports_broken_xml_instead_of_raising(tmp_path):
    broken = tmp_path / "broken.gml"
    broken.write_text("<CityModel><cityObjectMember>", encoding="utf-8")
    report = cli.inspect_citygml(broken)
    assert report["errors"] and "XML syntax error" in report["errors"][0]


def test_inspect_command_exit_status(fzk_lod2, tmp_path, capsys):
    assert cli.cmd_inspect([str(fzk_lod2)]) == 0
    assert json.loads(capsys.readouterr().out)["buildings"] == 1
    assert cli.cmd_inspect([str(tmp_path)]) == 1                    # a folder without .gml files
    assert json.loads(capsys.readouterr().out) == {"error": "no .gml files found"}


# ---- pykci.cli: global options --------------------------------------------------------------

def test_global_options_become_the_environment(monkeypatch, capsys, fzk_lod2):
    """--neo4j-* and --progress are how a calling program configures every command."""
    for name in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD", "PYKCI_PROGRESS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("os.environ", dict(__import__("os").environ))    # keep the changes inside the test
    status = cli.main(["--neo4j-uri", "bolt://example.invalid:1", "--neo4j-user", "u", "--neo4j-password", "p",
                       "--progress", "inspect", str(fzk_lod2)])
    assert status == 0
    import os
    assert (os.environ["NEO4J_URI"], os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]) == (
        "bolt://example.invalid:1", "u", "p")
    assert runtime.neo4j_uri() == "bolt://example.invalid:1" and runtime.progress_enabled()
    out = capsys.readouterr().out
    assert runtime.PROGRESS_PREFIX in out                           # inspect reported its progress
