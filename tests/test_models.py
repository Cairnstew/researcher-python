"""Offline tests for :mod:`researcher_python.models` — the shared record type."""

from __future__ import annotations

import pytest

from researcher_python.models import (
    SORT_MODES,
    Author,
    Paper,
    dedupe,
    merge,
    sort_papers,
)


def make(**kwargs: object) -> Paper:
    return Paper(**kwargs)  # type: ignore[arg-type]


# -- serialization ---------------------------------------------------------


def test_to_dict_from_dict_round_trip() -> None:
    original = make(
        sources=["openalex", "crossref"],
        ids={"doi": "10.1/x", "openalex": "W1"},
        doi="10.1/x",
        arxiv_id="1706.03762",
        title="A title",
        abstract="An abstract",
        authors=[Author(name="Ada Lovelace", orcid="0000-0002-1825-0097",
                        affiliations=["Somewhere"])],
        year=2021,
        published="2021-04-15",
        venue="Journal",
        citation_count=7,
        reference_count=3,
        is_open_access=True,
        categories=["Physics"],
        keywords=["testing"],
    )

    restored = Paper.from_dict(original.to_dict())

    assert restored.to_dict() == original.to_dict()
    assert restored.sources == ["openalex", "crossref"]
    assert restored.authors[0].affiliations == ["Somewhere"]
    assert restored.authors[0].orcid == "0000-0002-1825-0097"
    assert restored.citation_count == 7
    assert restored.is_open_access is True


def test_from_dict_ignores_unknown_keys() -> None:
    paper = Paper.from_dict({"title": "Kept", "brand_new_field": "ignored"})
    assert paper.title == "Kept"
    assert not hasattr(paper, "brand_new_field")


def test_from_dict_tolerates_missing_keys() -> None:
    paper = Paper.from_dict({})
    assert paper.title == ""
    assert paper.year is None
    assert paper.authors == []
    assert paper.ids == {}
    assert paper.sources == []


def test_from_dict_coerces_wrong_typed_values() -> None:
    paper = Paper.from_dict(
        {"year": "2019", "citation_count": None, "is_open_access": 1, "ids": "not-a-map"}
    )
    assert paper.year == 2019
    assert paper.citation_count is None
    assert paper.is_open_access is True
    assert paper.ids == {}


def test_to_dict_is_json_friendly_and_stable() -> None:
    first = make(title="T", year=2020, ids={"doi": "10.1/x"}).to_dict()
    second = make(title="T", year=2020, ids={"doi": "10.1/x"}).to_dict()
    assert first == second  # deterministic key order
    assert list(first)[:3] == ["sources", "ids", "doi"]


# -- identity helpers ------------------------------------------------------


def test_merge_keys_are_most_authoritative_first() -> None:
    paper = make(doi="10.1/x", arxiv_id="1706.03762", pmid="123", pmcid="PMC1")
    assert paper.merge_keys() == [
        "doi:10.1/x",
        "arxiv:1706.03762",
        "pmid:123",
        "pmcid:PMC1",
    ]
    # Empty identifiers contribute nothing.
    assert make(doi="10.1/x").merge_keys() == ["doi:10.1/x"]
    assert make().merge_keys() == []


def test_title_key_folds_and_returns_none_when_blank() -> None:
    assert make(title="Deep Learning!").title_key() == "deep learning"
    assert make(title="").title_key() is None


def test_primary_id_prefers_doi() -> None:
    assert make(doi="10.1/x", arxiv_id="1706.03762", pmid="1").primary_id() == "10.1/x"
    assert make(arxiv_id="1706.03762", pmid="1").primary_id() == "1706.03762"
    assert make(pmid="1").primary_id() == "1"
    assert make().primary_id() == ""


def test_source_and_id_for() -> None:
    paper = make(sources=["openalex"], ids={"openalex": "W1"})
    assert paper.source == "openalex"
    assert paper.id_for("openalex") == "W1"
    assert paper.id_for("crossref") == ""
    assert make().source == ""


# -- merge -----------------------------------------------------------------


def test_merge_target_wins_for_present_scalars() -> None:
    target = make(title="First", abstract="target abstract", pdf_url="https://a/p.pdf")
    other = make(title="Second", abstract="other abstract", pdf_url="https://b/p.pdf")

    merge(target, other)

    assert target.title == "First"                # already set -> untouched
    assert target.abstract == "target abstract"   # first record wins
    assert target.pdf_url == "https://a/p.pdf"


def test_merge_fills_empty_scalars_from_the_other_side() -> None:
    target = make(title="", abstract="", venue="Journal", year=None)
    other = make(title="Filled", abstract="Filled abstract", venue="Other", year=2020)

    merge(target, other)

    assert target.title == "Filled"
    assert target.abstract == "Filled abstract"
    assert target.venue == "Journal"  # target's value was already present
    assert target.year == 2020


def test_merge_unions_sources_ids_categories_and_keywords() -> None:
    target = make(sources=["openalex"], ids={"doi": "10.1/x"}, categories=["A"],
                  keywords=["k1"])
    other = make(sources=["crossref"], ids={"doi": "10.1/other", "pmid": "123"},
                 categories=["A", "B"], keywords=["k2"])

    merge(target, other)

    assert target.sources == ["openalex", "crossref"]
    assert target.ids == {"doi": "10.1/x", "pmid": "123"}  # target's id kept
    assert target.categories == ["A", "B"]
    assert target.keywords == ["k1", "k2"]


def test_merge_takes_the_larger_counts_and_or_open_access() -> None:
    target = make(citation_count=5, reference_count=None, is_open_access=False)
    other = make(citation_count=9, reference_count=2, is_open_access=True)

    merge(target, other)

    assert target.citation_count == 9   # max
    assert target.reference_count == 2  # filled in
    assert target.is_open_access is True


def test_merge_is_a_no_op_on_the_same_object() -> None:
    paper = make(title="Self")
    assert merge(paper, paper) is paper
    assert paper.title == "Self"


def test_merge_unions_authors_without_duplicates() -> None:
    target = make(authors=[Author(name="Ada Lovelace")])
    other = make(authors=[Author(name="Ada Lovelace"), Author(name="Alan Turing")])

    merge(target, other)

    assert [a.name for a in target.authors] == ["Ada Lovelace", "Alan Turing"]


# -- dedupe ----------------------------------------------------------------


def test_dedupe_by_doi_across_sources() -> None:
    first = make(sources=["openalex"], doi="10.1/x", title="One")
    second = make(sources=["crossref"], doi="10.1/x", title="Two", year=2020)

    result = dedupe([first, second])

    assert len(result) == 1
    assert result[0].sources == ["openalex", "crossref"]
    assert result[0].title == "One"  # first seen is canonical


def test_dedupe_by_arxiv_id_even_when_years_differ() -> None:
    preprint = make(arxiv_id="1706.03762", year=2017, title="Preprint")
    published = make(arxiv_id="1706.03762", year=2019, title="Published version")

    assert len(dedupe([preprint, published])) == 1


def test_dedupe_by_title_key_when_no_id_shared() -> None:
    first = make(title="Deep Learning", year=2020)
    second = make(title="deep learning!", year=2020)

    result = dedupe([first, second])

    assert len(result) == 1
    assert result[0] is first


def test_dedupe_title_fallback_still_requires_compatible_years() -> None:
    """Similar titles in different years are different papers."""
    first = make(title="Deep Learning", year=2012)
    second = make(title="Deep Learning", year=2021)

    assert len(dedupe([first, second])) == 2


def test_dedupe_title_fallback_when_one_year_is_unknown() -> None:
    first = make(title="Same Work")
    second = make(title="Same Work", year=2021)

    assert len(dedupe([first, second])) == 1  # a missing year cannot disagree


def test_dedupe_does_not_merge_different_titles() -> None:
    first = make(title="Attention Is All You Need", year=2017)
    second = make(title="Deep Residual Learning", year=2017)

    assert len(dedupe([first, second])) == 2


def test_dedupe_keeps_input_order() -> None:
    papers = [make(title=f"Paper {i}", doi=f"10.1/{i}") for i in range(5)]
    assert dedupe(papers) == papers


# -- sorting ---------------------------------------------------------------


def test_sort_relevance_preserves_input_order() -> None:
    papers = [make(title="B"), make(title="A"), make(title="C")]
    assert sort_papers(papers, "relevance") == papers


def test_sort_year_descending_with_unknowns_last() -> None:
    papers = [
        make(title="no year"),
        make(title="2019", year=2019),
        make(title="2021", year=2021),
    ]
    assert [p.title for p in sort_papers(papers, "year")] == ["2021", "2019", "no year"]


def test_sort_citations_descending_with_unknowns_last() -> None:
    papers = [
        make(title="none"),
        make(title="five", citation_count=5),
        make(title="nine", citation_count=9),
    ]
    assert [p.title for p in sort_papers(papers, "citations")] == [
        "nine",
        "five",
        "none",
    ]


def test_sort_title_ignores_case_and_punctuation() -> None:
    papers = [make(title="beta!"), make(title="Alpha"), make(title="gamma")]
    assert [p.title for p in sort_papers(papers, "title")] == [
        "Alpha",
        "beta!",
        "gamma",
    ]


def test_every_sort_mode_is_supported() -> None:
    papers = [make(title="B", year=2020, citation_count=1)]
    for mode in SORT_MODES:
        assert sort_papers(papers, mode) == papers


def test_unknown_sort_mode_raises() -> None:
    with pytest.raises(ValueError, match="unknown sort"):
        sort_papers([], "date")
