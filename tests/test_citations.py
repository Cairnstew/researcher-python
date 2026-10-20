"""Offline tests for the deterministic citation fan-out (fakes, no network).

The fakes are registered through the real source registry, exactly as
``test_pipeline.py`` does, so the code under test takes its normal path.
"""

from __future__ import annotations

from typing import Any

import pytest

import researcher_python.citation as citation_mod
import researcher_python.sources as registry
from researcher_python.citation import (
    CITATION_SOURCES,
    citations,
    references,
    sort_citations,
)
from researcher_python.config import Settings
from researcher_python.errors import (
    ConfigurationError,
    NotFoundError,
    RateLimitError,
    ResearcherError,
    SourceError,
    UnsupportedOperation,
)
from researcher_python.models import Paper
from researcher_python.sources.base import Capabilities, Source

#: Behaviour per registry key, e.g. {"references_results": [...], "references_error": ...}.
BEHAVIOUR: dict[str, dict[str, Any]] = {}
#: Every adapter entry point, in call order (single-source order is deterministic).
CALLS: list[str] = []
#: Durations the module was asked to sleep (never actually slept).
SLEEPS: list[float] = []
#: The per-source cap each fake observed via ``settings.max_results``.
CAPS: list[int] = []


def paper(
    title: str,
    *,
    doi: str = "",
    arxiv_id: str = "",
    pmid: str = "",
    year: int | None = None,
    citation_count: int | None = None,
    ids: dict[str, str] | None = None,
) -> Paper:
    return Paper(
        title=title,
        doi=doi,
        arxiv_id=arxiv_id,
        pmid=pmid,
        year=year,
        citation_count=citation_count,
        ids=ids or {},
    )


class FakeSource(Source):
    """Configurable stand-in; behaviour comes from ``BEHAVIOUR[self.name]``."""

    name = "fake"
    capabilities = Capabilities(
        search=True, get=True, references=True, citations=True, pdf=False
    )

    @property
    def _spec(self) -> dict[str, Any]:
        return BEHAVIOUR.setdefault(self.name, {})

    def _stamp(self, item: Paper) -> Paper:
        out = item.copy()
        out.sources = [self.name]
        return out

    def search(self, query: str, *, limit: int | None = None) -> list[Paper]:  # pragma: no cover
        raise NotImplementedError

    def get(self, key: str) -> Paper:  # pragma: no cover
        raise NotImplementedError

    def _behaviour(self, direction: str) -> list[Paper]:
        CALLS.append(f"{direction}:{self.name}")
        CAPS.append(self.settings.max_results)
        spec = self._spec
        error = spec.get(f"{direction}_error")
        if error is not None:
            # No count -> always raise; ``_raises: N`` -> only N times.
            remaining = spec.get(f"{direction}_raises")
            if remaining is None or remaining > 0:
                if remaining is not None:
                    spec[f"{direction}_raises"] = remaining - 1
                raise error
        items = list(spec.get(f"{direction}_results", []))
        if not spec.get("uncapped"):
            items = items[: self.settings.max_results]
        return [self._stamp(item) for item in items]

    def references(self, paper: Paper) -> list[Paper]:
        return self._behaviour("references")

    def citations(self, paper: Paper) -> list[Paper]:
        return self._behaviour("citations")


class FakeOpenAlex(FakeSource):
    name = "openalex"


class FakeSemanticScholar(FakeSource):
    name = "semantic_scholar"


class FakeCrossref(FakeSource):
    name = "crossref"


class FakePubmed(FakeSource):
    name = "pubmed"


class FakeCrossrefNoRefs(FakeSource):
    """Declares no ``references`` support (registered over ``crossref`` on demand)."""

    name = "crossref"
    capabilities = Capabilities(
        search=True, get=True, references=False, citations=True, pdf=False
    )


class FakeBroken(FakeSource):
    name = "broken"

    def __init__(self, settings: Any = None, **_: object) -> None:
        raise ConfigurationError("broken source requires RESEARCHER_EMAIL")


FAKES: dict[str, type[FakeSource]] = {
    "openalex": FakeOpenAlex,
    "semantic_scholar": FakeSemanticScholar,
    "crossref": FakeCrossref,
    "pubmed": FakePubmed,
    "broken": FakeBroken,
}


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Any:
    for key, cls in FAKES.items():
        monkeypatch.setitem(registry._REGISTRY, key, (__name__, cls.__name__))
    monkeypatch.setattr(citation_mod, "_sleep", SLEEPS.append)
    BEHAVIOUR.clear()
    CALLS.clear()
    SLEEPS.clear()
    CAPS.clear()
    yield
    BEHAVIOUR.clear()
    CALLS.clear()
    SLEEPS.clear()
    CAPS.clear()


def subject() -> Paper:
    return paper("Subject", doi="10.1/sub")


# -- fan-out ---------------------------------------------------------------


def test_references_merge_across_sources(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {
        "references_results": [paper("A", doi="10.1/a"), paper("B", doi="10.1/b")]
    }
    BEHAVIOUR["crossref"] = {"references_results": [paper("C", doi="10.1/c")]}

    result = references(subject(), sources=["openalex", "crossref"])

    assert result.direction == "references"
    assert result.errors == {}
    assert result.by_source == {"openalex": 2, "crossref": 1}
    assert {p.title for p in result.papers} == {"A", "B", "C"}


def test_direction_selects_the_operation(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {
        "references_results": [paper("Ref", doi="10.1/ref")],
        "citations_results": [paper("Cite", doi="10.1/cite")],
    }

    refs = references(subject(), sources=["openalex"])
    cites = citations(subject(), sources=["openalex"])

    assert [p.title for p in refs.papers] == ["Ref"]
    assert [p.title for p in cites.papers] == ["Cite"]
    assert CALLS == ["references:openalex", "citations:openalex"]


def test_default_sources_are_the_capable_ones_in_canonical_order(fakes: None) -> None:
    for name in ("openalex", "semantic_scholar", "crossref", "pubmed"):
        BEHAVIOUR[name] = {}

    result = references(subject())

    assert result.sources == ["openalex", "semantic_scholar", "crossref", "pubmed"]
    assert result.sources == list(CITATION_SOURCES)


def test_default_sources_skip_a_source_without_the_capability(
    fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(registry._REGISTRY, "crossref", (__name__, "FakeCrossrefNoRefs"))
    BEHAVIOUR["openalex"] = {"references_results": [paper("A", doi="10.1/a")]}

    result = references(subject())

    assert "crossref" not in result.sources
    assert "crossref" not in result.errors
    assert [p.title for p in result.papers] == ["A"]


def test_explicit_unsupported_source_is_recorded(fakes: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(registry._REGISTRY, "crossref", (__name__, "FakeCrossrefNoRefs"))

    result = references(subject(), sources=["crossref"])

    assert isinstance(result.errors["crossref"], UnsupportedOperation)
    assert result.papers == []


def test_unknown_source_raises_up_front(fakes: None) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        references(subject(), sources=["openalex", "nope"])
    assert "'nope'" in str(excinfo.value)
    assert CALLS == []  # nothing ran


def test_identifier_string_is_accepted_as_the_subject(fakes: None) -> None:
    """A DOI/arXiv/PMID string is resolved first, like the CLI and MCP tools.

    This used to raise ConfigurationError ("needs a Paper"), which made the
    obvious library call fail with a message that said what was wrong but not
    what to do. The three faces now agree: all accept an identifier.

    The fake source cannot resolve identifiers, so resolution fails — the point
    is *which* failure: no longer a type complaint about passing a str.
    """
    from researcher_python.errors import ConfigurationError as ConfigErr

    with pytest.raises(ResearcherError) as excinfo:
        references("10.1/x")  # resolution attempted, then the fake declines
    assert not isinstance(excinfo.value, ConfigErr)
    assert "citation query needs a Paper" not in str(excinfo.value)


def test_a_non_paper_non_string_still_raises_configuration_error(fakes: None) -> None:
    with pytest.raises(ConfigurationError):
        references(123)  # type: ignore[arg-type]


def test_construction_failure_is_recorded_not_fatal(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {"references_results": [paper("A", doi="10.1/a")]}

    result = references(subject(), sources=["openalex", "broken"])

    assert isinstance(result.errors["broken"], ConfigurationError)
    assert [p.title for p in result.papers] == ["A"]


def test_partial_failure_is_recorded_others_succeed(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {"references_results": [paper("A", doi="10.1/a")]}
    BEHAVIOUR["crossref"] = {"references_error": SourceError("crossref", "boom")}

    result = references(subject(), sources=["openalex", "crossref"])

    assert [p.title for p in result.papers] == ["A"]
    assert isinstance(result.errors["crossref"], SourceError)
    assert "openalex" not in result.errors
    assert result.by_source == {"openalex": 1, "crossref": 0}


# -- determinism -----------------------------------------------------------


def test_order_is_independent_of_source_argument_order(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {
        "references_results": [paper("B", doi="10.1/b"), paper("A", doi="10.1/a")]
    }
    BEHAVIOUR["crossref"] = {"references_results": [paper("C", doi="10.1/c")]}

    one = references(subject(), sources=["openalex", "crossref"])
    two = references(subject(), sources=["crossref", "openalex"])

    assert [p.title for p in one.papers] == [p.title for p in two.papers]
    assert one.edges == two.edges


def test_canonical_order_is_the_stable_default(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {
        "references_results": [paper("zeta"), paper("Beta"), paper("alpha")]
    }

    result = references(subject(), sources=["openalex"])

    assert [p.title for p in result.papers] == ["alpha", "Beta", "zeta"]


def test_a_second_identical_call_is_identical(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {
        "references_results": [paper("B", doi="10.1/b"), paper("A", doi="10.1/a")]
    }

    first = references(subject(), sources=["openalex"])
    second = references(subject(), sources=["openalex"])

    assert [p.to_dict() for p in first.papers] == [p.to_dict() for p in second.papers]
    assert first.edges == second.edges
    assert first.keys() == second.keys()


def test_order_year_puts_newest_first_and_unknown_last(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {
        "references_results": [
            paper("Old", doi="10.1/o", year=2000),
            paper("New", doi="10.1/n", year=2020),
            paper("Unknown", doi="10.1/u", year=None),
        ]
    }

    result = references(subject(), sources=["openalex"], order="year")

    assert [p.title for p in result.papers] == ["New", "Old", "Unknown"]


def test_unknown_order_raises_up_front(fakes: None) -> None:
    with pytest.raises(ConfigurationError):
        references(subject(), sources=["openalex"], order="date")
    assert CALLS == []


def test_sort_citations_rejects_an_unknown_order() -> None:
    with pytest.raises(ConfigurationError):
        sort_citations([paper("A")], "nope")


# -- provenance and dedupe -------------------------------------------------


def test_dedupe_merges_the_same_doi_and_keeps_provenance(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {"references_results": [paper("Same", doi="10.1/same")]}
    BEHAVIOUR["crossref"] = {"references_results": [paper("Same", doi="10.1/same")]}

    result = references(subject(), sources=["openalex", "crossref"])

    assert len(result.papers) == 1
    assert set(result.papers[0].sources) == {"openalex", "crossref"}
    assert result.edges == {"10.1/same": ["crossref", "openalex"]}
    assert result.by_source == {"openalex": 1, "crossref": 1}  # raw, pre-dedupe


def test_edges_map_every_record_to_its_sources(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {
        "references_results": [paper("A", doi="10.1/a"), paper("B", doi="10.1/b")]
    }

    result = references(subject(), sources=["openalex"])

    assert result.edges == {"10.1/a": ["openalex"], "10.1/b": ["openalex"]}


def test_subject_identity_is_recorded(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {}

    result = references(paper("Subject", doi="10.1/sub", pmid="123"))

    assert result.subject["doi"] == "10.1/sub"
    assert result.subject["pmid"] == "123"


# -- caps ------------------------------------------------------------------


def test_limit_is_applied_per_source_and_reaches_the_adapter(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {
        "references_results": [paper(f"R{i}", doi=f"10.1/r{i}") for i in range(5)]
    }

    result = references(subject(), sources=["openalex"], limit=3)

    assert CAPS == [3]
    assert result.requested == 3
    assert len(result.papers) == 3


def test_hitting_the_cap_is_flagged_as_truncated(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {
        "references_results": [paper(f"R{i}", doi=f"10.1/r{i}") for i in range(5)],
        "uncapped": True,  # an adapter (Crossref) that ignores max_results
    }

    result = references(subject(), sources=["openalex"], limit=2)

    assert len(result.papers) == 2
    assert result.by_source["openalex"] == 2
    assert result.truncated["openalex"] is True


def test_below_the_cap_is_not_truncated(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {"references_results": [paper("A", doi="10.1/a")]}

    result = references(subject(), sources=["openalex"], limit=10)

    assert result.truncated["openalex"] is False


@pytest.mark.parametrize("bad", [0, -1, 2.5, True])
def test_bad_limit_raises_up_front(fakes: None, bad: Any) -> None:
    with pytest.raises(ConfigurationError):
        references(subject(), sources=["openalex"], limit=bad)
    assert CALLS == []


# -- strict mode -----------------------------------------------------------


def test_strict_raises_when_a_source_fails(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {"references_results": [paper("A", doi="10.1/a")]}
    BEHAVIOUR["crossref"] = {"references_error": SourceError("crossref", "boom")}

    with pytest.raises(SourceError) as excinfo:
        references(subject(), sources=["openalex", "crossref"], strict=True)

    assert "strict references query incomplete" in str(excinfo.value)
    assert "crossref: boom" in str(excinfo.value)


def test_strict_all_not_found_raises_not_found(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {"references_error": NotFoundError("openalex", "no refs")}

    with pytest.raises(NotFoundError):
        references(subject(), sources=["openalex"], strict=True)


def test_strict_succeeds_when_every_source_answers(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {"references_results": [paper("A", doi="10.1/a")]}
    BEHAVIOUR["crossref"] = {"references_results": [paper("C", doi="10.1/c")]}

    result = references(subject(), sources=["openalex", "crossref"], strict=True)

    assert result.errors == {}
    assert result.strict is True
    assert {p.title for p in result.papers} == {"A", "C"}


# -- rate-limit retry (D8, shared with the pipeline) -----------------------


def test_rate_limit_is_retried_once(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {
        "references_error": RateLimitError("openalex", "slow down"),
        "references_raises": 1,
        "references_results": [paper("A", doi="10.1/a")],
    }

    result = references(subject(), sources=["openalex"])

    assert result.errors == {}
    assert [p.title for p in result.papers] == ["A"]
    assert CALLS.count("references:openalex") == 2
    assert SLEEPS == [2.0]  # the shared _retry helper, stubbed here


def test_retries_zero_gives_up_immediately(fakes: None) -> None:
    BEHAVIOUR["openalex"] = {"references_error": RateLimitError("openalex", "slow down")}

    result = references(
        subject(), sources=["openalex"], settings=Settings(retries=0)
    )

    assert isinstance(result.errors["openalex"], RateLimitError)
    assert CALLS.count("references:openalex") == 1
    assert SLEEPS == []
