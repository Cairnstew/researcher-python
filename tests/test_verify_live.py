"""Offline tests for ``scripts/verify_live.py`` (fakes in the registry, no network)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest

import researcher_python.sources as registry
from researcher_python.config import Settings
from researcher_python.errors import (
    ConfigurationError,
    NotFoundError,
    RateLimitError,
    SourceUnavailableError,
    UnsupportedOperation,
)
from researcher_python.models import Paper
from researcher_python.sources.base import Capabilities, Source

ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "verify_live", ROOT / "scripts" / "verify_live.py"
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    # dataclasses resolves annotations through sys.modules[cls.__module__].
    sys.modules["verify_live"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


verify_live = _load_script()

#: Every network entry point a fake was asked to serve.
CALLS: list[str] = []


def known_paper(name: str) -> Paper:
    identifier = verify_live.KNOWN_IDS[name]
    if verify_live.is_doi(identifier):
        return Paper(
            sources=[name],
            title=f"{name} record",
            doi=identifier,
            pdf_url="https://example.org/open.pdf",
        )
    if verify_live.is_arxiv_id(identifier):
        return Paper(
            sources=[name],
            title=f"{name} record",
            arxiv_id=identifier,
            pdf_url="https://example.org/open.pdf",
        )
    return Paper(sources=[name], title=f"{name} record", pmid=identifier)


class FakeSource(Source):
    """Class-level behaviour so each registered name is configured on its own."""

    name = "fake"
    capabilities = Capabilities(
        search=True, get=True, references=True, citations=True, pdf=True
    )
    papers: ClassVar[list[Paper]] = []
    get_result: ClassVar[Paper | None] = None
    get_errors: ClassVar[dict[str, Exception]] = {}
    search_error: ClassVar[Exception | None] = None
    refs: ClassVar[list[Paper]] = []
    cites: ClassVar[list[Paper]] = []

    def search(self, query: str, *, limit: int | None = None) -> list[Paper]:
        CALLS.append(f"search:{self.name}")
        if self.search_error is not None:
            raise self.search_error
        return list(self.papers)

    def get(self, key: str) -> Paper:
        CALLS.append(f"get:{self.name}")
        error = self.get_errors.get(key)
        if error is not None:
            raise error
        assert self.get_result is not None
        return self.get_result

    def references(self, paper: Paper) -> list[Paper]:
        # Mirrors the real adapters: refuse first, then work.
        self.require_support("references")
        CALLS.append(f"references:{self.name}")
        return list(self.refs)

    def citations(self, paper: Paper) -> list[Paper]:
        self.require_support("citations")
        CALLS.append(f"citations:{self.name}")
        return list(self.cites)


class ArxivFake(FakeSource):
    name = "arxiv"
    capabilities = Capabilities(
        search=True, get=True, references=False, citations=False, pdf=True
    )


class OpenAlexFake(FakeSource):
    name = "openalex"


class CrossrefFake(FakeSource):
    name = "crossref"
    capabilities = Capabilities(
        search=True, get=True, references=True, citations=False, pdf=False
    )


class SemanticScholarFake(FakeSource):
    name = "semantic_scholar"


class PubmedFake(FakeSource):
    name = "pubmed"
    capabilities = Capabilities(
        search=True, get=True, references=True, citations=True, pdf=False
    )


class UnconfiguredFake(FakeSource):
    name = "pubmed"

    def __init__(self, settings: Settings | None = None, **_: object) -> None:
        raise ConfigurationError("setting 'email' is required; export RESEARCHER_EMAIL first")


class MissingDependencyFake(FakeSource):
    name = "pubmed"

    def __init__(self, settings: Settings | None = None, **_: object) -> None:
        raise SourceUnavailableError("pubmed", "dependency 'Bio' is not installed")


class RateLimitedFake(FakeSource):
    name = "arxiv"
    search_error = None  # set per-test


class BrokenContractFake(FakeSource):
    """Declares no citations capability but answers anyway."""

    name = "arxiv"
    capabilities = Capabilities(
        search=True, get=True, references=False, citations=False, pdf=True
    )

    def citations(self, paper: Paper) -> list[Paper]:  # should raise instead
        CALLS.append(f"citations:{self.name}")
        return []


ALL_FAKES = (
    ArxivFake,
    OpenAlexFake,
    CrossrefFake,
    SemanticScholarFake,
    PubmedFake,
)


def configure_happy_fakes() -> None:
    for cls in ALL_FAKES:
        paper = known_paper(cls.name)
        cls.papers = [paper]
        cls.get_result = paper
        cls.get_errors = {
            verify_live.WRONG_IDS[cls.name]: NotFoundError(cls.name, "wrong kind")
        }
        cls.search_error = None
        cls.refs = [paper]
        cls.cites = [paper]


configure_happy_fakes()


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Register the five happy fakes and silence settings/network."""
    monkeypatch.setattr(
        registry,
        "_REGISTRY",
        {cls.name: (__name__, cls.__name__) for cls in ALL_FAKES},
    )
    monkeypatch.setattr(verify_live, "load_settings", lambda: Settings())
    monkeypatch.setattr(verify_live.time, "sleep", lambda _s: None)
    CALLS.clear()
    configure_happy_fakes()
    yield
    CALLS.clear()


def run(*args: str) -> tuple[int, str, str]:
    """Invoke main() in-process, capturing (code, stdout, stderr)."""
    import contextlib
    import io

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = verify_live.main(list(args))
    return code, out.getvalue(), err.getvalue()


# -- happy path ------------------------------------------------------------


def test_all_pass_run_exits_zero(world: None) -> None:
    code, out, err = run("--delay", "0")

    assert code == 0, out + err
    assert "0 FAIL" in out
    assert "0 RATELIMIT" in out
    assert "SOURCE" in out and "OUTCOME" in out  # the table was printed


def test_all_pass_run_has_only_pass_and_skip(world: None) -> None:
    code, out, _ = run("--delay", "0", "--format", "json")
    rows = json.loads(out)

    assert code == 0
    assert {row["outcome"] for row in rows} == {"PASS", "SKIP"}
    assert any(row["outcome"] == "PASS" for row in rows)


def test_every_happy_source_covers_the_expected_checks(world: None) -> None:
    code, out, _ = run("--delay", "0", "--format", "json")
    rows = json.loads(out)

    assert code == 0
    by_source: dict[str, list[str]] = {}
    for row in rows:
        by_source.setdefault(row["source"], []).append(row["check"])

    assert by_source["arxiv"] == ["construct", "search", "get", "wrong-id", "citations", "pdf-url"]
    assert by_source["crossref"] == [
        "construct", "search", "get", "wrong-id", "references", "citations", "doi-case",
    ]
    assert by_source["openalex"][-1] == "pdf-url"
    assert "doi-case" in by_source["crossref"]
    # capabilities are never hardcoded: pdf=False sources get no pdf-url row
    assert "pdf-url" not in by_source["pubmed"]
    assert "references" not in by_source["arxiv"]


# -- failures --------------------------------------------------------------


def test_wrong_id_raising_the_wrong_exception_is_a_fail(
    monkeypatch: pytest.MonkeyPatch, world: None
) -> None:
    # The wrong identifier resolves instead of raising NotFoundError.
    CrossrefFake.get_errors = {}

    code, out, _ = run("--delay", "0", "--source", "crossref", "--format", "json")

    rows = {row["check"]: row for row in json.loads(out)}
    assert rows["wrong-id"]["outcome"] == "FAIL"
    assert "returned a record instead of raising" in rows["wrong-id"]["detail"]
    assert code == 1


def test_configuration_error_is_a_skip_and_stops_that_source(
    world: None,
) -> None:
    registry._REGISTRY["pubmed"] = (__name__, "UnconfiguredFake")

    code, out, _ = run("--delay", "0", "--source", "pubmed", "--format", "json")

    rows = json.loads(out)
    assert code == 0  # a SKIP never fails a run
    assert len(rows) == 1
    assert rows[0]["check"] == "construct"
    assert rows[0]["outcome"] == "SKIP"
    assert rows[0]["detail"] == "needs RESEARCHER_EMAIL"


def test_missing_dependency_is_a_skip(world: None) -> None:
    registry._REGISTRY["pubmed"] = (__name__, "MissingDependencyFake")

    code, out, _ = run("--delay", "0", "--source", "pubmed", "--format", "json")

    rows = json.loads(out)
    assert code == 0
    assert rows[0]["outcome"] == "SKIP"
    assert "not installed" in rows[0]["detail"]


# -- rate limiting ---------------------------------------------------------


def test_rate_limit_skips_the_rest_and_exits_three(
    monkeypatch: pytest.MonkeyPatch, world: None
) -> None:
    ArxivFake.search_error = RateLimitError(
        "arxiv", "slow down", retry_after=5.0
    )

    code, out, _ = run("--delay", "0", "--source", "arxiv", "--format", "json")

    rows = json.loads(out)
    assert code == 3
    by_check = {row["check"]: row for row in rows}

    assert by_check["search"]["outcome"] == "RATELIMIT"
    assert "retry_after=5.0" in by_check["search"]["detail"]
    # Everything network-bound after it is skipped, not retried.
    assert by_check["get"]["outcome"] == "SKIP"
    assert by_check["get"]["detail"] == "after rate limit"
    assert by_check["wrong-id"]["outcome"] == "SKIP"
    assert by_check["citations"]["detail"] == "after rate limit"
    assert CALLS.count("search:arxiv") == 1  # exactly one attempt
    assert "get:arxiv" not in CALLS


def test_rate_limit_on_one_source_leaves_the_others_alone(
    world: None,
) -> None:
    ArxivFake.search_error = RateLimitError("arxiv", "slow down")

    code, out, _ = run("--delay", "0", "--format", "json")

    rows = json.loads(out)
    assert code == 3  # no FAIL anywhere
    sources_with_rate_limit = {r["source"] for r in rows if r["outcome"] == "RATELIMIT"}
    assert sources_with_rate_limit == {"arxiv"}
    assert any(
        r["outcome"] == "PASS" and r["source"] == "openalex" for r in rows
    )


# -- capability contract ---------------------------------------------------


def test_unsupported_citations_is_a_skip(world: None) -> None:
    code, out, _ = run("--delay", "0", "--source", "arxiv", "--format", "json")

    rows = {row["check"]: row for row in json.loads(out)}
    assert code == 0
    assert rows["citations"]["outcome"] == "SKIP"
    assert rows["citations"]["detail"] == "unsupported"
    # base.Source refused it, so no call reached the adapter.
    assert "citations:arxiv" not in CALLS


def test_citations_cap_without_a_refusal_is_a_fail(
    monkeypatch: pytest.MonkeyPatch, world: None
) -> None:
    registry._REGISTRY["arxiv"] = (__name__, "BrokenContractFake")

    code, out, _ = run("--delay", "0", "--source", "arxiv", "--format", "json")

    rows = {row["check"]: row for row in json.loads(out)}
    assert rows["citations"]["outcome"] == "FAIL"
    assert "did not raise UnsupportedOperation" in rows["citations"]["detail"]
    assert code == 1


# -- doi-case --------------------------------------------------------------

class MismatchedFake(CrossrefFake):
    """Answers every lookup, but with a different title each time."""

    name = "crossref"
    calls = 0

    def get(self, key: str) -> Paper:
        CALLS.append(f"get:{self.name}")
        MismatchedFake.calls += 1
        title = "mixed case title" if MismatchedFake.calls % 2 else "lower title"
        return Paper(sources=["crossref"], title=title, doi=key)




def test_doi_case_only_runs_for_crossref(world: None) -> None:
    code, out, _ = run("--delay", "0", "--format", "json")

    rows = json.loads(out)
    doi_case = [row for row in rows if row["check"] == "doi-case"]
    assert code == 0
    assert [row["source"] for row in doi_case] == ["crossref"]
    assert doi_case[0]["outcome"] == "PASS"


def test_doi_case_fails_when_the_two_readings_disagree(world: None) -> None:
    MismatchedFake.calls = 0
    registry._REGISTRY["crossref"] = (__name__, "MismatchedFake")

    code, out, _ = run("--delay", "0", "--source", "crossref", "--format", "json")

    rows = {row["check"]: row for row in json.loads(out)}
    assert rows["doi-case"]["outcome"] == "FAIL"
    assert "!=" in rows["doi-case"]["detail"]
    assert code == 1


# -- CLI -------------------------------------------------------------------


def test_dry_run_makes_zero_calls(world: None) -> None:
    code, out, _ = run("--dry-run")

    assert code == 0
    assert CALLS == []
    assert "checks planned" in out
    assert "construct" in out and "doi-case" in out


def test_dry_run_json_is_a_plan(world: None) -> None:
    code, out, _ = run("--dry-run", "--format", "json")

    plan = json.loads(out)
    assert code == 0
    assert CALLS == []
    assert all(set(row) == {"source", "check"} for row in plan)
    assert {row["source"] for row in plan} == set(verify_live.KNOWN_IDS)


def test_json_output_parses_with_the_documented_fields(world: None) -> None:
    code, out, _ = run("--delay", "0", "--source", "openalex", "--format", "json")

    rows = json.loads(out)
    assert code == 0
    assert isinstance(rows, list) and rows
    assert set(rows[0]) == {"source", "check", "outcome", "seconds", "detail"}


def test_source_filtering_restricts_the_run(world: None) -> None:
    code, out, _ = run("--delay", "0", "--source", "arxiv", "--format", "json")

    rows = json.loads(out)
    assert code == 0
    assert {row["source"] for row in rows} == {"arxiv"}
    assert CALLS == [c for c in CALLS if c.endswith(":arxiv")]


def test_unknown_source_exits_two(world: None) -> None:
    code, _out, err = run("--source", "nope")
    assert code == 2
    assert "nope" in err


def test_fail_beats_ratelimit_in_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, world: None
) -> None:
    ArxivFake.search_error = RateLimitError("arxiv", "slow down")
    CrossrefFake.get_errors = {}  # wrong-id then resolves -> FAIL

    code, _out, _err = run("--delay", "0")

    assert code == 1


def test_exit_code_precedence_is_encoded() -> None:
    rows = [
        verify_live.Check("a", "x", verify_live.FAIL, 0.0, ""),
        verify_live.Check("b", "y", verify_live.RATELIMIT, 0.0, ""),
        verify_live.Check("c", "z", verify_live.SKIP, 0.0, ""),
    ]
    assert verify_live.exit_code(rows) == 1
    assert verify_live.exit_code(rows[1:]) == 3
    assert verify_live.exit_code(rows[2:]) == 0
    assert verify_live.exit_code([]) == 0


def test_plan_derives_from_capabilities() -> None:
    full = verify_live.plan("crossref", CrossrefFake.capabilities)
    bare = verify_live.plan("arxiv", ArxivFake.capabilities)
    assert "references" in full and "references" not in bare
    assert "doi-case" in full and "doi-case" not in bare
    assert "citations" in bare  # always planned; reported as SKIP


def test_unsupported_operation_import_is_exercised() -> None:
    # Sanity: the contract the script asserts really is UnsupportedOperation.
    with pytest.raises(UnsupportedOperation):
        ArxivFake().citations(Paper())
