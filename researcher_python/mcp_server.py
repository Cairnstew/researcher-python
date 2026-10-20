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
from typing import Any

from . import __version__
from . import citation, extract, format as bib, matrix, pipeline, web_sources
from .config import Settings, load_settings
from .download import download_pdf
from .errors import ConfigurationError, ResearcherError
from .models import SORT_MODES, TRIAGE_LABELS, Paper
from .prisma import render_prisma_markdown
from .screening import LabelUpdate, PrescreenCriteria, apply_updates
from .sources.base import SEARCH_FIELDS, SearchFilters
from .sources import DEFAULT_SOURCES, source_names
from .web_sources import web_source_names
from .workspace import Workspace

__all__ = ["TOOLS", "build_server", "main", "server"]

_SERVER_NAME = "researcher-python"
_SERVER_INSTRUCTIONS = (
    "Unified scholarly search across arXiv, OpenAlex, Semantic Scholar, Crossref "
    "and PubMed. Use 'search_papers' to find works, 'get_paper' for one "
    "identifier (DOI / arXiv id / PMID), 'references'/'citations' to walk the "
    "citation graph, 'download_pdf' for an open-access PDF, 'bibliography' to "
    "render BibTeX/RIS/CSL-JSON, and 'web_search' for Stack Overflow / Reddit. "
    "'sources' and 'info' report what is configured.\n"
    "\n"
    "WORKSPACES: call 'open_workspace' with a directory to make search_papers "
    "persist automatically. A workspace owns results.json, an append-only "
    "search log and its meta; a search into an existing workspace dedupes "
    "against prior rounds and reports counts.new / counts.seen / "
    "counts.screened. 'workspace_status' shows the round, counts and "
    "provenance; 'workspace_next_round' snapshots and advances. Without an "
    "open workspace (or workspace='...' on the call) a search is pure and "
    "returns persisted=false.\n"
    "\n"
    "SEARCHING: prefer sort='score' plus a floor over raising max_results. Each "
    "source ranks by a private, mutually incomparable score, so a merged set "
    "otherwise keeps every source's weakest hits; max_results caps each source "
    "separately and cannot remove them afterwards. top_ratio (0.0<R<=1.0) keeps "
    "records scoring at least R x the best and is scale-free, so it cuts the "
    "tail however long the query; min_score (0.0-1.0) is an absolute floor and "
    "removes nothing on a long query. Both are optional and combine. The reply "
    "reports 'dropped' and the 'terms' scored against, and every record carries "
    "a local 'relevance' value.\n"
    "\n"
    "SCREENING IS PART OF SEARCHING, NOT AN OPTIONAL EXTRA. The deliverable is "
    "a Literature Matrix, and a matrix is defined by its triage decisions: rows "
    "still marked 'unreviewed' are a raw search dump, not a literature review. "
    "The workflow is search -> prescreen -> screen -> snowball -> matrix -> "
    "synthesis -> bibliography:\n"
    "  1. open_workspace, then search_papers (persists) — or search_papers "
    "with workspace='...'. The workspace is the result set.\n"
    "  2. 'prescreen' flags obvious exclusions by rule (no abstract, grant/"
    "standard/dataset, outside a year window). It reports by default; pass "
    "apply=True only after checking the candidates, because a rule can be "
    "wrong for grey literature. It never overwrites a human label.\n"
    "  3. 'screen' (alias 'label') sets the stored label: core | general | "
    "excluded, plus 'reason' for exclusions and 'note' for dated reading "
    "notes. Use 'updates': [{id|ids, label, reason, note}, ...] to screen a "
    "batch in ONE call; unmatched ids and invalid entries are reported. The "
    "old set_label/ids/note form still works.\n"
    "  4. 'snowball' expands the records labelled 'core' through references, "
    "citations or both. It returns only records not already in the workspace, "
    "ranked by how many core papers link to them (linked_by/link count). "
    "persist=True (the default) merges them and logs the expansion as a "
    "provenance row; persist=False previews.\n"
    "  5. 'matrix' with from_file=that file. Check screening.ready in the "
    "reply: it is False while any record is unreviewed. Pass "
    "require_screening=True to have the tool refuse an unscreened set instead "
    "of emitting one.\n"
    "  6. 'synthesis' for the dimension x label cross-tab that makes a research "
    "gap visible, 'prisma' for identification -> deduped -> screened -> "
    "included counts from the log and labels, and 'bibliography' with "
    "from_file= so the reference list is reproducible from exactly what was "
    "screened.\n"
    "Prefer from_file over a live query for steps 5-6: it renders what was "
    "actually screened instead of re-running a search whose results may since "
    "have changed."
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


#: Characters of abstract kept by ``compact=True`` (see :func:`_compact_paper`).
_COMPACT_ABSTRACT = 240


def _compact_paper(paper: Paper) -> dict[str, Any]:
    """A triage-sized projection of a record.

    The full ``to_dict()`` carries every field, including a complete abstract
    for each of up to dozens of records.  That is the right shape for a saved
    result file and the wrong shape for an agent's context window — in practice
    a 30-record search overflowed it twice.  This keeps exactly what triage
    needs (identity, standing, relevance, a readable excerpt) and drops the
    long tail, which ``get_paper`` still returns in full for any one record.
    """
    abstract = " ".join((paper.abstract or "").split())
    excerpt = (
        abstract
        if len(abstract) <= _COMPACT_ABSTRACT
        else abstract[: _COMPACT_ABSTRACT - 1].rstrip() + "…"
    )
    return {
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
        "pmid": paper.pmid,
        "title": paper.title,
        "year": paper.year,
        "venue": paper.venue,
        "work_type": paper.work_type,
        "authors": [a.name for a in paper.authors[:4]],
        "author_count": len(paper.authors),
        "citation_count": paper.citation_count,
        "is_open_access": paper.is_open_access,
        "relevance": paper.relevance,
        "label": paper.triage_label(),
        "abstract_excerpt": excerpt,
    }


def _search_payload(
    result: pipeline.PipelineResult, query: str, compact: bool = False
) -> dict[str, Any]:
    papers = [
        _compact_paper(p) if compact else p.to_dict() for p in result.papers
    ]
    attempted = list(result.by_source)
    failed = [name for name in attempted if name in result.errors]
    return _ok(
        query=query,
        count=len(papers),
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
    compact: bool = False,
    min_citations: int | None = None,
    workspace: str | None = None,
    types: list[str] | None = None,
    exclude_types: list[str] | None = None,
    default_excludes: bool = True,
    enrich: bool | None = None,
) -> dict[str, Any]:
    """Search with an optional publication window, field scope and floors.

    When a workspace is open (or ``workspace`` names one), the search is
    persisted through it: records merge into the canonical set and the reply
    reports ``new`` / ``seen`` / ``screened`` as well as the request params.
    Without a workspace the call stays pure and returns ``persisted: false``.
    """
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

    outcome = None
    try:
        target = _resolve_workspace(workspace)
        if target is not None:
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
    except ResearcherError as exc:
        return _fail(exc)

    payload = _search_payload(result, query, compact)
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
            "results": outcome.paths["results"],
            "log": outcome.paths["log"],
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
            "by_source": dict(result.by_source),
            "errors": {
                name: _fail(error)["message"]
                for name, error in result.errors.items()
            },
            "terms": list(result.terms),
        }
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


#: Matrix render formats (matrices are tables, not reference lists).
_MATRIX_FORMATS = ("csv", "markdown")


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
    "matrix",
    "synthesis",
    "screen_summary",
    "label",
    "screen",
    "prescreen",
    "snowball",
    "prisma",
    "extract_text",
    "find_quotes",
)


# ---------------------------------------------------------------------- #
# Server construction (the only place the mcp SDK is imported)
# ---------------------------------------------------------------------- #


def build_server(settings: Settings | None = None):
    """Create the :class:`FastMCP` server and register every tool.

    ``settings`` overrides the environment-loaded settings (tests use it).
    Raises a clear error when the optional ``mcp`` dependency is absent.
    """
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

    # --- discovery -----------------------------------------------------
    @server.tool()
    def sources() -> dict:
        """List every registered source (scholarly + web) and its capabilities."""
        return _tool_sources()

    @server.tool()
    def info() -> dict:
        """Report package version and effective settings (secrets as set/unset)."""
        return _tool_info()

    # --- workspace (state + provenance) --------------------------------
    @server.tool()
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

    @server.tool()
    def workspace_status(workspace: str | None = None) -> dict:
        """Counts, round, provenance and file paths for a workspace."""
        return _tool_workspace_status(workspace)

    @server.tool()
    def close_workspace() -> dict:
        """Clear the session-default workspace (searches stop persisting)."""
        return _tool_close_workspace()

    @server.tool()
    def workspace_next_round(
        label: str = "", workspace: str | None = None
    ) -> dict:
        """Snapshot the current round and advance to the next one."""
        return _tool_workspace_next_round(label, workspace)

    # --- scholarly core ------------------------------------------------
    @server.tool()
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
        compact: bool = False,
        min_citations: int | None = None,
        workspace: str | None = None,
        types: list[str] | None = None,
        exclude_types: list[str] | None = None,
        default_excludes: bool = True,
        enrich: bool | None = None,
    ) -> dict:
        """Search papers across sources and return merged, deduped records.

        ``sources`` defaults to all scholarly sources.  ``sort`` is one of
        relevance, score, year, citations, title.  ``year_from``/``year_to`` bound
        the publication window and ``field`` narrows matching to ``title`` or
        ``abstract`` — stated once, applied by every source, so the search is
        repeatable.  A per-source failure is reported in ``errors`` and never
        aborts the call.

        **Prefer ``sort="score"`` plus a floor over raising ``max_results`` when
        the aim is relevance.**  Each source ranks by a private, mutually
        incomparable score, so a merged set otherwise keeps whatever every
        source's weakest hits were; ``max_results`` caps each source separately
        and cannot remove them afterwards.

        Two floors, applied to the merged set, with ``dropped`` in the reply
        saying how many records each removed:

        * ``min_score`` (0.0–1.0) — absolute.  Does not scale with query
          length: on a long query almost any on-subject paper clears a low bar.
        * ``top_ratio`` (0.0<R<=1.0) — keep records scoring at least
          ``R x the best score``.  Scale-free, so it cuts the tail whatever the
          query length.  Usually the better lever.

        Every record also carries a ``relevance`` field: the local score used.
        An empty result set explains itself in ``hint`` (e.g. a field-scoped
        AND match that is too long to match anything).

        ``types`` is an allow-list of canonical work types (``article``,
        ``review``, ``preprint``, ``book``, ``dataset``, ``grant``, ...);
        source spellings such as ``journal-article`` are accepted.  By default
        searches exclude ``grant`` and ``standard``; an explicit ``types`` list
        replaces that default, and ``exclude_types`` adds to it.  Set
        ``default_excludes=False`` to start with no exclusions.  A record with
        an unknown type is kept.

        ``min_citations`` drops records below a raw citation count.  An
        **unknown** count is kept (never hide grey literature), and raw counts
        reward age — the matrix's ``citations_per_year`` column is the
        age-normalised view and the better triage signal.

        ``compact=True`` returns a triage-sized projection — identity, standing,
        relevance and a 240-character abstract excerpt — instead of the full
        record.  Use it when scanning many results; use ``get_paper`` for the
        complete record of one.
        """
        return _tool_search(
            query, sources, max_results, sort, year_from, year_to, field,
            min_score, top_ratio, compact, min_citations, workspace,
            types, exclude_types, default_excludes, enrich,
        )

    @server.tool()
    def get_paper(identifier: str, source: str | None = None) -> dict:
        """Fetch one paper by DOI, arXiv id, or PMID (optionally a fixed source)."""
        return _tool_get(identifier, source)

    @server.tool()
    def references(
        identifier: str,
        sources: list[str] | None = None,
        limit: int = 20,
        order: str = "canonical",
        strict: bool = False,
    ) -> dict:
        """Works that the given paper references (merged across sources)."""
        return _tool_citations(identifier, "references", sources, limit, order, strict)

    @server.tool()
    def citations(
        identifier: str,
        sources: list[str] | None = None,
        limit: int = 20,
        order: str = "canonical",
        strict: bool = False,
    ) -> dict:
        """Works that cite the given paper (merged across sources)."""
        return _tool_citations(identifier, "citations", sources, limit, order, strict)

    @server.tool()
    def download_pdf(identifier: str, output_dir: str | None = None) -> dict:
        """Download the open-access PDF for one paper (Unpaywall fallback)."""
        return _tool_download(identifier, output_dir)

    @server.tool()
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
    @server.tool()
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

    # --- screening and matrices ---------------------------------------
    @server.tool()
    def matrix(
        identifiers: list[str] | None = None,
        query: str | None = None,
        from_file: str | None = None,
        sources: list[str] | None = None,
        max_results: int = 25,
        columns: list[str] | None = None,
        format: str = "csv",
        require_screening: bool = False,
        workspace: str | None = None,
    ) -> dict:
        """Literature Matrix: one row per paper, as CSV or Markdown.

        Reads the reader-owned triage state (``label``, ``notes``) carried on
        each record, so the matrix reflects screening rather than raw search
        output.  Pass ``columns`` to choose fields (``score`` records the local
        relevance that admitted each row).  The reply's ``screening.ready`` is
        ``False`` while any record is still ``unreviewed``.
        """
        return _tool_matrix(
            identifiers, query, from_file, sources, max_results, columns, format,
            require_screening, workspace,
        )

    @server.tool()
    def synthesis(
        identifiers: list[str] | None = None,
        query: str | None = None,
        from_file: str | None = None,
        sources: list[str] | None = None,
        max_results: int = 25,
        dimension: str = "year",
        format: str = "csv",
        require_screening: bool = False,
        workspace: str | None = None,
    ) -> dict:
        """Synthesis Matrix: a dimension cross-tabbed against screening labels.

        ``dimension`` is label, year, type, venue or open_access.  Also returns
        the screening summary, so a research-gap argument has its counts.
        """
        return _tool_synthesis(
            identifiers, query, from_file, sources, max_results, dimension, format,
            require_screening, workspace,
        )

    @server.tool()
    def screen_summary(
        identifiers: list[str] | None = None,
        query: str | None = None,
        from_file: str | None = None,
        sources: list[str] | None = None,
        max_results: int = 25,
        workspace: str | None = None,
    ) -> dict:
        """Screening counts per triage label (unreviewed/core/general/excluded)."""
        return _tool_screen_summary(
            identifiers, query, from_file, sources, max_results, workspace
        )

    @server.tool()
    def label(
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
        """Set screening labels / append notes (``label`` is the stored field).

        Screening is written back to the JSON (or the workspace's canonical
        ``results.json``) so later matrix, synthesis and bibliography renders
        see it.  Prefer the ``screen`` alias for the action.  Use
        ``updates=[{id|ids, label, reason, note}, ...]`` to apply a batch in
        one call; unmatched ids and invalid entries are reported.  Omitting
        ``ids`` (and ``updates``) applies the single label to every record.
        """
        return _tool_label(
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

    @server.tool()
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

        The verb form of ``label``.  Batch shape:
        ``updates=[{"id": "10.1/a", "label": "core"},
        {"ids": ["10.1/b", "10.1/c"], "label": "excluded",
        "reason": "off topic"}]``.  A batch is validated up front and written
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

    @server.tool()
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

    @server.tool()
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

    @server.tool()
    def prisma(workspace: str | None = None, format: str = "json") -> dict:
        """PRISMA-style counts: identified -> deduped -> screened -> included.

        Derived from the workspace's append-only log (identification and
        per-source counts) and the reader's labels.  ``format="markdown"``
        returns a paste-ready appendix table.  Gaps are reported in
        ``warnings`` rather than smoothed over: an imported workspace with no
        log has no honest identification count.
        """
        return _tool_prisma(workspace=workspace, format=format)

    # --- PDF text ------------------------------------------------------
    @server.tool()
    def extract_text(path: str, pages: str | None = None) -> dict:
        """Plain text of a local PDF; ``pages`` selects a range like "1-5,10"."""
        return _tool_extract_text(path, pages)

    @server.tool()
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
