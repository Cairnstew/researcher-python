"""Offline tests for the workspace store (no network).

The workspace is the one write path for provenance, so these tests focus on
the properties that matter more than formatting: a record is never lost, a
reader's label is never clobbered, new/seen/screened counts are honest, and the
Markdown header and rows cannot disagree.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from researcher_python import export
from researcher_python.config import Settings
from researcher_python.errors import ConfigurationError, WorkspaceError
from researcher_python.models import Paper, merge_sets
from researcher_python.pipeline import PipelineResult
from researcher_python.workspace import (
    LOG_JSONL,
    LOG_MD,
    META_FILE,
    RESULTS_FILE,
    SearchLog,
    SearchParams,
    Workspace,
    import_workspace,
    log_entry_for,
    parse_legacy_log,
    render_log_markdown,
    resolve_workspace_path,
)


def _paper(doi: str, title: str = "", **kwargs) -> Paper:
    return Paper(doi=doi, title=title or doi, sources=["openalex"], **kwargs)


def _result(papers: list[Paper], **kwargs) -> PipelineResult:
    return PipelineResult(
        papers=list(papers),
        by_source=kwargs.pop("by_source", {"openalex": len(papers)}),
        terms=kwargs.pop("terms", ["term"]),
        dropped=kwargs.pop("dropped", 0),
        **kwargs,
    )


def _params(query: str = "q", **kwargs) -> SearchParams:
    return SearchParams(
        query=query,
        sources=kwargs.pop("sources", ("openalex",)),
        round=kwargs.pop("round", 1),
        channel=kwargs.pop("channel", "test"),
        **kwargs,
    )


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path / "workspace", settings=Settings())


def test_search_params_round_trip_the_type_and_enrich_policy() -> None:
    params = SearchParams(
        query="q",
        sources=("openalex",),
        types=("article",),
        exclude_types=(),
        enrich=False,
    )
    loaded = SearchParams.from_dict(params.to_dict())
    assert loaded.types == ("article",)
    assert loaded.exclude_types == ()
    assert loaded.enrich is False


# ---------------------------------------------------------------------- #
# Creation and layout
# ---------------------------------------------------------------------- #


def test_create_writes_the_four_stores(ws: Workspace) -> None:
    assert ws.exists()
    for name in (META_FILE, RESULTS_FILE, LOG_JSONL, LOG_MD):
        assert (ws.path / name).is_file()


def test_create_is_idempotent(ws: Workspace) -> None:
    before = (ws.path / RESULTS_FILE).read_text()
    Workspace(ws.path, settings=Settings())
    assert (ws.path / RESULTS_FILE).read_text() == before


def test_creating_over_an_existing_directory_loads_it(ws: Workspace) -> None:
    ws.record_search(_params(), _result([_paper("10.1/a")]))
    reopened = Workspace(ws.path, settings=Settings())
    assert len(reopened.papers) == 1


# ---------------------------------------------------------------------- #
# Merge counts and reader state
# ---------------------------------------------------------------------- #


def test_first_search_is_all_new(ws: Workspace) -> None:
    outcome = ws.record_search(_params(), _result([_paper("10.1/a"), _paper("10.1/b")]))
    assert (outcome.new, outcome.seen, outcome.total) == (2, 0, 2)
    assert "2 new, 0 already seen" in outcome.summary


def test_second_search_reports_seen_and_preserves_the_label(ws: Workspace) -> None:
    ws.record_search(_params("first"), _result([_paper("10.1/a")]))
    ws.label(ids=["10.1/a"], set_label="core")

    revised = _paper("10.1/a", title="Revised title", abstract="new")
    outcome = ws.record_search(
        _params("second"), _result([revised, _paper("10.1/b")])
    )

    assert (outcome.new, outcome.seen, outcome.screened) == (1, 1, 1)
    stored = {paper.doi: paper for paper in ws.papers}
    assert stored["10.1/a"].triage_label() == "core"
    # First seen wins for scalars; gaps are filled.
    assert stored["10.1/a"].title == "10.1/a"
    # The prior record had no abstract, so the gap is filled.
    assert stored["10.1/a"].abstract == "new"


def test_a_title_year_match_counts_as_seen(ws: Workspace) -> None:
    ws.record_search(
        _params("first"), _result([Paper(title="Same Work", year=2020)])
    )
    incoming = Paper(doi="10.1/new", title="Same Work", year=2020)
    outcome = ws.record_search(_params("second"), _result([incoming]))
    assert outcome.new == 0
    assert outcome.seen == 1


def test_same_title_different_year_is_new(ws: Workspace) -> None:
    ws.record_search(
        _params("first"), _result([Paper(title="Same Work", year=2020)])
    )
    incoming = Paper(doi="10.1/new", title="Same Work", year=2024)
    outcome = ws.record_search(_params("second"), _result([incoming]))
    assert outcome.new == 1


def test_excluded_record_stays_excluded(ws: Workspace) -> None:
    ws.record_search(_params("first"), _result([_paper("10.1/a")]))
    ws.label(ids=["10.1/a"], set_label="excluded", reason="off topic")

    outcome = ws.record_search(
        _params("second"), _result([_paper("10.1/a", abstract="new")])
    )

    assert outcome.seen == 1 and outcome.screened == 1
    stored = {paper.doi: paper for paper in ws.papers}
    assert stored["10.1/a"].triage_label() == "excluded"
    assert stored["10.1/a"].exclusion_reason == "off topic"


def test_merge_sets_returns_prior_order_then_new(tmp_path: Path) -> None:
    prior = [_paper("10.1/a"), _paper("10.1/b")]
    incoming = [_paper("10.1/b", abstract="x"), _paper("10.1/c")]
    outcome = merge_sets(prior, incoming)
    assert [paper.doi for paper in outcome.papers] == [
        "10.1/a",
        "10.1/b",
        "10.1/c",
    ]
    assert [paper.doi for paper in outcome.new] == ["10.1/c"]
    assert [paper.doi for paper in outcome.seen] == ["10.1/b"]


# ---------------------------------------------------------------------- #
# Provenance files
# ---------------------------------------------------------------------- #


def test_results_envelope_carries_the_search_meta(ws: Workspace) -> None:
    outcome = ws.record_search(
        _params("alpha", min_score=0.2, top_ratio=0.5),
        _result([_paper("10.1/a")], dropped=3, terms=["alpha"]),
    )
    envelope = export.read_envelope(ws.path / RESULTS_FILE)

    assert envelope.meta["search_id"] == outcome.search_id
    assert envelope.meta["round"] == 1
    assert envelope.meta["search"]["query"] == "alpha"
    assert envelope.meta["search"]["min_score"] == 0.2
    assert envelope.meta["outcome"]["dropped"] == 3
    assert envelope.meta["terms"] == ["alpha"]
    assert envelope.meta["by_source"] == {"openalex": 1}


def test_jsonl_log_has_every_parameter_and_error(ws: Workspace) -> None:
    from researcher_python.errors import RateLimitError

    result = _result([_paper("10.1/a")])
    result.errors["semantic_scholar"] = RateLimitError(
        "semantic_scholar", "slow down"
    )
    ws.record_search(
        _params(
            "alpha",
            field="title",
            year_from=2019,
            year_to=2024,
            min_citations=5,
        ),
        result,
    )

    entry = json.loads((ws.path / LOG_JSONL).read_text().splitlines()[0])
    assert entry["params"]["query"] == "alpha"
    assert entry["params"]["field"] == "title"
    assert entry["params"]["year_from"] == 2019
    assert entry["params"]["year_to"] == 2024
    assert entry["params"]["min_citations"] == 5
    assert entry["errors"]["semantic_scholar"]["type"] == "RateLimitError"
    assert entry["by_source"] == {"openalex": 1}


def test_log_header_and_rows_have_the_same_cell_count(ws: Workspace) -> None:
    ws.record_search(_params("one"), _result([_paper("10.1/a")]))
    ws.record_search(_params("two"), _result([_paper("10.1/b")]))

    lines = [
        line
        for line in (ws.path / LOG_MD).read_text().splitlines()
        if line.startswith("|")
    ]
    header, rule, *rows = lines
    expected = len(header.strip().strip("|").split("|"))
    assert len(rule.strip().strip("|").split("|")) == expected
    assert all(len(row.strip().strip("|").split("|")) == expected for row in rows)
    assert len(rows) == 2


def test_a_pipe_and_newline_in_the_query_do_not_break_the_table(
    ws: Workspace,
) -> None:
    ws.record_search(
        _params("alpha | beta\ngamma"), _result([_paper("10.1/a")])
    )
    text = (ws.path / LOG_MD).read_text()
    rows = [line for line in text.splitlines() if line.startswith("| 20")]
    assert len(rows) == 1
    from researcher_python.workspace import _split_markdown_row

    cells = _split_markdown_row(rows[0])
    assert len(cells) == 12
    assert cells[1] == "alpha | beta gamma"


def test_workspace_meta_tracks_counts_and_last_search(ws: Workspace) -> None:
    ws.record_search(_params("one"), _result([_paper("10.1/a")]))
    ws.label(ids=["10.1/a"], set_label="core", note="read it")

    meta = json.loads((ws.path / META_FILE).read_text())
    assert meta["counts"]["records"] == 1
    assert meta["counts"]["core"] == 1
    assert meta["counts"]["screened"] == 1
    assert meta["searches"] == 1
    assert meta["last_search"]["query"] == "one"
    assert meta["last_label"]["set"] == "core"


def test_workspace_status_reports_paths_and_provenance(ws: Workspace) -> None:
    ws.record_search(_params(), _result([_paper("10.1/a")]))
    status = ws.status()
    assert status["exists"] is True
    assert status["round"] == 1
    assert status["log_rows"] == 1
    assert status["files"]["results"].endswith(RESULTS_FILE)
    assert status["provenance"]["unaccounted"] == 0


# ---------------------------------------------------------------------- #
# Rounds
# ---------------------------------------------------------------------- #


def test_start_round_snapshots_then_advances(ws: Workspace) -> None:
    ws.record_search(_params("round one"), _result([_paper("10.1/a")]))
    new_round = ws.start_round("look deeper")

    assert new_round == 2
    assert ws.round == 2
    snapshot = ws.path / "rounds" / "round-1"
    assert (snapshot / RESULTS_FILE).is_file()
    assert (snapshot / LOG_MD).is_file()
    assert len(export.read_envelope(snapshot / RESULTS_FILE).papers) == 1

    ws.record_search(
        _params("round two", round=2), _result([_paper("10.1/b")])
    )
    assert ws.round == 2


def test_set_round_rejects_zero(ws: Workspace) -> None:
    with pytest.raises(ConfigurationError):
        ws.set_round(0)


# ---------------------------------------------------------------------- #
# Crash journal
# ---------------------------------------------------------------------- #


def test_pending_log_is_completed_when_results_committed(ws: Workspace) -> None:
    outcome = ws.record_search(_params("one"), _result([_paper("10.1/a")]))
    entry = json.loads((ws.path / LOG_JSONL).read_text().splitlines()[0])

    # Simulate a crash after results.json but before the JSONL append.
    (ws.path / LOG_JSONL).write_text("")
    (ws.path / "pending.json").write_text(
        json.dumps(
            {
                "search_id": outcome.search_id,
                "entry": entry,
                "last_search": ws.meta["last_search"],
            }
        )
    )

    ws.reload()

    assert len((ws.path / LOG_JSONL).read_text().splitlines()) == 1
    assert not (ws.path / "pending.json").exists()


def test_pending_log_is_discarded_when_results_not_committed(ws: Workspace) -> None:
    ws.record_search(_params("one"), _result([_paper("10.1/a")]))
    (ws.path / "pending.json").write_text(
        json.dumps({"search_id": "never-committed", "entry": {}})
    )

    ws.reload()

    assert not (ws.path / "pending.json").exists()


# ---------------------------------------------------------------------- #
# Paths and settings
# ---------------------------------------------------------------------- #


def test_relative_path_resolves_under_researcher_home(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path / "home")
    resolved = resolve_workspace_path("cw1", settings)
    assert resolved == (tmp_path / "home" / "cw1").resolve()


def test_absolute_path_wins_over_home(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path / "home")
    absolute = tmp_path / "elsewhere" / "cw1"
    assert resolve_workspace_path(absolute, settings) == absolute.resolve()


def test_default_workspace_comes_from_settings(tmp_path: Path) -> None:
    settings = Settings(workspace=tmp_path / "default")
    workspace = Workspace.default(settings)
    assert workspace.path == (tmp_path / "default").resolve()


def test_empty_path_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        resolve_workspace_path("", Settings())


# ---------------------------------------------------------------------- #
# Concurrency
# ---------------------------------------------------------------------- #


def test_concurrent_searches_do_not_lose_records(ws: Workspace) -> None:
    def add(index: int) -> None:
        ws.record_search(
            _params(f"q{index}"), _result([_paper(f"10.1/{index}")])
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(add, range(8)))

    assert len(ws.papers) == 8
    assert len((ws.path / LOG_JSONL).read_text().splitlines()) == 8


# ---------------------------------------------------------------------- #
# Standalone log (the legacy --search-log path)
# ---------------------------------------------------------------------- #


def test_standalone_search_log_uses_the_same_renderer(tmp_path: Path) -> None:
    log = SearchLog(tmp_path / "rounds.md")
    log.append(
        log_entry_for(
            _params("alpha"), _result([_paper("10.1/a")]), search_id="x"
        )
    )
    text = (tmp_path / "rounds.md").read_text()
    assert text.startswith("| when | query | field | sources |")
    assert "alpha" in text
    header = text.splitlines()[0]
    row = text.splitlines()[2]
    assert len(header.strip().strip("|").split("|")) == len(
        row.strip().strip("|").split("|")
    )


def test_render_log_markdown_is_empty_table_with_header() -> None:
    text = render_log_markdown([])
    assert text.count("\n") == 2
    assert "| when | query | field | sources |" in text


# ---------------------------------------------------------------------- #
# Legacy import / reconciliation
# ---------------------------------------------------------------------- #


LEGACY_LOG = """\
| when | query | field | sources | kept | dropped | terms |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-10-20 14:08 | professional responsibility `field=title` | openalex, acm | 6 | 0 | professional; responsibility |
| 2026-10-20 14:09 | code of ethics `field=title` | openalex | 4 | 1 | code; ethics |
"""


def test_parse_legacy_log_splits_the_folded_field() -> None:
    entries = parse_legacy_log(LEGACY_LOG)
    assert len(entries) == 2
    assert entries[0]["params"]["query"] == "professional responsibility"
    assert entries[0]["params"]["field"] == "title"
    assert entries[0]["outcome"]["kept"] == 6
    assert entries[1]["params"]["sources"] == ["openalex"]
    assert entries[0]["legacy"] is True


def test_import_reports_the_unaccounted_gap_without_writing_on_dry_run(
    tmp_path: Path,
) -> None:
    results = tmp_path / "legacy.json"
    export.to_json([_paper(f"10.1/{i}") for i in range(12)], results)
    log = tmp_path / "legacy-log.md"
    log.write_text(LEGACY_LOG)

    report = import_workspace(
        tmp_path / "ws",
        results,
        log_file=log,
        round=3,
        dry_run=True,
    )

    assert report["records"] == 12
    assert report["log_rows"] == 2
    assert report["logged_kept"] == 10
    assert report["unaccounted"] == 2
    assert not (tmp_path / "ws").exists()


def test_import_records_the_gap_and_leaves_the_source_files_alone(
    tmp_path: Path,
) -> None:
    results = tmp_path / "legacy.json"
    export.to_json([_paper(f"10.1/{i}") for i in range(12)], results)
    log = tmp_path / "legacy-log.md"
    log.write_text(LEGACY_LOG)
    before_results = results.read_bytes()
    before_log = log.read_bytes()

    report = import_workspace(tmp_path / "ws", results, log_file=log, round=3)

    assert report["unaccounted"] == 2
    workspace = Workspace(tmp_path / "ws", settings=Settings(), create=False)
    assert len(workspace.papers) == 12
    assert workspace.status()["provenance"]["legacy"] is True
    assert workspace.status()["provenance"]["logged_kept"] == 10
    assert workspace.round == 3
    assert results.read_bytes() == before_results
    assert log.read_bytes() == before_log


def test_import_without_a_log_does_not_claim_reconciliation(tmp_path: Path) -> None:
    results = tmp_path / "legacy.json"
    export.to_json([_paper("10.1/a")], results)

    report = import_workspace(tmp_path / "ws", results)

    assert report["logged_kept"] is None
    assert report["unaccounted"] is None
    assert report["no_log_records"] == 1


# ---------------------------------------------------------------------- #
# No secrets
# ---------------------------------------------------------------------- #


def test_settings_are_never_written_into_the_workspace(tmp_path: Path) -> None:
    settings = Settings(
        email="secret@example.com",
        semantic_scholar_api_key="s2-secret",
    )
    workspace = Workspace(tmp_path / "ws", settings=settings)
    workspace.record_search(_params(), _result([_paper("10.1/a")]))

    written = "\n".join(
        path.read_text()
        for path in workspace.path.iterdir()
        if path.is_file()
    )
    assert "secret@example.com" not in written
    assert "s2-secret" not in written


def test_import_merge_folds_disjoint_round_files(tmp_path: Path) -> None:
    first = tmp_path / "round-1.json"
    second = tmp_path / "round-2.json"
    export.to_json([_paper(f"10.1/{i}") for i in range(3)], first)
    export.to_json([_paper(f"10.1/{i}") for i in range(3, 5)], second)

    one = import_workspace(tmp_path / "ws", first, round=1)
    two = import_workspace(
        tmp_path / "ws", second, round=2, mode="merge"
    )

    assert one["total"] == 3
    assert two["new"] == 2
    assert two["seen"] == 0
    assert two["total"] == 5

    workspace = Workspace(tmp_path / "ws", settings=Settings(), create=False)
    assert len(workspace.papers) == 5
    provenance = workspace.status()["provenance"]
    assert len(provenance["imports"]) == 2
    assert provenance["records"] == 5
    assert provenance["no_log_records"] == 5


def test_record_search_defaults_to_the_workspace_round(ws: Workspace) -> None:
    ws.set_round(5)
    # No explicit round: the live workspace round wins, not SearchParams' default.
    ws.record_search(SearchParams(query="q", sources=("openalex",)), _result([_paper("10.1/a")]))
    entry = json.loads((ws.path / LOG_JSONL).read_text().splitlines()[0])
    assert entry["round"] == 5
    assert ws.round == 5
