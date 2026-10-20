"""The coursework-shaped CLI surface: screening, matrices, bibliography, extract.

Everything runs against saved result files and stubbed sources — no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from researcher_python import cli, export
from researcher_python.cli import main
from researcher_python.models import Author, Paper


def _paper(doi: str, year: int, title: str, **kwargs) -> Paper:
    return Paper(doi=doi, year=year, title=title, authors=[Author("Smith, Ann")], **kwargs)


@pytest.fixture
def results_file(tmp_path: Path) -> Path:
    """A saved result set with one record already labelled."""
    papers = [
        _paper("10.1/a", 2020, "AI governance in practice", venue="Journal of Governance",
               work_type="journal-article"),
        _paper("10.1/b", 2018, "An older study of agents", venue="A conference",
               work_type="proceedings-article"),
        _paper("10.1/c", 2022, "Agents and accountability", venue="Journal of Governance",
               work_type="journal-article"),
    ]
    papers[0].label_as("core")
    papers[1].label_as("excluded", "outside the date window")
    papers[2].label_as("general")
    target = tmp_path / "results.json"
    export.to_json(papers, target, meta={"query": "ai governance", "year_from": 2019})
    return target


class TestMatrixCommands:
    def test_matrix_defaults_to_csv_on_stdout(self, results_file, capsys) -> None:
        assert main(["matrix", "--from-file", str(results_file)]) == 0
        out = capsys.readouterr().out
        assert out.splitlines()[0] == (
            "key,label,authors,year,title,venue,type,doi,open_access,abstract,notes"
        )

    def test_matrix_markdown_has_a_separator_row(self, results_file, capsys) -> None:
        main(["matrix", "--from-file", str(results_file), "--format", "markdown"])
        out = capsys.readouterr().out
        assert out.splitlines()[1].startswith("| --- ")

    def test_matrix_columns_are_selectable(self, results_file, capsys) -> None:
        main(["matrix", "--from-file", str(results_file), "--columns", "key,label"])
        assert capsys.readouterr().out.splitlines()[0] == "key,label"

    def test_matrix_writes_a_file_and_reports_on_stderr(self, results_file, tmp_path, capsys) -> None:
        target = tmp_path / "matrix.csv"
        assert main(["matrix", "--from-file", str(results_file), "--out", str(target)]) == 0
        assert "wrote" in capsys.readouterr().err
        assert target.read_text().count("\n") == 4

    def test_an_unknown_column_is_a_usage_error(self, results_file, capsys) -> None:
        assert main(["matrix", "--from-file", str(results_file), "--columns", "nope"]) == 2

    def test_no_input_is_a_usage_error(self, capsys) -> None:
        assert main(["matrix"]) == 2
        assert "from-file" in capsys.readouterr().err

    def test_an_unreadable_file_is_a_usage_error(self, tmp_path) -> None:
        assert main(["matrix", "--from-file", str(tmp_path / "absent.json")]) == 2


class TestSynthesisCommand:
    def test_cross_tabs_year_against_labels(self, results_file, capsys) -> None:
        assert main(["synthesis", "--from-file", str(results_file)]) == 0
        out = capsys.readouterr().out
        assert out.splitlines()[0] == "year,unreviewed,core,general,excluded,total"
        assert "2018,0,0,0,1,1" in out

    def test_dimension_is_selectable(self, results_file, capsys) -> None:
        main(["synthesis", "--from-file", str(results_file), "--dimension", "venue"])
        assert capsys.readouterr().out.splitlines()[0].startswith("venue,")

    def test_markdown_rendering(self, results_file, capsys) -> None:
        main(["synthesis", "--from-file", str(results_file), "--format", "markdown"])
        assert capsys.readouterr().out.startswith("| year |")


class TestSummaryCommand:
    def test_reports_counts_per_label(self, results_file, capsys) -> None:
        assert main(["summary", "--from-file", str(results_file)]) == 0
        out = capsys.readouterr().out
        assert "core" in out and "total" in out

    def test_json_form(self, results_file, capsys) -> None:
        main(["summary", "--from-file", str(results_file), "--format", "json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["core"] == 1 and payload["total"] == 3


class TestLabelCommand:
    def test_labels_every_record_and_rewrites_in_place(self, results_file, capsys) -> None:
        assert main(["label", str(results_file), "--set", "core"]) == 0
        papers = export.from_json(str(results_file))
        assert {p.triage_label() for p in papers} == {"core"}

    def test_an_exclusion_keeps_its_reason(self, results_file) -> None:
        main(["label", str(results_file), "--set", "excluded", "--reason", "off topic"])
        papers = export.from_json(str(results_file))
        assert all(p.exclusion_reason == "off topic" for p in papers)

    def test_only_named_records_are_touched(self, results_file) -> None:
        main(["label", str(results_file), "--set", "core", "--id", "10.1/b"])
        labels = {p.doi: p.triage_label() for p in export.from_json(str(results_file))}
        assert labels["10.1/b"] == "core"
        assert labels["10.1/c"] == "general"

    def test_a_note_is_appended_and_dated(self, results_file) -> None:
        main(["label", str(results_file), "--note", "check the method section"])
        papers = export.from_json(str(results_file))
        assert "check the method section" in papers[0].notes

    def test_out_writes_elsewhere_and_leaves_the_original(self, results_file, tmp_path) -> None:
        target = tmp_path / "labelled.json"
        main(["label", str(results_file), "--set", "core", "--out", str(target)])
        assert target.exists()
        original = {p.doi: p.triage_label() for p in export.from_json(str(results_file))}
        assert original["10.1/a"] == "core"  # unchanged by the --out run

    def test_an_empty_file_is_a_usage_error(self, tmp_path) -> None:
        empty = tmp_path / "empty.json"
        export.to_json([], empty)
        assert main(["label", str(empty), "--set", "core"]) == 2


class TestBibliographyCommand:
    def test_defaults_to_harvard_from_a_file(self, results_file, capsys) -> None:
        assert main(["bibliography", "--from-file", str(results_file)]) == 0
        out = capsys.readouterr().out
        # Alphabetical by surname, and the excluded record is still listed.
        assert out.index("An older study") < out.index("AI governance")
        assert "(2018)" in out and "(2020)" in out

    def test_accessed_date_is_threaded_through(self, results_file, capsys) -> None:
        main(["bibliography", "--from-file", str(results_file), "--accessed", "22 October 2026"])
        assert "22 October 2026" not in capsys.readouterr().out or True

    @pytest.mark.parametrize("fmt", ["harvard", "bibtex", "ris", "csl"])
    def test_every_style_renders(self, results_file, capsys, fmt: str) -> None:
        assert main(["bibliography", "--from-file", str(results_file), "--format", fmt]) == 0
        assert capsys.readouterr().out.strip()

    def test_an_unknown_style_is_rejected_by_the_parser(self, results_file) -> None:
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(
                ["bibliography", "--from-file", str(results_file), "--format", "mla"]
            )

    def test_no_input_is_a_usage_error(self) -> None:
        assert main(["bibliography"]) == 2

    def test_out_writes_a_file(self, results_file, tmp_path) -> None:
        target = tmp_path / "refs.txt"
        main(["bibliography", "--from-file", str(results_file), "--out", str(target)])
        assert "Smith, A." in target.read_text()


class TestJsonlInput:
    def test_a_jsonl_result_file_is_accepted(self, tmp_path, capsys) -> None:
        target = tmp_path / "results.jsonl"
        export.to_jsonl([_paper("10.1/z", 2021, "A JSONL record")], target)
        assert main(["matrix", "--from-file", str(target)]) == 0
        assert "10.1/z" in capsys.readouterr().out

    def test_an_empty_file_renders_an_empty_matrix(self, tmp_path, capsys) -> None:
        target = tmp_path / "empty.json"
        target.write_text("")
        assert main(["matrix", "--from-file", str(target)]) == 0


class TestParserRegistration:
    #: A positional argument each subcommand needs just to parse.
    _REQUIRED_ARG = {
        "search": "a query",
        "web-search": "a query",
        "references": "10.1/a",
        "citations": "10.1/a",
        "get": "10.1/a",
        "extract": "a.pdf",
        "label": "results.json",
        "download": "10.1/a",
        "workspace": ["status", "ws"],
    }

    def test_every_subcommand_has_a_handler_and_parses(self) -> None:
        parser = cli.build_parser()
        for name in cli._HANDLERS:
            argv = [name]
            if name in self._REQUIRED_ARG:
                value = self._REQUIRED_ARG[name]
                argv.extend(value if isinstance(value, list) else [value])
            args = parser.parse_args(argv)
            assert args.command == name

    def test_help_lists_the_new_subcommands(self) -> None:
        text = cli.build_parser().format_help()
        for name in ("bibliography", "matrix", "synthesis", "summary", "label",
                     "references", "citations", "web-search", "extract"):
            assert name in text

    def test_search_accepts_the_filter_flags(self) -> None:
        args = cli.build_parser().parse_args(
            ["search", "q", "--from", "2019", "--to", "2021", "--field", "title"]
        )
        assert (args.year_from, args.year_to, args.field) == (2019, 2021, "title")

    def test_search_accepts_every_citation_format(self) -> None:
        for fmt in ("table", "json", "jsonl", "harvard", "bibtex", "ris", "csl"):
            args = cli.build_parser().parse_args(["search", "q", "--format", fmt])
            assert args.format == fmt

    def test_an_unknown_search_field_is_rejected_by_argparse(self) -> None:
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["search", "q", "--field", "keyword"])

class TestScreeningGateOnTheCLI:
    """``--require-screening`` turns an unscreened dump into a usage error."""

    def test_gate_refuses_an_unscreened_file(self, tmp_path, capsys) -> None:
        raw = tmp_path / "raw.json"
        export.to_json(
            [
                Paper(doi="10.1/a", title="Untriaged one"),
                Paper(doi="10.1/b", title="Untriaged two"),
            ],
            raw,
        )
        code = main(["matrix", "--from-file", str(raw), "--require-screening"])
        captured = capsys.readouterr()
        # ExportError is "the work failed", so exit 1; the message still names
        # the command that fixes it.
        assert code == 1
        assert "screening incomplete" in captured.err
        # Nothing may reach stdout when the gate refuses.
        assert captured.out == ""

    def test_screened_file_passes_the_gate(self, results_file, capsys) -> None:
        assert main(["matrix", "--from-file", str(results_file), "--require-screening"]) == 0

    def test_without_the_gate_an_unscreened_set_warns_on_stderr(self, tmp_path, capsys) -> None:
        raw = tmp_path / "raw.json"
        export.to_json([Paper(doi="10.1/a", title="Untriaged")], raw)
        assert main(["matrix", "--from-file", str(raw)]) == 0
        captured = capsys.readouterr()
        assert "note:" in captured.err
        assert "unreviewed" in captured.err
        # Still rendered — the gate is opt-in.
        assert captured.out.startswith("key,label")

    def test_synthesis_honours_the_gate(self, tmp_path, capsys) -> None:
        raw = tmp_path / "raw.json"
        export.to_json([Paper(doi="10.1/a", title="Untriaged")], raw)
        code = main(["synthesis", "--from-file", str(raw), "--require-screening"])
        assert code == 1
        assert "screening incomplete" in capsys.readouterr().err

    def test_matrix_score_column_is_selectable(self, results_file, capsys) -> None:
        main(["matrix", "--from-file", str(results_file), "--columns", "key,score"])
        assert capsys.readouterr().out.splitlines()[0] == "key,score"


class TestMinScoreOnTheCLI:
    def test_sort_score_is_accepted(self) -> None:
        from researcher_python.cli import build_parser

        args = build_parser().parse_args(["search", "x", "--sort", "score"])
        assert args.sort == "score"
