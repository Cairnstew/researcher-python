"""Offline tests for the MCP server surface (fakes, no network).

Two layers are covered:

* the **framework-free** tool implementations (``_tool_*``), which are plain
  functions returning JSON-able dicts — the part that owns all the logic; and
* the **registered** server (``build_server``), asserting the SDK exposes
  exactly the declared tool names, that each MCP tool is wired to its
  implementation, and that the ``mcp`` import is lazy.

The ``mcp`` SDK is an optional extra.  If it is not importable the
server-building tests skip, but the ``_tool_*`` tests still run — they have no
SDK dependency at all, which is the point of keeping the logic framework-free.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import researcher_python.mcp_server as mcp_mod
import researcher_python.sources as registry
from researcher_python.config import Settings
from researcher_python.errors import NotFoundError, SourceError
from researcher_python.models import Paper
from researcher_python.sources.base import Capabilities, Source


def _require_mcp():
    """Import ``FastMCP`` or skip the test when the optional extra is absent."""
    return pytest.importorskip(
        "mcp.server.fastmcp", reason="the 'mcp' extra is not installed"
    )


# ---------------------------------------------------------------------- #
# Fakes registered through the real registry (same pattern as test_pipeline)
# ---------------------------------------------------------------------- #

#: identifier -> Paper, for the fake sources' ``get``.
KNOWLEDGE: dict[str, Paper] = {}
#: query -> list[Paper], for ``search``.
SEARCHES: dict[str, list[Paper]] = {}
#: errors keyed by (direction, identifier).
FAILURES: dict[tuple[str, str], Exception] = {}


def _known(title: str, **kwargs: Any) -> Paper:
    return Paper(title=title, sources=["openalex"], **kwargs)


class FakeSource(Source):
    name = "fake"
    capabilities = Capabilities(
        search=True, get=True, references=True, citations=True, pdf=False
    )

    def _bump(self, paper: Paper) -> Paper:
        out = paper.copy()
        out.sources = [self.name]
        return out

    def search(
        self, query: str, *, limit: int | None = None, filters=None
    ) -> list[Paper]:
        failure = FAILURES.get(("search", query))
        if failure is not None:
            raise failure
        papers = SEARCHES.get(query, [])
        return [self._bump(p) for p in papers[: (limit or len(papers))]]

    def get(self, key: str) -> Paper:
        paper = KNOWLEDGE.get(key)
        if paper is None:
            raise NotFoundError(self.name, f"no record for {key!r}")
        return self._bump(paper)

    def _direction(self, paper: Paper, direction: str) -> list[Paper]:
        key = FAILURES.get((direction, paper.title))
        if key is not None:
            raise key
        return [self._bump(_known(f"{direction}-of-{paper.title}"))]

    def references(self, paper: Paper) -> list[Paper]:
        return self._direction(paper, "references")

    def citations(self, paper: Paper) -> list[Paper]:
        return self._direction(paper, "citations")


class FakeOpenAlex(FakeSource):
    name = "openalex"


class FakeArxiv(FakeSource):
    name = "arxiv"


class FakePubmed(FakeSource):
    name = "pubmed"


@pytest.fixture
def fakes(monkeypatch):
    """Replace the real adapters with the fakes above and reset their state."""
    # The registry stores (module path, class name); this module holds the fakes.
    for key, cls in (("openalex", FakeOpenAlex), ("arxiv", FakeArxiv), ("pubmed", FakePubmed)):
        monkeypatch.setitem(registry._REGISTRY, key, (__name__, cls.__name__))

    KNOWLEDGE.clear()
    SEARCHES.clear()
    FAILURES.clear()
    try:
        yield
    finally:
        KNOWLEDGE.clear()
        SEARCHES.clear()
        FAILURES.clear()


@pytest.fixture(autouse=True)
def settings():
    """Give every tool an explicit Settings so `.env` never leaks in."""
    injected = Settings(max_results=10, retries=0)
    mcp_mod._set_settings(injected)
    mcp_mod._set_workspace(None)
    try:
        yield injected
    finally:
        mcp_mod._set_workspace(None)
        mcp_mod._set_settings(None)


# ---------------------------------------------------------------------- #
# _tool_sources / _tool_info
# ---------------------------------------------------------------------- #


def test_sources_lists_registry_and_web_sources(fakes):
    payload = mcp_mod._tool_sources()
    assert payload["ok"] is True
    names = {entry["name"] for entry in payload["scholarly"]}
    assert "openalex" in names
    assert all("capabilities" in entry for entry in payload["scholarly"])
    assert isinstance(payload["web"], list)
    assert "openalex" in payload["defaults"] or payload["defaults"]


def test_info_reports_settings_with_secrets_redacted(settings):
    payload = mcp_mod._tool_info()
    assert payload["ok"] is True
    assert payload["version"] == mcp_mod.__version__
    # Secrets are booleans (set/unset), never the value.
    for key in ("semantic_scholar_api_key", "openalex_api_key", "ncbi_api_key"):
        assert isinstance(payload["settings"][key], bool)


def test_info_never_echoes_a_configured_key(monkeypatch):
    mcp_mod._set_settings(
        Settings(semantic_scholar_api_key="super-secret-value", openalex_api_key="also-secret")
    )
    text = repr(mcp_mod._tool_info())
    assert "super-secret-value" not in text
    assert "also-secret" not in text


# ---------------------------------------------------------------------- #
# _tool_search
# ---------------------------------------------------------------------- #


def test_search_returns_merged_records_and_diagnostics(fakes):
    KNOWLEDGE.clear()
    SEARCHES["attention"] = [_known("Attention Is All You Need", year=2017)]
    payload = mcp_mod._tool_search("attention", sources=["openalex"], max_results=5)
    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["papers"][0]["title"] == "Attention Is All You Need"
    assert payload["by_source"]["openalex"] == 1
    assert payload["all_sources_failed"] is False


def test_search_partial_failure_is_reported_not_fatal(fakes):
    FAILURES[("search", "boom")] = SourceError("openalex", "upstream is down")
    SEARCHES["boom"] = [_known("Survivor")]
    payload = mcp_mod._tool_search("boom", sources=["openalex", "arxiv"], max_results=5)
    # search() isolates failure, so the surviving source still yields a record.
    assert payload["ok"] is True
    assert payload["errors"], "the failing source must be reported"


def test_search_unknown_source_is_a_clean_error(fakes):
    payload = mcp_mod._tool_search("x", sources=["definitely-not-a-source"])
    assert payload["ok"] is False
    assert payload["error"] == "ConfigurationError"
    assert "known sources" in payload["message"]


def test_search_unknown_sort_is_a_clean_error(fakes):
    payload = mcp_mod._tool_search("x", sources=["openalex"], sort="nonsense")
    assert payload["ok"] is False
    assert payload["error"] == "ConfigurationError"


# ---------------------------------------------------------------------- #
# _tool_get
# ---------------------------------------------------------------------- #


def test_get_resolves_a_known_identifier(fakes):
    KNOWLEDGE["10.1/abc"] = _known("A Paper", doi="10.1/abc")
    payload = mcp_mod._tool_get("10.1/abc", source="openalex")
    assert payload["ok"] is True
    assert payload["paper"]["title"] == "A Paper"


def test_get_missing_identifier_is_a_clean_error(fakes):
    payload = mcp_mod._tool_get("10.1/nope", source="openalex")
    assert payload["ok"] is False
    assert payload["error"] in {"NotFoundError", "SourceError"}


# ---------------------------------------------------------------------- #
# _tool_citations (references / citations)
# ---------------------------------------------------------------------- #


def test_references_walks_the_graph(fakes):
    KNOWLEDGE["10.1/abc"] = _known("Subject", doi="10.1/abc")
    payload = mcp_mod._tool_citations("10.1/abc", "references", sources=["openalex"])
    assert payload["ok"] is True
    assert payload["direction"] == "references"
    assert payload["subject"]["doi"] == "10.1/abc"
    assert payload["count"] >= 1


def test_citations_walks_the_graph(fakes):
    KNOWLEDGE["10.1/abc"] = _known("Subject", doi="10.1/abc")
    payload = mcp_mod._tool_citations("10.1/abc", "citations", sources=["openalex"])
    assert payload["ok"] is True
    assert payload["direction"] == "citations"


def test_citation_bad_subject_is_a_clean_error(fakes):
    payload = mcp_mod._tool_citations("10.1/missing", "references")
    assert payload["ok"] is False


def test_citation_bad_order_is_a_clean_error(fakes):
    KNOWLEDGE["10.1/abc"] = _known("Subject", doi="10.1/abc")
    payload = mcp_mod._tool_citations("10.1/abc", "references", order="wat")
    assert payload["ok"] is False
    assert payload["error"] == "ConfigurationError"


# ---------------------------------------------------------------------- #
# _tool_bibliography
# ---------------------------------------------------------------------- #


def test_bibliography_from_identifiers_defaults_to_harvard(fakes):
    KNOWLEDGE["10.1/abc"] = _known("A Paper", doi="10.1/abc", year=2020)
    payload = mcp_mod._tool_bibliography(identifiers=["10.1/abc"])
    assert payload["ok"] is True
    # The default is harvard, not bibtex: it is the style the coursework
    # module mandates, so it must be what an unqualified call returns.
    assert payload["format"] == "harvard"
    assert payload["text"].startswith("(")
    assert "A Paper" in payload["text"]


def test_bibliography_renders_ris_and_csl(fakes):
    KNOWLEDGE["10.1/abc"] = _known("A Paper", doi="10.1/abc", year=2020)
    ris = mcp_mod._tool_bibliography(identifiers=["10.1/abc"], format="ris")
    assert ris["ok"] is True and "TY" in ris["text"]
    csl = mcp_mod._tool_bibliography(identifiers=["10.1/abc"], format="csl_json")
    assert csl["ok"] is True and "title" in csl["text"]


def test_bibliography_unknown_format_is_a_clean_error(fakes):
    payload = mcp_mod._tool_bibliography(identifiers=["10.1/abc"], format="docx")
    assert payload["ok"] is False
    assert "docx" in payload["message"]


def test_bibliography_needs_identifiers_or_query(fakes):
    payload = mcp_mod._tool_bibliography()
    assert payload["ok"] is False
    assert "identifiers" in payload["message"]


# ---------------------------------------------------------------------- #
# _tool_download
# ---------------------------------------------------------------------- #


def test_download_reports_missing_pdf_as_data(monkeypatch, fakes, tmp_path):
    KNOWLEDGE["10.1/abc"] = _known("No PDF", doi="10.1/abc")
    monkeypatch.setattr(mcp_mod, "download_pdf", _raise_no_pdf)
    payload = mcp_mod._tool_download("10.1/abc", str(tmp_path))
    assert payload["ok"] is False
    assert payload["error"] == "NoPdfError"


def _raise_no_pdf(*_args, **_kwargs):
    from researcher_python.errors import NoPdfError

    raise NoPdfError("no PDF URL")


# ---------------------------------------------------------------------- #
# The SDK-facing server
# ---------------------------------------------------------------------- #


def test_mcp_import_is_lazy_not_at_package_import():
    """Importing the module must not require the optional 'mcp' extra."""
    # The module was imported at the top of this file *without* mcp present
    # being fatal, which is the lazy-import contract; assert the import is
    # inside build_server rather than at module scope.
    import inspect

    source = inspect.getsource(mcp_mod.build_server)
    assert "from mcp.server.fastmcp import FastMCP" in source


def test_build_server_registers_every_declared_tool():
    _require_mcp()
    server = mcp_mod.build_server(Settings())

    async def names():
        return sorted(t.name for t in await server.list_tools())

    assert asyncio.run(names()) == sorted(mcp_mod.TOOLS)


def test_build_server_tool_help_is_populated():
    _require_mcp()
    server = mcp_mod.build_server(Settings())

    async def descriptions():
        return {t.name: (t.description or "") for t in await server.list_tools()}

    for name, text in asyncio.run(descriptions()).items():
        assert text.strip(), f"tool {name} has no description"


def test_call_tool_returns_structured_error_for_bad_args():
    _require_mcp()
    server = mcp_mod.build_server(Settings())

    async def call():
        return await server.call_tool("search_papers", {"query": "x", "sources": ["nope"]})

    result = asyncio.run(call())
    text = getattr(result[0], "text", "")
    assert '"ok": false' in text or "'ok': false" in text
    assert "ConfigurationError" in text


def test_call_tool_sources_succeeds_without_network():
    _require_mcp()
    server = mcp_mod.build_server(Settings())

    async def call():
        return await server.call_tool("sources", {})

    result = asyncio.run(call())
    text = getattr(result[0], "text", "")
    assert '"ok": true' in text or "'ok': true" in text
    assert "openalex" in text


# ---------------------------------------------------------------------- #
# Workspace-backed tools
# ---------------------------------------------------------------------- #


def test_open_workspace_makes_search_persist(fakes, tmp_path):
    SEARCHES["attention"] = [_known("Attention", doi="10.1/a")]

    opened = mcp_mod._tool_open_workspace(str(tmp_path / "ws"))
    assert opened["ok"] is True
    assert opened["workspace"]["exists"] is True

    first = mcp_mod._tool_search("attention", sources=["openalex"], max_results=5)
    assert first["persisted"] is True
    assert first["counts"]["new"] == 1
    assert first["counts"]["total"] == 1
    assert first["workspace"]["path"].endswith("ws")
    assert first["meta"]["params"]["query"] == "attention"
    assert first["meta"]["by_source"]["openalex"] == 1

    second = mcp_mod._tool_search("attention", sources=["openalex"], max_results=5)
    assert second["counts"]["new"] == 0
    assert second["counts"]["seen"] == 1
    assert second["counts"]["total"] == 1

    status = mcp_mod._tool_workspace_status()
    assert status["ok"] is True
    assert status["workspace"]["counts"]["records"] == 1
    assert status["workspace"]["log_rows"] == 2


def test_search_without_a_workspace_stays_pure(fakes, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    SEARCHES["attention"] = [_known("Attention", doi="10.1/a")]

    payload = mcp_mod._tool_search("attention", sources=["openalex"], max_results=5)
    assert payload["persisted"] is False
    assert "open_workspace" in payload["hint"]
    assert list(tmp_path.iterdir()) == []


def test_explicit_workspace_overrides_the_session_default(fakes, tmp_path):
    SEARCHES["attention"] = [_known("Attention", doi="10.1/a")]
    mcp_mod._tool_open_workspace(str(tmp_path / "default"))

    payload = mcp_mod._tool_search(
        "attention",
        sources=["openalex"],
        max_results=5,
        workspace=str(tmp_path / "override"),
    )
    assert payload["persisted"] is True
    assert payload["workspace"]["path"].endswith("override")

    assert mcp_mod._tool_workspace_status()["workspace"]["counts"]["records"] == 0
    override = mcp_mod._tool_workspace_status(str(tmp_path / "override"))
    assert override["workspace"]["counts"]["records"] == 1


def test_close_workspace_stops_persistence(fakes, tmp_path):
    SEARCHES["attention"] = [_known("Attention", doi="10.1/a")]
    mcp_mod._tool_open_workspace(str(tmp_path / "ws"))
    closed = mcp_mod._tool_close_workspace()
    assert closed["closed"].endswith("ws")

    payload = mcp_mod._tool_search("attention", sources=["openalex"], max_results=5)
    assert payload["persisted"] is False


def test_workspace_status_without_a_workspace_is_actionable():
    payload = mcp_mod._tool_workspace_status()
    assert payload["ok"] is False
    assert payload["error"] == "ConfigurationError"
    assert "open_workspace" in payload["message"]


def test_workspace_next_round_snapshots(fakes, tmp_path):
    SEARCHES["attention"] = [_known("Attention", doi="10.1/a")]
    workspace = str(tmp_path / "ws")
    mcp_mod._tool_open_workspace(workspace)
    mcp_mod._tool_search("attention", sources=["openalex"], max_results=5)

    advanced = mcp_mod._tool_workspace_next_round("deeper")
    assert advanced["ok"] is True
    assert advanced["round"] == 2
    assert advanced["workspace"]["round"] == 2
    assert (tmp_path / "ws" / "rounds" / "round-1" / "results.json").is_file()


def test_matrix_and_bibliography_read_a_workspace(fakes, tmp_path):
    SEARCHES["attention"] = [_known("Attention", doi="10.1/a", year=2017)]
    workspace = str(tmp_path / "ws")
    mcp_mod._tool_open_workspace(workspace)
    mcp_mod._tool_search("attention", sources=["openalex"], max_results=5)

    table = mcp_mod._tool_matrix(workspace=workspace)
    assert table["ok"] is True
    assert table["count"] == 1
    assert table["text"].splitlines()[0].startswith("key,label")

    refs = mcp_mod._tool_bibliography(workspace=workspace)
    assert refs["ok"] is True
    assert refs["count"] == 1
    assert "10.1/a" in refs["text"]


def test_label_workspace_writes_through_the_store(fakes, tmp_path):
    SEARCHES["attention"] = [_known("Attention", doi="10.1/a")]
    workspace = str(tmp_path / "ws")
    mcp_mod._tool_open_workspace(workspace)
    mcp_mod._tool_search("attention", sources=["openalex"], max_results=5)

    labelled = mcp_mod._tool_label(workspace=workspace, set_label="core")
    assert labelled["ok"] is True
    assert labelled["labelled"] == 1
    assert labelled["summary"]["core"] == 1

    status = mcp_mod._tool_workspace_status(workspace)
    assert status["workspace"]["counts"]["core"] == 1


def test_workspace_and_from_file_is_ambiguous(tmp_path):
    payload = mcp_mod._tool_matrix(
        from_file=str(tmp_path / "x.json"), workspace=str(tmp_path / "ws")
    )
    assert payload["ok"] is False
    assert payload["error"] == "ConfigurationError"
    assert "do not also pass" in payload["message"]
