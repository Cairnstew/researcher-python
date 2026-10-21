"""Expose researcher-python as Model Context Protocol (MCP) tools.

The scholarly pipeline is already a clean library and a clean CLI; this module
is a **third, thin face** on the same public API so an MCP client (opencode,
Claude Desktop, ...) can search, read, cite and download papers without shelling
out to the CLI.

Design rules (house style — see :mod:`researcher_python.cli`):

* **Thin.** Every tool is a direct call into the existing pipeline
  (:func:`researcher_python.pipeline.search`, :func:`researcher_python.get`,
  :func:`researcher_python.citation.references`, ...).  No re-implemented
  logic, no new data model — a tool is just the library with an MCP signature.
* **Lazy import.** The ``mcp`` SDK is an *optional* extra (``pip install
  'researcher-python[mcp]'``), so ``import researcher_python`` stays light and
  the offline suite runs without it.  The SDK is imported when
  :func:`build_server` runs; a missing package raises a message naming the
  extra, never a bare ``ImportError``.
* **Honest settings.** Tools read :func:`researcher_python.load_settings` once
  at server build time, so ``.env`` / ``RESEARCHER_*`` are honoured (the
  documented library-default trap: a bare :class:`Settings` silently ignores
  them).
* **Errors are data.** Every :class:`~researcher_python.errors.ResearcherError`
  is caught and returned as ``{"error": ...}`` rather than crashing the server;
  :class:`ConfigurationError` keeps its message so the model can name the env
  var it must set.  Unexpected exceptions still raise (a bug should be loud).

Transport is **stdio** (``researcher-python-mcp``), the standard MCP default.
Run ``researcher-python-mcp`` to serve; the client spawns it as a subprocess.
"""

from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Literal

from . import __version__
from . import citation, extract, format as bib, handoff as handoff_mod, matrix, pipeline, relevance, web_sources
from .config import Settings, load_settings
from .download import download_pdf
from .errors import ConfigurationError, ResearcherError
from .models import SORT_MODES, TRIAGE_LABELS, Paper, sort_papers
from .prisma import render_prisma_markdown
from .screening import LabelUpdate, PrescreenCriteria, apply_updates
from .sources.base import SEARCH_FIELDS, SearchFilters
from .sources import DEFAULT_SOURCES, source_names
from .web_sources import web_source_names
from .workspace import Workspace, WorkspaceSearch

__all__ = ["LITE_TOOLS", "TOOLS", "build_server", "main", "server"]

_SERVER_NAME = "researcher-python"
_SERVER_INSTRUCTIONS = (
    "Unified scholarly search across arXiv, OpenAlex, Semantic Scholar, "
    "Crossref, PubMed, ACM and OpenAIRE. Tools: search_papers, get_paper, "
    "references/citations, download_pdf, bibliography, web_search, report, "
    "screen, prescreen, snowball, sources, info, and the workspace tools.\n"
    "\n"
    "PAGING: search_papers returns a *summary page* by default (limit=10). Read "
    "summary, kept, has_more and next_offset; pass offset for the next page. "
    "detail='compact' adds a 240-char abstract excerpt; detail='full' or "
    "get_paper returns the complete record. fields=[...] projects exact keys. "
    "max_results caps each source; limit pages the merged set.\n"
    "\n"
    "SEARCHING: prefer sort='score' plus top_ratio over raising max_results: "
    "each source ranks by a private, mutually incomparable score. min_score is "
    "absolute and removes nothing on a long query; top_ratio is scale-free and "
    "cuts the tail. The reply reports dropped, terms and each record's "
    "relevance.\n"
    "\n"
    "WORKSPACES: open_workspace(path) makes search_papers persist (merge, "
    "append-only log, one search_id). A page after the first reuses that "
    "result and adds no log row; if it is gone, the page is served read-only "
    "from the persisted set (page_source says which). workspace_status shows "
    "counts/round/paths; workspace_next_round snapshots.\n"
    "\n"
    "SCREENING IS PART OF THE DELIVERABLE, NOT AN EXTRA. prescreen flags "
    "obvious exclusions by rule (report-only unless apply=True); screen sets "
    "core|general|excluded with an optional reason/note, batch via "
    "updates=[{id|ids,label,reason,note},...]; snowball expands the core set "
    "through the citation graph. report(kind=...) renders matrix (columns, "
    "csv|markdown), synthesis (dimension), screening (counts) or prisma "
    "(workspace only); screen_summary is an alias for screening. Check "
    "screening.ready or pass require_screening=True before treating a matrix "
    "as screened. Prefer from_file/workspace over a live query for reports.\n"
    "\n"
    "HANDOFF: handoff(source, out_dir) writes a versioned bundle (included "
    "papers, references.bib, citation-map.json, matrix/synthesis/PRISMA) for "
    "paper-handler research import. It refuses an unscreened set by default.\n"
    "\n"
    "PROFILE: RESEARCHER_MCP_PROFILE=lite registers only search/read/cite and "
    "the workspace (no screening, download, extraction or handoff)."
)

#: Bibliography formats accepted by :func:`_bibliography`.
#: Reference-list styles.  ``harvard`` is first and the default: it is the
#: style this package's own coursework module mandates.
_BIB_FORMATS = ("harvard", "bibtex", "ris", "csl_json")


# ---------------------------------------------------------------------- #
# Settings (shared, loaded once)
# ---------------------------------------------------------------------- #

#: Resolved at server build time; every tool closes over this.
_SETTINGS: Settings | None = None


def _settings() -> Settings:
    """The server's :class:`Settings`, loaded lazily from the environment."""
    global _SETTINGS
    if _SETTINGS is None:
        # load_settings() honours .env + RESEARCHER_* (never a bare Settings()).
        _SETTINGS = load_settings()
    return _SETTINGS


def _set_settings(settings: Settings | None) -> None:
    """Override the server settings (tests inject a fixture Settings)."""
    global _SETTINGS
    _SETTINGS = settings


#: Tools dropped by ``RESEARCHER_MCP_PROFILE=lite``.  The lite profile is for
#: the writing phase: search, read, cite and the workspace, without the
#: screening/download/extraction surface a paper agent does not call.
_LITE_EXCLUDED = frozenset(
    {
        "workspace_next_round",
        "download_pdf",
        "report",
        "screen",
        "prescreen",
        "snowball",
        "handoff",
        "extract_text",
        "find_quotes",
    }
)


def _resolve_profile(profile: str | None = None) -> str:
    """``full`` (default) or ``lite``; an unknown value is a config error."""
    value = (profile or os.environ.get("RESEARCHER_MCP_PROFILE", "full")).strip().lower()
    if value not in ("full", "lite"):
        raise ConfigurationError(
            f"RESEARCHER_MCP_PROFILE must be 'full' or 'lite', not {value!r}"
        )
    return value


#: Session-default workspace (one stdio server per client, so process-global is
#: the right scope).  ``open_workspace`` sets it; ``close_workspace`` clears it.
_WORKSPACE: Workspace | None = None
_WORKSPACE_LOCK = threading.Lock()


def _set_workspace(workspace: Workspace | None) -> None:
    global _WORKSPACE
    with _WORKSPACE_LOCK:
        _WORKSPACE = workspace


def _resolve_workspace(path: str | None = None) -> Workspace | None:
    """Resolve an explicit path, then the session default, then the env default."""
    if path:
        return Workspace(path, settings=_settings())
    if _WORKSPACE is not None:
        return _WORKSPACE
    settings = _settings()
    if settings.workspace is not None:
        return Workspace.default(settings)
    return None


# ---------------------------------------------------------------------- #
# Result shaping
# ---------------------------------------------------------------------- #


def _ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def _fail(exc: ResearcherError) -> dict[str, Any]:
    """Turn a pipeline error into structured, actionable data."""
    return {
        "ok": False,
        "error": type(exc).__name__,
        "message": " ".join(str(exc).split()),
    }


#: Characters of abstract kept by ``detail="compact"``.
_COMPACT_ABSTRACT = 240
#: Detail presets for a record in a reply.  ``summary`` is the default: a
#: 30-record search with full records measured ~107 KB, which is more than the
#: entire tool catalogue; the summary page measured ~4 KB.
_DETAIL_LEVELS = ("summary", "compact", "full")
_DEFAULT_DETAIL = "summary"
#: Default page size for the merged result (``max_results`` is the per-source
#: retrieval cap — a different knob).
_DEFAULT_PAGE = 10

#: Fields ``fields=[...]`` accepts: every top-level ``to_dict()`` key plus the
#: three derived values a triage list uses.  Derived explicitly from the model
#: so a new ``Paper`` field becomes projectable without a second list to edit.
_PAPER_FIELDS = tuple(Paper().to_dict()) + (
    "key",
    "author_count",
    "abstract_excerpt",
)


def _excerpt(paper: Paper) -> str:
    abstract = " ".join((paper.abstract or "").split())
    return (
        abstract
        if len(abstract) <= _COMPACT_ABSTRACT
        else abstract[: _COMPACT_ABSTRACT - 1].rstrip() + "…"
    )


def _summary_paper(paper: Paper) -> dict[str, Any]:
    """The default triage projection: no abstract, no long tail.

    Identity, standing, relevance and the reader's own label — exactly what a
    scan needs.  ``get_paper`` still returns the complete record for one paper.
    """
    return {
        "key": paper.primary_id() or " ".join((paper.title or "").split())[:80],
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
        "pmid": paper.pmid,
        "title": paper.title,
        "year": paper.year,
        "venue": paper.venue,
        "work_type": paper.work_type,
        "authors": [a.name for a in paper.authors[:3]],
        "author_count": len(paper.authors),
        "citation_count": paper.citation_count,
        "is_open_access": paper.is_open_access,
        "relevance": paper.relevance,
        "label": paper.triage_label(),
    }


def _compact_paper(paper: Paper) -> dict[str, Any]:
    """Summary plus a readable abstract excerpt (older opt-in shape)."""
    return {**_summary_paper(paper), "abstract_excerpt": _excerpt(paper)}


def _project_paper(paper: Paper, fields: list[str]) -> dict[str, Any]:
    """Exact top-level projection; ``fields`` wins over ``detail``."""
    data = paper.to_dict()
    projected: dict[str, Any] = {}
    for name in fields:
        if name == "key":
            projected[name] = _summary_paper(paper)["key"]
        elif name == "author_count":
            projected[name] = len(paper.authors)
        elif name == "abstract_excerpt":
            projected[name] = _excerpt(paper)
        else:
            projected[name] = data.get(name)
    return projected


def _shape_paper(
    paper: Paper, detail: str = _DEFAULT_DETAIL, fields: list[str] | None = None
) -> dict[str, Any]:
    if fields:
        return _project_paper(paper, fields)
    if detail == "full":
        return paper.to_dict()
    if detail == "compact":
        return _compact_paper(paper)
    return _summary_paper(paper)


def _search_summary(kept: int, shown: int, offset: int, failed: int) -> str:
    """The human sentence an agent can quote: "143 kept, 3 sources errored, top 10 shown"."""
    parts = [f"{kept} kept"]
    if failed:
        parts.append(f"{failed} source{'s' if failed != 1 else ''} errored")
    if not shown:
        parts.append("no records shown")
    elif offset:
        parts.append(f"rows {offset + 1}-{offset + shown} shown")
    else:
        parts.append(f"top {shown} shown")
    return ", ".join(parts)


def _search_payload(
    result: pipeline.PipelineResult,
    query: str,
    *,
    detail: str = _DEFAULT_DETAIL,
    fields: list[str] | None = None,
    offset: int = 0,
    limit: int | None = _DEFAULT_PAGE,
) -> dict[str, Any]:
    """Shape one page of a merged result.

    Paging is **presentation-only**: callers must slice here, after the
    workspace has merged the complete result, or a paged search would persist
    just its first page.
    """
    kept = len(result.papers)
    page = (
        result.papers[offset:]
        if limit is None
        else result.papers[offset : offset + limit]
    )
    papers = [_shape_paper(p, detail, fields) for p in page]
    attempted = list(result.by_source)
    failed = [name for name in attempted if name in result.errors]
    has_more = offset + len(papers) < kept
    return _ok(
        query=query,
        summary=_search_summary(kept, len(papers), offset, len(failed)),
        kept=kept,
        count=len(papers),
        offset=offset,
        limit=limit,
        has_more=has_more,
        next_offset=(offset + len(papers)) if has_more else None,
        papers=papers,
        by_source=dict(result.by_source),
        errors={name: _fail(err)["message"] for name, err in result.errors.items()},
        # 0 failed sources when nothing was attempted is *not* success, so the
        # caller can tell "found nothing" from "everything broke".
        partial=bool(failed) and len(failed) < len(attempted),
        all_sources_failed=bool(attempted) and len(failed) == len(attempted),
        # Every record carries a local `relevance`; these say what it was
        # measured against and what a min_score floor removed, so a thin result
        # set is auditable rather than mysteriously small.
        terms=list(result.terms),
        dropped=result.dropped,
    )


def _validate_view(
    detail: str, fields: list[str] | None, limit: int | None, offset: int
) -> tuple[str, list[str] | None, int | None, int]:
    """Validate the presentation knobs before any network call."""
    if detail not in _DETAIL_LEVELS:
        raise ConfigurationError(
            f"unknown detail {detail!r}; expected one of {', '.join(_DETAIL_LEVELS)}"
        )
    if fields is not None:
        if not fields:
            raise ConfigurationError(
                "fields is empty; omit it for a detail preset or name at least one field"
            )
        unknown = [name for name in fields if name not in _PAPER_FIELDS]
        if unknown:
            raise ConfigurationError(
                f"unknown field(s) {', '.join(unknown)}; available: "
                f"{', '.join(_PAPER_FIELDS)}"
            )
        # Deduplicate while keeping the caller's order.
        fields = list(dict.fromkeys(fields))
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
    ):
        raise ConfigurationError(
            f"limit must be a positive integer or null, got {limit!r}"
        )
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ConfigurationError(
            f"offset must be a non-negative integer, got {offset!r}"
        )
    return detail, fields, limit, offset


def _citation_payload(result: citation.CitationResult) -> dict[str, Any]:
    return _ok(
        direction=result.direction,
        subject=dict(result.subject),
        count=len(result.papers),
        papers=[p.to_dict() for p in result.papers],
        by_source=dict(result.by_source),
        edges=dict(result.edges),
        truncated=dict(result.truncated),
        errors={name: _fail(err)["message"] for name, err in result.errors.items()},
        order=result.order,
        requested=result.requested,
    )


def _web_payload(result: web_sources.WebSearchResult) -> dict[str, Any]:
    return _ok(
        source=result.source,
        count=len(result.results),
        results=[r.to_dict() for r in result.results],
        truncated=result.truncated,
        quota=dict(result.quota),
    )


# ---------------------------------------------------------------------- #
# Tool implementations (framework-free, so they are unit-testable)
# ---------------------------------------------------------------------- #


def _tool_sources() -> dict[str, Any]:
    """Registry view: every scholarly and web source, its capabilities."""
    from .sources import get_source_class

    scholarly = []
    for name in source_names():
        try:
            caps = get_source_class(name).capabilities
        except ResearcherError:
            scholarly.append({"name": name, "capabilities": {}, "available": False})
            continue
        scholarly.append(
            {
                "name": name,
                "default": name in DEFAULT_SOURCES,
                "capabilities": {
                    field: bool(getattr(caps, field, False))
                    for field in ("search", "get", "references", "citations", "pdf")
                },
                "available": True,
            }
        )
    return _ok(
        scholarly=scholarly,
        web=web_source_names(),
        defaults=list(DEFAULT_SOURCES),
    )


def _tool_open_workspace(
    path: str, create: bool = True, round: int | None = None
) -> dict[str, Any]:
    """Open a workspace and make it this session's persistence target."""
    try:
        workspace = Workspace(
            path, settings=_settings(), create=create, round=round
        )
        _set_workspace(workspace)
        # A reopened path must not serve a page cached from the previous session.
        _clear_page_cache()
        return _ok(workspace=workspace.status())
    except ResearcherError as exc:
        return _fail(exc)


def _tool_workspace_status(workspace: str | None = None) -> dict[str, Any]:
    try:
        target = _resolve_workspace(workspace)
        if target is None:
            return {
                "ok": False,
                "error": "ConfigurationError",
                "message": (
                    "no workspace open; call open_workspace(path) or pass "
                    "workspace='...'"
                ),
            }
        return _ok(workspace=target.status())
    except ResearcherError as exc:
        return _fail(exc)


def _tool_close_workspace() -> dict[str, Any]:
    previous = _WORKSPACE
    _set_workspace(None)
    _clear_page_cache()
    return _ok(closed=str(previous.path) if previous is not None else None)


def _tool_workspace_next_round(
    label: str = "", workspace: str | None = None
) -> dict[str, Any]:
    try:
        target = _resolve_workspace(workspace)
        if target is None:
            return {
                "ok": False,
                "error": "ConfigurationError",
                "message": (
                    "no workspace open; call open_workspace(path) or pass "
                    "workspace='...'"
                ),
            }
        number = target.start_round(label)
        if workspace is None:
            _set_workspace(target)
        return _ok(round=number, workspace=target.status())
    except ResearcherError as exc:
        return _fail(exc)


def _tool_info() -> dict[str, Any]:
    settings = _settings()
    return _ok(
        version=__version__,
        settings={
            "email": settings.email or None,
            "output_dir": str(settings.output_dir),
            "home": str(settings.home) if settings.home is not None else None,
            "workspace": (
                str(settings.workspace)
                if settings.workspace is not None
                else None
            ),
            "env_file": (
                str(settings.env_file) if settings.env_file is not None else None
            ),
            "log_level": settings.log_level,
            "max_results": settings.max_results,
            "timeout": settings.timeout,
            "retries": settings.retries,
            # Secrets are reported set/unset only, never by value.
            "semantic_scholar_api_key": bool(settings.semantic_scholar_api_key),
            "openalex_api_key": bool(settings.openalex_api_key),
            "ncbi_api_key": bool(settings.ncbi_api_key),
            "reddit_client_id": bool(settings.reddit_client_id),
        },
        web_settings={
            "stackexchange_key": bool(settings.stackexchange_key),
            "reddit_configured": bool(
                settings.reddit_client_id and settings.reddit_client_secret
            ),
        },
    )


@dataclass(slots=True)
class _CachedSearch:
    """The full result of the last search, kept only to serve later pages."""

    result: pipeline.PipelineResult
    outcome: WorkspaceSearch | None = None


#: The last few full results, keyed by (workspace path or "__pure__", params).
#: Paging must never append a second log row or mint a second search id, so a
#: page call reuses the result the first call already produced.
_PAGE_CACHE_MAX = 8
_PAGE_CACHE: OrderedDict[tuple[str, tuple[Any, ...]], _CachedSearch] = OrderedDict()
_PAGE_CACHE_LOCK = threading.Lock()


def _cache_namespace(path: Any) -> str:
    return "__pure__" if path is None else str(path)


def _page_cache_get(
    namespace: str, signature: tuple[Any, ...]
) -> _CachedSearch | None:
    with _PAGE_CACHE_LOCK:
        key = (namespace, signature)
        entry = _PAGE_CACHE.get(key)
        if entry is not None:
            _PAGE_CACHE.move_to_end(key)
        return entry


def _page_cache_put(
    namespace: str, signature: tuple[Any, ...], entry: _CachedSearch
) -> None:
    with _PAGE_CACHE_LOCK:
        key = (namespace, signature)
        _PAGE_CACHE[key] = entry
        _PAGE_CACHE.move_to_end(key)
        while len(_PAGE_CACHE) > _PAGE_CACHE_MAX:
            _PAGE_CACHE.popitem(last=False)


def _clear_page_cache() -> None:
    with _PAGE_CACHE_LOCK:
        _PAGE_CACHE.clear()


def _search_signature(
    query: str,
    *,
    sources: list[str] | str | None,
    max_results: int,
    sort: str,
    filters: SearchFilters,
    min_score: float | None,
    top_ratio: float | None,
    min_citations: int | None,
    enrich: bool | None,
    round_number: int | None,
) -> tuple[Any, ...]:
    """Every input that changes the result — a page must match its first call."""
    if isinstance(sources, str):
        source_key: tuple[str, ...] | None = (sources,)
    else:
        source_key = tuple(sources) if sources else None
    return (
        query,
        source_key,
        max_results,
        sort,
        filters.year_from,
        filters.year_to,
        filters.field,
        tuple(filters.types or ()),
        tuple(filters.exclude_types or ()),
        min_score,
        top_ratio,
        min_citations,
        enrich,
        round_number,
    )


def _persisted_page_result(
    target: Workspace, query: str, sort: str
) -> pipeline.PipelineResult:
    """A page of the canonical set, for when the original result is gone.

    Used only on a page request the in-process cache cannot serve (a restart or
    a changed search).  It never writes: the workspace already owns the set and
    its log row, so paging stays read-only.
    """
    terms = relevance.query_terms(query)
    papers = sort_papers(list(target.papers), sort, query=query or None)
    return pipeline.PipelineResult(papers=papers, terms=terms)


def _tool_search(
    query: str,
    sources: list[str] | None = None,
    max_results: int = 10,
    sort: str = "relevance",
    year_from: int | None = None,
    year_to: int | None = None,
    field: str = "any",
    min_score: float | None = None,
    top_ratio: float | None = None,
    min_citations: int | None = None,
    workspace: str | None = None,
    types: list[str] | None = None,
    exclude_types: list[str] | None = None,
    default_excludes: bool = True,
    enrich: bool | None = None,
    detail: str = _DEFAULT_DETAIL,
    fields: list[str] | None = None,
    limit: int | None = _DEFAULT_PAGE,
    offset: int = 0,
) -> dict[str, Any]:
    """Search, summarize and page; persist once when a workspace is open.

    ``max_results`` is the per-source retrieval cap.  ``limit``/``offset`` page
    the *merged* set (default 10 rows).  A page after the first is served from
    the in-process result of the first call, so it appends no search-log row and
    mints no second ``search_id``; if that result is gone, a workspace page is
    served read-only from the persisted set (``page_source="workspace_set"``).
    """
    try:
        detail, fields, limit, offset = _validate_view(
            detail, fields, limit, offset
        )
    except ResearcherError as exc:
        return _fail(exc)
    if sort not in SORT_MODES:
        return _fail(
            ConfigurationError(
                f"unknown sort {sort!r}; expected one of {', '.join(SORT_MODES)}"
            )
        )
    try:
        from .work_types import DEFAULT_SEARCH_EXCLUDE_TYPES, resolve_types

        requested_types = resolve_types(types or ())
        if exclude_types is not None:
            effective_excludes = resolve_types(exclude_types)
        elif requested_types or not default_excludes:
            effective_excludes = ()
        else:
            effective_excludes = DEFAULT_SEARCH_EXCLUDE_TYPES
        filters = SearchFilters(
            year_from=year_from,
            year_to=year_to,
            field=field,
            types=requested_types,
            exclude_types=effective_excludes,
        )
    except ValueError as exc:
        return {
            "ok": False,
            "error": "ConfigurationError",
            "message": str(exc),
        }

    outcome: WorkspaceSearch | None = None
    target: Workspace | None = None
    page_source = "search"
    try:
        target = _resolve_workspace(workspace)
        namespace = _cache_namespace(target.path if target is not None else None)
        round_number = target.round if target is not None else None
        signature = _search_signature(
            query,
            sources=sources,
            max_results=max_results,
            sort=sort,
            filters=filters,
            min_score=min_score,
            top_ratio=top_ratio,
            min_citations=min_citations,
            enrich=enrich,
            round_number=round_number,
        )
        cached = _page_cache_get(namespace, signature) if offset > 0 else None
        if cached is not None:
            outcome = cached.outcome
            result = cached.result
            page_source = "cached_search"
        elif offset > 0 and target is not None:
            # The original result is gone (restart / changed search).  Serve the
            # page read-only from the canonical set rather than searching twice.
            result = _persisted_page_result(target, query, sort)
            page_source = "workspace_set"
        elif target is not None:
            outcome = target.search(
                query,
                sources=sources,
                max_results=max_results,
                sort=sort,
                filters=filters,
                min_score=min_score,
                top_ratio=top_ratio,
                min_citations=min_citations,
                enrich=enrich,
                channel="mcp",
                settings=_settings(),
            )
            result = outcome.result
            _page_cache_put(
                namespace, signature, _CachedSearch(result, outcome)
            )
        else:
            result = pipeline.search(
                query,
                sources=sources,
                max_results=max_results,
                sort=sort,
                settings=_settings(),
                filters=filters,
                min_score=min_score,
                top_ratio=top_ratio,
                min_citations=min_citations,
                enrich=enrich,
            )
            _page_cache_put(namespace, signature, _CachedSearch(result))
    except ResearcherError as exc:
        return _fail(exc)

    payload = _search_payload(
        result, query, detail=detail, fields=fields, offset=offset, limit=limit
    )
    payload["page_source"] = page_source
    payload["filters"] = {
        "year_from": year_from,
        "year_to": year_to,
        "field": field,
        "types": list(filters.types),
        "exclude_types": list(filters.exclude_types or ()),
    }
    payload["min_score"] = min_score
    payload["top_ratio"] = top_ratio
    payload["min_citations"] = min_citations
    payload["enrich"] = enrich
    payload["versions_linked"] = result.versions_linked
    payload["dropped_types"] = result.dropped_types
    payload["dropped_type_counts"] = dict(result.dropped_type_counts)
    payload["enrichment"] = (
        result.enrichment.to_dict() if result.enrichment is not None else None
    )
    if result.hint:
        payload["hint"] = result.hint

    if outcome is not None:
        payload["persisted"] = True
        payload["workspace"] = {
            "path": outcome.paths["workspace"],
            "round": outcome.round,
            "search_id": outcome.search_id,
        }
        payload["counts"] = {
            "returned": len(result.papers),
            "new": outcome.new,
            "seen": outcome.seen,
            "screened": outcome.screened,
            "total": outcome.total,
            "dropped": result.dropped,
        }
        payload["meta"] = {
            "round": outcome.round,
            "search_id": outcome.search_id,
            "params": outcome.params,
        }
    elif target is not None:
        last = target.meta.get("last_search") or {}
        payload["persisted"] = True
        payload["workspace"] = {
            "path": str(target.path),
            "round": last.get("round", target.round),
            "search_id": last.get("id"),
        }
        payload["counts"] = {
            "returned": len(result.papers),
            "total": len(result.papers),
        }
        payload["meta"] = {
            "round": last.get("round", target.round),
            "search_id": last.get("id"),
        }
        payload["hint"] = (
            "page served from the persisted workspace set; no new search or log row"
        )
    else:
        payload["persisted"] = False
        payload["hint"] = payload.get("hint") or (
            "not persisted: call open_workspace(path) or pass workspace=..."
        )
    return payload


# ---------------------------------------------------------------------- #
# Screening, matrices and PDF extraction
# ---------------------------------------------------------------------- #


def _tool_matrix(
    identifiers: list[str] | None = None,
    query: str | None = None,
    from_file: str | None = None,
    sources: list[str] | None = None,
    max_results: int = 25,
    columns: list[str] | None = None,
    format: str = "csv",
    require_screening: bool = False,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Render the Literature Matrix (one row per paper) as CSV or Markdown."""
    try:
        papers = _papers_for(
            identifiers, query, from_file, sources, max_results, workspace
        )
    except ResearcherError as exc:
        return _fail(exc)
    if format not in _MATRIX_FORMATS:
        return _fail_bad_matrix_format(format)
    names = tuple(columns) if columns else None
    try:
        render = (
            matrix.render_matrix_csv
            if format == "csv"
            else matrix.render_matrix_markdown
        )
        text = render(papers, names, require_screening=require_screening)
    except ResearcherError as exc:
        return _fail(exc)
    return _ok(
        format=format,
        count=len(papers),
        text=text,
        # Always reported: with the gate off, this is how a caller learns the
        # matrix is not actually a screened one.
        screening=matrix.screening_report(papers),
    )


def _tool_synthesis(
    identifiers: list[str] | None = None,
    query: str | None = None,
    from_file: str | None = None,
    sources: list[str] | None = None,
    max_results: int = 25,
    dimension: str = "year",
    format: str = "csv",
    require_screening: bool = False,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Render the Synthesis Matrix: ``dimension`` cross-tabbed against screening."""
    try:
        papers = _papers_for(
            identifiers, query, from_file, sources, max_results, workspace
        )
    except ResearcherError as exc:
        return _fail(exc)
    if format not in _MATRIX_FORMATS:
        return _fail_bad_matrix_format(format)
    try:
        if format == "markdown":
            text = matrix.render_synthesis_markdown(
                papers, dimension, require_screening=require_screening
            )
        else:
            text = matrix.render_synthesis_csv(
                papers, dimension, require_screening=require_screening
            )
    except ResearcherError as exc:
        return _fail(exc)
    return _ok(
        format=format,
        dimension=dimension,
        count=len(papers),
        text=text,
        summary=matrix.screen_summary(papers),
        screening=matrix.screening_report(papers),
    )


def _tool_screen_summary(
    identifiers: list[str] | None = None,
    query: str | None = None,
    from_file: str | None = None,
    sources: list[str] | None = None,
    max_results: int = 25,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Screening counts per triage label, for a methods/appendix section."""
    try:
        papers = _papers_for(
            identifiers, query, from_file, sources, max_results, workspace
        )
    except ResearcherError as exc:
        return _fail(exc)
    return _ok(
        count=len(papers),
        labels=list(TRIAGE_LABELS),
        summary=matrix.screen_summary(papers),
        # `ready`/`message` answer "can this be submitted as a screened set?".
        screening=matrix.screening_report(papers),
    )


#: Matrix render formats (matrices are tables, not reference lists).
_MATRIX_FORMATS = ("csv", "markdown")

#: Kinds of artefact ``report`` renders.  ``screen_summary`` is the old tool
#: name, kept as an accepted alias so a memorised call still works.
REPORT_KINDS = ("matrix", "synthesis", "screening", "prisma")
_REPORT_ALIASES = {"screen_summary": "screening"}
_REPORT_FORMATS: dict[str, tuple[str, ...]] = {
    "matrix": _MATRIX_FORMATS,
    "synthesis": _MATRIX_FORMATS,
    "screening": (),
    "prisma": ("json", "markdown"),
}
_REPORT_DEFAULT_FORMAT = {"matrix": "csv", "synthesis": "csv", "prisma": "json"}


def _tool_report(
    kind: str,
    identifiers: list[str] | None = None,
    query: str | None = None,
    from_file: str | None = None,
    sources: list[str] | None = None,
    max_results: int = 25,
    columns: list[str] | None = None,
    dimension: str = "year",
    format: str | None = None,
    require_screening: bool = False,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Dispatch one of the report artefacts (matrix, synthesis, counts, PRISMA).

    One tool instead of four: the input model is shared, and a single enum is
    cheaper in the tool schema than four near-identical signatures.  Each kind
    delegates to the function that already implements it, so this is a router,
    not a second implementation.
    """
    resolved = _REPORT_ALIASES.get(kind, kind)
    if resolved not in REPORT_KINDS:
        return _fail(
            ConfigurationError(
                f"unknown report kind {kind!r}; expected one of "
                f"{', '.join(REPORT_KINDS)}"
            )
        )

    if resolved == "prisma":
        if identifiers or query or from_file:
            return _fail(
                ConfigurationError(
                    "kind='prisma' reads the workspace; do not pass "
                    "identifiers/query/from_file"
                )
            )
        if columns is not None:
            return _fail(
                ConfigurationError("columns applies only to kind='matrix'")
            )
        if dimension != "year":
            return _fail(
                ConfigurationError(
                    "dimension applies only to kind='synthesis'"
                )
            )
        if require_screening:
            return _fail(
                ConfigurationError(
                    "require_screening applies only to matrix/synthesis"
                )
            )
        chosen = _REPORT_DEFAULT_FORMAT["prisma"] if format is None else format
        if chosen not in _REPORT_FORMATS["prisma"]:
            return _fail(
                ConfigurationError(
                    f"unknown prisma format {chosen!r}; expected one of "
                    f"{', '.join(_REPORT_FORMATS['prisma'])}"
                )
            )
        payload = _tool_prisma(workspace=workspace, format=chosen)
        return {**payload, "kind": resolved}

    if columns is not None and resolved != "matrix":
        return _fail(ConfigurationError("columns applies only to kind='matrix'"))
    if dimension != "year" and resolved != "synthesis":
        return _fail(
            ConfigurationError("dimension applies only to kind='synthesis'")
        )

    if resolved == "screening":
        if format is not None:
            return _fail(
                ConfigurationError("kind='screening' takes no format")
            )
        # The screening report is counts + readiness; there is no render gate.
        if require_screening:
            return _fail(
                ConfigurationError(
                    "require_screening applies only to matrix/synthesis"
                )
            )
        payload = _tool_screen_summary(
            identifiers, query, from_file, sources, max_results, workspace
        )
        return {**payload, "kind": resolved}

    chosen = _REPORT_DEFAULT_FORMAT[resolved] if format is None else format
    if chosen not in _REPORT_FORMATS[resolved]:
        return _fail(
            ConfigurationError(
                f"unknown {resolved} format {chosen!r}; expected one of "
                f"{', '.join(_REPORT_FORMATS[resolved])}"
            )
        )
    if resolved == "matrix":
        payload = _tool_matrix(
            identifiers,
            query,
            from_file,
            sources,
            max_results,
            columns,
            chosen,
            require_screening,
            workspace,
        )
    else:
        payload = _tool_synthesis(
            identifiers,
            query,
            from_file,
            sources,
            max_results,
            dimension,
            chosen,
            require_screening,
            workspace,
        )
    return {**payload, "kind": resolved}


def _tool_label(
    from_file: str | None = None,
    set_label: str | None = None,
    ids: list[str] | None = None,
    reason: str = "",
    note: str = "",
    out_file: str | None = None,
    workspace: str | None = None,
    updates: list[dict[str, Any]] | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Screen records: one label/note, or a batch of per-record updates.

    This tool is also exposed as ``screen`` (the verb); ``label`` names the
    stored field and remains for compatibility.  With ``updates`` the whole
    batch is applied in one locked write — the difference between screening a
    thousand records and making a thousand calls.

    Input resolution follows the other tools: an explicit ``from_file`` or
    ``workspace`` wins, and neither means "use the session workspace".
    """
    if bool(from_file) and bool(workspace):
        return {
            "ok": False,
            "error": "ConfigurationError",
            "message": "give exactly one of from_file or workspace",
        }
    if updates is not None:
        if set_label or ids or note or reason:
            return {
                "ok": False,
                "error": "ConfigurationError",
                "message": "updates cannot be combined with set_label/ids/note/reason",
            }
        raw_updates: list[dict[str, Any] | LabelUpdate] = list(updates)
        if not raw_updates:
            return {
                "ok": False,
                "error": "ConfigurationError",
                "message": "updates is empty",
            }
    else:
        if set_label is None and not note:
            return {
                "ok": False,
                "error": "ConfigurationError",
                "message": "give set_label (and/or note), or updates=[...]",
            }
        raw_updates = [
            LabelUpdate(
                ids=tuple(ids or ()),
                label=set_label,
                note=note,
                reason=reason,
                all_records=not ids,
            )
        ]

    if from_file:
        try:
            papers = _load_result_file(from_file)
            outcome = apply_updates(papers, raw_updates, strict=strict)
            target = out_file or from_file
            from .export import to_json

            to_json(papers, target)
        except ResearcherError as exc:
            return _fail(exc)
        return _ok(
            file=target,
            labelled=outcome.labelled,
            total=len(papers),
            summary=matrix.screen_summary(papers),
            unmatched=outcome.unmatched,
            invalid=outcome.invalid,
        )

    try:
        target_workspace = _resolve_workspace(workspace)
        if target_workspace is None:
            return {
                "ok": False,
                "error": "ConfigurationError",
                "message": "no workspace open; call open_workspace(path) or pass workspace='...'",
            }
        result = target_workspace.label_many(raw_updates, strict=strict)
    except ResearcherError as exc:
        return _fail(exc)
    return _ok(
        workspace=target_workspace.status(),
        file=str(target_workspace.results_path),
        labelled=result["labelled"],
        total=result["total"],
        summary=result["summary"],
        unmatched=result["unmatched"],
        invalid=result["invalid"],
    )


def _tool_screen(**kwargs: Any) -> dict[str, Any]:
    """Alias of :func:`_tool_label` — the verb is "screen"."""
    return _tool_label(**kwargs)


def _tool_prescreen(
    from_file: str | None = None,
    workspace: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    exclude_types: list[str] | None = None,
    rules: list[str] | None = None,
    require_abstract: bool = True,
    require_title: bool = True,
    apply: bool = False,
    limit: int = 50,
    out_file: str | None = None,
) -> dict[str, Any]:
    """Flag obvious exclusions by rule; report unless ``apply=True``.

    A pre-screen is a *candidate* list, not a decision: "no abstract" is a
    false positive for grey literature, which is why applying is opt-in and
    never overwrites a record that already carries a human label.
    """
    if bool(from_file) and bool(workspace):
        return {
            "ok": False,
            "error": "ConfigurationError",
            "message": "give exactly one of from_file or workspace",
        }
    try:
        criteria = PrescreenCriteria(
            year_from=year_from,
            year_to=year_to,
            exclude_types=(
                tuple(exclude_types)
                if exclude_types is not None
                else PrescreenCriteria().exclude_types
            ),
            require_abstract=require_abstract,
            require_title=require_title,
            rules=tuple(rules) if rules else None,
        )
    except ResearcherError as exc:
        return _fail(exc)

    if from_file:
        try:
            papers = _load_result_file(from_file)
            from .screening import prescreen as _prescreen

            report = _prescreen(papers, criteria)
            applied = 0
            if apply:
                for hit in report.hits:
                    if hit.paper.triage_label() != "unreviewed":
                        continue
                    hit.paper.label_as("excluded", "pre-screen: " + hit.reason)
                    applied += 1
                from .export import to_json

                to_json(papers, out_file or from_file)
            payload = report.as_dict(limit=limit)
            payload["applied"] = applied
            payload["summary"] = matrix.screen_summary(papers)
            payload["file"] = out_file or from_file
            return _ok(**payload)
        except ResearcherError as exc:
            return _fail(exc)

    try:
        target = _resolve_workspace(workspace)
        if target is None:
            return {
                "ok": False,
                "error": "ConfigurationError",
                "message": "no workspace open; call open_workspace(path) or pass workspace='...'",
            }
        payload = target.prescreen(apply=apply, criteria=criteria, limit=limit)
    except ResearcherError as exc:
        return _fail(exc)
    return _ok(workspace=target.status(), **payload)


def _tool_snowball(
    workspace: str | None = None,
    direction: str = "both",
    ids: list[str] | None = None,
    core_label: str = "core",
    limit: int | None = None,
    sources: list[str] | None = None,
    min_links: int = 1,
    max_results: int = 25,
    persist: bool = True,
) -> dict[str, Any]:
    """Expand the core set through references/citations, ranked by link count.

    Returns only records not already in the workspace.  ``linked_by`` names
    the core papers that linked each candidate, so a record linked by five
    core papers is visibly stronger than one linked by one.  ``persist=True``
    merges the candidates and logs the expansion; ``False`` previews.
    """
    try:
        target = _resolve_workspace(workspace)
        if target is None:
            return {
                "ok": False,
                "error": "ConfigurationError",
                "message": "no workspace open; call open_workspace(path) or pass workspace='...'",
            }
        payload = target.expand(
            direction=direction,
            ids=ids,
            core_label=core_label,
            limit=limit,
            sources=sources,
            min_links=min_links,
            max_results=max_results,
            persist=persist,
        )
    except ResearcherError as exc:
        return _fail(exc)
    return _ok(**payload)


def _tool_prisma(
    workspace: str | None = None, format: str = "json"
) -> dict[str, Any]:
    """PRISMA-style counts derived from the workspace log and labels."""
    if format not in ("json", "markdown"):
        return {
            "ok": False,
            "error": "ConfigurationError",
            "message": (
                f"unknown prisma format {format!r}; expected json or markdown"
            ),
        }
    try:
        target = _resolve_workspace(workspace)
        if target is None:
            return {
                "ok": False,
                "error": "ConfigurationError",
                "message": "no workspace open; call open_workspace(path) or pass workspace='...'",
            }
        report = target.prisma()
    except ResearcherError as exc:
        return _fail(exc)
    if format == "markdown":
        return _ok(format="markdown", text=render_prisma_markdown(report), report=report)
    return _ok(format="json", **report)


def _tool_handoff(
    source: str | None = None,
    out_dir: str = "",
    labels: list[str] | None = None,
    require_screening: bool = True,
    include_prisma: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Write a versioned research bundle for a paper-writing tool."""
    if not out_dir:
        return {
            "ok": False,
            "error": "ConfigurationError",
            "message": "out_dir is required",
        }
    target = source
    if not target:
        workspace = _resolve_workspace(None)
        if workspace is None:
            return {
                "ok": False,
                "error": "ConfigurationError",
                "message": (
                    "no source given; pass source='workspace' or call "
                    "open_workspace(path) first"
                ),
            }
        target = str(workspace.path)
    try:
        return handoff_mod.build_handoff(
            target,
            out_dir,
            settings=_settings(),
            labels=labels,
            require_screening=require_screening,
            include_prisma=include_prisma,
            force=force,
        )
    except ResearcherError as exc:
        return _fail(exc)


def _tool_extract_text(path: str, pages: str | None = None) -> dict[str, Any]:
    """Plain text of a local PDF, optionally a page range (``"1-5,10"``)."""
    try:
        text = extract.extract_text(path, pages=pages)
    except ResearcherError as exc:
        return _fail(exc)
    return _ok(path=path, characters=len(text), text=text)


def _tool_find_quotes(
    path: str,
    terms: str | None = None,
    pages: str | None = None,
    window: int = 240,
    limit: int = 10,
) -> dict[str, Any]:
    """Page-anchored quotes from a local PDF, for notes that cite a page."""
    try:
        quotes = extract.find_quotes(
            path, terms=terms, pages=pages, window=window, limit=limit
        )
    except ResearcherError as exc:
        return _fail(exc)
    return _ok(path=path, count=len(quotes), quotes=quotes)


def _papers_for(
    identifiers: list[str] | None,
    query: str | None,
    from_file: str | None,
    sources: list[str] | None,
    max_results: int,
    workspace: str | None = None,
) -> list[Paper]:
    """Shared input resolution for the screening/matrix tools."""
    if workspace:
        if from_file or identifiers or query:
            raise ConfigurationError(
                "workspace is a complete input; do not also pass "
                "from_file/identifiers/query"
            )
        target = _resolve_workspace(workspace)
        if target is None:
            raise ConfigurationError(
                "no workspace open; pass workspace='...'"
            )
        return target.papers
    if from_file:
        return _load_result_file(from_file)
    if identifiers:
        papers: list[Paper] = []
        for identifier in identifiers:
            papers.append(pipeline.get(identifier, settings=_settings()))
        return papers
    if query:
        return pipeline.search(
            query, sources=sources, max_results=max_results, settings=_settings()
        ).papers
    raise ConfigurationError(
        "provide identifiers [...], query '...', or from_file 'FILE'"
    )


def _tool_get(identifier: str, source: str | None = None) -> dict[str, Any]:
    try:
        paper = pipeline.get(identifier, source=source, settings=_settings())
    except ResearcherError as exc:
        return _fail(exc)
    return _ok(paper=paper.to_dict())


def _tool_citations(
    identifier: str,
    direction: str,
    sources: list[str] | None = None,
    limit: int = 20,
    order: str = "canonical",
    strict: bool = False,
) -> dict[str, Any]:
    """Walk the citation graph, resolving ``identifier`` to a Paper first."""
    try:
        paper = pipeline.get(identifier, settings=_settings())
    except ResearcherError as exc:
        return _fail(exc)
    func = citation.references if direction == "references" else citation.citations
    try:
        result = func(
            paper,
            sources=sources,
            strict=strict,
            limit=limit,
            order=order,
            settings=_settings(),
        )
    except ResearcherError as exc:
        return _fail(exc)
    return _citation_payload(result)


def _tool_download(identifier: str, output_dir: str | None = None) -> dict[str, Any]:
    try:
        paper = pipeline.get(identifier, settings=_settings())
        result = download_pdf(
            paper, output_dir, overwrite=False, settings=_settings()
        )
    except ResearcherError as exc:
        return _fail(exc)
    return _ok(
        identifier=identifier,
        path=str(result.path),
        url=result.url,
        via=result.via,
    )


def _tool_bibliography(
    identifiers: list[str] | None = None,
    query: str | None = None,
    format: str = "harvard",
    sources: list[str] | None = None,
    max_results: int = 10,
    from_file: str | None = None,
    accessed: str = "",
    workspace: str | None = None,
) -> dict[str, Any]:
    """Render a reference list: harvard, BibTeX, RIS or CSL-JSON.

    Sources: explicit ``identifiers``, a live ``query``, or ``from_file`` — a
    saved JSON/JSONL result set, which is the reproducible route: it renders
    what was actually screened instead of re-running a search whose results may
    since have changed.
    """
    if format not in _BIB_FORMATS:
        return _fail_bad_format(format)
    errors: dict[str, str] = {}
    if workspace and (from_file or identifiers or query):
        return {
            "ok": False,
            "error": "ConfigurationError",
            "message": (
                "workspace is a complete input; do not also pass "
                "from_file/identifiers/query"
            ),
        }
    try:
        if workspace:
            target = _resolve_workspace(workspace)
            if target is None:
                return {
                    "ok": False,
                    "error": "ConfigurationError",
                    "message": "no workspace open; pass workspace='...'",
                }
            papers = target.papers
        elif from_file:
            papers = _load_result_file(from_file)
        elif identifiers:
            papers = []
            errors: dict[str, str] = {}
            for ident in identifiers:
                try:
                    papers.append(pipeline.get(ident, settings=_settings()))
                except ResearcherError as exc:
                    errors[ident] = _fail(exc)["message"]
        elif query:
            result = pipeline.search(
                query,
                sources=sources,
                max_results=max_results,
                settings=_settings(),
            )
            papers = result.papers
            errors = {n: _fail(e)["message"] for n, e in result.errors.items()}
        else:
            return {
                "ok": False,
                "error": "ConfigurationError",
                "message": (
                    "provide identifiers [...], query '...', or from_file 'FILE'"
                ),
            }
    except ResearcherError as exc:
        return _fail(exc)

    renderer = {
        "harvard": lambda ps: bib.render_harvard(ps, accessed=accessed),
        "bibtex": bib.render_bibtex,
        "ris": bib.render_ris,
        "csl_json": bib.render_csl_json,
    }[format]
    return _ok(format=format, count=len(papers), text=renderer(papers), errors=errors)


def _load_result_file(path: str) -> list[Paper]:
    """Read a saved JSON envelope / JSONL stream back into ``Paper`` objects."""
    import io

    from .export import from_json, from_jsonl

    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise ConfigurationError(f"cannot read {path}: {exc}") from exc
    if not text.strip():
        return []
    if path.endswith(".jsonl"):
        return from_jsonl(io.StringIO(text))
    return from_json(io.StringIO(text))


def _fail_bad_format(format: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "ConfigurationError",
        "message": (
            f"unknown bibliography format {format!r}; "
            f"expected one of {', '.join(_BIB_FORMATS)}"
        ),
    }


def _fail_bad_matrix_format(format: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "ConfigurationError",
        "message": (
            f"unknown matrix format {format!r}; "
            f"expected one of {', '.join(_MATRIX_FORMATS)}"
        ),
    }


def _tool_web_search(
    query: str,
    source: str = "stackoverflow",
    max_results: int = 10,
    sort: str | None = None,
    subreddit: str | None = None,
    time_range: str | None = None,
    site: str | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if subreddit:
        kwargs["subreddit"] = subreddit
    if time_range:
        kwargs["time_range"] = time_range
    if site:
        kwargs["site"] = site
    try:
        result = web_sources.web_search(
            query,
            source=source,
            max_results=max_results,
            sort=sort,
            settings=_settings(),
            **kwargs,
        )
    except ResearcherError as exc:
        return _fail(exc)
    return _web_payload(result)


#: (name, callable, description) for every tool, exposed for introspection/tests.
TOOLS = (
    "sources",
    "info",
    "open_workspace",
    "workspace_status",
    "close_workspace",
    "workspace_next_round",
    "search_papers",
    "get_paper",
    "references",
    "citations",
    "download_pdf",
    "bibliography",
    "web_search",
    "report",
    "screen",
    "prescreen",
    "snowball",
    "handoff",
    "extract_text",
    "find_quotes",
)

#: ``TOOLS`` minus the screening/download/extraction surface (see ``_LITE_EXCLUDED``).
LITE_TOOLS = tuple(name for name in TOOLS if name not in _LITE_EXCLUDED)


# ---------------------------------------------------------------------- #
# Server construction (the only place the mcp SDK is imported)
# ---------------------------------------------------------------------- #


def build_server(settings: Settings | None = None, profile: str | None = None):
    """Create the :class:`FastMCP` server and register every enabled tool.

    ``settings`` overrides the environment-loaded settings (tests use it).
    ``profile`` is ``full`` (default) or ``lite``; when omitted,
    ``RESEARCHER_MCP_PROFILE`` decides.  Raises a clear error when the optional
    ``mcp`` dependency is absent or the profile is unknown.
    """
    active_profile = _resolve_profile(profile)
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise ImportError(
            "the MCP server needs the optional 'mcp' dependency; install it "
            "with: pip install 'researcher-python[mcp]'  "
            "(or add `mcp` to the Nix shell)"
        ) from exc

    if settings is not None:
        _set_settings(settings)

    server = FastMCP(
        _SERVER_NAME,
        instructions=_SERVER_INSTRUCTIONS,
    )

    excluded = _LITE_EXCLUDED if active_profile == "lite" else frozenset()

    def register(fn):
        """Register a tool unless the active profile excludes it."""
        if fn.__name__ in excluded:
            return fn
        return server.tool()(fn)

    # --- discovery -----------------------------------------------------
    @register
    def sources() -> dict:
        """List every registered source (scholarly + web) and its capabilities."""
        return _tool_sources()

    @register
    def info() -> dict:
        """Report package version and effective settings (secrets as set/unset)."""
        return _tool_info()

    # --- workspace (state + provenance) --------------------------------
    @register
    def open_workspace(
        path: str, create: bool = True, round: int | None = None
    ) -> dict:
        """Open a workspace directory and make it this session's default.

        Once open, ``search_papers`` persists automatically: records merge into
        the canonical set and the reply reports ``new`` / ``seen`` /
        ``screened``.  A relative path resolves under ``RESEARCHER_HOME`` when
        configured, so the server works from any project or vault.
        """
        return _tool_open_workspace(path, create, round)

    @register
    def workspace_status(workspace: str | None = None) -> dict:
        """Counts, round, provenance and file paths for a workspace."""
        return _tool_workspace_status(workspace)

    @register
    def close_workspace() -> dict:
        """Clear the session-default workspace (searches stop persisting)."""
        return _tool_close_workspace()

    @register
    def workspace_next_round(
        label: str = "", workspace: str | None = None
    ) -> dict:
        """Snapshot the current round and advance to the next one."""
        return _tool_workspace_next_round(label, workspace)

    # --- scholarly core ------------------------------------------------
    @register
    def search_papers(
        query: str,
        sources: list[str] | None = None,
        max_results: int = 10,
        sort: str = "relevance",
        year_from: int | None = None,
        year_to: int | None = None,
        field: str = "any",
        min_score: float | None = None,
        top_ratio: float | None = None,
        min_citations: int | None = None,
        workspace: str | None = None,
        types: list[str] | None = None,
        exclude_types: list[str] | None = None,
        default_excludes: bool = True,
        enrich: bool | None = None,
        detail: Literal["summary", "compact", "full"] = "summary",
        fields: list[str] | None = None,
        limit: int | None = 10,
        offset: int = 0,
    ) -> dict:
        """Search papers across sources; merged, deduped, summarized and paged.

        ``max_results`` caps each source's retrieval. ``limit``/``offset`` page
        the merged set (default 10 rows); the reply reports ``summary``,
        ``kept``, ``has_more`` and ``next_offset``. ``detail`` is ``summary``
        (default, no abstract), ``compact`` (240-char excerpt) or ``full``;
        ``fields`` projects exact keys and wins over it. ``sort='score'`` plus
        ``top_ratio`` beats a large ``max_results`` because source scores are
        incomparable. ``errors`` reports per-source failures without aborting
        the call. Use ``get_paper`` for one complete record.
        """
        return _tool_search(
            query,
            sources=sources,
            max_results=max_results,
            sort=sort,
            year_from=year_from,
            year_to=year_to,
            field=field,
            min_score=min_score,
            top_ratio=top_ratio,
            min_citations=min_citations,
            workspace=workspace,
            types=types,
            exclude_types=exclude_types,
            default_excludes=default_excludes,
            enrich=enrich,
            detail=detail,
            fields=fields,
            limit=limit,
            offset=offset,
        )

    @register
    def get_paper(identifier: str, source: str | None = None) -> dict:
        """Fetch one paper by DOI, arXiv id, or PMID (optionally a fixed source)."""
        return _tool_get(identifier, source)

    @register
    def references(
        identifier: str,
        sources: list[str] | None = None,
        limit: int = 20,
        order: str = "canonical",
        strict: bool = False,
    ) -> dict:
        """Works that the given paper references (merged across sources)."""
        return _tool_citations(identifier, "references", sources, limit, order, strict)

    @register
    def citations(
        identifier: str,
        sources: list[str] | None = None,
        limit: int = 20,
        order: str = "canonical",
        strict: bool = False,
    ) -> dict:
        """Works that cite the given paper (merged across sources)."""
        return _tool_citations(identifier, "citations", sources, limit, order, strict)

    @register
    def download_pdf(identifier: str, output_dir: str | None = None) -> dict:
        """Download the open-access PDF for one paper (Unpaywall fallback)."""
        return _tool_download(identifier, output_dir)

    @register
    def bibliography(
        identifiers: list[str] | None = None,
        query: str | None = None,
        format: str = "harvard",
        sources: list[str] | None = None,
        max_results: int = 10,
        from_file: str | None = None,
        accessed: str = "",
        workspace: str | None = None,
    ) -> dict:
        """Render a reference list as harvard (default), BibTeX, RIS or CSL-JSON.

        Inputs: explicit ``identifiers``, a live ``query``, or ``from_file`` —
        a saved JSON/JSONL result set.  ``from_file`` is the reproducible
        route: it renders exactly what was screened.  ``accessed`` supplies the
        access date Harvard's "Available at" line wants.
        """
        return _tool_bibliography(
            identifiers, query, format, sources, max_results, from_file,
            accessed, workspace,
        )

    # --- web sources ---------------------------------------------------
    @register
    def web_search(
        query: str,
        source: str = "stackoverflow",
        max_results: int = 10,
        sort: str | None = None,
        subreddit: str | None = None,
        time_range: str | None = None,
        site: str | None = None,
    ) -> dict:
        """Search a web source (stackoverflow, reddit) for structured records."""
        return _tool_web_search(
            query, source, max_results, sort, subreddit, time_range, site
        )

    # --- screening and reports ----------------------------------------
    @register
    def report(
        kind: Literal[
            "matrix", "synthesis", "screening", "prisma", "screen_summary"
        ],
        identifiers: list[str] | None = None,
        query: str | None = None,
        from_file: str | None = None,
        sources: list[str] | None = None,
        max_results: int = 25,
        columns: list[str] | None = None,
        dimension: str = "year",
        format: str | None = None,
        require_screening: bool = False,
        workspace: str | None = None,
    ) -> dict:
        """Render one report artefact from a saved set or workspace.

        ``kind='matrix'`` (``columns``, csv|markdown), ``'synthesis'``
        (``dimension``, csv|markdown), ``'screening'`` (counts per label) or
        ``'prisma'`` (workspace only, json|markdown). Inputs: identifiers,
        query, from_file or workspace. Check ``screening.ready`` before
        treating a matrix as screened; pass ``require_screening=True`` to
        refuse an unscreened set. ``screen_summary`` is an alias for
        ``screening``.
        """
        return _tool_report(
            kind,
            identifiers=identifiers,
            query=query,
            from_file=from_file,
            sources=sources,
            max_results=max_results,
            columns=columns,
            dimension=dimension,
            format=format,
            require_screening=require_screening,
            workspace=workspace,
        )

    @register
    def screen(
        from_file: str | None = None,
        set_label: str | None = None,
        ids: list[str] | None = None,
        reason: str = "",
        note: str = "",
        out_file: str | None = None,
        workspace: str | None = None,
        updates: list[dict[str, Any]] | None = None,
        strict: bool = True,
    ) -> dict:
        """Screen records: set the stored label / notes, one or many.

        ``label`` is the stored field; ``screen`` is the action. Batch shape:
        ``updates=[{"id": "10.1/a", "label": "core"},
        {"ids": ["10.1/b", "10.1/c"], "label": "excluded",
        "reason": "off topic"}]``. A batch is validated up front and written
        once; ``strict=False`` applies the valid entries and reports the rest.
        """
        return _tool_screen(
            from_file=from_file,
            set_label=set_label,
            ids=ids,
            reason=reason,
            note=note,
            out_file=out_file,
            workspace=workspace,
            updates=updates,
            strict=strict,
        )

    @register
    def prescreen(
        from_file: str | None = None,
        workspace: str | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        exclude_types: list[str] | None = None,
        rules: list[str] | None = None,
        require_abstract: bool = True,
        require_title: bool = True,
        apply: bool = False,
        limit: int = 50,
        out_file: str | None = None,
    ) -> dict:
        """Flag obvious exclusions by rule, without spending judgement on them.

        Rules: no-title, no-abstract, type (grant/standard/dataset by default)
        and outside-year (only when a bound is given; an unknown year is kept).
        Reports by default; ``apply=True`` writes those records as ``excluded``
        with a ``pre-screen:`` reason and never overwrites an existing label.
        """
        return _tool_prescreen(
            from_file=from_file,
            workspace=workspace,
            year_from=year_from,
            year_to=year_to,
            exclude_types=exclude_types,
            rules=rules,
            require_abstract=require_abstract,
            require_title=require_title,
            apply=apply,
            limit=limit,
            out_file=out_file,
        )

    @register
    def snowball(
        workspace: str | None = None,
        direction: str = "both",
        ids: list[str] | None = None,
        core_label: str = "core",
        limit: int | None = None,
        sources: list[str] | None = None,
        min_links: int = 1,
        max_results: int = 25,
        persist: bool = True,
    ) -> dict:
        """Expand the core set through references/citations, ranked by links.

        The core set is the explicit ``ids`` or every record labelled ``core``.
        Only records not already in the workspace are returned, sorted by how
        many core papers link to them; ``linked_by`` names those cores.  This
        is the recall engine a keyword search cannot be.  ``persist=True``
        merges the candidates and records the expansion in the provenance log;
        ``persist=False`` previews.  A source failure is reported per core and
        never aborts the rest.
        """
        return _tool_snowball(
            workspace=workspace,
            direction=direction,
            ids=ids,
            core_label=core_label,
            limit=limit,
            sources=sources,
            min_links=min_links,
            max_results=max_results,
            persist=persist,
        )

    # --- PDF text ------------------------------------------------------
    @register
    def handoff(
        source: str | None = None,
        out_dir: str = "",
        labels: list[str] | None = None,
        require_screening: bool = True,
        include_prisma: bool = True,
        force: bool = False,
    ) -> dict:
        """Write a versioned research bundle for a paper-writing tool.

        Includes only ``core`` + ``general`` by default, renders the bibliography
        and a citation-key map from one pass, and refuses an unscreened set
        unless ``require_screening=False``. The bundle is designed for
        ``paper-handler research import`` and contains no network side effects.
        """
        return _tool_handoff(
            source,
            out_dir,
            labels,
            require_screening,
            include_prisma,
            force,
        )

    @register
    def extract_text(path: str, pages: str | None = None) -> dict:
        """Plain text of a local PDF; ``pages`` selects a range like "1-5,10"."""
        return _tool_extract_text(path, pages)

    @register
    def find_quotes(
        path: str,
        terms: str | None = None,
        pages: str | None = None,
        window: int = 240,
        limit: int = 10,
    ) -> dict:
        """Page-anchored quotes from a local PDF, so a note can cite a page."""
        return _tool_find_quotes(path, terms, pages, window, limit)

    return server


def server():
    """Build the default server (environment-loaded settings)."""
    return build_server()


def main() -> None:
    """Entry point: serve over stdio (the standard MCP transport)."""
    # stderr is the logging channel; stdout is the MCP wire and must stay clean.
    from .logging_config import setup_logging

    setup_logging(_settings().log_level)
    server().run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
