"""CLI tests (offline — no network, no subprocess)."""

from __future__ import annotations

import pytest

from researcher_python import __version__
from researcher_python.cli import build_parser, main


def test_parser_exposes_expected_commands() -> None:
    parser = build_parser()
    args = parser.parse_args(["info"])
    assert args.command == "info"


def test_info_command_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["info"]) == 0
    out = capsys.readouterr().out
    assert f"researcher-python {__version__}" in out
    assert "researcher_python" in out


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "usage:" in out


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out
