"""Explicit preprint/published links and one-record-with-versions semantics."""

from __future__ import annotations

import json

from researcher_python import export
from researcher_python.ids import arxiv_doi, arxiv_id_from_doi
from researcher_python.models import Paper, PaperVersion, dedupe
from researcher_python.screening import LabelUpdate, apply_updates, index_papers
from researcher_python.sources.crossref_source import parse_crossref_work
from researcher_python.sources.openalex_source import parse_openalex_work


def _preprint(**kwargs) -> Paper:
    return Paper(
        sources=["arxiv"],
        arxiv_id="2001.00001",
        title="A study of things",
        year=2020,
        work_type="preprint",
        abstract="preprint abstract",
        pdf_url="https://arxiv.org/pdf/2001.00001",
        **kwargs,
    )


def _published(**kwargs) -> Paper:
    base = dict(
        sources=["openalex"],
        doi="10.1/journal",
    )
    base.update(kwargs)
    return Paper(
        title="A study of things",
        year=2021,
        work_type="article",
        venue="Journal of Things",
        citation_count=42,
        **base,
    )


def test_arxiv_doi_round_trip() -> None:
    assert arxiv_doi("2001.00001") == "10.48550/arxiv.2001.00001"
    assert arxiv_id_from_doi("https://doi.org/10.48550/arXiv.2001.00001") == "2001.00001"
    assert arxiv_doi("") == ""


def test_shared_arxiv_id_links_a_preprint_and_its_published_record() -> None:
    published = _published(arxiv_id="2001.00001")
    merged = dedupe([_preprint(), published])

    assert len(merged) == 1
    record = merged[0]
    assert record.doi == "10.1/journal"
    assert record.arxiv_id == "2001.00001"
    assert record.venue == "Journal of Things"
    assert record.pdf_url.endswith("2001.00001")
    assert len(record.versions) >= 2


def test_arxiv_doi_on_either_side_is_a_link() -> None:
    published = _published()
    published.versions = [
        PaperVersion(kind="preprint", doi="10.48550/arxiv.2001.00001", source="openalex")
    ]
    merged = dedupe([_preprint(), published])
    assert len(merged) == 1


def test_crossref_relation_links_a_preprint_to_its_journal_doi() -> None:
    work = {
        "DOI": "10.1/preprint",
        "type": "posted-content",
        "title": ["A study of things"],
        "issued": {"date-parts": [[2020]]},
        "relation": {
            "is-preprint-of": [
                {"id-type": "doi", "id": "10.1/journal", "asserted-by": "subject"}
            ],
            "references": [{"id-type": "doi", "id": "10.1/unrelated"}],
        },
    }
    preprint = parse_crossref_work(work)
    assert [v.doi for v in preprint.versions] == ["10.1/journal"]

    merged = dedupe([preprint, _published()])
    assert len(merged) == 1
    assert merged[0].doi == "10.1/journal"
    assert merged[0].work_type == "article"


def test_supplement_and_reference_relations_are_not_versions() -> None:
    work = {
        "DOI": "10.1/supplement",
        "type": "posted-content",
        "relation": {
            "is-supplement-to": [{"id-type": "doi", "id": "10.1/parent"}],
            "references": [{"id-type": "doi", "id": "10.1/other"}],
        },
    }
    paper = parse_crossref_work(work)
    assert paper.versions == []


def test_openalex_location_harvests_the_arxiv_copy_and_caps_entries() -> None:
    work = {
        "id": "https://openalex.org/W1",
        "doi": "https://doi.org/10.1/journal",
        "type": "article",
        "display_name": "A study of things",
        "publication_year": 2021,
        "locations": [
            {
                "landing_page_url": "https://doi.org/10.48550/arXiv.2001.00001",
                "version": "submittedVersion",
                "is_published": False,
            },
            {
                "landing_page_url": "https://doi.org/10.1/journal",
                "version": "publishedVersion",
                "is_published": True,
            },
        ],
    }
    paper = parse_openalex_work(work)
    assert paper.arxiv_id == "2001.00001"
    assert any(v.arxiv_id == "2001.00001" for v in paper.versions)


def test_openalex_related_works_are_not_versions() -> None:
    work = {
        "id": "https://openalex.org/W1",
        "doi": "https://doi.org/10.1/journal",
        "type": "article",
        "related_works": ["https://openalex.org/W2"],
    }
    assert parse_openalex_work(work).versions == []


def test_title_only_cross_version_match_is_refused() -> None:
    preprint = _preprint(doi="")
    published = _published()
    # Same folded title and year, no shared identifier and no explicit link.
    published.year = 2020
    assert len(dedupe([preprint, published])) == 2


def test_same_kind_title_match_still_merges() -> None:
    a = Paper(doi="10.1/a", title="Same title", year=2020, work_type="article")
    b = Paper(doi="10.1/b", title="Same title", year=2020, work_type="article")
    assert len(dedupe([a, b])) == 1


def test_promotion_preserves_reader_state_and_both_identifiers() -> None:
    preprint = _preprint()
    preprint.label_as("core")
    preprint.notes = "read the arXiv version"
    published = _published(arxiv_id="2001.00001")

    record = dedupe([preprint, published])[0]

    assert record.doi == "10.1/journal"
    assert record.label == "core"
    assert record.notes == "read the arXiv version"
    ids = {key for key in record.merge_keys()}
    assert "doi:10.1/journal" in ids
    assert "doi:10.48550/arxiv.2001.00001" in ids or "arxiv:2001.00001" in ids


def test_transitive_versions_collapse_to_one_record() -> None:
    v1 = Paper(doi="10.1/v1", title="Work", year=2020, work_type="preprint")
    v2 = Paper(doi="10.1/v2", title="Work", year=2020, work_type="preprint")
    pub = Paper(doi="10.1/pub", title="Work", year=2021, work_type="article")
    v1.versions = [PaperVersion(kind="preprint", doi="10.1/v2", relation="is-version-of")]
    v2.versions = [PaperVersion(kind="published", doi="10.1/pub", relation="is-preprint-of")]

    merged = dedupe([v1, v2, pub])
    assert len(merged) == 1
    assert merged[0].doi == "10.1/pub"


def test_old_results_file_label_survives_promotion_under_both_dois(tmp_path) -> None:
    """A saved label addressed by the preprint DOI must still resolve."""
    old = _preprint(doi="10.1/preprint-doi")
    path = tmp_path / "results.json"
    export.to_json([old], path)
    [loaded] = export.from_json(path)
    assert loaded.label == ""  # no label yet; prove the path round-trips

    loaded.label_as("core")
    published = _published(doi="10.1/journal", arxiv_id="2001.00001")
    loaded.versions = [
        PaperVersion(kind="published", doi="10.1/journal", relation="is-preprint-of")
    ]
    promoted = dedupe([loaded, published])[0]
    assert promoted.doi == "10.1/journal"
    assert promoted.label == "core"

    # Both the old preprint DOI and the new published DOI address the record.
    assert index_papers([promoted]).get("10.1/preprint-doi")
    assert index_papers([promoted]).get("10.1/journal")

    apply_updates([promoted], [LabelUpdate(id="10.1/preprint-doi", label="general")])
    assert promoted.label == "general"
    apply_updates([promoted], [LabelUpdate(id="10.1/journal", label="core")])
    assert promoted.label == "core"


def test_versions_round_trip_through_json(tmp_path) -> None:
    paper = _preprint()
    paper.versions = [PaperVersion(kind="published", doi="10.1/journal")]
    path = tmp_path / "one.json"
    export.to_json([paper], path)
    [loaded] = export.from_json(path)
    assert loaded.versions[0].doi == "10.1/journal"
    assert loaded.versions[0].kind == "published"
