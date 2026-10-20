"""MCP tools added for screening, matrices and PDF extraction — offline."""

from __future__ import annotations

import json

import pytest

from researcher_python import export, mcp_server as mcp_mod, snowball
from researcher_python.citation import CitationResult
from researcher_python.config import Settings
from researcher_python.models import Author, Paper
from researcher_python.pipeline import PipelineResult
from researcher_python.workspace import SearchParams, Workspace


def _paper(doi: str, year: int, title: str, **kwargs) -> Paper:
    return Paper(doi=doi, year=year, title=title, authors=[Author("Smith, Ann")], **kwargs)


@pytest.fixture
def results_file(tmp_path):
    papers = [
        _paper("10.1/a", 2020, "AI governance in practice", venue="Journal of Governance",
               work_type="journal-article"),
        _paper("10.1/b", 2018, "An older study of agents", venue="A conference",
               work_type="proceedings-article"),
    ]
    papers[0].label_as("core")
    papers[1].label_as("excluded", "outside the window")
    target = tmp_path / "results.json"
    export.to_json(papers, target)
    return str(target)


class TestToolRegistration:
    def test_the_current_tools_are_declared(self) -> None:
        for name in ("report", "screen", "prescreen", "snowball",
                     "extract_text", "find_quotes"):
            assert name in mcp_mod.TOOLS

    def test_the_folded_tools_are_no_longer_registered(self) -> None:
        # report() replaces three tools, prisma folded in, and label was a
        # byte-for-byte duplicate of screen.
        for old in ("matrix", "synthesis", "screen_summary", "label", "prisma"):
            assert old not in mcp_mod.TOOLS

    def test_report_kinds_are_declared(self) -> None:
        assert mcp_mod.REPORT_KINDS == (
            "matrix", "synthesis", "screening", "prisma"
        )

    def test_bibliography_defaults_to_harvard(self) -> None:
        assert mcp_mod._BIB_FORMATS[0] == "harvard"


class TestScreeningLoopTools:
    def test_batch_updates_apply_many_labels(self, results_file) -> None:
        payload = mcp_mod._tool_label(
            from_file=results_file,
            updates=[
                {"id": "10.1/a", "label": "general"},
                {"id": "10.1/b", "label": "core", "note": "re-read"},
            ],
        )
        assert payload["ok"] is True
        assert payload["labelled"] == 2
        papers = {p.doi: p for p in export.from_json(results_file)}
        assert papers["10.1/a"].triage_label() == "general"
        assert papers["10.1/b"].triage_label() == "core"
        assert "re-read" in papers["10.1/b"].notes

    def test_updates_cannot_be_mixed_with_the_single_form(self, results_file) -> None:
        payload = mcp_mod._tool_label(
            from_file=results_file,
            set_label="core",
            updates=[{"id": "10.1/a", "label": "general"}],
        )
        assert payload["ok"] is False
        assert "updates cannot be combined" in payload["message"]

    def test_label_falls_back_to_the_session_workspace(self, tmp_path) -> None:
        ws = Workspace(tmp_path / "ws", settings=Settings())
        ws.record_search(
            SearchParams(query="seed", sources=("openalex",), round=1),
            PipelineResult(papers=[_paper("10.1/a", 2020, "A")], by_source={"openalex": 1}),
        )
        mcp_mod._set_workspace(ws)
        try:
            payload = mcp_mod._tool_label(set_label="core")
        finally:
            mcp_mod._set_workspace(None)
        assert payload["ok"] is True
        assert payload["labelled"] == 1

    def test_prescreen_reports_then_applies(self, tmp_path) -> None:
        target = tmp_path / "raw.json"
        export.to_json([_paper("10.1/a", 2020, "No abstract")], target)
        dry = mcp_mod._tool_prescreen(from_file=str(target))
        assert dry["ok"] is True
        assert dry["flagged"] == 1
        assert dry["applied"] == 0
        applied = mcp_mod._tool_prescreen(from_file=str(target), apply=True)
        assert applied["applied"] == 1
        assert export.from_json(str(target))[0].triage_label() == "excluded"

    def test_snowball_returns_only_new_records_ranked_by_links(
        self, monkeypatch, tmp_path
    ) -> None:
        ws = Workspace(tmp_path / "ws", settings=Settings())
        ws.record_search(
            SearchParams(query="seed", sources=("openalex",), round=1),
            PipelineResult(papers=[_paper("10.1/a", 2020, "A")], by_source={"openalex": 1}),
        )
        ws.label(ids=["10.1/a"], set_label="core")
        monkeypatch.setattr(
            snowball.citation,
            "references",
            lambda paper, **kwargs: CitationResult(
                direction="references",
                papers=[_paper("10.1/c", 2021, "Candidate")],
                by_source={"openalex": 1},
            ),
        )
        payload = mcp_mod._tool_snowball(
            workspace=str(ws.path), direction="references", persist=False
        )
        assert payload["ok"] is True
        assert payload["new"] == 1
        assert payload["candidates"][0]["id"] == "10.1/c"
        assert payload["candidates"][0]["links"] == 1

    def test_prisma_renders_markdown(self, tmp_path) -> None:
        ws = Workspace(tmp_path / "ws", settings=Settings())
        ws.record_search(
            SearchParams(query="seed", sources=("openalex",), round=1),
            PipelineResult(
                papers=[_paper("10.1/a", 2020, "A")], by_source={"openalex": 1}
            ),
        )
        ws.label(ids=["10.1/a"], set_label="core")
        payload = mcp_mod._tool_prisma(workspace=str(ws.path), format="markdown")
        assert payload["ok"] is True
        assert "PRISMA-style" in payload["text"]
        assert payload["report"]["included"] == 1


class TestMatrixTools:
    def test_matrix_renders_csv_from_a_file(self, results_file) -> None:
        payload = mcp_mod._tool_matrix(from_file=results_file)
        assert payload["ok"] is True
        assert payload["count"] == 2
        assert payload["text"].splitlines()[0].startswith("key,label")

    def test_matrix_renders_markdown(self, results_file) -> None:
        payload = mcp_mod._tool_matrix(from_file=results_file, format="markdown")
        assert payload["text"].startswith("| key |")

    def test_matrix_honours_explicit_columns(self, results_file) -> None:
        payload = mcp_mod._tool_matrix(from_file=results_file, columns=["key", "label"])
        assert payload["text"].splitlines()[0] == "key,label"

    def test_an_unknown_column_is_reported_as_data(self, results_file) -> None:
        payload = mcp_mod._tool_matrix(from_file=results_file, columns=["nope"])
        assert payload["ok"] is False
        assert payload["error"] == "ExportError"

    def test_an_unknown_format_is_a_configuration_error(self, results_file) -> None:
        payload = mcp_mod._tool_matrix(from_file=results_file, format="xlsx")
        assert payload["error"] == "ConfigurationError"

    def test_no_input_is_a_configuration_error(self) -> None:
        payload = mcp_mod._tool_matrix()
        assert payload["error"] == "ConfigurationError"
        assert "from_file" in payload["message"]

    def test_an_unreadable_file_is_a_configuration_error(self, tmp_path) -> None:
        payload = mcp_mod._tool_matrix(from_file=str(tmp_path / "absent.json"))
        assert payload["error"] == "ConfigurationError"

    def test_synthesis_returns_the_screening_summary_too(self, results_file) -> None:
        payload = mcp_mod._tool_synthesis(from_file=results_file)
        assert payload["summary"]["core"] == 1
        assert payload["summary"]["excluded"] == 1
        assert "total" in payload["text"].splitlines()[0]

    def test_screen_summary_counts_every_label(self, results_file) -> None:
        payload = mcp_mod._tool_screen_summary(from_file=results_file)
        assert payload["summary"]["total"] == 2
        assert payload["labels"][0] == "unreviewed"


class TestReportTool:
    def test_matrix_kind_renders_csv(self, results_file) -> None:
        payload = mcp_mod._tool_report("matrix", from_file=results_file)
        assert payload["ok"] is True
        assert payload["kind"] == "matrix"
        assert payload["count"] == 2
        assert payload["text"].splitlines()[0].startswith("key,label")

    def test_synthesis_kind_defaults_to_csv(self, results_file) -> None:
        payload = mcp_mod._tool_report("synthesis", from_file=results_file)
        assert payload["kind"] == "synthesis"
        assert "total" in payload["text"].splitlines()[0]
        assert payload["summary"]["total"] == 2

    def test_screening_kind_accepts_the_old_name_as_an_alias(
        self, results_file
    ) -> None:
        for kind in ("screening", "screen_summary"):
            payload = mcp_mod._tool_report(kind, from_file=results_file)
            assert payload["ok"] is True
            assert payload["kind"] == "screening"
            assert payload["summary"]["total"] == 2

    def test_prisma_kind_reads_the_workspace(self, tmp_path) -> None:
        ws = Workspace(tmp_path / "ws", settings=Settings())
        ws.record_search(
            SearchParams(query="seed", sources=("openalex",), round=1),
            PipelineResult(
                papers=[_paper("10.1/a", 2020, "A")], by_source={"openalex": 1}
            ),
        )
        ws.label(ids=["10.1/a"], set_label="core")
        payload = mcp_mod._tool_report(
            "prisma", workspace=str(ws.path), format="markdown"
        )
        assert payload["kind"] == "prisma"
        assert "PRISMA-style" in payload["text"]
        assert payload["report"]["included"] == 1

    def test_unknown_kind_is_actionable(self) -> None:
        payload = mcp_mod._tool_report("nope")
        assert payload["ok"] is False
        assert payload["error"] == "ConfigurationError"
        assert "matrix" in payload["message"]

    def test_kind_specific_arguments_are_rejected(self, results_file) -> None:
        cases = (
            {"kind": "matrix", "format": "json"},
            {"kind": "matrix", "dimension": "label"},
            {"kind": "synthesis", "columns": ["key"]},
            {"kind": "screening", "format": "csv"},
            {"kind": "prisma", "query": "x"},
            {"kind": "prisma", "require_screening": True},
        )
        for case in cases:
            payload = mcp_mod._tool_report(
                from_file=results_file, **case
            )
            assert payload["ok"] is False, case
            assert payload["error"] == "ConfigurationError", case


class TestLabelTool:
    def test_labels_are_written_back(self, results_file) -> None:
        payload = mcp_mod._tool_label(results_file, set_label="general")
        assert payload["ok"] is True and payload["labelled"] == 2
        assert payload["summary"]["general"] == 2

    def test_ids_limit_the_change(self, results_file) -> None:
        mcp_mod._tool_label(results_file, set_label="core", ids=["10.1/b"])
        labels = {p.doi: p.triage_label() for p in export.from_json(results_file)}
        assert labels["10.1/b"] == "core"
        assert labels["10.1/a"] == "core"  # was already core
        assert "general" not in labels.values()

    def test_a_reason_is_stored(self, results_file) -> None:
        mcp_mod._tool_label(results_file, set_label="excluded", reason="off topic")
        assert export.from_json(results_file)[0].exclusion_reason == "off topic"

    def test_a_note_is_stored(self, results_file) -> None:
        mcp_mod._tool_label(results_file, note="re-read the method")
        assert "re-read the method" in export.from_json(results_file)[0].notes

    def test_an_unknown_label_is_a_configuration_error(self, results_file) -> None:
        payload = mcp_mod._tool_label(results_file, set_label="maybe")
        assert payload["error"] == "ConfigurationError"

    def test_doing_nothing_is_a_configuration_error(self, results_file) -> None:
        payload = mcp_mod._tool_label(results_file)
        assert payload["error"] == "ConfigurationError"

    def test_out_file_leaves_the_original_alone(self, results_file, tmp_path) -> None:
        target = tmp_path / "other.json"
        mcp_mod._tool_label(results_file, set_label="general", out_file=str(target))
        assert target.exists()
        assert export.from_json(results_file)[0].triage_label() == "core"


class TestBibliographyFromFile:
    def test_reads_a_saved_result_set(self, results_file) -> None:
        payload = mcp_mod._tool_bibliography(from_file=results_file)
        assert payload["ok"] is True
        assert payload["format"] == "harvard"
        assert "Smith, A." in payload["text"]

    def test_renders_each_style(self, results_file) -> None:
        for fmt, needle in (
            ("bibtex", "@"),
            ("ris", "TY  -"),
            ("csl_json", '"container-title"'),
        ):
            payload = mcp_mod._tool_bibliography(from_file=results_file, format=fmt)
            assert needle in payload["text"], fmt

    def test_an_empty_file_yields_an_empty_list(self, tmp_path) -> None:
        target = tmp_path / "empty.json"
        target.write_text("")
        payload = mcp_mod._tool_bibliography(from_file=str(target))
        assert payload["count"] == 0

    def test_a_jsonl_file_is_accepted(self, tmp_path) -> None:
        target = tmp_path / "r.jsonl"
        export.to_jsonl([_paper("10.1/z", 2021, "A JSONL record")], target)
        payload = mcp_mod._tool_bibliography(from_file=str(target))
        assert payload["count"] == 1


class TestSearchFilters:
    def test_filters_are_echoed_back(self) -> None:
        payload = mcp_mod._tool_search(
            "agents", sources=["openalex"], year_from=2019, field="title"
        )
        assert payload["filters"]["year_from"] == 2019
        assert payload["filters"]["field"] == "title"

    def test_an_invalid_filter_is_data_not_a_crash(self) -> None:
        payload = mcp_mod._tool_search("agents", sources=["openalex"], field="keyword")
        assert payload["ok"] is False
        assert payload["error"] == "ConfigurationError"
        assert "unknown search field" in payload["message"]

    def test_an_implausible_year_is_rejected(self) -> None:
        payload = mcp_mod._tool_search("agents", sources=["openalex"], year_from=99)
        assert payload["ok"] is False


class TestExtractTools:
    def test_extract_text_returns_characters_and_text(self, monkeypatch) -> None:
        from researcher_python import extract

        monkeypatch.setattr(extract, "iter_pages", lambda path: [(1, "hello"), (2, "world")])
        payload = mcp_mod._tool_extract_text("fake.pdf")
        assert payload["ok"] is True
        assert payload["text"] == "hello\n\nworld"

    def test_find_quotes_returns_page_anchored_hits(self, monkeypatch) -> None:
        from researcher_python import extract

        monkeypatch.setattr(
            extract, "iter_pages", lambda path: [(1, "agents here"), (2, "no match")]
        )
        payload = mcp_mod._tool_find_quotes("fake.pdf", terms="agents")
        assert payload["count"] == 1
        assert payload["quotes"][0]["page"] == 1

    def test_a_missing_dependency_is_reported_as_data(self, monkeypatch) -> None:
        from researcher_python import extract
        from researcher_python.errors import SourceUnavailableError

        def boom(path):
            raise SourceUnavailableError("pdf", "dependency 'pypdf' is not installed")

        monkeypatch.setattr(extract, "iter_pages", boom)
        payload = mcp_mod._tool_extract_text("fake.pdf")
        assert payload["ok"] is False
        assert payload["error"] == "SourceUnavailableError"
        assert "pypdf" in payload["message"]

class TestScreeningGateOverMCP:
    """The matrix is only a literature matrix once it has been screened."""

    def test_matrix_reports_screening_state_when_gate_is_off(self, results_file) -> None:
        payload = mcp_mod._tool_matrix(from_file=results_file)
        assert payload["screening"]["ready"] is True
        assert payload["screening"]["screened"] == 2

    def test_require_screening_refuses_an_unscreened_set(self, tmp_path) -> None:
        papers = [_paper("10.1/a", 2020, "Untriaged one"),
                  _paper("10.1/b", 2021, "Untriaged two")]
        target = tmp_path / "raw.json"
        export.to_json(papers, target)
        payload = mcp_mod._tool_matrix(from_file=str(target), require_screening=True)
        assert payload["ok"] is False
        assert payload["error"] == "ExportError"
        assert "screening incomplete" in payload["message"]

    def test_unscreened_matrix_still_renders_but_says_it_is_not_screened(
        self, tmp_path
    ) -> None:
        papers = [_paper("10.1/a", 2020, "Untriaged one")]
        target = tmp_path / "raw.json"
        export.to_json(papers, target)
        payload = mcp_mod._tool_matrix(from_file=str(target))
        assert payload["ok"] is True
        assert payload["screening"]["ready"] is False
        assert payload["screening"]["message"]

    def test_synthesis_carries_the_screening_report(self, results_file) -> None:
        payload = mcp_mod._tool_synthesis(from_file=results_file)
        assert payload["screening"]["ready"] is True

    def test_synthesis_gate_refuses_unscreened(self, tmp_path) -> None:
        papers = [_paper("10.1/a", 2020, "Untriaged")]
        target = tmp_path / "raw.json"
        export.to_json(papers, target)
        payload = mcp_mod._tool_synthesis(
            from_file=str(target), require_screening=True
        )
        assert payload["ok"] is False

    def test_screen_summary_reports_readiness(self, results_file) -> None:
        payload = mcp_mod._tool_screen_summary(from_file=results_file)
        assert payload["screening"]["ready"] is True
        assert payload["summary"]["total"] == 2

    def test_bad_matrix_format_is_named(self, results_file) -> None:
        payload = mcp_mod._tool_matrix(from_file=results_file, format="xlsx")
        assert payload["ok"] is False
        assert "unknown matrix format" in payload["message"]


class TestMinScoreOverMCP:
    def test_min_score_is_echoed_and_drop_is_reported(self) -> None:
        payload = mcp_mod._tool_search("legal liability", min_score=0.3)
        assert payload["ok"] is True
        assert payload["min_score"] == 0.3
        assert "dropped" in payload and "terms" in payload

    def test_out_of_range_min_score_is_a_configuration_error(self) -> None:
        payload = mcp_mod._tool_search("x", min_score=2.0)
        assert payload["ok"] is False
        assert payload["error"] == "ConfigurationError"
        assert "min_score" in payload["message"]

    def test_search_payload_reports_terms_and_scores(self) -> None:
        payload = mcp_mod._tool_search("legal liability")
        assert isinstance(payload["terms"], list)
        assert payload["dropped"] == 0


class TestCompactSearchPayload:
    """A 30-record search with full abstracts overflowed an agent's context.

    Offline: the pipeline is stubbed at its boundary, so these assert the
    payload shape rather than anything the network happens to return.
    """

    def _stub(self, monkeypatch, n=3):
        import researcher_python.pipeline as pipeline

        papers = [
            Paper(
                doi=f"10.1/{i}",
                title=f"Paper {i}",
                year=2020 + i,
                abstract="word " * 400,
                citation_count=10 * i,
                authors=[Author(f"Author {j}") for j in range(6)],
            )
            for i in range(n)
        ]
        for paper in papers:
            paper.relevance = 0.5

        def fake_search(query, **kwargs):
            return pipeline.PipelineResult(
                papers=papers, by_source={"fake": n}, terms=["paper"]
            )

        monkeypatch.setattr(mcp_mod.pipeline, "search", fake_search)
        return papers

    def test_compact_returns_a_triage_projection(self, monkeypatch) -> None:
        self._stub(monkeypatch)
        payload = mcp_mod._tool_search(
            "x", sources=["openalex"], detail="compact"
        )
        assert payload["ok"] is True
        keys = set(payload["papers"][0])
        assert "abstract_excerpt" in keys
        assert "abstract" not in keys
        assert "relevance" in keys
        assert len(payload["papers"][0]["authors"]) <= 3
        assert payload["papers"][0]["author_count"] == 6

    def test_summary_is_materially_smaller_than_full(self, monkeypatch) -> None:
        self._stub(monkeypatch)
        full = mcp_mod._tool_search(
            "x", sources=["openalex"], detail="full", limit=None
        )
        summary = mcp_mod._tool_search(
            "x", sources=["openalex"], detail="summary", limit=None
        )
        assert len(json.dumps(summary)) < len(json.dumps(full)) * 0.2

    def test_excerpt_is_truncated_not_dropped(self) -> None:
        from researcher_python.mcp_server import _compact_paper

        paper = Paper(title="t", abstract="word " * 400)
        out = _compact_paper(paper)
        assert out["abstract_excerpt"].endswith("\u2026")
        assert len(out["abstract_excerpt"]) <= 240

    def test_summary_is_the_default(self, monkeypatch) -> None:
        self._stub(monkeypatch, n=1)
        payload = mcp_mod._tool_search("x", sources=["openalex"])
        assert "abstract" not in payload["papers"][0]
        assert "abstract_excerpt" not in payload["papers"][0]
        assert payload["summary"] == "1 kept, top 1 shown"
        assert payload["kept"] == 1

    def test_detail_full_returns_the_complete_record(self, monkeypatch) -> None:
        self._stub(monkeypatch, n=1)
        payload = mcp_mod._tool_search(
            "x", sources=["openalex"], detail="full"
        )
        assert "abstract" in payload["papers"][0]

    def test_hint_is_surfaced_when_the_set_is_empty(self, monkeypatch) -> None:
        import researcher_python.pipeline as pipeline

        monkeypatch.setattr(
            mcp_mod.pipeline,
            "search",
            lambda *a, **k: pipeline.PipelineResult(
                papers=[], by_source={"fake": 0}, terms=["zzz"],
                hint="0 kept: field='title' requires ALL 9 query terms",
            ),
        )
        payload = mcp_mod._tool_search("x", sources=["openalex"], field="title")
        assert payload["count"] == 0
        assert "requires ALL" in payload["hint"]
