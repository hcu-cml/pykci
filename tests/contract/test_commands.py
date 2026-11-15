"""Every command of the `pykci` command line: its target exists, `--help` works, and the
documents that list the commands name it. A new entry in pykci.cli.COMMANDS is checked here
without touching this file."""

import pytest

from pykci import cli
from pykci.testing import commands

ALL = [*cli.COMMANDS, *cli.BUILTINS]


@pytest.mark.parametrize("name", list(cli.COMMANDS))
def test_target_exists(name):
    kind, target = cli.COMMANDS[name]
    assert commands.target_problem(kind, target, cli.ROOT) is None


@pytest.mark.parametrize("name", list(cli.COMMANDS))
def test_module_commands_belong_to_the_package(name):
    """Installed commands are modules of the package; only research scripts in eval/ may be
    reached by path (they need a source checkout)."""
    kind, target = cli.COMMANDS[name]
    if kind == "module":
        assert target.startswith("pykci.")
    else:
        assert target.startswith("eval/")


@pytest.mark.slow
@pytest.mark.parametrize("name", ALL)
def test_help(name):
    assert commands.help_problem("pykci.cli", name) is None


def test_documented(repo):
    """CLI.md is the command reference; the module docstring is what `pykci --help` users read."""
    assert commands.undocumented(ALL, repo / "CLI.md", repo / "src" / "pykci" / "cli.py") == {}


def test_unknown_command_is_refused():
    result = commands.help_of("pykci.cli", "no-such-command")
    assert result.returncode == 2 and "invalid choice" in result.stderr


def test_extension_table_adds_commands(capsys):
    """main() takes further commands for one call; COMMANDS is left as it is."""
    before = dict(cli.COMMANDS)
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--help"], commands={"my-command": ("module", "my_package.my_command")}, prog="my-cli")
    assert exit_.value.code == 0
    text = capsys.readouterr().out
    assert "my-command" in text and "ingest-citygml" in text and "usage: my-cli" in text
    assert cli.COMMANDS == before
