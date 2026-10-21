"""The research handoff bundle: filtering, citation keys and screening gates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from researcher_python import format as bib
from researcher_python.errors import ConfigurationError
from researcher_python.handoff import DEFAULT_LABELS, build_handoff
from researcher_python.models import Author, Paper
from researcher_python.pipeline import PipelineResult
from researcher_python.config import Settings
from researcher_python.workspace import SearchParams, Workspace


def _paper(
    *,
    doi: str = "",
    arxiv_id: str = "",
    title: str = "A paper",
    author: str = "Smith, Ann",
    year: int = 2020,
    label: str = "",
) -> Paper:
    paper = Paper(
        doi=doi,
        arxiv_id=arxiv_id,
        title=title,
        authors=[Author(author)],
        year=year,
        venue="Journal of Tests",
        work_type="journal-article",
    )
    if label:
        paper.label_as(label)
    return paper


def _workspace(tmp_path: Path, papers: list[Paper], *, topic: str = "Testing") -> Workspace:
    workspace = Workspace(tmp_path / "ws", settings=Settings(), create=True)
    workspace.meta["topic"] = topic
    workspace._write_meta(workspace.meta)
    workspace.record_search(
        SearchParams(query="testing", sources=("openalex",)),
        PipelineResult(papers=papers, by_source={"openalex": len(papers)}),
    )
    return workspace


def _ready_papers() -> list[Paper]:
    return [
        _paper(doi="10.1/a", title="Attention and memory", label="core"),
        _paper(doi="10.1/b", title="A second source", author="Jones, Bo", year=2021, label="general"),
        _paper(doi="10.1/c", title="Off topic", author="Other, O.", year=2019, label="excluded"),
    ]


def test_writes_the_bundle_and_filters_labels(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, _ready_papers())
    receipt = build_handoff(workspace.path, tmp_path / "handoff", settings=Settings())

    assert receipt["ok"] is True
    out = Path(receipt["out_dir"])
    for name in (
        "handoff.json",
        "papers.json",
        "references.bib",
        "citation-map.json",
        "matrix.md",
        "synthesis.md",
        "prisma.md",
        "README.md",
    ):
        assert (out / name).is_file(), name

    manifest = json.loads((out / "handoff.json").read_text())
    assert manifest["schema_version"] == 1
    assert manifest["labels"] == list(DEFAULT_LABELS)
    assert manifest["counts"]["core"] == 1
    assert manifest["counts"]["general"] == 1
    assert manifest["counts"]["excluded"] == 1
    assert manifest["counts"]["unreviewed"] == 0
    assert manifest["counts"]["included"] == 2
    assert manifest["screening"]["ready"] is True

    bib_text = (out / "references.bib").read_text()
    assert bib_text.count("@") == 2
    assert "Off topic" not in bib_text

    citation_map = json.loads((out / "citation-map.json").read_text())
    assert set(citation_map["by_id"]) == {"10.1/a", "10.1/b"}
    assert len(citation_map["entries"]) == 2

    # Every mapped key is actually the key in the bibliography.
    for entry in citation_map["entries"]:
        assert f"{{{entry['key']}," in bib_text


def test_excluded_and_unreviewed_are_not_in_the_bibliography(tmp_path: Path) -> None:
    papers = _ready_papers() + [
        _paper(doi="10.1/d", title="Unreviewed", author="New, N.", year=2022)
    ]
    workspace = _workspace(tmp_path, papers)
    receipt = build_handoff(
        workspace.path,
        tmp_path / "handoff",
        settings=Settings(),
        require_screening=False,
    )
    assert receipt["counts"]["unreviewed"] == 1
    bib_text = (Path(receipt["out_dir"]) / "references.bib").read_text()
    assert bib_text.count("@") == 2
    assert "Unreviewed" not in bib_text


def test_unscreened_set_is_refused_by_default(tmp_path: Path) -> None:
    papers = _ready_papers() + [
        _paper(doi="10.1/d", title="Unreviewed", author="New, N.", year=2022)
    ]
    workspace = _workspace(tmp_path, papers)

    with pytest.raises(ConfigurationError, match="screening incomplete"):
        build_handoff(workspace.path, tmp_path / "handoff", settings=Settings())


def test_labels_narrow_the_included_set(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, _ready_papers())
    receipt = build_handoff(
        workspace.path,
        tmp_path / "handoff",
        settings=Settings(),
        labels=("core",),
    )
    assert receipt["counts"]["included"] == 1
    assert (Path(receipt["out_dir"]) / "references.bib").read_text().count("@") == 1


def test_collision_keys_match_the_citation_map(tmp_path: Path) -> None:
    papers = [
        _paper(doi="10.1/a", title="Same title", author="Smith, Ann", year=2020, label="core"),
        _paper(doi="10.1/b", title="Same topic", author="Smith, Ann", year=2020, label="core"),
    ]
    workspace = _workspace(tmp_path, papers)
    receipt = build_handoff(workspace.path, tmp_path / "handoff", settings=Settings())

    bib_text = (Path(receipt["out_dir"]) / "references.bib").read_text()
    keys = [entry["key"] for entry in json.loads(
        (Path(receipt["out_dir"]) / "citation-map.json").read_text()
    )["entries"]]
    assert len(set(keys)) == 2
    assert all(f"{{{key}," in bib_text for key in keys)


def test_result_file_source_has_no_prisma_or_workspace_requirement(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, _ready_papers())
    results = tmp_path / "results.json"
    workspace.export_papers(results)

    receipt = build_handoff(
        results,
        tmp_path / "handoff",
        settings=Settings(),
        include_prisma=False,
    )
    out = Path(receipt["out_dir"])
    assert receipt["source_kind"] == "file"
    assert (out / "papers.json").is_file()
    assert not (out / "prisma.md").exists()
    assert json.loads((out / "handoff.json").read_text())["source_kind"] == "file"


def test_existing_bundle_requires_force(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, _ready_papers())
    build_handoff(workspace.path, tmp_path / "handoff", settings=Settings())
    with pytest.raises(ConfigurationError, match="already exists"):
        build_handoff(workspace.path, tmp_path / "handoff", settings=Settings())

    assert build_handoff(
        workspace.path,
        tmp_path / "handoff",
        settings=Settings(),
        force=True,
    )["ok"]


def test_unknown_label_is_a_configuration_error(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, _ready_papers())
    with pytest.raises(ConfigurationError, match="unknown label"):
        build_handoff(
            workspace.path,
            tmp_path / "handoff",
            settings=Settings(),
            labels=("included",),
        )


def test_bibtex_key_map_uses_the_same_unique_keys() -> None:
    papers = [
        _paper(doi="10.1/a", title="Same title", label="core"),
        _paper(doi="10.1/b", title="Same title", label="core"),
    ]
    pairs = bib.bibtex_pairs(papers)
    mapping = bib.bibtex_key_map(papers)
    assert [key for _paper, key, _entry in pairs] == ["smith2020same", "smith2020samea"]
    assert mapping == {"10.1/a": "smith2020same", "10.1/b": "smith2020samea"}
    assert bib.render_bibtex(papers).count("@") == 2
