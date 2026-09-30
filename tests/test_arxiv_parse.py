"""Offline tests for :mod:`researcher_python.sources.arxiv_source`.

``parse_arxiv_result`` is pure, so it is fed stand-in objects carrying the
same attributes ``arxiv.Result`` exposes — no network, no XML parsing.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from researcher_python.errors import NotFoundError
from researcher_python.sources.arxiv_source import ArxivSource, parse_arxiv_result


def link(href: str, *, title: str = "", rel: str = "") -> SimpleNamespace:
    return SimpleNamespace(href=href, title=title, rel=rel, content_type="text/html")


def author(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


def result(**overrides: object) -> SimpleNamespace:
    """A minimal ``arxiv.Result`` stand-in."""
    fields: dict[str, object] = {
        "entry_id": "http://arxiv.org/abs/1706.03762v7",
        "updated": datetime(2017, 12, 3, 4, 5, 6),
        "published": datetime(2017, 6, 12, 9, 0, 0),
        "title": "Attention Is All You Need",
        "authors": [author("Ashish Vaswani"), author("Noam Shazeer")],
        "summary": "The dominant sequence transduction models are based on\ncomplex recurrent or convolutional neural networks.",
        "comment": "15 pages",
        "journal_ref": "NIPS 2017",
        "doi": "10.5555/attention",
        "primary_category": "cs.CL",
        "categories": ["cs.CL", "cs.LG"],
        "links": [
            link("http://arxiv.org/abs/1706.03762v7", rel="alternate"),
            link("http://arxiv.org/pdf/1706.03762v7", title="pdf"),
            link("http://arxiv.org/pdf/1706.03762v7.pdf", title="pdf"),
        ],
        "pdf_url": "http://arxiv.org/pdf/1706.03762v7",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)  # type: ignore[arg-type]


# -- mapping ---------------------------------------------------------------


def test_maps_the_core_fields() -> None:
    paper = parse_arxiv_result(result())

    assert paper.sources == ["arxiv"]
    assert paper.title == "Attention Is All You Need"
    # Hard-wrapped abstracts are collapsed to one line.
    assert paper.abstract.startswith("The dominant sequence transduction models")
    assert "\n" not in paper.abstract
    assert [a.name for a in paper.authors] == ["Ashish Vaswani", "Noam Shazeer"]
    assert paper.year == 2017
    assert paper.published == "2017-06-12"
    assert paper.venue == "NIPS 2017"
    assert paper.work_type == "preprint"
    assert paper.is_open_access is True


def test_maps_identifiers() -> None:
    paper = parse_arxiv_result(result())

    # Version suffix stripped from the id, case folded for the dedupe key.
    assert paper.arxiv_id == "1706.03762"
    assert paper.doi == "10.5555/attention"
    assert paper.ids["arxiv"] == "1706.03762"
    assert paper.ids["doi"] == "10.5555/attention"


def test_maps_urls_from_the_link_list() -> None:
    paper = parse_arxiv_result(result())

    assert paper.url == "http://arxiv.org/abs/1706.03762v7"  # rel=alternate
    assert paper.pdf_url == "http://arxiv.org/pdf/1706.03762v7"


def test_primary_category_is_listed_first() -> None:
    paper = parse_arxiv_result(result())
    assert paper.categories[0] == "cs.CL"
    assert set(paper.categories) == {"cs.CL", "cs.LG"}


def test_uses_the_entry_id_url_when_no_alternate_link_exists() -> None:
    paper = parse_arxiv_result(
        result(
            links=[link("http://arxiv.org/pdf/x", title="pdf")],
            pdf_url="",
        )
    )
    assert paper.url == "http://arxiv.org/abs/1706.03762v7"
    assert paper.pdf_url == "http://arxiv.org/pdf/x"


# -- sparse / malformed input ----------------------------------------------


def test_missing_optional_fields_stay_empty() -> None:
    paper = parse_arxiv_result(
        result(
            doi="",
            journal_ref="",
            summary="",
            authors=[],
            categories=[],
            links=[],
            pdf_url="",
            published=None,
        )
    )

    assert paper.doi == ""
    assert paper.venue == ""
    assert paper.abstract == ""
    assert paper.authors == []
    assert paper.categories == []
    assert paper.pdf_url == ""
    assert paper.year is None
    assert paper.published == ""
    # The arXiv id still comes from the entry id.
    assert paper.arxiv_id == "1706.03762"


def test_no_doi_key_is_omitted_entirely() -> None:
    paper = parse_arxiv_result(result(doi=""))
    assert "doi" not in paper.ids
    assert paper.ids["arxiv"] == "1706.03762"


def test_non_iso_date_does_not_raise() -> None:
    paper = parse_arxiv_result(result(published="not a datetime"))
    assert paper.published == "not a datetime"
    assert paper.year is None


# -- get() identifier handling (no network) --------------------------------


def test_get_rejects_a_doi_with_a_hint() -> None:
    with pytest.raises(NotFoundError) as excinfo:
        ArxivSource().get("https://doi.org/10.1038/nature14539")
    message = str(excinfo.value)
    assert "arXiv looks up arXiv ids only" in message
    assert "try the 'crossref' source" in message
    assert "source 'arxiv'" in str(excinfo.value.source) or excinfo.value.source == "arxiv"


def test_get_rejects_a_pmid_with_a_hint() -> None:
    with pytest.raises(NotFoundError) as excinfo:
        ArxivSource().get("22470460")
    assert "try the 'crossref' source" in str(excinfo.value)


def test_capabilities_match_the_documented_surface() -> None:
    caps = ArxivSource.capabilities
    assert (caps.search, caps.get, caps.references, caps.citations, caps.pdf) == (
        True,
        True,
        False,
        False,
        True,
    )
