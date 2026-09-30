"""Offline tests for the Crossref adapter (D1: no network, D4: pure parsing)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from researcher_python.errors import (
    NotFoundError,
    RateLimitError,
    SourceError,
    SourceUnavailableError,
    UnsupportedOperation,
)
from researcher_python.ids import normalize_doi
from researcher_python.models import Paper
from researcher_python.sources.crossref_source import (
    CrossrefSource,
    map_http_error,
    parse_crossref_reference,
    parse_crossref_work,
    strip_jats,
    wire_doi,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "crossref_work.json"


def load_work() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text())


# -- parsing ---------------------------------------------------------------


def test_parses_full_work() -> None:
    paper = parse_crossref_work(load_work())

    assert paper.sources == ["crossref"]
    # D3: lowercased for the key, never on the wire.
    assert paper.doi == "10.1103/physrevlett.116.061102"
    assert paper.ids == {
        "crossref": "10.1103/physrevlett.116.061102",
        "doi": "10.1103/physrevlett.116.061102",
    }
    assert paper.url == "https://doi.org/10.1103/PhysRevLett.116.061102"
    assert paper.title == "A synthetic Crossref work used by the offline parser tests"
    assert paper.venue == "Physical Review Letters"
    assert paper.year == 2016
    assert paper.published == "2016-02-11"
    assert paper.volume == "116"
    assert paper.issue == "6"
    assert paper.pages == "061102"
    assert paper.publisher == "American Physical Society (APS)"
    assert paper.language == "en"
    assert paper.work_type == "journal-article"
    assert paper.categories == ["Physics and Astronomy (all)"]
    assert paper.citation_count == 1234
    assert paper.reference_count == 4
    assert paper.license == "https://journals.aps.org/copyright"
    # capabilities.pdf is False: text-mining links are behind the paywall.
    assert paper.pdf_url == ""
    assert paper.is_open_access is False

    assert [a.name for a in paper.authors] == ["Ada Lovelace", "Alan Turing"]
    assert paper.authors[0].orcid == "0000-0002-1825-0097"
    assert paper.authors[0].affiliations == [
        "Analytical Engine Institute",
        "Second Affiliation",
    ]
    assert paper.authors[1].affiliations == ["University of Manchester"]


def test_abstract_is_stripped_of_jats() -> None:
    abstract = parse_crossref_work(load_work()).abstract
    assert abstract == (
        "Abstract prose with emphasis and the entity \u2010 inside."
    )


def test_missing_fields_degrade_to_empty() -> None:
    paper = parse_crossref_work({"DOI": "10.1234/only.a.doi"})
    assert paper.doi == "10.1234/only.a.doi"
    assert paper.ids == {"crossref": "10.1234/only.a.doi", "doi": "10.1234/only.a.doi"}
    assert paper.title == ""
    assert paper.abstract == ""
    assert paper.authors == []
    assert paper.year is None
    assert paper.published == ""
    assert paper.venue == ""
    assert paper.citation_count is None
    assert paper.reference_count is None
    assert paper.license == ""


def test_malformed_shapes_do_not_raise() -> None:
    paper = parse_crossref_work(
        {
            "DOI": "10.1234/malformed",
            "title": [],
            "author": "not a list",
            "issued": {"date-parts": "nope"},
            "license": [None, {"URL": 42}],
        }
    )
    assert paper.doi == "10.1234/malformed"
    assert paper.title == ""
    assert paper.authors == []
    assert paper.year is None
    assert paper.license == ""


@pytest.mark.parametrize(
    ("parts", "published", "year"),
    [
        ([[1999]], "1999", 1999),
        ([[1999, 5]], "1999-05", 1999),
        ([[1999, 5, 6]], "1999-05-06", 1999),
    ],
)
def test_date_parts_variants(parts: list[list[int]], published: str, year: int) -> None:
    paper = parse_crossref_work({"DOI": "10.1/x", "issued": {"date-parts": parts}})
    assert paper.published == published
    assert paper.year == year


def test_no_date_parts_leaves_year_unset() -> None:
    paper = parse_crossref_work({"DOI": "10.1/x", "issued": {"date-parts": []}})
    assert paper.year is None
    assert paper.published == ""


# -- DOI casing (D3) -------------------------------------------------------


def test_wire_doi_preserves_case() -> None:
    assert wire_doi("10.1103/PhysRevLett.116.061102") == "10.1103/PhysRevLett.116.061102"
    assert (
        wire_doi("https://doi.org/10.1103/PhysRevLett.116.061102")
        == "10.1103/PhysRevLett.116.061102"
    )
    assert wire_doi("doi:10.1103/PhysRevLett.116.061102") == "10.1103/PhysRevLett.116.061102"
    assert wire_doi("10.1038/nature14539.") == "10.1038/nature14539"


def test_wire_doi_rejects_non_dois() -> None:
    assert wire_doi("arXiv:1706.03762") == ""
    assert wire_doi("22470460") == ""
    assert wire_doi("not an id") == ""
    assert wire_doi("") == ""
    assert wire_doi(None) == ""


def test_normalize_doi_lowercases_for_dedupe_keys() -> None:
    assert (
        normalize_doi("https://doi.org/10.1103/PhysRevLett.116.061102")
        == "10.1103/physrevlett.116.061102"
    )


# -- abstract markup -------------------------------------------------------


def test_strip_jats_variants() -> None:
    assert strip_jats(None) == ""
    assert strip_jats("") == ""
    assert strip_jats("<jats:p>Hello <jats:italic>world</jats:italic>.</jats:p>") == (
        "Hello world."
    )
    # Block tags separate, inline tags delete: no space before the period,
    # but two paragraphs do not run together.
    assert strip_jats("<jats:p>One.</jats:p><jats:p>Two.</jats:p>") == "One. Two."
    assert strip_jats("Plain abstract &amp; more") == "Plain abstract & more"
    assert strip_jats("  spaced   out  ") == "spaced out"


# -- reference entries -----------------------------------------------------


def test_references_keep_only_doi_entries() -> None:
    stubs = [
        stub
        for entry in load_work()["reference"]
        if (stub := parse_crossref_reference(entry)) is not None
    ]
    assert len(stubs) == 2
    assert [s.doi for s in stubs] == ["10.1103/physrev.136.b1630", "10.1000/xyz123"]
    assert stubs[0].venue == "Phys. Rev."
    assert stubs[0].volume == "136"
    assert stubs[1].title == "A cited work title"
    assert stubs[1].authors[0].name == "Smith, J."
    assert stubs[1].year == 2001
    assert stubs[1].ids["doi"] == "10.1000/xyz123"


def test_reference_without_doi_is_skipped() -> None:
    assert parse_crossref_reference({"key": "Ref2", "unstructured": "no doi here"}) is None
    assert parse_crossref_reference({"DOI": ""}) is None
    assert parse_crossref_reference("not a mapping") is None  # type: ignore[arg-type]


# -- HTTP error mapping ----------------------------------------------------


def _http_error(
    status: int, *, text: str = "", headers: dict[str, str] | None = None
) -> httpx.HTTPStatusError:
    """Raise the exact exception habanero lets through, and hand it back."""
    response = httpx.Response(
        status,
        headers=headers or {},
        text=text,
        request=httpx.Request("GET", "https://api.crossref.org/works/10.0000/test"),
    )
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        response.raise_for_status()
    return excinfo.value


def test_404_maps_to_not_found() -> None:
    exc = _http_error(404, text="Resource not found.")
    error = map_http_error("crossref", exc)
    assert isinstance(error, NotFoundError)
    assert "Resource not found." in str(error)
    assert error.source == "crossref"


def test_429_maps_to_rate_limit_with_retry_after() -> None:
    exc = _http_error(429, headers={"Retry-After": "7"})
    error = map_http_error("crossref", exc)
    assert isinstance(error, RateLimitError)
    assert "Retry-After: 7s" in str(error)
    assert "RESEARCHER_EMAIL" in str(error)
    assert error.retry_after == 7.0  # the server's own value, unclamped


def test_429_without_retry_after_still_maps() -> None:
    error = map_http_error("crossref", _http_error(429))
    assert isinstance(error, RateLimitError)
    assert "Retry-After" not in str(error)
    assert error.retry_after is None


def test_429_with_an_unparsable_retry_after_maps_to_none() -> None:
    exc = _http_error(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
    error = map_http_error("crossref", exc)
    assert isinstance(error, RateLimitError)
    assert error.retry_after is None


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_maps_to_source_error(status: int) -> None:
    """D8: 5xx is a transport failure (SourceError), never SourceUnavailableError."""
    error = map_http_error("crossref", _http_error(status))
    assert type(error) is SourceError
    assert not isinstance(error, SourceUnavailableError)
    assert f"HTTP {status}" in str(error)


def test_other_statuses_map_to_source_error() -> None:
    error = map_http_error("crossref", _http_error(400, text="bad request"))
    assert type(error) is SourceError
    assert "bad request" in str(error)


def test_mapping_accepts_a_plain_fake_response_object() -> None:
    """Duck-typed: the mapper only reads status_code / headers / text."""

    class FakeResponse:
        status_code = 404
        headers: dict[str, str] = {}
        text = "nope"

    class FakeError(Exception):
        def __init__(self, response: Any) -> None:
            super().__init__("boom")
            self.response = response

    error = map_http_error("crossref", FakeError(FakeResponse()))
    assert isinstance(error, NotFoundError)
    assert "nope" in str(error)


# -- adapter behaviour (offline) -------------------------------------------


def test_capability_table() -> None:
    caps = CrossrefSource.capabilities
    assert (caps.search, caps.get, caps.references, caps.citations, caps.pdf) == (
        True,
        True,
        True,
        False,
        False,
    )


def test_citations_is_refused() -> None:
    with pytest.raises(UnsupportedOperation):
        CrossrefSource().citations(Paper())


@pytest.mark.parametrize(
    ("key", "hint"),
    [
        ("arXiv:1706.03762", "use the 'arxiv' source"),
        ("22470460", "use the 'pubmed' source"),
        ("not-an-id", "looks up DOIs only"),
    ],
)
def test_get_rejects_non_doi_with_a_hint(key: str, hint: str) -> None:
    """Raises before any request: non-DOIs never reach the wire."""
    with pytest.raises(NotFoundError) as excinfo:
        CrossrefSource().get(key)
    assert hint in str(excinfo.value)


def test_references_without_a_doi_refuses_before_fetching() -> None:
    with pytest.raises(NotFoundError) as excinfo:
        CrossrefSource().references(Paper(title="orphan"))
    assert "no DOI" in str(excinfo.value)
