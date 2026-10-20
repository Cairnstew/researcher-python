"""Snowballing: link-count ranking, only-new filtering and provenance.

The citation APIs are faked here, so the tests pin the local transformation:
which records survive, how they are ranked, and what the workspace writes.
"""

from __future__ import annotations

import json

from researcher_python import snowball
from researcher_python.citation import CitationResult
from researcher_python.config import Settings
from researcher_python.errors import SourceError
from researcher_python.models import Paper
from researcher_python.pipeline import PipelineResult
from researcher_python.workspace import SearchParams, Workspace


def _paper(key: str, **kwargs) -> Paper:
    return Paper(doi=key, title=kwargs.pop("title", key), sources=["openalex"], **kwargs)


def _answer(direction: str, papers: list[Paper], **kwargs) -> CitationResult:
    return CitationResult(
        direction=direction,
        papers=papers,
        by_source=kwargs.pop("by_source", {"openalex": len(papers)}),
        errors=kwargs.pop("errors", {}),
        truncated=kwargs.pop("truncated", {}),
    )


def test_candidates_linked_by_more_core_papers_rank_first(monkeypatch) -> None:
    core = [_paper("10.1/a"), _paper("10.1/b")]
    shared = _paper("10.1/shared", citation_count=100)
    lone = _paper("10.1/lone", citation_count=999)

    def fake(paper, **kwargs):
        return _answer(
            "references",
            [shared, lone] if paper.doi == "10.1/a" else [shared],
        )

    monkeypatch.setattr(snowball.citation, "references", fake)
    result = snowball.expand(core, direction="references")

    ranked = result.ranked()
    assert [item.paper.doi for item in ranked] == ["10.1/shared", "10.1/lone"]
    assert ranked[0].links == 2
    assert ranked[0].linked_by == ["10.1/a", "10.1/b"]


def test_records_already_in_known_are_dropped(monkeypatch) -> None:
    known = _paper("10.1/known")
    monkeypatch.setattr(
        snowball.citation,
        "references",
        lambda paper, **kwargs: _answer("references", [known, _paper("10.1/new")]),
    )
    result = snowball.expand([_paper("10.1/a")], known=[known])
    assert result.known == 1
    assert [item.paper.doi for item in result.candidates] == ["10.1/new"]


def test_both_directions_union_and_dedupe(monkeypatch) -> None:
    core = [_paper("10.1/a")]
    candidate = _paper("10.1/c")
    monkeypatch.setattr(
        snowball.citation,
        "references",
        lambda paper, **kwargs: _answer("references", [candidate]),
    )
    monkeypatch.setattr(
        snowball.citation,
        "citations",
        lambda paper, **kwargs: _answer("citations", [candidate]),
    )
    result = snowball.expand(core, direction="both")
    assert len(result.candidates) == 1
    assert result.candidates[0].linked_by == ["10.1/a"]


def test_min_links_filters_the_weak_tail(monkeypatch) -> None:
    core = [_paper("10.1/a"), _paper("10.1/b")]
    shared = _paper("10.1/shared")
    lone = _paper("10.1/lone")
    monkeypatch.setattr(
        snowball.citation,
        "references",
        lambda paper, **kwargs: _answer(
            "references", [shared, lone] if paper.doi == "10.1/a" else [shared]
        ),
    )
    result = snowball.expand(core, direction="references")
    assert [item.paper.doi for item in result.ranked(min_links=2)] == ["10.1/shared"]


def test_one_core_error_does_not_abort_the_others(monkeypatch) -> None:
    def fake(paper, **kwargs):
        if paper.doi == "10.1/bad":
            raise SourceError("openalex", "boom")
        return _answer("references", [_paper("10.1/c")])

    monkeypatch.setattr(snowball.citation, "references", fake)
    result = snowball.expand(
        [_paper("10.1/bad"), _paper("10.1/good")], direction="references"
    )
    assert result.errors["10.1/bad"]["query"] == "boom"
    assert [item.paper.doi for item in result.candidates] == ["10.1/c"]


def test_a_core_without_an_identifier_is_reported(monkeypatch) -> None:
    monkeypatch.setattr(
        snowball.citation,
        "references",
        lambda paper, **kwargs: _answer("references", []),
    )
    result = snowball.expand([Paper(title="No id")], direction="references")
    assert result.core == []
    assert result.skipped[0]["reason"].startswith("no identifier")


def test_direction_aliases_are_accepted() -> None:
    assert snowball.normalize_direction("refs") == "references"
    assert snowball.normalize_direction("cites") == "citations"


def test_workspace_expand_persists_and_logs_one_row(monkeypatch, tmp_path) -> None:
    ws = Workspace(tmp_path / "ws", settings=Settings())
    core = _paper("10.1/a")
    ws.record_search(
        SearchParams(query="seed", sources=("openalex",), round=1),
        PipelineResult(papers=[core], by_source={"openalex": 1}),
    )
    ws.label(ids=["10.1/a"], set_label="core")

    candidate = _paper("10.1/c", title="Candidate")
    monkeypatch.setattr(
        snowball.citation,
        "references",
        lambda paper, **kwargs: _answer(
            "references", [candidate], by_source={"openalex": 1}
        ),
    )
    monkeypatch.setattr(
        snowball.citation,
        "citations",
        lambda paper, **kwargs: _answer("citations", [], by_source={}),
    )

    payload = ws.expand(direction="both", persist=True)
    assert payload["persisted"] is True
    assert payload["new"] == 1
    assert "10.1/c" in {p.doi for p in ws.papers}

    entry = ws.log_entries()[-1]
    assert entry["channel"] == "snowball"
    assert entry["params"]["query"] == "expand:both"
    assert entry["expansion"]["core"] == ["10.1/a"]
    assert entry["expansion"]["direction"] == "both"


def test_workspace_expand_preview_writes_nothing(monkeypatch, tmp_path) -> None:
    ws = Workspace(tmp_path / "ws", settings=Settings())
    ws.record_search(
        SearchParams(query="seed", sources=("openalex",), round=1),
        PipelineResult(papers=[_paper("10.1/a")], by_source={"openalex": 1}),
    )
    ws.label(ids=["10.1/a"], set_label="core")
    before = json.loads((ws.path / "results.json").read_text())
    monkeypatch.setattr(
        snowball.citation,
        "references",
        lambda paper, **kwargs: _answer("references", [_paper("10.1/c")]),
    )
    payload = ws.expand(direction="references", persist=False)
    assert payload["persisted"] is False
    assert payload["new"] == 1
    assert json.loads((ws.path / "results.json").read_text()) == before
