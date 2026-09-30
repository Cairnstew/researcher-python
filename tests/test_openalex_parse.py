"""Offline tests for :mod:`researcher_python.sources.openalex_source`.

The adapter was verified live once (D1); these tests pin its *pure* parsing
and its error mapping against a hand-written fixture, with the HTTP session
stubbed — no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import researcher_python.sources.openalex_source as openalex_mod
from researcher_python.config import Settings
from researcher_python.errors import NotFoundError, RateLimitError, SourceError
from researcher_python.models import Paper
from researcher_python.sources.openalex_source import (
    OpenAlexSource,
    parse_openalex_work,
    reconstruct_abstract,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "openalex_work.json"

#: ``time.sleep`` calls the adapter made, cleared per test.
SLEEPS: list[float] = []


class FakeResponse:
    def __init__(self, status: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def load_work() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text())


@pytest.fixture
def source(monkeypatch: pytest.MonkeyPatch) -> OpenAlexSource:
    src = OpenAlexSource(Settings())
    src._gate.min_interval = 0.0  # never wait in tests
    SLEEPS.clear()
    # Retry backoff would otherwise add seconds to the 429/503 cases.
    monkeypatch.setattr(openalex_mod, "time", SimpleNamespace(sleep=SLEEPS.append))
    return src


def stub(source: OpenAlexSource, responder) -> list[dict[str, Any]]:  # noqa: ANN001
    """Replace the session's ``get``; return the list of params it was given."""
    seen: list[dict[str, Any]] = []

    def fake_get(url: str, params: Any = None, timeout: Any = None) -> Any:
        seen.append(dict(params or {}))
        return responder(url, params)

    source._session.get = fake_get  # type: ignore[method-assign]
    return seen


# -- abstract reconstruction ------------------------------------------------


def test_reconstruct_abstract_places_words_by_position() -> None:
    inverted = {"world": [1], "hello": [0]}
    assert reconstruct_abstract(inverted) == "hello world"


def test_reconstruct_abstract_handles_repeated_words() -> None:
    assert reconstruct_abstract({"a": [0, 3], "b": [1], "c": [2]}) == "a b c a"


def test_reconstruct_abstract_tolerates_out_of_order_positions() -> None:
    assert reconstruct_abstract({"second": [1], "first": [0]}) == "first second"


@pytest.mark.parametrize("value", [None, {}, "not a mapping", []])
def test_reconstruct_abstract_returns_empty_for_missing_input(value: Any) -> None:
    assert reconstruct_abstract(value) == ""


# -- work mapping -----------------------------------------------------------


def test_parses_the_fixture_work() -> None:
    paper = parse_openalex_work(load_work())

    assert paper.sources == ["openalex"]
    assert paper.ids["openalex"] == "W2029604167"
    assert paper.doi == "10.1371/journal.pone.0033693"
    assert paper.pmid == "22470460"
    assert paper.title == "Sample article title from OpenAlex"
    assert paper.year == 2012
    assert paper.published == "2012-05-09"
    assert paper.venue == "PLOS ONE"
    assert paper.publisher == "PLOS"
    assert paper.url.startswith("https://journals.plos.org/")
    assert paper.volume == "7" and paper.issue == "5"
    assert paper.pages == "e33693"  # empty last_page is not appended
    assert paper.citation_count == 42
    assert paper.reference_count == 31
    assert paper.is_open_access is True
    assert paper.oa_status == "gold"
    assert paper.license == "cc-by"
    assert paper.pdf_url == "https://example.org/open/sample.pdf"
    assert paper.arxiv_id == "1706.03762"  # found in a location landing page


def test_parses_authors_from_both_shapes() -> None:
    authors = parse_openalex_work(load_work()).authors

    assert [a.name for a in authors] == ["Ada Lovelace", "Anonymous Collaborator"]
    assert authors[0].orcid == "0000-0002-1825-0097"
    assert authors[0].affiliations == ["Example University"]  # from institutions
    assert authors[1].affiliations == ["Somewhere Else"]  # raw affiliation fallback


def test_parses_topics_and_keywords() -> None:
    paper = parse_openalex_work(load_work())
    assert paper.categories == ["Topic Alpha", "Topic Beta"]
    assert paper.keywords == ["keyword one", "keyword two"]


def test_parses_abstract_from_the_inverted_index() -> None:
    paper = parse_openalex_work(load_work())
    assert paper.abstract == "Background is an inverted index words and repeat words"


def test_pmcid_is_parsed_from_the_url_form() -> None:
    paper = parse_openalex_work(
        {"ids": {"pmcid": "https://pmc.ncbi.nlm.nih.gov/articles/PMC3325049"}}
    )
    assert paper.pmcid == "PMC3325049"


def test_pmcid_is_parsed_from_the_bare_form() -> None:
    paper = parse_openalex_work({"ids": {"pmcid": "PMC3325049"}})
    assert paper.pmcid == "PMC3325049"


def test_missing_optional_blocks_degrade_gracefully() -> None:
    paper = parse_openalex_work({"id": "https://openalex.org/W1", "display_name": "T"})

    assert paper.doi == ""
    assert paper.pmid == ""
    assert paper.pmcid == ""
    assert paper.abstract == ""
    assert paper.authors == []
    assert paper.categories == []
    assert paper.keywords == []
    assert paper.pdf_url == ""
    assert paper.is_open_access is False
    assert paper.citation_count is None
    assert paper.year is None
    assert paper.ids == {"openalex": "W1"}


# -- request parameters -----------------------------------------------------


def test_params_adds_mailto_and_api_key(source: OpenAlexSource) -> None:
    src = OpenAlexSource(Settings(email="a@example.com", openalex_api_key="KEY"))
    params = src._params({"search": "query"})

    assert params["search"] == "query"
    assert params["mailto"] == "a@example.com"
    assert params["api_key"] == "KEY"


def test_params_omits_absent_credentials() -> None:
    params = OpenAlexSource(Settings())._params({"search": "query"})
    assert "mailto" not in params
    assert "api_key" not in params


# -- error mapping ----------------------------------------------------------


def test_404_maps_to_not_found(source: OpenAlexSource) -> None:
    stub(source, lambda url, params: FakeResponse(404, {"error": "not found"}))

    with pytest.raises(NotFoundError) as excinfo:
        source._get("https://api.openalex.org/works/W0", {})
    assert excinfo.value.source == "openalex"
    assert "not found" in str(excinfo.value)


def test_429_retries_bounded_then_maps_to_rate_limit(
    source: OpenAlexSource,
) -> None:
    stub(source, lambda url, params: FakeResponse(429, {"error": "rate limited"}))

    with pytest.raises(RateLimitError):
        source._get("https://api.openalex.org/works", {})

    assert len(SLEEPS) == 2  # 3 attempts -> 2 backoffs, then it gives up
    assert all(0 < s <= 30 for s in SLEEPS)


def test_503_retries_then_maps_to_source_error(source: OpenAlexSource) -> None:
    stub(
        source,
        lambda url, params: FakeResponse(
            503, {"error": "Anonymous search is paused for your IP"}
        ),
    )

    with pytest.raises(SourceError) as excinfo:
        source._get("https://api.openalex.org/works", {})

    assert "Anonymous search is paused" in str(excinfo.value)
    assert len(SLEEPS) == 2


def test_non_json_success_maps_to_source_error(source: OpenAlexSource) -> None:
    response = FakeResponse(200, None)
    response.json = lambda: (_ for _ in ()).throw(ValueError("nope"))  # type: ignore[method-assign]
    stub(source, lambda url, params: response)

    with pytest.raises(SourceError, match="non-JSON"):
        source._get("https://api.openalex.org/works", {})


# -- paging and chunking ----------------------------------------------------


def _minimal_work(index: int) -> dict[str, Any]:
    return {"id": f"https://openalex.org/W{index}", "display_name": f"Work {index}"}


def test_search_pages_at_page_size(source: OpenAlexSource) -> None:
    calls: list[dict[str, Any]] = []

    def responder(url: str, params: Any) -> FakeResponse:
        count = 100 if len(calls) == 0 else 50
        calls.append(dict(params or {}))
        return FakeResponse(
            200, {"results": [_minimal_work(i) for i in range(count)], "meta": {"count": 150}}
        )

    stub(source, responder)
    papers = source.search("query", limit=150)

    assert len(papers) == 150
    assert len(calls) == 2
    assert calls[0]["per-page"] == 100  # _PAGE_SIZE
    assert calls[1]["per-page"] == 50
    assert [c["page"] for c in calls] == [1, 2]


def test_references_chunks_the_filter_expression(source: OpenAlexSource) -> None:
    """120 referenced ids must split at _FILTER_CHUNK (50) into 50/50/20."""
    from researcher_python.sources.openalex_source import _FILTER_CHUNK

    referenced = [f"https://openalex.org/W{i}" for i in range(120)]
    work = {
        "id": "https://openalex.org/W1",
        "display_name": "Citing work",
        "referenced_works": referenced,
    }
    filter_calls: list[str] = []

    def responder(url: str, params: Any) -> FakeResponse:
        if url.rstrip("/").endswith("/works/W1"):
            return FakeResponse(200, work)
        filter_calls.append(dict(params or {}).get("filter", ""))
        return FakeResponse(200, {"results": [], "meta": {"count": 0}})

    stub(source, responder)

    paper = Paper(sources=["openalex"], ids={"openalex": "W1"})
    assert source.references(paper) == []

    assert len(filter_calls) == 3
    sizes = [len(expr.split(":", 1)[1].split("|")) for expr in filter_calls]
    assert sizes == [_FILTER_CHUNK, _FILTER_CHUNK, 120 - 2 * _FILTER_CHUNK]
    for expr in filter_calls:
        assert expr.startswith("openalex:W")


def test_references_without_an_addressable_id_raises(source: OpenAlexSource) -> None:
    with pytest.raises(NotFoundError, match="no OpenAlex addressable id"):
        source.references(Paper(title="orphan"))


def test_citations_uses_the_cites_filter(source: OpenAlexSource) -> None:
    seen: list[str] = []

    def responder(url: str, params: Any) -> FakeResponse:
        query = dict(params or {})
        if "filter" not in query:  # /works/W42 -> the record itself
            return FakeResponse(200, {"id": "https://openalex.org/W42",
                                      "display_name": "Citing work"})
        seen.append(query["filter"])
        return FakeResponse(200, {"results": [], "meta": {"count": 0}})

    stub(source, responder)

    paper = Paper(sources=["openalex"], ids={"openalex": "W42"})
    source.citations(paper)

    assert seen == ["cites:W42"]


def test_get_maps_a_doi_through_the_resolver_url(source: OpenAlexSource) -> None:
    seen: list[str] = []

    def responder(url: str, params: Any) -> FakeResponse:
        seen.append(url)
        return FakeResponse(
            200,
            {
                "id": "https://openalex.org/W1",
                "display_name": "T",
                "doi": "https://doi.org/10.1038/Nature14539",
            },
        )

    stub(source, responder)

    assert source.get("10.1038/Nature14539").doi == "10.1038/nature14539"
    assert seen[0].endswith("/works/https%3A%2F%2Fdoi.org%2F10.1038%2Fnature14539")


def test_429_passes_the_servers_retry_after_on(source: OpenAlexSource) -> None:
    response = FakeResponse(429, {"error": "rate limited"})
    response.headers = {"Retry-After": "15"}
    stub(source, lambda url, params: response)

    with pytest.raises(RateLimitError) as excinfo:
        source._get("https://api.openalex.org/works", {})

    # The raw server value travels on the exception; the backoff is clamped.
    assert excinfo.value.retry_after == 15.0
    assert SLEEPS and max(SLEEPS) <= 30.0


def test_429_without_a_retry_after_header_maps_to_none(source: OpenAlexSource) -> None:
    stub(source, lambda url, params: FakeResponse(429, {"error": "rate limited"}))

    with pytest.raises(RateLimitError) as excinfo:
        source._get("https://api.openalex.org/works", {})

    assert excinfo.value.retry_after is None


def test_429_with_an_http_date_retry_after_maps_to_none(
    source: OpenAlexSource,
) -> None:
    response = FakeResponse(429, {"error": "rate limited"})
    response.headers = {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}
    stub(source, lambda url, params: response)

    with pytest.raises(RateLimitError) as excinfo:
        source._get("https://api.openalex.org/works", {})

    assert excinfo.value.retry_after is None
