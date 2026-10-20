"""Offline tests for the pipeline (fakes registered through the registry — no network)."""

from __future__ import annotations

from typing import Any

import pytest

import researcher_python.pipeline as pipeline
import researcher_python.sources as registry
from researcher_python.config import Settings
from researcher_python.errors import (
    ConfigurationError,
    NotFoundError,
    RateLimitError,
    SourceError,
    UnsupportedOperation,
)
from researcher_python.models import Paper
from researcher_python.sources.base import Capabilities, Source

#: Behaviour spec per registry key, mutated by each test.
BEHAVIOUR: dict[str, dict[str, Any]] = {}
#: Every fake entry point, in call order.
CALLS: list[str] = []
#: Durations the pipeline asked us to sleep (never actually slept).
SLEEPS: list[float] = []


def paper(
    title: str,
    *,
    doi: str = "",
    arxiv_id: str = "",
    pmid: str = "",
    year: int | None = None,
) -> Paper:
    return Paper(title=title, doi=doi, arxiv_id=arxiv_id, pmid=pmid, year=year)


class FakeSource(Source):
    """Configurable stand-in; behaviour comes from :data:`BEHAVIOUR[self.name]`."""

    name = "fake"
    capabilities = Capabilities(
        search=True, get=True, references=False, citations=False, pdf=False
    )

    @property
    def _spec(self) -> dict[str, Any]:
        return BEHAVIOUR.setdefault(self.name, {})

    def _stamp(self, item: Paper) -> Paper:
        out = item.copy()
        out.sources = [self.name]
        return out

    def search(
        self, query: str, *, limit: int | None = None, filters=None
    ) -> list[Paper]:
        CALLS.append(f"search:{self.name}")
        spec = self._spec
        error = spec.get("search_error")
        remaining = spec.get("search_raises", 0)
        if error is not None and remaining > 0:
            spec["search_raises"] = remaining - 1
            raise error
        results = spec.get("search_results", [])
        if limit is not None:
            results = results[:limit]
        return [self._stamp(item) for item in results]

    def get(self, key: str) -> Paper:
        CALLS.append(f"get:{self.name}")
        spec = self._spec
        error = spec.get("get_error")
        if error is not None:
            raise error
        found = spec.get("get_paper")
        if found is None:
            raise NotFoundError(self.name, f"no record in {self.name}")
        return self._stamp(found)


class FakeOpenAlex(FakeSource):
    name = "openalex"


class FakeCrossref(FakeSource):
    name = "crossref"


class FakeSemanticScholar(FakeSource):
    name = "semantic_scholar"


class FakeArxiv(FakeSource):
    name = "arxiv"


class FakePubmed(FakeSource):
    name = "pubmed"


class FakeBroken(FakeSource):
    name = "broken"

    def __init__(self, settings: Any = None, **_: object) -> None:
        raise ConfigurationError("broken source requires RESEARCHER_EMAIL")


FAKES: dict[str, type[FakeSource]] = {
    "openalex": FakeOpenAlex,
    "crossref": FakeCrossref,
    "semantic_scholar": FakeSemanticScholar,
    "arxiv": FakeArxiv,
    "pubmed": FakePubmed,
    "broken": FakeBroken,
}


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Any:
    for key, cls in FAKES.items():
        monkeypatch.setitem(registry._REGISTRY, key, (__name__, cls.__name__))
    monkeypatch.setattr(pipeline, "_sleep", SLEEPS.append)
    BEHAVIOUR.clear()
    CALLS.clear()
    SLEEPS.clear()
    yield
    BEHAVIOUR.clear()
    CALLS.clear()
    SLEEPS.clear()


# -- search: fan out -------------------------------------------------------


def test_fan_out_returns_merged_results(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {
        "search_results": [paper("A", doi="10.1/a"), paper("B", doi="10.1/b")]
    }
    BEHAVIOUR["crossref"] = {"search_results": [paper("C", doi="10.1/c")]}

    result = pipeline.search("query", sources=["openalex", "crossref"])

    assert result.errors == {}
    assert result.by_source == {"openalex": 2, "crossref": 1}
    assert {p.title for p in result.papers} == {"A", "B", "C"}
    assert len(result.papers) == 3


def test_partial_failure_is_recorded_others_succeed(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {"search_results": [paper("A", doi="10.1/a")]}
    BEHAVIOUR["crossref"] = {
        "search_error": SourceError("crossref", "boom"),
        "search_raises": 1,
    }

    result = pipeline.search("query", sources=["openalex", "crossref"])

    assert [p.title for p in result.papers] == ["A"]
    assert isinstance(result.errors["crossref"], SourceError)
    assert "openalex" not in result.errors
    assert result.by_source == {"openalex": 1, "crossref": 0}


def test_cross_source_dedupe_merges_the_same_doi(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {"search_results": [paper("Same work", doi="10.1/same")]}
    BEHAVIOUR["crossref"] = {"search_results": [paper("Same work", doi="10.1/same")]}

    result = pipeline.search("query", sources=["openalex", "crossref"])

    assert len(result.papers) == 1
    merged = result.papers[0]
    assert set(merged.sources) == {"openalex", "crossref"}
    assert merged.doi == "10.1/same"
    assert result.by_source == {"openalex": 1, "crossref": 1}  # raw, pre-dedupe


def test_max_results_applies_per_source(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {
        "search_results": [paper(f"O{i}", doi=f"10.1/o{i}") for i in range(5)]
    }
    BEHAVIOUR["crossref"] = {"search_results": [paper("C", doi="10.1/c")]}

    result = pipeline.search("query", sources=["openalex", "crossref"], max_results=2)

    assert result.by_source == {"openalex": 2, "crossref": 1}
    assert len(result.papers) == 3  # not truncated after dedupe


def test_unsupported_search_capability_is_recorded(
    fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        FakeCrossref,
        "capabilities",
        Capabilities(search=False, get=True, references=False, citations=False, pdf=False),
    )
    BEHAVIOUR["openalex"] = {"search_results": [paper("A", doi="10.1/a")]}

    result = pipeline.search("query", sources=["openalex", "crossref"])

    assert isinstance(result.errors["crossref"], UnsupportedOperation)
    assert [p.title for p in result.papers] == ["A"]


def test_unknown_source_raises_up_front(fakes: None) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        pipeline.search("query", sources=["openalex", "nope"])
    assert "'nope'" in str(excinfo.value)
    assert "known sources" in str(excinfo.value)
    assert CALLS == []  # nothing ran


def test_invalid_sort_raises_up_front(fakes: None) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        pipeline.search("query", sources=["openalex"], sort="date")
    assert "date" in str(excinfo.value)
    assert CALLS == []


def test_construction_failure_is_recorded_not_fatal(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {"search_results": [paper("A", doi="10.1/a")]}

    result = pipeline.search("query", sources=["openalex", "broken"])

    assert isinstance(result.errors["broken"], ConfigurationError)
    assert [p.title for p in result.papers] == ["A"]
    assert result.by_source == {"openalex": 1, "broken": 0}


# -- search: rate-limit retry (D8) -----------------------------------------


def test_rate_limit_retry_succeeds_on_the_second_attempt(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {
        "search_error": RateLimitError("openalex", "slow down"),
        "search_raises": 1,
        "search_results": [paper("A", doi="10.1/a")],
    }

    result = pipeline.search("query", sources=["openalex"])

    assert result.errors == {}
    assert [p.title for p in result.papers] == ["A"]
    assert CALLS.count("search:openalex") == 2
    # No Retry-After on the error -> the default delay, captured not slept.
    assert SLEEPS == [2.0]


def test_rate_limit_gives_up_after_the_configured_retries(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {
        "search_error": RateLimitError("openalex", "slow down"),
        "search_raises": 99,
    }

    result = pipeline.search("query", sources=["openalex"])

    assert isinstance(result.errors["openalex"], RateLimitError)
    assert CALLS.count("search:openalex") == 2  # 1 attempt + 1 default retry
    assert len(SLEEPS) == 1


def test_retries_zero_gives_up_immediately(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {
        "search_error": RateLimitError("openalex", "slow down"),
        "search_raises": 99,
    }

    result = pipeline.search("query", sources=["openalex"], settings=Settings(retries=0))

    assert isinstance(result.errors["openalex"], RateLimitError)
    assert CALLS.count("search:openalex") == 1  # no extra attempt
    assert SLEEPS == []  # and therefore nothing to wait for


def test_retries_two_succeeds_on_the_third_try(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {
        "search_error": RateLimitError("openalex", "slow down"),
        "search_raises": 2,
        "search_results": [paper("A", doi="10.1/a")],
    }

    result = pipeline.search("query", sources=["openalex"], settings=Settings(retries=2))

    assert result.errors == {}
    assert [p.title for p in result.papers] == ["A"]
    assert CALLS.count("search:openalex") == 3  # 1 + 2 extras
    assert SLEEPS == [2.0, 2.0]  # slept twice, stubbed


def test_retries_two_gives_up_after_three_attempts(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {
        "search_error": RateLimitError("openalex", "slow down"),
        "search_raises": 99,
    }

    result = pipeline.search("query", sources=["openalex"], settings=Settings(retries=2))

    assert isinstance(result.errors["openalex"], RateLimitError)
    assert CALLS.count("search:openalex") == 3  # exactly 1 + 2
    assert len(SLEEPS) == 2  # sleeps happen between attempts, not after the last


# -- get: routing ----------------------------------------------------------


def test_get_routes_a_doi_openalex_then_crossref_then_s2(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {"get_error": NotFoundError("openalex", "nope")}
    BEHAVIOUR["crossref"] = {"get_paper": paper("Found via crossref", doi="10.1/x")}
    BEHAVIOUR["semantic_scholar"] = {"get_paper": paper("must not be used")}

    found = pipeline.get("10.1103/PhysRevLett.116.061102")

    assert found.title == "Found via crossref"
    assert CALLS == ["get:openalex", "get:crossref"]  # third never tried


def test_get_routes_an_arxiv_id_to_arxiv_first(fakes: None) -> None:
    BEHAVIOUR["arxiv"] = {"get_error": NotFoundError("arxiv", "nope")}
    BEHAVIOUR["openalex"] = {"get_paper": paper("via openalex", arxiv_id="1706.03762")}

    found = pipeline.get("arXiv:1706.03762")

    assert found.title == "via openalex"
    assert CALLS == ["get:arxiv", "get:openalex"]


def test_get_routes_a_pmid_to_pubmed_only(fakes: None) -> None:
    BEHAVIOUR["pubmed"] = {"get_paper": paper("via pubmed", pmid="22470460")}

    found = pipeline.get("22470460")

    assert found.title == "via pubmed"
    assert CALLS == ["get:pubmed"]


def test_get_with_an_explicit_source_uses_only_that_source(fakes: None) -> None:
    BEHAVIOUR["pubmed"] = {"get_error": NotFoundError("pubmed", "nope")}

    with pytest.raises(NotFoundError):
        pipeline.get("10.1/anything", source="pubmed")

    assert CALLS == ["get:pubmed"]


def test_get_with_an_unknown_explicit_source_raises(fakes: None) -> None:
    with pytest.raises(ConfigurationError):
        pipeline.get("10.1/x", source="nope")
    assert CALLS == []


# -- get: error aggregation ------------------------------------------------


def test_get_all_not_found_raises_not_found_listing_every_source(
    fakes: None,
) -> None:
    for name in ("openalex", "crossref", "semantic_scholar"):
        BEHAVIOUR[name] = {"get_error": NotFoundError(name, f"no {name} record")}

    with pytest.raises(NotFoundError) as excinfo:
        pipeline.get("10.1/absent")

    message = str(excinfo.value)
    assert "no source has this record" in message
    for name in ("openalex", "crossref", "semantic_scholar"):
        assert name in message
    assert excinfo.value.source == "pipeline"


def test_get_mixed_failures_raise_source_error_listing_every_source(
    fakes: None,
) -> None:
    BEHAVIOUR["openalex"] = {"get_error": NotFoundError("openalex", "nope")}
    BEHAVIOUR["crossref"] = {"get_error": SourceError("crossref", "boom")}
    BEHAVIOUR["semantic_scholar"] = {"get_error": NotFoundError("semantic_scholar", "nope")}

    with pytest.raises(SourceError) as excinfo:
        pipeline.get("10.1/x")

    assert type(excinfo.value) is SourceError  # not the NotFoundError subclass
    assert "every source failed" in str(excinfo.value)
    assert "crossref: boom" in str(excinfo.value)


def test_rate_limit_sleep_uses_the_servers_retry_after(fakes: None) -> None:
    """The exception's own value must reach _sleep, not the 2s default."""
    BEHAVIOUR["openalex"] = {
        "search_error": RateLimitError(
            "openalex", "slow down", retry_after=7.0
        ),
        "search_raises": 99,
    }

    result = pipeline.search("query", sources=["openalex"])

    assert isinstance(result.errors["openalex"], RateLimitError)
    assert SLEEPS == [7.0]  # not the 2.0 fallback


def test_rate_limit_without_retry_after_falls_back_to_two_seconds(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {
        "search_error": RateLimitError("openalex", "slow down"),
        "search_raises": 99,
    }

    pipeline.search("query", sources=["openalex"])

    assert SLEEPS == [2.0]


def test_retry_after_is_capped_at_ten_seconds(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {
        "search_error": RateLimitError("openalex", "slow down", retry_after=600.0),
        "search_raises": 99,
    }

    pipeline.search("query", sources=["openalex"])

    assert SLEEPS == [10.0]  # min(retry_after or 2, 10)
