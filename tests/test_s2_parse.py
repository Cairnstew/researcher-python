"""Offline tests for the Semantic Scholar adapter (D1: no live calls at all)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from researcher_python.config import ENV_KEYS, Settings
from researcher_python.errors import (
    NotFoundError,
    RateLimitError,
    SourceError,
    SourceUnavailableError,
)
from researcher_python.models import Paper
from researcher_python.sources.semantic_scholar_source import (
    SemanticScholarSource,
    parse_s2_paper,
    unwrap_graph_list,
)

_FIXTURES = Path(__file__).parent / "fixtures"

#: Same spelling the production code must use — read from ENV_KEYS, never hardcoded.
_KEY_ENV_VAR = next(
    name for name, field in ENV_KEYS.items() if field == "semantic_scholar_api_key"
)


def load(name: str) -> Any:
    return json.loads((_FIXTURES / name).read_text())


class FakeResponse:
    """Stands in for a requests.Response."""

    def __init__(
        self,
        status_code: int,
        *,
        payload: Any = None,
        text: str = "",
        headers: dict[str, str] | None = None,
        has_json: bool = True,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}
        self._has_json = has_json

    def json(self) -> Any:
        if not self._has_json:
            raise ValueError("no json body")
        return self._payload


def make_source(**overrides: Any) -> SemanticScholarSource:
    """A source that never actually sleeps between requests."""
    source = SemanticScholarSource(Settings(**overrides))
    source._gate.min_interval = 0.0
    return source


def stub_get(
    monkeypatch: pytest.MonkeyPatch,
    source: SemanticScholarSource,
    response: Any,
    seen: list[dict[str, Any]] | None = None,
) -> None:
    def fake_get(url: str, params: Any = None, headers: Any = None, timeout: Any = None):
        if seen is not None:
            seen.append(
                {"url": url, "params": params, "headers": headers, "timeout": timeout}
            )
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(source._session, "get", fake_get)


# -- parsing (D4) ----------------------------------------------------------


def test_parses_paper_fields() -> None:
    paper = parse_s2_paper(load("s2_paper.json"))

    assert paper.sources == ["semantic_scholar"]
    assert paper.title.startswith("ImageNet Classification")
    assert paper.year == 2017
    assert paper.venue == "Communications of the ACM"
    assert paper.citation_count == 98765
    assert paper.doi == "10.1145/3065386"
    assert paper.arxiv_id == "1409.0576"
    assert paper.pmid == ""
    assert paper.ids == {
        "semantic_scholar": "7c8b9d0e1f2a3b4c5d6e7f8091a2b3c4d5e6f708",
        "arxiv": "1409.0576",
        "doi": "10.1145/3065386",
    }
    # A DOI wins over the Semantic Scholar landing page.
    assert paper.url == "https://doi.org/10.1145/3065386"
    assert paper.pdf_url == "https://dl.acm.org/doi/pdf/10.1145/3065386"
    assert paper.is_open_access is True
    assert [a.name for a in paper.authors] == [
        "Alex Krizhevsky",
        "Ilya Sutskever",
        "Geoffrey E. Hinton",
    ]
    assert paper.authors[0].author_id == "145963066"


def test_pmid_and_pmcid_from_external_ids() -> None:
    paper = parse_s2_paper(
        {
            "title": "Biomedical record",
            "externalIds": {"PMID": "22470460", "PMCID": "pmc3257301"},
        }
    )
    assert paper.pmid == "22470460"
    assert paper.pmcid == "PMC3257301"
    assert paper.ids["pmid"] == "22470460"
    assert paper.ids["pmc"] == "PMC3257301"


def test_missing_and_null_fields_degrade_to_empty() -> None:
    paper = parse_s2_paper({"title": "Nothing else here"})

    assert paper.abstract == ""
    assert paper.year is None
    assert paper.authors == []
    assert paper.doi == paper.arxiv_id == paper.pmid == paper.pmcid == ""
    assert paper.ids == {}
    assert paper.pdf_url == ""
    assert paper.is_open_access is False
    assert paper.venue == ""
    assert paper.citation_count is None
    assert paper.url == ""


def test_paper_id_falls_back_to_the_s2_url_without_a_doi() -> None:
    paper = parse_s2_paper(
        {"title": "No DOI here", "paperId": "abc123def456"}
    )
    assert paper.ids["semantic_scholar"] == "abc123def456"
    assert paper.doi == ""
    assert paper.url == "https://www.semanticscholar.org/paper/abc123def456"


def test_absent_paper_id_leaves_ids_and_url_empty() -> None:
    paper = parse_s2_paper({"title": "No id at all", "externalIds": {"ArXiv": "1706.03762"}})
    assert "semantic_scholar" not in paper.ids
    assert paper.url == ""  # no DOI and no paperId -> nothing to link to
    assert paper.arxiv_id == "1706.03762"


def test_wrong_typed_scalars_become_none() -> None:
    paper = parse_s2_paper({"title": "T", "year": "2019", "citationCount": True})
    assert paper.year is None
    assert paper.citation_count is None


def test_non_mapping_input_is_safe() -> None:
    paper = parse_s2_paper(None)  # type: ignore[arg-type]
    assert paper.title == ""
    assert paper.sources == ["semantic_scholar"]


# -- reference / citation unwrapping ---------------------------------------


def test_unwraps_references_and_skips_null_cited_paper() -> None:
    payload = load("s2_references.json")
    rows = unwrap_graph_list(payload, "citedPaper")
    assert len(rows) == 2  # the null row is dropped
    assert rows[0]["title"].startswith("Deep Residual")
    assert rows[1]["title"].startswith("A record with no external ids")

    papers = [parse_s2_paper(row) for row in rows]
    assert papers[0].arxiv_id == "1512.03385"
    assert papers[0].pdf_url == "https://arxiv.org/pdf/1512.03385"
    assert papers[1].ids == {}
    assert papers[1].year is None


def test_unwraps_citations_with_citing_paper_key() -> None:
    payload = {
        "data": [
            {"citingPaper": {"title": "Later work", "year": 2021}},
            {"citingPaper": None},
        ]
    }
    rows = unwrap_graph_list(payload, "citingPaper")
    assert [row["title"] for row in rows] == ["Later work"]


@pytest.mark.parametrize("payload", [None, {}, {"data": None}, {"data": [1, "x"]}])
def test_unwrap_tolerates_malformed_payloads(payload: Any) -> None:
    assert unwrap_graph_list(payload, "citedPaper") == []


# -- id prefix building (D3) -----------------------------------------------


def test_address_prefixes() -> None:
    source = make_source()
    # Case is preserved on the wire (D3).
    assert source._address("https://doi.org/10.1103/PhysRevLett.116.061102") == (
        "DOI:10.1103/PhysRevLett.116.061102"
    )
    assert source._address("10.1038/Nature14539") == "DOI:10.1038/Nature14539"
    assert source._address("arXiv:1706.03762") == "ARXIV:1706.03762"
    assert source._address("22470460") == "PMID:22470460"


def test_address_rejects_other_ids_with_a_hint() -> None:
    source = make_source()
    with pytest.raises(NotFoundError) as excinfo:
        source._address("W2741809807")
    assert "use the 'openalex' source" in str(excinfo.value)

    with pytest.raises(NotFoundError) as excinfo:
        source._address("not-an-id")
    assert "DOIs, arXiv ids and PMIDs only" in str(excinfo.value)


def test_get_builds_an_encoded_url_and_sends_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []
    source = make_source(semantic_scholar_api_key="k123")
    stub_get(
        monkeypatch,
        source,
        FakeResponse(200, payload=load("s2_paper.json")),
        seen,
    )

    paper = source.get("https://doi.org/10.1145/3065386")

    assert paper.doi == "10.1145/3065386"
    assert len(seen) == 1
    assert seen[0]["url"].endswith("/paper/DOI%3A10.1145%2F3065386")
    assert seen[0]["headers"]["x-api-key"] == "k123"
    assert seen[0]["params"]["fields"].startswith("paperId,title,abstract,year")
    assert seen[0]["timeout"] == pytest.approx(Settings().timeout)


def test_keyless_request_omits_the_header(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    source = make_source()
    stub_get(monkeypatch, source, FakeResponse(200, payload={"title": "x"}), seen)
    source.get("10.1/x")
    assert "x-api-key" not in seen[0]["headers"]


# -- HTTP error mapping (D8) -----------------------------------------------


def test_404_maps_to_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    source = make_source()
    stub_get(monkeypatch, source, FakeResponse(404, text="paper not found"))
    with pytest.raises(NotFoundError) as excinfo:
        source.get("10.1/missing")
    assert "paper not found" in str(excinfo.value)


def test_429_without_key_names_the_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_source()
    stub_get(
        monkeypatch,
        source,
        FakeResponse(429, text="slow down", headers={"Retry-After": "5"}),
    )
    with pytest.raises(RateLimitError) as excinfo:
        source.get("10.1/throttled")
    message = str(excinfo.value)
    assert "Retry-After: 5s" in message
    assert _KEY_ENV_VAR in message  # D7: name the env var, don't hardcode it
    assert excinfo.value.retry_after == 5.0
    assert _KEY_ENV_VAR == "RESEARCHER_SEMANTIC_SCHOLAR_API_KEY"


def test_429_with_key_is_plain_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    source = make_source(semantic_scholar_api_key="k123")
    stub_get(
        monkeypatch,
        source,
        FakeResponse(429, text="slow down", headers={"Retry-After": "5"}),
    )
    with pytest.raises(RateLimitError) as excinfo:
        source.get("10.1/throttled")
    message = str(excinfo.value)
    assert "rate limit" in message
    assert _KEY_ENV_VAR not in message  # already configured


def test_429_without_retry_after_still_maps(monkeypatch: pytest.MonkeyPatch) -> None:
    source = make_source()
    stub_get(monkeypatch, source, FakeResponse(429))
    with pytest.raises(RateLimitError) as excinfo:
        source.get("10.1/throttled")
    assert "Retry-After" not in str(excinfo.value)
    assert excinfo.value.retry_after is None


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_maps_to_source_error_never_unavailable(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """D8: 5xx -> SourceError; SourceUnavailableError is for adapters only (D5)."""
    source = make_source()
    stub_get(monkeypatch, source, FakeResponse(status, text="upstream broke"))
    with pytest.raises(SourceError) as excinfo:
        source.get("10.1/broken")
    assert type(excinfo.value) is SourceError
    assert not isinstance(excinfo.value, SourceUnavailableError)
    assert "upstream broke" in str(excinfo.value)


def test_timeout_maps_to_source_error_with_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_source()
    timeout = source._requests.Timeout("read timed out")
    stub_get(monkeypatch, source, timeout)
    with pytest.raises(SourceError) as excinfo:
        source.get("10.1/slow")
    assert not isinstance(excinfo.value, SourceUnavailableError)
    assert excinfo.value.__cause__ is timeout


def test_connection_error_maps_to_source_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_source()
    error = source._requests.ConnectionError("refused")
    stub_get(monkeypatch, source, error)
    with pytest.raises(SourceError) as excinfo:
        source.get("10.1/refused")
    assert excinfo.value.__cause__ is error


def test_non_json_success_maps_to_source_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_source()
    stub_get(monkeypatch, source, FakeResponse(200, has_json=False))
    with pytest.raises(SourceError) as excinfo:
        source.get("10.1/junk")
    assert "non-JSON" in str(excinfo.value)


# -- adapter behaviour (offline) -------------------------------------------


def test_capability_table() -> None:
    caps = SemanticScholarSource.capabilities
    assert (caps.search, caps.get, caps.references, caps.citations, caps.pdf) == (
        True,
        True,
        True,
        True,
        True,
    )


def test_default_interval_is_one_second_class_for_both_tiers() -> None:
    assert SemanticScholarSource(Settings()).default_interval == pytest.approx(1.1)
    assert (
        SemanticScholarSource(Settings(semantic_scholar_api_key="k")).default_interval
        == pytest.approx(1.1)
    )


def test_references_without_an_addressable_id_refuses_before_fetching() -> None:
    with pytest.raises(NotFoundError) as excinfo:
        make_source().references(Paper(title="orphan"))
    assert "no Semantic Scholar id, DOI, arXiv id or PMID" in str(excinfo.value)


def test_references_use_the_stored_semantic_scholar_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record carrying only ids['semantic_scholar'] must still resolve."""
    source = make_source()
    seen: list[dict[str, Any]] = []
    stub_get(monkeypatch, source, FakeResponse(200, payload=load("s2_references.json")), seen)

    paper = Paper(title="Only an S2 id", ids={"semantic_scholar": "abc123def456"})
    papers = source.references(paper)

    assert seen and seen[0]["url"].endswith("/paper/abc123def456/references")
    assert len(papers) == 2  # the null citedPaper row is dropped


def test_429_with_garbage_retry_after_maps_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_source()
    stub_get(
        monkeypatch,
        source,
        FakeResponse(429, headers={"Retry-After": "later"}),
    )
    with pytest.raises(RateLimitError) as excinfo:
        source.get("10.1/throttled")
    assert excinfo.value.retry_after is None


def test_publication_types_fill_work_type_and_the_search_field() -> None:
    from researcher_python.sources.base import SearchFilters

    record = load("s2_paper.json")
    record["publicationTypes"] = ["JournalArticle"]
    assert parse_s2_paper(record).work_type == "JournalArticle"

    source = make_source()
    seen: list[dict[str, Any]] = []

    def fake_get(path, params):
        seen.append(dict(params))
        return {"data": []}

    source._get = fake_get  # type: ignore[method-assign]
    source.search(
        "agents",
        limit=5,
        filters=SearchFilters(types=("article",), exclude_types=()),
    )
    assert seen[0]["publicationTypes"] == "JournalArticle"
    assert "publicationTypes" in seen[0]["fields"]
