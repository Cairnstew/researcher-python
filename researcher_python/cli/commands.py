"""Subcommand implementations for the ``researcher-python`` CLI.

These are plain ``(args, settings) -> exit code`` functions so
:mod:`researcher_python.cli` can dispatch them and apply one shared error
handler.  Nothing here prints to stderr — errors are raised as
:class:`~researcher_python.errors.ResearcherError` and reported upstream.

Both subcommands make **no network calls**: ``sources`` only *constructs*
each adapter, and ``info`` only reads :class:`~researcher_python.config.Settings`.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .. import citation, download, export, format as bib, matrix, pipeline
from .. import __version__
from ..config import ENV_KEYS, Settings
from ..errors import ConfigurationError, ResearcherError, SourceUnavailableError
from ..handoff import build_handoff
from ..models import Paper
from ..prisma import render_prisma_markdown
from ..screening import (
    PrescreenCriteria,
    LabelUpdate,
    apply_updates,
    prescreen,
)
from ..sources import DEFAULT_SOURCES, create_source, get_source_class, source_names
from ..sources.base import SEARCH_FIELDS, SearchFilters
from ..workspace import (
    SearchLog,
    SearchParams,
    Workspace,
    import_workspace,
    log_entry_for,
    render_log_markdown,
)

__all__ = [
    "cmd_bibliography",
    "cmd_citations",
    "cmd_download",
    "cmd_extract",
    "cmd_get",
    "cmd_handoff",
    "cmd_info",
    "cmd_label",
    "cmd_matrix",
    "cmd_prescreen",
    "cmd_prisma",
    "cmd_references",
    "cmd_screen",
    "cmd_search",
    "cmd_snowball",
    "cmd_sources",
    "cmd_synthesis",
    "cmd_summary",
    "cmd_web_search",
    "cmd_workspace",
]

#: ``(field, letter)`` pairs, in the order shown by the table's CAPS column.
_CAP_FIELDS = (
    ("search", "S"),
    ("get", "G"),
    ("references", "R"),
    ("citations", "C"),
    ("pdf", "P"),
)

#: Settings whose value is a secret and must only ever be shown set/unset.
_KEY_FIELDS = (
    "semantic_scholar_api_key",
    "openalex_api_key",
    "ncbi_api_key",
)


# ---------------------------------------------------------------------- #
# Shared rendering
# ---------------------------------------------------------------------- #


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    """Plain-text table — no tabulate, no alignment library."""
    widths = [
        max([len(header)] + [len(str(row[i])) for row in rows])
        for i, header in enumerate(headers)
    ]

    def line(cells: list[Any]) -> str:
        return "  ".join(
            str(cell).ljust(width) for cell, width in zip(cells, widths)
        ).rstrip()

    out = [line(headers), line(["-" * width for width in widths])]
    out.extend(line(row) for row in rows)
    return "\n".join(out)


def _set_unset(value: str) -> str:
    return "set" if value else "unset"


def _line(value: Any) -> str:
    """Collapse a possibly multi-line message onto one line (CLI contract)."""
    return " ".join(str(value).split())


def _env_var_from_error(exc: ConfigurationError) -> str:
    """The env var named by a ``ConfigurationError``, if it names one."""
    match = re.search(r"export (\S+) first", str(exc))
    if match:
        return match.group(1)
    for env_name, field in ENV_KEYS.items():
        if field in str(exc):
            return env_name
    return ""


# ---------------------------------------------------------------------- #
# info
# ---------------------------------------------------------------------- #


def cmd_info(args: argparse.Namespace, settings: Settings) -> int:
    """Print version, Python version and settings (secrets redacted)."""
    payload = _info_payload(settings)
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False))
        return 0

    print(f"researcher-python {payload['version']}")
    print(f"package: {payload['package']}")
    print(f"python: {payload['python']}")
    print()
    print(
        _table(
            ["SETTING", "VALUE"],
            # An unset optional setting is `None` in the payload (JSON null);
            # the table shows it as a word rather than an empty cell.
            [
                [name, "unset" if value is None else value]
                for name, value in payload["settings"].items()
            ],
        )
    )
    return 0


def _info_payload(settings: Settings) -> dict[str, Any]:
    import researcher_python

    rendered: dict[str, Any] = {
        # Email is printable when set, but absent rather than "" when not, so
        # `info --format json` emits null instead of an empty string.
        "email": settings.email or None,
        "output_dir": str(settings.output_dir),
        "log_level": settings.log_level,
        "max_results": settings.max_results,
        "timeout": settings.timeout,
        "retries": settings.retries,
    }
    for field in _KEY_FIELDS:
        # Never the value — only whether it is configured.
        rendered[field] = _set_unset(getattr(settings, field))

    return {
        "version": __version__,
        "package": researcher_python.__file__ or "",
        "python": sys.version.split()[0],
        "settings": rendered,
    }


# ---------------------------------------------------------------------- #
# sources
# ---------------------------------------------------------------------- #


def cmd_sources(args: argparse.Namespace, settings: Settings) -> int:
    """List every registered source with its capabilities and readiness."""
    entries = [_describe(name, settings) for name in source_names()]

    if args.format == "json":
        print(json.dumps(entries, indent=2, ensure_ascii=False, sort_keys=False))
        return 0

    rows = [
        [
            entry["name"],
            "yes" if entry["default"] else "no",
            _caps_string(entry["capabilities"]),
            entry["status"],
        ]
        for entry in entries
    ]
    print(_table(["NAME", "DEFAULT", "CAPS", "STATUS"], rows))
    return 0


def _describe(name: str, settings: Settings) -> dict[str, Any]:
    return {
        "name": name,
        "default": name in DEFAULT_SOURCES,
        "capabilities": _capabilities(name),
        "status": _status(name, settings),
    }


def _capabilities(name: str) -> dict[str, bool]:
    """Read the capability flags off the class — never constructs the source."""
    try:
        caps = get_source_class(name).capabilities
    except ResearcherError:
        return {field: False for field, _ in _CAP_FIELDS}
    return {field: bool(getattr(caps, field, False)) for field, _ in _CAP_FIELDS}


def _caps_string(capabilities: dict[str, bool]) -> str:
    return " ".join(
        letter if capabilities.get(field, False) else "-"
        for field, letter in _CAP_FIELDS
    )


def _status(name: str, settings: Settings) -> str:
    """Construct the adapter (no network) and classify what happens."""
    try:
        create_source(name, settings)
    except ConfigurationError as exc:
        env = _env_var_from_error(exc)
        return f"needs config ({env})" if env else "needs config"
    except SourceUnavailableError as exc:
        return f"unavailable ({exc.reason})" if exc.reason else "unavailable"
    except ResearcherError as exc:
        return f"error ({exc})"
    return "ready"


# ---------------------------------------------------------------------- #
# search
# ---------------------------------------------------------------------- #


def cmd_search(args: argparse.Namespace, settings: Settings) -> int:
    """Fan ``query`` across sources and print one merged, deduped result set.

    ``--from``/``--to``/``--field`` become a :class:`SearchFilters` applied by
    every source, and are recorded in the JSON envelope's ``meta`` so the
    search is described by the artefact it produced, not just by memory.

    With ``--workspace`` the search is persisted through
    :class:`researcher_python.workspace.Workspace`: records merge into the
    canonical set, the log row is appended, and the reply says how many were
    new, already seen and already screened.  ``--workspace`` owns its log, so
    combining it with ``--search-log`` is a usage error rather than two write
    paths.
    """
    if args.out and args.format == "table":
        # --out can only hold a written format; a table is a terminal-only view.
        raise ConfigurationError(
            "--out writes JSON, JSONL or a reference format; "
            "add e.g. --format json"
        )

    workspace_path = getattr(args, "workspace", None)
    search_log = getattr(args, "search_log", None)
    if workspace_path and search_log:
        raise ConfigurationError(
            "--workspace owns its own search log; "
            "drop --search-log or use a workspace without --workspace"
        )

    # An explicit --type list is an allow-list and replaces the default
    # exclusions; --exclude-type otherwise appends to them.  Resolve the
    # effective tuple here so the saved meta describes the search that ran.
    requested_types = tuple(getattr(args, "work_types", None) or ())
    extra_excludes = tuple(getattr(args, "exclude_types", None) or ())
    if requested_types or getattr(args, "no_default_exclude", False):
        exclude_types: tuple[str, ...] = extra_excludes
    else:
        from ..work_types import DEFAULT_SEARCH_EXCLUDE_TYPES

        exclude_types = DEFAULT_SEARCH_EXCLUDE_TYPES + extra_excludes
    filters = SearchFilters(
        year_from=args.year_from,
        year_to=args.year_to,
        field=args.field,
        types=requested_types,
        exclude_types=exclude_types,
    )
    requested = _requested_sources(args.sources)
    if getattr(args, "no_enrich", False):
        enrich: bool | None = False
    elif getattr(args, "force_enrich", False):
        enrich = True
    else:
        enrich = None

    outcome = None
    workspace = Workspace(workspace_path, settings=settings) if workspace_path else None
    if workspace is not None:
        outcome = workspace.search(
            args.query,
            sources=args.sources,
            max_results=args.max,
            sort=args.sort,
            filters=filters,
            min_score=args.min_score,
            top_ratio=args.top_ratio,
            min_citations=getattr(args, "min_citations", None),
            enrich=enrich,
            channel="cli",
        )
        result = outcome.result
    else:
        result = pipeline.search(
            args.query,
            sources=args.sources,
            max_results=args.max,
            sort=args.sort,
            settings=settings,
            filters=filters,
            min_score=args.min_score,
            top_ratio=args.top_ratio,
            min_citations=getattr(args, "min_citations", None),
            enrich=enrich,
        )

    if args.format == "json":
        meta = {
            "query": args.query,
            "sources": requested,
            "sort": args.sort,
            "year_from": filters.year_from,
            "year_to": filters.year_to,
            "field": filters.field,
            # The relevance floor and the terms it was measured against: without
            # these the saved file cannot explain its own omissions.
            "min_score": args.min_score,
            "top_ratio": args.top_ratio,
            "min_citations": getattr(args, "min_citations", None),
            "types": list(filters.types),
            "exclude_types": list(filters.exclude_types or ()),
            "dropped": result.dropped,
            "dropped_types": result.dropped_types,
            "dropped_type_counts": dict(result.dropped_type_counts),
            "versions_linked": result.versions_linked,
            "enrich": enrich,
            "enrichment": (
                result.enrichment.to_dict() if result.enrichment else None
            ),
            "terms": result.terms,
            "errors": {name: str(err) for name, err in result.errors.items()},
        }
        if outcome is not None:
            meta["workspace"] = {
                "path": outcome.paths["workspace"],
                "round": outcome.round,
                "search_id": outcome.search_id,
            }
            meta["counts"] = {
                "new": outcome.new,
                "seen": outcome.seen,
                "screened": outcome.screened,
                "total": outcome.total,
            }
        export.to_json(result.papers, args.out or sys.stdout, meta=meta)
    elif args.format == "jsonl":
        export.to_jsonl(result.papers, args.out or sys.stdout)
    else:
        _emit(result.papers, args)

    # Diagnostics: one line per failed source, then the summary, on stderr.
    for name, error in result.errors.items():
        print(f"{name}: {type(error).__name__}: {_line(error)}", file=sys.stderr)

    attempted = list(result.by_source)
    failed = [name for name in attempted if name in result.errors]
    papers, sources_ok = len(result.papers), len(attempted) - len(failed)
    print(
        f"{papers} {'paper' if papers == 1 else 'papers'} from "
        f"{sources_ok} {'source' if sources_ok == 1 else 'sources'} "
        f"(failed: {', '.join(failed) if failed else 'none'})",
        file=sys.stderr,
    )
    # A floor that removed records must say so: a thin result set is otherwise
    # indistinguishable from a sparse one.
    if result.enrichment and result.enrichment.ok:
        report = result.enrichment
        print(
            "enriched "
            f"{report.citation_filled} citation count(s), "
            f"{report.abstract_filled} abstract(s) "
            f"(citation coverage {report.coverage_before:.0%} -> "
            f"{report.coverage_after:.0%})",
            file=sys.stderr,
        )
    elif result.enrichment and result.enrichment.errors:
        print(
            f"enrichment failed: {'; '.join(result.enrichment.errors)}",
            file=sys.stderr,
        )
    if result.dropped_type_counts:
        by_type = ", ".join(
            f"{name}={count}"
            for name, count in sorted(result.dropped_type_counts.items())
        )
        print(
            f"dropped {result.dropped_types} record(s) by work type "
            f"({by_type})",
            file=sys.stderr,
        )
    if result.dropped:
        floors = []
        if args.min_score is not None:
            floors.append(f"--min-score {args.min_score}")
        if args.top_ratio is not None:
            floors.append(f"--top-ratio {args.top_ratio}")
        print(
            f"dropped {result.dropped} record(s) below "
            f"{' and '.join(floors) or 'the relevance floor'} "
            f"(scored against: {', '.join(result.terms) or 'no terms'})",
            file=sys.stderr,
        )
    # An empty result set usually has a guessable cause; say it rather than
    # leaving "0 papers" to mean nine different things.
    if result.hint:
        print(f"hint: {result.hint}", file=sys.stderr)
    # 0 whenever at least one source answered, even if it found nothing.
    if outcome is not None:
        print(f"workspace: {outcome.summary}", file=sys.stderr)
        print(
            f"logged {outcome.search_id} -> {outcome.paths['results']}",
            file=sys.stderr,
        )
    elif search_log:
        SearchLog(search_log).append(
            _search_log_entry(args, settings, result, requested)
        )
        print(f"appended to search log {search_log}", file=sys.stderr)
    return 1 if attempted and len(failed) == len(attempted) else 0


def _search_log_entry(
    args: argparse.Namespace,
    settings: Settings,
    result: pipeline.PipelineResult,
    requested: list[str],
) -> dict[str, Any]:
    """Build the same log entry a workspace would, for a standalone log."""
    params = SearchParams(
        query=args.query,
        sources=tuple(requested),
        max_results=args.max if args.max is not None else settings.max_results,
        sort=args.sort,
        field=args.field or "any",
        year_from=args.year_from,
        year_to=args.year_to,
        min_score=args.min_score,
        top_ratio=args.top_ratio,
        min_citations=getattr(args, "min_citations", None),
        round=1,
        channel="cli",
    )
    return log_entry_for(params, result)


def _append_search_log(
    path: str, args: argparse.Namespace, result: pipeline.PipelineResult,
    requested: list[str],
) -> None:
    """Backwards-compatible wrapper around :class:`SearchLog` (tests/tools)."""
    SearchLog(path).append(_search_log_entry(args, Settings(), result, requested))


def _requested_sources(sources: list[str] | None) -> list[str]:
    if not sources:
        return list(DEFAULT_SOURCES)
    resolved: list[str] = []
    for name in sources:
        if name not in resolved:
            resolved.append(name)
    return resolved


def _first_author(paper: Paper) -> str:
    if not paper.authors:
        return ""
    name = paper.authors[0].name
    return f"{name} et al." if len(paper.authors) > 1 else name


def _truncate(text: str, width: int) -> str:
    text = _line(text)
    if len(text) <= width:
        return text
    return text[: width - 3].rstrip() + "..."


def _best_id(paper: Paper) -> str:
    return paper.doi or paper.arxiv_id or paper.pmid or paper.pmcid or paper.isbn


def _search_table(papers: list[Paper]) -> str:
    rows = [
        [
            "" if paper.year is None else str(paper.year),
            _first_author(paper),
            _truncate(paper.title, 70),
            ", ".join(paper.sources),
            _best_id(paper),
        ]
        for paper in papers
    ]
    return _table(["YEAR", "AUTHOR", "TITLE", "SOURCES", "ID"], rows)


# ---------------------------------------------------------------------- #
# get
# ---------------------------------------------------------------------- #


def cmd_get(args: argparse.Namespace, settings: Settings) -> int:
    """Resolve one identifier and print a detail view (or its ``to_dict()``)."""
    paper = pipeline.get(args.identifier, source=args.source, settings=settings)
    if args.format == "json":
        print(json.dumps(paper.to_dict(), indent=2, ensure_ascii=False, sort_keys=False))
    else:
        print(_detail_view(paper))
    return 0


def _detail_view(paper: Paper) -> str:
    rows: list[list[Any]] = [
        ["title", paper.title],
        ["authors", ", ".join(author.name for author in paper.authors)],
        ["year", "" if paper.year is None else str(paper.year)],
        ["venue", paper.venue],
        ["url", paper.url],
        ["pdf_url", paper.pdf_url],
        [
            "citations",
            "" if paper.citation_count is None else str(paper.citation_count),
        ],
    ]
    # One line per identifier, so nothing is hidden behind a collapsed tuple.
    for key, value in paper.ids.items():
        rows.append(["ids", f"{key}: {value}"])

    out = _table(["FIELD", "VALUE"], rows)
    if paper.abstract:
        out += "\n\nabstract\n" + textwrap.fill(_line(paper.abstract), width=88)
    return out


# ---------------------------------------------------------------------- #
# download
# ---------------------------------------------------------------------- #


def cmd_download(args: argparse.Namespace, settings: Settings) -> int:
    """Fetch a PDF per identifier; keep going after individual failures."""
    failures = 0
    for identifier in args.identifiers:
        try:
            paper = pipeline.get(identifier, source=args.source, settings=settings)
            result = download.download_pdf(
                paper,
                args.output_dir,
                overwrite=args.overwrite,
                settings=settings,
            )
        except ConfigurationError:
            # Affects every identifier (e.g. no Unpaywall email) — stop at once.
            raise
        except ResearcherError as exc:
            failures += 1
            print(
                f"{identifier}: {type(exc).__name__}: {_line(exc)}", file=sys.stderr
            )
            continue

        print(result.path)  # stdout: one saved path per line
        print(f"{identifier}: via {result.via}", file=sys.stderr)
    return 1 if failures else 0


# ---------------------------------------------------------------------- #
# references / citations
# ---------------------------------------------------------------------- #

#: Renderers shared by every subcommand that emits a citation list.
_CITE_RENDERERS = {
    # Only Harvard has an "Available at ... (Accessed: ...)" line, so only it
    # takes the access date; the other three renderers take papers only.
    "harvard": lambda papers, **kw: bib.render_harvard(
        papers, accessed=kw.get("accessed", "")
    ),
    "bibtex": lambda papers, **_kw: bib.render_bibtex(papers),
    "ris": lambda papers, **_kw: bib.render_ris(papers),
    "csl": lambda papers, **_kw: bib.render_csl_json(papers),
    "json": lambda papers, **kw: json.dumps(
        [p.to_dict() for p in papers], indent=2, ensure_ascii=False
    )
    + "\n",
    "jsonl": lambda papers, **kw: "".join(
        json.dumps(p.to_dict(), ensure_ascii=False) + "\n" for p in papers
    ),
}


def _emit(papers: list[Paper], args: argparse.Namespace) -> None:
    """Write a result set in ``--format``, honouring ``--out``."""
    if args.format == "table":
        text = _search_table(papers)
    else:
        text = _CITE_RENDERERS[args.format](papers, accessed=getattr(args, "accessed", ""))
    if getattr(args, "out", None):
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"wrote {args.out} ({len(papers)} records)", file=sys.stderr)
    else:
        print(text)


def _citation_command(args: argparse.Namespace, settings: Settings, which: str) -> int:
    """Shared body of ``references`` and ``citations``."""
    paper = pipeline.get(args.identifier, source=args.get_source, settings=settings)
    run = citation.references if which == "references" else citation.citations
    result = run(
        paper,
        sources=args.sources,
        strict=args.strict,
        limit=args.max,
        order=args.order,
        settings=settings,
    )

    _emit(result.papers, args)

    for name, error in result.errors.items():
        print(f"{name}: {type(error).__name__}: {_line(error)}", file=sys.stderr)

    # `truncated` is reported, never hidden: a capped answer that looks
    # complete is the failure mode this command exists to avoid.
    capped = [name for name, flag in (result.truncated or {}).items() if flag]
    if capped:
        print(f"truncated at the per-source limit: {', '.join(capped)}", file=sys.stderr)

    print(
        f"{len(result.papers)} {'record' if len(result.papers) == 1 else 'records'} "
        f"from {which} of {paper.primary_id() or paper.title}",
        file=sys.stderr,
    )
    return 0


def cmd_references(args: argparse.Namespace, settings: Settings) -> int:
    """List the works a paper cites."""
    return _citation_command(args, settings, "references")


def cmd_citations(args: argparse.Namespace, settings: Settings) -> int:
    """List the works that cite a paper."""
    return _citation_command(args, settings, "citations")


# ---------------------------------------------------------------------- #
# bibliography
# ---------------------------------------------------------------------- #


def cmd_bibliography(args: argparse.Namespace, settings: Settings) -> int:
    """Render a reference list from identifiers, a query, or a saved result file.

    ``--from-file`` reads a JSON/JSONL result set written by ``search``, so a
    reference list can be regenerated from what was actually screened rather
    than from a second, non-reproducible live search.
    """
    from ..export import from_json, from_jsonl

    workspace_path = getattr(args, "workspace", None)
    if workspace_path and args.from_file:
        raise ConfigurationError(
            "give --workspace or --from-file, not both"
        )

    errors: dict[str, str] = {}
    if workspace_path:
        papers = Workspace(workspace_path, settings=settings).papers
    elif args.from_file:
        papers = _load_result_file(args.from_file)
    elif args.identifiers:
        papers = []
        for identifier in args.identifiers:
            try:
                papers.append(
                    pipeline.get(identifier, source=args.get_source, settings=settings)
                )
            except ResearcherError as exc:
                errors[identifier] = _line(exc)
    elif args.query:
        result = pipeline.search(
            args.query,
            sources=args.sources,
            max_results=args.max,
            settings=settings,
        )
        papers = result.papers
        errors = {name: _line(err) for name, err in result.errors.items()}
    else:
        raise ConfigurationError(
            "give identifiers, --query TEXT, or --from-file FILE"
        )

    _emit(papers, args)

    for identifier, message in errors.items():
        print(f"{identifier}: {message}", file=sys.stderr)
    print(f"{len(papers)} references", file=sys.stderr)
    return 1 if errors and not papers else 0


def _load_result_file(path: str) -> list[Paper]:
    """Read a JSON envelope or JSONL stream back into ``Paper`` objects."""
    from ..export import from_json, from_jsonl

    import os

    text = ""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise ConfigurationError(f"cannot read {path}: {exc}") from exc
    stripped = text.lstrip()
    if not stripped:
        return []
    if path.endswith(".jsonl"):
        return from_jsonl(io.StringIO(text))
    return from_json(io.StringIO(text))


# ---------------------------------------------------------------------- #
# matrix
# ---------------------------------------------------------------------- #


def cmd_matrix(args: argparse.Namespace, settings: Settings) -> int:
    """Emit the Literature Matrix, the Synthesis Matrix, or the screening summary."""
    kind = getattr(args, "kind", "literature")
    papers = _matrix_input(args, settings)
    require = getattr(args, "require_screening", False)

    if kind == "summary":
        counts = matrix.screen_summary(papers)
        if args.format == "json":
            print(json.dumps(counts, indent=2))
        else:
            print(_table(["LABEL", "COUNT"], [[k, str(v)] for k, v in counts.items()]))
        return 0

    # Rendered first: --require-screening raises here, before anything is
    # written to stdout, so a refused matrix leaves no half-output behind.
    if kind == "literature":
        columns = _columns(getattr(args, "columns", None))
        text = (
            matrix.render_matrix_markdown(
                papers, columns, require_screening=require
            )
            if args.format == "markdown"
            else matrix.render_matrix_csv(papers, columns, require_screening=require)
        )
    else:
        dimension = getattr(args, "dimension", "year")
        text = (
            matrix.render_synthesis_markdown(
                papers, dimension, require_screening=require
            )
            if args.format == "markdown"
            else matrix.render_synthesis_csv(
                papers, dimension, require_screening=require
            )
        )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"wrote {args.out} ({len(papers)} rows)", file=sys.stderr)
    else:
        print(text, end="" if text.endswith("\n") else "\n")

    # The gate is off by default, so without --require-screening the set says
    # out loud whether it is actually a screened matrix.
    if not require:
        report = matrix.screening_report(papers)
        if not report["ready"]:
            print(f"note: {report['message']}", file=sys.stderr)
    return 0


def _columns(raw: str | None) -> tuple[str, ...] | None:
    """Parse ``--columns a,b,c``; ``None`` keeps the default set.

    An unknown name is a *usage* error, so it raises ConfigurationError
    (exit 2) rather than the ExportError the renderer itself would raise.
    """
    if not raw:
        return None
    names = tuple(x.strip() for x in raw.split(",") if x.strip())
    if not names:
        return None
    known = matrix.column_names()
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ConfigurationError(
            f"unknown matrix column(s) {', '.join(unknown)}; "
            f"available: {', '.join(known)}"
        )
    return names


def cmd_synthesis(args: argparse.Namespace, settings: Settings) -> int:
    """The Synthesis Matrix (see :func:`cmd_matrix`)."""
    args.kind = "synthesis"
    return cmd_matrix(args, settings)


def cmd_summary(args: argparse.Namespace, settings: Settings) -> int:
    """Screening counts per triage label (see :func:`cmd_matrix`)."""
    args.kind = "summary"
    return cmd_matrix(args, settings)


def _matrix_input(args: argparse.Namespace, settings: Settings) -> list[Paper]:
    workspace_path = getattr(args, "workspace", None)
    if workspace_path and args.from_file:
        raise ConfigurationError(
            "give --workspace or --from-file, not both"
        )
    if workspace_path:
        return Workspace(workspace_path, settings=settings).papers
    if args.from_file:
        return _load_result_file(args.from_file)
    if args.identifiers:
        papers = []
        for identifier in args.identifiers:
            papers.append(
                pipeline.get(identifier, source=args.get_source, settings=settings)
            )
        return papers
    if args.query:
        return pipeline.search(
            args.query, sources=args.sources, max_results=args.max, settings=settings
        ).papers
    raise ConfigurationError("give identifiers, --query TEXT, or --from-file FILE")


def _read_batch(path: str) -> list[dict[str, Any]]:
    """Read a batch file: a JSON array/object, or one JSON object per line."""
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"cannot read {path}: {exc}") from exc
    if not text.strip():
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ConfigurationError(
                    f"{path}:{lineno}: invalid JSON in batch: {exc}"
                ) from exc
    if isinstance(payload, Mapping):
        if isinstance(payload.get("updates"), list):
            payload = payload["updates"]
        else:
            payload = [payload]
    if not isinstance(payload, list):
        raise ConfigurationError(
            f"{path}: a batch must be a JSON array or one object per line"
        )
    items: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise ConfigurationError(
                f"{path}: every batch entry must be an object, got "
                f"{type(item).__name__}"
            )
        items.append(dict(item))
    return items


def _print_label_result(result: Mapping[str, Any], path: str) -> None:
    summary = result.get("summary") or {}
    print(
        _table(
            ["LABEL", "COUNT"],
            [[key, str(value)] for key, value in summary.items()],
        )
    )
    print(f"{result.get('labelled', 0)} record(s) labelled in {path}", file=sys.stderr)
    unmatched = result.get("unmatched") or []
    if unmatched:
        print("unmatched ids: " + ", ".join(str(item) for item in unmatched), file=sys.stderr)
    invalid = result.get("invalid") or []
    for item in invalid:
        print(
            f"update {item.get('index')} ({item.get('id')}): {item.get('error')}",
            file=sys.stderr,
        )


def cmd_screen(args: argparse.Namespace, settings: Settings) -> int:
    """Set screening labels / append notes, one record or a whole batch.

    The verb is ``screen``; the stored field is ``label``.  ``label`` remains
    a parser alias so existing scripts keep working.  With ``--batch`` the
    file is read once and applied in one locked write, which is what makes
    screening a few hundred records practical.
    """
    workspace_path = getattr(args, "workspace", None)
    from_file = getattr(args, "from_file", None)
    if bool(workspace_path) == bool(from_file):
        raise ConfigurationError("give exactly one of a FILE or --workspace DIR")
    if workspace_path and getattr(args, "out", None):
        raise ConfigurationError("--out is for a standalone FILE, not --workspace")

    batch = getattr(args, "batch", None)
    if batch:
        if args.set or args.note or args.reason or args.id:
            raise ConfigurationError(
                "--batch cannot be combined with --set/--note/--reason/--id"
            )
        raw_updates = _read_batch(batch)
        if not raw_updates:
            raise ConfigurationError(f"{batch} holds no label updates")
    else:
        if not args.set and not args.note:
            raise ConfigurationError("give --set and/or --note, or --batch FILE")
        raw_updates = [
            LabelUpdate(
                ids=tuple(args.id or ()),
                label=args.set,
                note=args.note,
                reason=args.reason or "",
                all_records=not args.id,
            )
        ]

    strict = not getattr(args, "lenient", False)

    if workspace_path:
        workspace = Workspace(workspace_path, settings=settings)
        result = workspace.label_many(raw_updates, strict=strict)
        _print_label_result(result, str(workspace.path))
        if not result["labelled"] and (result["unmatched"] or result["invalid"]):
            return 1
        return 0

    papers = _load_result_file(from_file)  # type: ignore[arg-type]
    if not papers:
        raise ConfigurationError(f"{from_file} holds no records")
    outcome = apply_updates(papers, raw_updates, strict=strict)
    target = str(args.out or from_file)
    export.to_json(papers, target)
    summary = matrix.screen_summary(papers)
    _print_label_result(
        {
            "labelled": outcome.labelled,
            "total": len(papers),
            "summary": summary,
            "unmatched": outcome.unmatched,
            "invalid": outcome.invalid,
        },
        str(target),
    )
    if not outcome.labelled and (outcome.unmatched or outcome.invalid):
        return 1
    return 0


#: Compatibility name: the command was ``label`` before the verb was aligned
#: with the "screen" step in the workflow.
cmd_label = cmd_screen


def _prescreen_criteria(args: argparse.Namespace) -> PrescreenCriteria:
    raw_types = getattr(args, "exclude_types", None)
    raw_rules = getattr(args, "rules", None)
    return PrescreenCriteria(
        year_from=getattr(args, "year_from", None),
        year_to=getattr(args, "year_to", None),
        exclude_types=(
            tuple(item.strip() for item in raw_types.split(",") if item.strip())
            if raw_types
            else PrescreenCriteria().exclude_types
        ),
        require_abstract=not getattr(args, "allow_no_abstract", False),
        require_title=not getattr(args, "allow_no_title", False),
        rules=(
            tuple(item.strip() for item in raw_rules.split(",") if item.strip())
            if raw_rules
            else None
        ),
    )


def cmd_prescreen(args: argparse.Namespace, settings: Settings) -> int:
    """Flag obvious exclusions by rule; apply only with ``--apply``."""
    workspace_path = getattr(args, "workspace", None)
    from_file = getattr(args, "from_file", None)
    if bool(workspace_path) == bool(from_file):
        raise ConfigurationError("give exactly one of a FILE or --workspace DIR")
    criteria = _prescreen_criteria(args)

    if workspace_path:
        workspace = Workspace(workspace_path, settings=settings)
        payload = workspace.prescreen(
            apply=getattr(args, "apply", False),
            criteria=criteria,
            limit=getattr(args, "limit", 50),
        )
    else:
        papers = _load_result_file(from_file)  # type: ignore[arg-type]
        report = prescreen(papers, criteria)
        applied = 0
        if getattr(args, "apply", False):
            for hit in report.hits:
                if hit.paper.triage_label() != "unreviewed":
                    continue
                hit.paper.label_as("excluded", "pre-screen: " + hit.reason)
                applied += 1
            export.to_json(papers, str(args.out or from_file))
        payload = report.as_dict(limit=getattr(args, "limit", 50))
        payload["applied"] = applied
        payload["summary"] = matrix.screen_summary(papers)

    if getattr(args, "format", "table") == "json":
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    else:
        rows = [[rule, str(count)] for rule, count in payload["by_rule"].items()]
        text = _table(["RULE", "FLAGGED"], rows) + "\n"
        for hit in payload["candidates"]:
            text += (
                f"{hit['id'] or '-'}  {hit.get('year') or '-'}  "
                f"{hit['title']}  [{'; '.join(hit['reasons'])}]\n"
            )
        if payload["truncated"]:
            text += f"... {payload['truncated']} more\n"
        text += (
            f"considered {payload['considered']}, flagged {payload['flagged']}, "
            f"applied {payload['applied']}\n"
        )
    out = getattr(args, "out", None)
    # With --apply and a standalone file, --out is the rewritten result set;
    # writing the report there too would overwrite the JSON.  In every other
    # case --out is the report itself.
    report_to_out = bool(out) and (
        bool(workspace_path) or not getattr(args, "apply", False)
    )
    if report_to_out and out is not None:
        Path(out).expanduser().write_text(text, encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    else:
        print(text, end="")
    return 0


def cmd_snowball(args: argparse.Namespace, settings: Settings) -> int:
    """Expand the core set through references/citations, ranked by link count."""
    workspace_path = getattr(args, "workspace", None)
    if not workspace_path:
        raise ConfigurationError("snowball needs --workspace DIR (a core set and a log)")
    workspace = Workspace(workspace_path, settings=settings)
    payload = workspace.expand(
        direction=getattr(args, "direction", "both"),
        ids=getattr(args, "id", None),
        core_label=getattr(args, "core_label", "core"),
        limit=getattr(args, "limit", None),
        min_links=getattr(args, "min_links", 1),
        max_results=getattr(args, "max_results", 25),
        persist=not getattr(args, "no_persist", False),
    )
    text = (
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        if getattr(args, "format", "table") == "json"
        else _table(
            ["LINKS", "YEAR", "ID", "TITLE"],
            [
                [
                    str(item["links"]),
                    str(item.get("year") or "-"),
                    item.get("id") or "-",
                    item["title"],
                ]
                for item in payload["candidates"]
            ],
        )
        + "\n"
        + (
            f"core {payload['core_count']}, fetched {payload['fetched']}, "
            f"new {payload['new']}, known {payload['known']}, "
            f"shown {payload['returned']}"
            + (f", {payload['truncated']} more" if payload["truncated"] else "")
            + "\n"
        )
    )
    out = getattr(args, "out", None)
    if out:
        Path(out).expanduser().write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {out}", file=sys.stderr)
    else:
        print(text, end="")
    return 0


def cmd_prisma(args: argparse.Namespace, settings: Settings) -> int:
    """Print the PRISMA-style flow derived from the workspace log and labels."""
    workspace_path = getattr(args, "path", None)
    if not workspace_path:
        raise ConfigurationError("prisma needs a workspace PATH")
    workspace = Workspace(workspace_path, settings=settings, create=False)
    if not workspace.exists():
        raise ConfigurationError(f"{workspace.path} is not a workspace")
    report = workspace.prisma()
    text = (
        json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        if getattr(args, "format", "json") == "json"
        else render_prisma_markdown(report)
    )
    out = getattr(args, "out", None)
    if out:
        Path(out).expanduser().write_text(text, encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    else:
        print(text, end="")
    return 0


def cmd_handoff(args: argparse.Namespace, settings: Settings) -> int:
    """Write a versioned research bundle for a paper-writing tool."""
    raw_labels = getattr(args, "labels", "core,general") or "core,general"
    labels = tuple(item.strip() for item in raw_labels.split(",") if item.strip())
    receipt = build_handoff(
        args.source,
        args.out,
        settings=settings,
        labels=labels,
        require_screening=getattr(args, "require_screening", True),
        include_prisma=getattr(args, "include_prisma", True),
        force=getattr(args, "force", False),
    )
    if getattr(args, "format", "json") == "json":
        print(json.dumps(receipt, indent=2, ensure_ascii=False))
        return 0

    counts = receipt["counts"]
    print(f"handoff: {receipt['out_dir']}")
    print(f"included: {counts['included']} of {counts['records']} records")
    print(
        "screening: "
        + ("ready" if receipt["screening"]["ready"] else "incomplete")
    )
    for name, path in receipt["files"].items():
        print(f"  {name}: {path}")
    return 0


# ---------------------------------------------------------------------- #
# workspace (state + provenance)
# ---------------------------------------------------------------------- #


def cmd_workspace(args: argparse.Namespace, settings: Settings) -> int:
    """Create, inspect, log, round, export or import a workspace."""
    action = getattr(args, "workspace_command", None)

    if action == "init":
        workspace = Workspace(args.path, settings=settings, create=False)
        if not workspace.exists():
            workspace.create(topic=args.topic)
            print(f"created {workspace.path}", file=sys.stderr)
        elif args.topic:
            workspace.meta["topic"] = args.topic
            workspace._write_meta(workspace.meta)
        print(json.dumps(workspace.status(), indent=2, ensure_ascii=False))
        return 0

    if action == "status":
        workspace = Workspace(args.path, settings=settings, create=False)
        status = workspace.status()
        if args.format == "json":
            print(json.dumps(status, indent=2, ensure_ascii=False))
        else:
            _print_workspace_status(status)
        return 0 if status["exists"] else 1

    if action == "log":
        workspace = Workspace(args.path, settings=settings, create=False)
        entries = workspace.log_entries()
        if getattr(args, "round", None) is not None:
            entries = [
                entry
                for entry in entries
                if int(entry.get("round", 1)) == args.round
            ]
        if args.format == "md":
            print(render_log_markdown(entries), end="")
        elif args.format == "jsonl":
            for entry in entries:
                print(json.dumps(entry, ensure_ascii=False))
        else:
            print(json.dumps(entries, indent=2, ensure_ascii=False))
        return 0

    if action == "round":
        workspace = Workspace(args.path, settings=settings, create=True)
        if args.next:
            number = workspace.start_round(args.label or "")
        else:
            number = workspace.set_round(args.set, args.label or "")
        print(f"round {number}")
        return 0

    if action == "export":
        workspace = Workspace(args.path, settings=settings, create=False)
        target = Path(args.out)
        if args.format == "jsonl":
            export.to_jsonl(workspace.papers, target)
        else:
            workspace.export_papers(target)
        print(
            f"wrote {target} ({len(workspace.papers)} records)",
            file=sys.stderr,
        )
        return 0

    if action == "import":
        report = import_workspace(
            args.path,
            args.results,
            log_file=args.log,
            round=args.round,
            topic=args.topic,
            settings=settings,
            dry_run=args.dry_run,
            mode=args.mode,
        )
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    raise ConfigurationError(f"unknown workspace action {action!r}")


def _print_workspace_status(status: dict[str, Any]) -> None:
    if not status.get("exists"):
        print(f"{status['path']}: not a workspace", file=sys.stderr)
        return
    counts = status["counts"]
    provenance = status.get("provenance") or {}
    print(f"workspace: {status['path']}")
    print(f"topic: {status['topic'] or '-'}")
    print(f"round: {status['round']} ({status['round_label']})")
    print(f"records: {counts['records']}")
    print(
        f"screened: {counts['screened']} "
        f"(core {counts['core']}, general {counts['general']}, "
        f"excluded {counts['excluded']})"
    )
    print(f"searches: {status['searches']} ({status['log_rows']} log rows)")
    if provenance.get("legacy"):
        print(
            f"provenance: legacy import; logged {provenance.get('logged_kept')}, "
            f"unaccounted {provenance.get('unaccounted')}, "
            f"no-log {provenance.get('no_log_records', 0)}"
        )
    else:
        print(
            f"provenance: logged {provenance.get('logged_records', 0)}, "
            f"unaccounted {provenance.get('unaccounted', 0)}"
        )
    last = status.get("last_search")
    if last:
        print(
            f"last search: {last['query']} — {last['new']} new, "
            f"{last['seen']} seen, {last['screened']} screened"
        )
    print(f"results: {status['files']['results']}")
    print(f"log: {status['files']['log']}")


# ---------------------------------------------------------------------- #
# web-search
# ---------------------------------------------------------------------- #


def cmd_web_search(args: argparse.Namespace, settings: Settings) -> int:
    """Search a web source (Stack Overflow / Reddit) and cite the records."""
    from ..web_sources import web_search as _web_search
    from ..web_sources import format as web_format

    kwargs: dict[str, Any] = {}
    if args.site:
        kwargs["site"] = args.site
    if args.subreddit:
        kwargs["subreddit"] = args.subreddit
    if args.time:
        kwargs["time_range"] = args.time

    result = _web_search(
        args.query,
        source=args.source,
        max_results=args.max,
        sort=args.sort,
        settings=settings,
        **kwargs,
    )
    records = result.results

    if args.format == "table":
        rows = [
            [r.score, web_format.web_date(r), " ".join(f"#{t}" for t in r.tags), _truncate(r.title, 70), r.url]
            for r in records
        ]
        text = _table(["SCORE", "DATE", "TAGS", "TITLE", "URL"], rows)
    elif args.format == "json":
        text = json.dumps([r.to_dict() for r in records], indent=2, ensure_ascii=False) + "\n"
    else:
        renderer = {
            "bibtex": web_format.render_web_bibtex,
            "ris": web_format.render_web_ris,
            "csl": web_format.render_web_csl_json,
        }[args.format]
        text = renderer(records, accessed=args.accessed or "")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"wrote {args.out} ({len(records)} records)", file=sys.stderr)
    else:
        print(text, end="" if text.endswith("\n") else "\n")
    return 0


# ---------------------------------------------------------------------- #
# extract (PDF text)
# ---------------------------------------------------------------------- #


def cmd_extract(args: argparse.Namespace, settings: Settings) -> int:
    """Pull text or page-anchored quotes out of a PDF (or every PDF in a dir)."""
    from .. import extract

    targets = _expand_targets(args.paths)
    if not targets:
        raise ConfigurationError("no readable PDFs among the given paths")

    captured: list[dict[str, Any]] = []
    failures = 0
    for path in targets:
        try:
            if args.quotes:
                quotes = extract.find_quotes(
                    path,
                    terms=args.terms,
                    window=args.window,
                    limit=args.limit,
                )
                captured.append({"path": path, "quotes": quotes})
                print(f"{path}: {len(quotes)} quotes", file=sys.stderr)
            else:
                text = extract.extract_text(path, pages=args.pages)
                if args.out:
                    with open(args.out, "a", encoding="utf-8") as handle:
                        handle.write(text)
                else:
                    print(text)
                print(f"{path}: {len(text)} chars", file=sys.stderr)
        except ResearcherError as exc:
            failures += 1
            print(f"{path}: {type(exc).__name__}: {_line(exc)}", file=sys.stderr)

    if args.quotes:
        payload = json.dumps(captured, indent=2, ensure_ascii=False)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(payload + "\n")
            print(f"wrote {args.out}", file=sys.stderr)
        else:
            print(payload)
    return 1 if failures else 0


def _expand_targets(paths: list[str]) -> list[str]:
    """Expand any directory into the ``*.pdf`` files inside it (one level)."""
    import os

    out: list[str] = []
    for path in paths:
        if os.path.isdir(path):
            out.extend(
                sorted(
                    os.path.join(path, name)
                    for name in os.listdir(path)
                    if name.lower().endswith(".pdf")
                )
            )
        else:
            out.append(path)
    return out
