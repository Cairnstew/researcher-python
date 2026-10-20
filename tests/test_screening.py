"""Batch screening: apply many label updates without losing a record.

The single-record path already existed; these tests pin the properties that
make a batch safe: validate before writing, report unmatched ids, apply a
group update to every member, and never turn "last update wins" into an
unpredictable merge.
"""

from __future__ import annotations

import json

import pytest

from researcher_python.config import Settings
from researcher_python.errors import ConfigurationError
from researcher_python.models import Paper
from researcher_python.pipeline import PipelineResult
from researcher_python.screening import LabelUpdate, apply_updates
from researcher_python.workspace import META_FILE, SearchParams, Workspace


def _paper(key: str, **kwargs) -> Paper:
    return Paper(doi=key, title=kwargs.pop("title", key), sources=["openalex"], **kwargs)


def _seed(ws: Workspace, papers: list[Paper]) -> None:
    ws.record_search(
        SearchParams(query="seed", sources=("openalex",), round=1),
        PipelineResult(papers=papers, by_source={"openalex": len(papers)}),
    )


def test_apply_updates_labels_many_records_in_one_pass() -> None:
    papers = [_paper("10.1/a"), _paper("10.1/b"), _paper("10.1/c")]
    outcome = apply_updates(
        papers,
        [
            {"id": "10.1/a", "label": "core"},
            {"id": "10.1/b", "label": "excluded", "reason": "off topic"},
        ],
    )
    assert outcome.labelled == 2
    assert [p.triage_label() for p in papers] == ["core", "excluded", "unreviewed"]
    assert papers[1].exclusion_reason == "off topic"


def test_a_note_only_update_leaves_the_label_alone() -> None:
    paper = _paper("10.1/a", title="A")
    paper.label_as("core")
    outcome = apply_updates([paper], [{"id": "10.1/a", "note": "re-read methods"}])
    assert outcome.labelled == 1
    assert paper.triage_label() == "core"
    assert "re-read methods" in paper.notes


def test_ids_applies_one_update_to_a_group() -> None:
    papers = [_paper("10.1/a"), _paper("10.1/b"), _paper("10.1/c")]
    outcome = apply_updates(
        papers, [{"ids": ["10.1/a", "10.1/c"], "label": "general"}]
    )
    assert outcome.labelled == 2
    assert [p.triage_label() for p in papers] == ["general", "unreviewed", "general"]


def test_all_records_is_explicit() -> None:
    papers = [_paper("10.1/a"), _paper("10.1/b")]
    outcome = apply_updates(papers, [{"all": True, "label": "excluded"}])
    assert outcome.labelled == 2
    assert {p.triage_label() for p in papers} == {"excluded"}


def test_an_identifier_can_be_any_known_id_or_the_title() -> None:
    paper = Paper(
        doi="10.1/a",
        arxiv_id="2401.00001",
        pmid="12345",
        title="An exact title",
        sources=["openalex"],
    )
    for identifier in ("10.1/a", "2401.00001", "12345", "An exact title"):
        paper.label = ""
        outcome = apply_updates([paper], [{"id": identifier, "label": "core"}])
        assert outcome.labelled == 1, identifier
        assert paper.triage_label() == "core"


def test_unmatched_ids_are_reported_not_silently_ignored() -> None:
    outcome = apply_updates([_paper("10.1/a")], [{"id": "10.1/typo", "label": "core"}])
    assert outcome.labelled == 0
    assert outcome.unmatched == ["10.1/typo"]


def test_an_invalid_label_refuses_the_whole_batch_by_default() -> None:
    papers = [_paper("10.1/a")]
    with pytest.raises(ConfigurationError, match="update 1"):
        apply_updates(
            papers,
            [
                {"id": "10.1/a", "label": "core"},
                {"id": "10.1/a", "label": "maybe"},
            ],
        )
    # Validation happened before mutation.
    assert papers[0].triage_label() == "unreviewed"


def test_lenient_mode_applies_the_valid_updates_and_reports_the_rest() -> None:
    papers = [_paper("10.1/a"), _paper("10.1/b")]
    outcome = apply_updates(
        papers,
        [
            {"id": "10.1/a", "label": "core"},
            {"id": "10.1/b", "label": "maybe"},
        ],
        strict=False,
    )
    assert outcome.labelled == 1
    assert papers[0].triage_label() == "core"
    assert papers[1].triage_label() == "unreviewed"
    assert outcome.invalid[0]["index"] == 1


def test_last_update_for_one_record_wins_and_counts_it_once() -> None:
    paper = _paper("10.1/a")
    outcome = apply_updates(
        [paper],
        [
            {"id": "10.1/a", "label": "core"},
            {"id": "10.1/a", "label": "excluded", "reason": "final"},
        ],
    )
    assert outcome.labelled == 1
    assert paper.triage_label() == "excluded"
    assert paper.exclusion_reason == "final"


def test_label_update_dataclass_accepts_a_mapping() -> None:
    update = LabelUpdate.from_dict({"ids": "10.1/a", "set_label": "core"})
    assert update.ids == ("10.1/a",)
    assert update.label == "core"


def test_workspace_label_many_writes_meta_and_summary(tmp_path) -> None:
    ws = Workspace(tmp_path / "ws", settings=Settings())
    _seed(ws, [_paper("10.1/a"), _paper("10.1/b")])
    result = ws.label_many(
        [
            {"id": "10.1/a", "label": "core"},
            {"id": "10.1/b", "label": "excluded", "reason": "off topic"},
        ]
    )
    assert result["labelled"] == 2
    assert result["summary"]["core"] == 1
    assert result["summary"]["excluded"] == 1
    meta = json.loads((ws.path / META_FILE).read_text())
    assert meta["counts"]["screened"] == 2
    assert meta["last_label"]["labels"] == ["core", "excluded"]


def test_workspace_label_still_labels_everything_without_ids(tmp_path) -> None:
    ws = Workspace(tmp_path / "ws", settings=Settings())
    _seed(ws, [_paper("10.1/a"), _paper("10.1/b")])
    result = ws.label(set_label="general")
    assert result["labelled"] == 2
    assert {p.triage_label() for p in ws.papers} == {"general"}


def test_workspace_screen_is_the_verb_alias(tmp_path) -> None:
    ws = Workspace(tmp_path / "ws", settings=Settings())
    _seed(ws, [_paper("10.1/a")])
    result = ws.screen(ids=["10.1/a"], set_label="core")
    assert result["labelled"] == 1
    assert ws.papers[0].triage_label() == "core"
