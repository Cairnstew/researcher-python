"""Rule-based pre-screening: flag the obvious, leave the middle to judgement."""

from __future__ import annotations

import pytest

from researcher_python.config import Settings
from researcher_python.errors import ConfigurationError
from researcher_python.models import Paper
from researcher_python.pipeline import PipelineResult
from researcher_python.screening import PrescreenCriteria, prescreen
from researcher_python.workspace import SearchParams, Workspace


def _paper(key: str, **kwargs) -> Paper:
    return Paper(doi=key, title=kwargs.pop("title", key), sources=["openalex"], **kwargs)


def test_each_rule_flags_its_own_reason() -> None:
    papers = [
        _paper("10.1/title", title="", abstract="a"),
        _paper("10.1/abstract", abstract=""),
        _paper("10.1/type", abstract="a", work_type="dataset"),
        _paper("10.1/year", abstract="a", year=2010),
        _paper("10.1/ok", abstract="a", year=2022, work_type="article"),
    ]
    report = prescreen(
        papers,
        PrescreenCriteria(year_from=2019, year_to=2024),
    )
    reasons = {hit.paper.doi: hit.reasons for hit in report.hits}
    assert reasons["10.1/title"] == ["no title"]
    assert reasons["10.1/abstract"] == ["no abstract"]
    assert reasons["10.1/type"] == ["type: dataset"]
    assert reasons["10.1/year"][0].startswith("outside year window")
    assert "10.1/ok" not in reasons


def test_an_unknown_year_is_kept() -> None:
    report = prescreen(
        [_paper("10.1/a", abstract="a", year=None)],
        PrescreenCriteria(year_from=2019),
    )
    assert report.flagged == 0


def test_an_unknown_work_type_is_kept() -> None:
    report = prescreen(
        [_paper("10.1/a", abstract="a", work_type="")],
        PrescreenCriteria(),
    )
    assert report.flagged == 0


def test_a_known_human_label_is_skipped_by_default() -> None:
    paper = _paper("10.1/a", abstract="")
    paper.label_as("core")
    report = prescreen([paper], PrescreenCriteria())
    assert report.flagged == 0
    assert report.skipped_labelled == 1


def test_a_rule_subset_is_selectable() -> None:
    report = prescreen(
        [_paper("10.1/a", abstract="")],
        PrescreenCriteria(rules=("no-title",)),
    )
    assert report.flagged == 0


def test_an_unknown_rule_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError, match="unknown pre-screen rule"):
        prescreen([_paper("10.1/a")], PrescreenCriteria(rules=("no-such-rule",)))


def test_counts_are_per_rule_and_reasons_combine() -> None:
    report = prescreen(
        [_paper("10.1/a", title="", abstract="")],
        PrescreenCriteria(),
    )
    assert report.flagged == 1
    assert report.hits[0].reasons == ["no title", "no abstract"]
    assert report.counts_by_rule["no-title"] == 1
    assert report.counts_by_rule["no-abstract"] == 1


def test_workspace_apply_writes_only_unreviewed_records(tmp_path) -> None:
    ws = Workspace(tmp_path / "ws", settings=Settings())
    kept = _paper("10.1/kept", abstract="", year=2020)
    kept.label_as("core")
    ws.record_search(
        SearchParams(query="seed", sources=("openalex",), round=1),
        PipelineResult(papers=[kept, _paper("10.1/flagged", abstract="")]),
    )

    dry = ws.prescreen(criteria=PrescreenCriteria())
    assert dry["flagged"] == 1
    assert ws.papers[1].triage_label() == "unreviewed"

    applied = ws.prescreen(apply=True, criteria=PrescreenCriteria())
    assert applied["applied"] == 1
    assert ws.papers[0].triage_label() == "core"  # never overwritten
    assert ws.papers[1].triage_label() == "excluded"
    assert ws.papers[1].exclusion_reason.startswith("pre-screen:")

    again = ws.prescreen(apply=True, criteria=PrescreenCriteria())
    assert again["applied"] == 0


def test_report_dict_caps_candidates() -> None:
    papers = [_paper(f"10.1/{i}", abstract="") for i in range(5)]
    report = prescreen(papers, PrescreenCriteria())
    payload = report.as_dict(limit=2)
    assert payload["flagged"] == 5
    assert len(payload["candidates"]) == 2
    assert payload["truncated"] == 3
