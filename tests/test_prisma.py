"""PRISMA-style counts: the flow must add up, and gaps must be visible."""

from __future__ import annotations

import json

from researcher_python.config import Settings
from researcher_python.models import Paper
from researcher_python.pipeline import PipelineResult
from researcher_python.prisma import prisma_report, render_prisma_markdown
from researcher_python.workspace import SearchParams, Workspace


def _paper(key: str, label: str = "", reason: str = "") -> Paper:
    paper = Paper(doi=key, title=key, sources=["openalex"])
    if label:
        paper.label_as(label, reason)
    return paper


def _entry(returned: int, by_source: dict[str, int], **kwargs) -> dict:
    return {
        "search_id": kwargs.pop("search_id", "s1"),
        "channel": kwargs.pop("channel", "search"),
        "round": kwargs.pop("round", 1),
        "by_source": by_source,
        "outcome": {"returned": returned, "kept": returned},
        **kwargs,
    }


def test_the_flow_counts_identification_duplicates_and_screening() -> None:
    papers = [
        _paper("10.1/a", "core"),
        _paper("10.1/b", "general"),
        _paper("10.1/c", "excluded", "off topic"),
        _paper("10.1/d"),
    ]
    # Two searches identified 6 raw records; 4 survive dedupe.
    entries = [
        _entry(3, {"openalex": 2, "crossref": 1}),
        _entry(3, {"openalex": 2, "crossref": 1}, search_id="s2"),
    ]
    report = prisma_report(papers, entries)
    assert report["identified"] == 6
    assert report["identified_merged"] == 6
    assert report["deduped"] == 4
    assert report["duplicates_removed"] == 2
    assert report["screened"] == 3
    assert report["excluded"] == 1
    assert report["included"] == 2
    assert report["core"] == 1
    assert report["general"] == 1
    assert report["unreviewed"] == 1
    assert report["ready"] is False
    assert report["by_source"] == {"crossref": 2, "openalex": 4}


def test_type_exclusions_are_not_counted_as_duplicates() -> None:
    papers = [_paper("10.1/a", "core"), _paper("10.1/b", "general")]
    entry = {
        "search_id": "s1",
        "channel": "search",
        "round": 1,
        "by_source": {"openalex": 4},
        "outcome": {
            "returned": 2,
            "kept": 2,
            "dropped_types": 2,
            "dropped_type_counts": {"grant": 1, "standard": 1},
        },
    }
    report = prisma_report(papers, [entry])
    assert report["identified"] == 4
    assert report["type_excluded"] == 2
    assert report["type_excluded_by_type"] == {"grant": 1, "standard": 1}
    assert report["duplicates_removed"] == 0
    assert report["unaccounted"] == 0


def test_an_imported_set_without_a_log_is_reported_as_unaccounted() -> None:
    report = prisma_report([_paper("10.1/a"), _paper("10.1/b")], [])
    assert report["identified"] == 0
    assert report["unaccounted"] == 2
    assert report["duplicates_removed"] == 0
    assert any("no search log rows" in warning for warning in report["warnings"])


def test_more_rows_than_the_set_is_clamped_not_negative() -> None:
    report = prisma_report([_paper("10.1/a")], [_entry(10, {"openalex": 10})])
    assert report["duplicates_removed"] == 9
    assert report["unaccounted"] == 0


def test_exclusion_reasons_are_grouped_and_prescreen_is_separate() -> None:
    papers = [
        _paper("10.1/a", "excluded", "off topic"),
        _paper("10.1/b", "excluded", "off topic"),
        _paper("10.1/c", "excluded", "pre-screen: no abstract"),
        _paper("10.1/d", "excluded", ""),
    ]
    report = prisma_report(papers, [_entry(4, {"openalex": 4})])
    assert report["excluded_by_reason"] == {
        "off topic": 2,
        "(no reason given)": 1,
        "pre-screen: no abstract": 1,
    }
    assert report["prescreen_excluded"] == 1


def test_legacy_provenance_warns() -> None:
    report = prisma_report(
        [_paper("10.1/a", "core")],
        [_entry(1, {"openalex": 1}, channel="legacy")],
        provenance={"legacy": True},
    )
    assert any("legacy import" in warning for warning in report["warnings"])


def test_markdown_renders_the_flow_and_notes() -> None:
    report = prisma_report(
        [_paper("10.1/a", "core"), _paper("10.1/b", "excluded", "off topic")],
        [_entry(2, {"openalex": 2})],
    )
    text = render_prisma_markdown(report)
    assert "# PRISMA-style screening flow" in text
    assert "| Records identified | 2 |" in text
    assert "| Excluded" not in text
    assert "off topic" in text
    assert "full-text eligibility" in text


def test_workspace_prisma_uses_its_log_and_labels(tmp_path) -> None:
    ws = Workspace(tmp_path / "ws", settings=Settings())
    ws.record_search(
        SearchParams(query="seed", sources=("openalex",), round=1),
        PipelineResult(
            papers=[_paper("10.1/a"), _paper("10.1/b")],
            by_source={"openalex": 2},
        ),
    )
    ws.label(ids=["10.1/a"], set_label="core")
    report = ws.prisma()
    assert report["identified"] == 2
    assert report["deduped"] == 2
    assert report["included"] == 1
    assert report["unreviewed"] == 1
    # The meta file is JSON-serialisable, and so is the report.
    json.dumps(report)
