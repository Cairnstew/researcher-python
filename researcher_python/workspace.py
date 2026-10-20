"""A workspace owns a result set, its provenance log and its metadata.

The library's search pipeline is deliberately pure: :func:`pipeline.search`
performs network I/O and returns records, but writes nothing.  A
:class:`Workspace` is the *stateful* layer on top.  It exists so the CLI, the
MCP server and an embedding application cannot disagree about where a result
set lives or how a search is recorded.

Every write goes through :meth:`Workspace.record_search`:

* the records are merged into ``results.json`` (new vs already-seen);
* one append-only entry is written to ``search-log.jsonl`` with *all* search
  parameters, per-source counts and per-source errors;
* ``search-log.md`` is regenerated from that JSONL (one renderer, so the
  header and the rows cannot drift apart — the exact bug this module replaces);
* ``workspace.json`` is updated with counts, the current round and the last
  search.

Nothing here logs credentials.  ``Settings`` is used for paths and defaults
only; the recorded params are the search request, not the environment.

Layout::

    <workspace>/
      workspace.json
      results.json
      search-log.jsonl
      search-log.md
      rounds/round-N/{results.json,search-log.md}
      pdf/
      .lock
      pending.json        # short-lived crash journal

A workspace path is explicit.  An absolute path is used as-is; a relative path
resolves under ``RESEARCHER_HOME`` when it is set, and only falls back to the
process cwd for backwards compatibility.  That is what lets a globally
registered MCP server work from an Obsidian vault.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import export, pipeline
from . import __version__
from .config import Settings, load_settings
from .errors import ConfigurationError, ResearcherError, SourceError, WorkspaceError
from .models import TRIAGE_LABELS, MergeOutcome, Paper, merge_sets
from .prisma import prisma_report
from .screening import (
    LabelUpdate,
    PrescreenCriteria,
    apply_updates,
    index_papers,
    prescreen,
)
from .snowball import expand as snowball_expand
from .sources import DEFAULT_SOURCES

try:  # POSIX (Linux/macOS).  A no-op elsewhere; the package targets both.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX only
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "LOG_JSONL",
    "LOG_MD",
    "META_FILE",
    "RESULTS_FILE",
    "WORKSPACE_SCHEMA_VERSION",
    "SearchLog",
    "SearchParams",
    "Workspace",
    "WorkspaceSearch",
    "append_jsonl",
    "import_workspace",
    "log_entry_for",
    "parse_legacy_log",
    "render_log_markdown",
    "resolve_workspace_path",
]

#: Bump only when the on-disk workspace shape changes incompatibly.
WORKSPACE_SCHEMA_VERSION = 1

META_FILE = "workspace.json"
RESULTS_FILE = "results.json"
LOG_JSONL = "search-log.jsonl"
LOG_MD = "search-log.md"
PENDING_FILE = "pending.json"


# ---------------------------------------------------------------------- #
# Small helpers
# ---------------------------------------------------------------------- #


def _text(value: Any) -> str:
    return " ".join(str(value).split())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        match = re.search(r"-?\d+", value)
        return int(match.group()) if match else None
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        return float(match.group()) if match else None
    return None


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON via a sibling temp file, then ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp"
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise WorkspaceError(f"cannot read {path}: {exc}", cause=exc) from exc
    except json.JSONDecodeError as exc:
        raise WorkspaceError(f"invalid JSON in {path}: {exc}", cause=exc) from exc
    if not isinstance(payload, dict):
        raise WorkspaceError(
            f"{path} must contain a JSON object, got {type(payload).__name__}"
        )
    return payload


def _read_log_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    entry = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise WorkspaceError(
                        f"{path}:{lineno}: invalid JSON in search log: {exc}",
                        cause=exc,
                    ) from exc
                if not isinstance(entry, dict):
                    raise WorkspaceError(
                        f"{path}:{lineno}: search log entry is not an object"
                    )
                entries.append(entry)
    except OSError as exc:
        raise WorkspaceError(f"cannot read {path}: {exc}", cause=exc) from exc
    return entries


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp"
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------- #
# Path resolution
# ---------------------------------------------------------------------- #


def resolve_workspace_path(
    path: str | os.PathLike[str], settings: Settings | None = None
) -> Path:
    """Resolve ``path`` to an absolute workspace directory.

    Absolute paths and ``~`` win.  A relative path resolves under
    ``settings.home`` (``RESEARCHER_HOME``) when configured; otherwise it
    resolves against the process cwd, preserving the pre-workspace behaviour.
    The result is always absolute, so a later status/log call can show exactly
    which directory was written.
    """
    raw = str(path).strip()
    if not raw:
        raise ConfigurationError("workspace path is empty")
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()

    settings = settings if settings is not None else load_settings()
    if settings.home is not None:
        return (Path(settings.home).expanduser() / candidate).resolve()
    return candidate.resolve()


# ---------------------------------------------------------------------- #
# Params and outcome
# ---------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SearchParams:
    """The canonical, JSON-able description of one search request."""

    query: str
    sources: tuple[str, ...] = ()
    max_results: int | None = None
    sort: str = "relevance"
    field: str = "any"
    year_from: int | None = None
    year_to: int | None = None
    min_score: float | None = None
    top_ratio: float | None = None
    min_citations: int | None = None
    round: int | None = None
    channel: str = "library"

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "sources": list(self.sources),
            "max_results": self.max_results,
            "sort": self.sort,
            "field": self.field,
            "year_from": self.year_from,
            "year_to": self.year_to,
            "min_score": self.min_score,
            "top_ratio": self.top_ratio,
            "min_citations": self.min_citations,
            "round": self.round,
            "channel": self.channel,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SearchParams":
        raw_sources = data.get("sources") or ()
        if isinstance(raw_sources, str):
            sources = (raw_sources,)
        else:
            sources = tuple(str(s) for s in raw_sources)
        return cls(
            query=_text(data.get("query", "")),
            sources=sources,
            max_results=_as_int(data.get("max_results")),
            sort=str(data.get("sort") or "relevance"),
            field=str(data.get("field") or "any"),
            year_from=_as_int(data.get("year_from")),
            year_to=_as_int(data.get("year_to")),
            min_score=_as_float(data.get("min_score")),
            top_ratio=_as_float(data.get("top_ratio")),
            min_citations=_as_int(data.get("min_citations")),
            round=_as_int(data.get("round")),
            channel=str(data.get("channel") or "library"),
        )


@dataclass(slots=True)
class WorkspaceSearch:
    """One search as the workspace saw it — pipeline result plus merge counts."""

    result: pipeline.PipelineResult
    new: int
    seen: int
    screened: int
    total: int
    round: int
    search_id: str
    persisted: bool = True
    paths: dict[str, str] = field(default_factory=dict)
    #: The exact resolved request recorded in the log (sources resolved, round
    #: filled in), so a caller can show provenance without re-deriving it.
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def papers(self) -> list[Paper]:
        return self.result.papers

    @property
    def summary(self) -> str:
        """The human sentence every face prints."""
        screened = f" ({self.screened} screened)" if self.screened else ""
        return (
            f"{self.new} new, {self.seen} already seen{screened}, "
            f"{self.total} total"
        )


# ---------------------------------------------------------------------- #
# Log rendering
# ---------------------------------------------------------------------- #

#: Markdown log columns.  Header and rows are rendered from this one tuple, so
#: a header/row cell-count mismatch is structurally impossible.
_LOG_COLUMNS: tuple[tuple[str, Any], ...] = (
    ("when", lambda e: _text(e.get("at", ""))),
    ("query", lambda e: _text((e.get("params") or {}).get("query", ""))),
    ("field", lambda e: _text((e.get("params") or {}).get("field", "any"))),
    (
        "sources",
        lambda e: ", ".join(
            _text(s) for s in ((e.get("params") or {}).get("sources") or [])
        ),
    ),
    ("round", lambda e: str(e.get("round", ""))),
    ("kept", lambda e: str((e.get("outcome") or {}).get("kept", 0))),
    ("new", lambda e: str((e.get("outcome") or {}).get("new", ""))),
    ("seen", lambda e: str((e.get("outcome") or {}).get("seen", ""))),
    ("screened", lambda e: str((e.get("outcome") or {}).get("screened", ""))),
    ("dropped", lambda e: str((e.get("outcome") or {}).get("dropped", 0))),
    (
        "errors",
        lambda e: "; ".join(
            f"{name}: {(info or {}).get('type', 'error')}"
            for name, info in (e.get("errors") or {}).items()
        ),
    ),
    (
        "terms",
        lambda e: "; ".join(
            _text(t) for t in ((e.get("outcome") or {}).get("terms") or [])
        ),
    ),
)


def _cell(value: Any) -> str:
    """Escape a Markdown table cell; collapse newlines, escape pipes."""
    return _text(value).replace("|", "\\|")


def _row(entry: Mapping[str, Any]) -> str:
    return "| " + " | ".join(_cell(getter(entry)) for _, getter in _LOG_COLUMNS) + " |"


def render_log_markdown(entries: Iterable[Mapping[str, Any]]) -> str:
    """Render the whole Markdown table from log entries.

    The header and every row are built by the same column tuple, which is why
    the historical 7-column-header / 6-cell-row bug cannot recur.
    """
    items = list(entries)
    header = "| " + " | ".join(name for name, _ in _LOG_COLUMNS) + " |"
    rule = "| " + " | ".join("---" for _ in _LOG_COLUMNS) + " |"
    rows = [_row(entry) for entry in items]
    return "\n".join([header, rule, *rows]) + "\n"


def append_jsonl(path: str | os.PathLike[str], entry: Mapping[str, Any]) -> None:
    """Append one JSON object as a line (the durable provenance record)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False, sort_keys=False) + "\n"
    try:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError as exc:
        raise WorkspaceError(
            f"could not append to {target}: {exc}", cause=exc
        ) from exc


def append_markdown(path: str | os.PathLike[str], entry: Mapping[str, Any]) -> None:
    """Append one rendered row, writing the header only for an empty file.

    Used by the legacy ``--search-log FILE`` path, which has no JSONL backing
    store.  The row renderer is the same one a workspace uses.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        new_file = not target.exists() or target.stat().st_size == 0
        with open(target, "a", encoding="utf-8") as handle:
            if new_file:
                handle.write(render_log_markdown([]))
            handle.write(_row(entry) + "\n")
    except OSError as exc:
        raise WorkspaceError(
            f"could not append to {target}: {exc}", cause=exc
        ) from exc


class SearchLog:
    """A standalone log file, for callers that do not have a full workspace.

    ``jsonl=True`` writes the machine log; ``markdown=True`` writes the human
    table.  A workspace uses the module functions directly because it also
    needs to regenerate the Markdown from the JSONL; a bare ``--search-log``
    uses this class and writes Markdown only.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        jsonl: bool = False,
        markdown: bool = True,
    ) -> None:
        self.path = Path(path)
        self.jsonl = jsonl
        self.markdown = markdown

    def append(self, entry: Mapping[str, Any]) -> None:
        if self.jsonl:
            append_jsonl(self.path, entry)
        if self.markdown:
            append_markdown(self.path, entry)


# ---------------------------------------------------------------------- #
# Legacy parsing / import
# ---------------------------------------------------------------------- #


def _split_markdown_row(line: str) -> list[str]:
    """Split a Markdown table row on unescaped pipes."""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    return [
        cell.strip().replace("\\|", "|")
        for cell in re.split(r"(?<!\\)\|", text)
    ]


def parse_legacy_log(text: str) -> list[dict[str, Any]]:
    """Parse a pre-workspace Markdown round log, tolerating its old shape.

    The historical writer emitted a 7-column header but 6-cell rows, with the
    field folded into the query cell as ``... `field=title` ``.  This parser
    accepts either shape and produces normal log entries with ``legacy: true``
    so imported provenance is distinguishable from workspace-written rows.
    """
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = _split_markdown_row(line)
        if not cells:
            continue
        if cells[0].lower() == "when":
            continue
        if all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells):
            continue
        if len(cells) < 4:
            continue

        when = cells[0]
        field = "any"
        if len(cells) >= 6 and "`field=" in cells[1]:
            match = re.search(r"`field=([^`]+)`", cells[1])
            field = match.group(1) if match else "any"
            query = cells[1][: match.start()].strip() if match else cells[1]
            sources = cells[2]
            kept, dropped = cells[3], cells[4]
            terms = cells[5]
        elif len(cells) >= 7:
            query, field, sources = cells[1], cells[2] or "any", cells[3]
            kept, dropped, terms = cells[4], cells[5], cells[6]
        else:
            query = cells[1]
            sources = cells[2]
            kept, dropped = cells[3], cells[4]
            terms = cells[5] if len(cells) > 5 else ""

        entries.append(
            {
                "search_id": f"legacy-{len(entries) + 1}",
                "at": when,
                "round": 1,
                "channel": "legacy",
                "legacy": True,
                "package_version": "",
                "params": {
                    "query": query,
                    "sources": [
                        s.strip() for s in sources.split(",") if s.strip()
                    ],
                    "max_results": None,
                    "sort": "relevance",
                    "field": field,
                    "year_from": None,
                    "year_to": None,
                    "min_score": None,
                    "top_ratio": None,
                    "min_citations": None,
                    "round": 1,
                    "channel": "legacy",
                },
                "outcome": {
                    "returned": _as_int(kept) or 0,
                    "kept": _as_int(kept) or 0,
                    "new": None,
                    "seen": None,
                    "screened": None,
                    "total": None,
                    "dropped": _as_int(dropped) or 0,
                    "terms": [t.strip() for t in terms.split(";") if t.strip()],
                },
                "by_source": {},
                "errors": {},
                "duration_ms": 0,
            }
        )
    return entries


def _load_any_results(
    path: str | os.PathLike[str],
) -> tuple[list[Paper], dict[str, Any]]:
    """Read ``.json`` (envelope + meta) or ``.jsonl`` (papers only)."""
    target = Path(path)
    try:
        if target.suffix.lower() == ".jsonl":
            return export.from_jsonl(target), {}
        envelope = export.read_envelope(target)
        return envelope.papers, dict(envelope.meta)
    except Exception as exc:  # noqa: BLE001 - re-raised as a workspace error
        if isinstance(exc, WorkspaceError):
            raise
        raise WorkspaceError(f"could not read {target}: {exc}", cause=exc) from exc


def import_workspace(
    destination: str | os.PathLike[str],
    results_file: str | os.PathLike[str],
    *,
    log_file: str | os.PathLike[str] | None = None,
    round: int = 1,
    topic: str = "",
    settings: Settings | None = None,
    dry_run: bool = False,
    mode: str = "snapshot",
) -> dict[str, Any]:
    """Adopt a pre-workspace result set (and optionally a legacy round log).

    ``mode="snapshot"`` treats the file as the whole set at that point;
    ``mode="merge"`` folds it into an existing workspace and reports how many
    records were new vs already seen.  Merge is the right mode for a set of
    disjoint legacy round files.

    The import is deliberately honest about gaps: it records how many records
    the log accounts for, how many the result set holds, and the difference.
    It never invents a row for an unlogged record and never rewrites the
    original files.  ``dry_run`` performs every parse and count but writes
    nothing.
    """
    if mode not in ("snapshot", "merge"):
        raise ConfigurationError(
            f"unknown import mode {mode!r}; expected 'snapshot' or 'merge'"
        )
    settings = settings if settings is not None else load_settings()
    dest = resolve_workspace_path(destination, settings)
    papers, source_meta = _load_any_results(results_file)

    entries: list[dict[str, Any]] = []
    if log_file is not None:
        log_path = Path(log_file)
        try:
            text = log_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigurationError(f"cannot read {log_path}: {exc}") from exc
        entries = parse_legacy_log(text)
        for index, entry in enumerate(entries, start=1):
            entry["round"] = int(round)
            entry["params"]["round"] = int(round)
            # Legacy logs all start at legacy-1; scope the id to the round so
            # importing several rounds cannot collide.
            entry["search_id"] = f"legacy-r{int(round)}-{index}"

    logged_kept = sum(
        int((entry.get("outcome") or {}).get("kept") or 0) for entry in entries
    )
    reconciled = bool(entries)
    unaccounted = max(0, len(papers) - logged_kept) if reconciled else None
    not_in_result = max(0, logged_kept - len(papers)) if reconciled else None

    # Work out the resulting set before a dry run returns, so the report can
    # say what a real import would do.
    current: list[Paper] = []
    if mode == "merge":
        probe = Workspace(dest, settings=settings, create=False)
        if probe.exists():
            current = probe.papers
    if mode == "merge":
        merged = merge_sets(current, papers)
        final_set = merged.papers
        new_count, seen_count = len(merged.new), len(merged.seen)
    else:
        final_set = list(papers)
        new_count, seen_count = len(papers), 0

    import_record = {
        "results_file": str(results_file),
        "log_file": str(log_file) if log_file is not None else None,
        "round": int(round),
        "mode": mode,
        "records": len(papers),
        "new": new_count,
        "seen": seen_count,
        "logged_kept": logged_kept if reconciled else None,
        "unaccounted": unaccounted,
        "not_in_result": not_in_result,
        # A file imported without a log has no provenance at all.  Report it
        # separately instead of folding it into `unaccounted`, which measures
        # only the gap *within* an imported log.
        "no_log_records": 0 if reconciled else len(papers),
    }
    report = {
        "workspace": str(dest),
        **import_record,
        "log_rows": len(entries),
        "total": len(final_set),
        "source_meta": source_meta,
        "dry_run": dry_run,
    }
    if dry_run:
        return report

    workspace = Workspace(dest, settings=settings, create=True, round=int(round))
    with workspace._locked():
        workspace.load()
        current = workspace.papers if mode == "merge" else []
        if mode == "merge":
            merged = merge_sets(current, papers)
            final_set = merged.papers
            new_count, seen_count = len(merged.new), len(merged.seen)
            import_record["new"] = new_count
            import_record["seen"] = seen_count
        else:
            # Snapshot: keep reader-owned labels from the live workspace by
            # primary id, but the imported file is otherwise authoritative.
            existing = {
                paper.primary_id(): paper
                for paper in current
                if paper.primary_id()
            }
            for paper in papers:
                prior = existing.get(paper.primary_id())
                if prior is not None and prior.triage_label() != "unreviewed":
                    paper.label_as(prior.triage_label(), prior.exclusion_reason)
                    if prior.notes:
                        paper.notes = prior.notes
            final_set = list(papers)

        now = _now()
        meta = workspace._meta or {}
        prior_provenance = dict(meta.get("provenance") or {})
        imports = list(prior_provenance.get("imports") or [])
        if mode == "snapshot":
            imports = []
        imports.append(import_record)

        def _sum(key: str) -> int | None:
            values = [
                item.get(key) for item in imports if item.get(key) is not None
            ]
            return sum(values) if values else None

        provenance = {
            "legacy": True,
            "imports": imports,
            "records": len(final_set),
            "logged_kept": _sum("logged_kept"),
            "unaccounted": _sum("unaccounted"),
            "not_in_result": _sum("not_in_result"),
            "no_log_records": _sum("no_log_records"),
            "imported_from": [
                path
                for item in imports
                for path in (
                    [item["results_file"]]
                    + ([item["log_file"]] if item.get("log_file") else [])
                )
            ],
            "note": (
                "Pre-workspace artefacts. 'unaccounted' records are present in "
                "the result set but have no search row; their query is unknown "
                "and is not fabricated. 'not_in_result' rows were logged but "
                "did not survive dedupe/filters."
            ),
        }

        meta["topic"] = topic or meta.get("topic", "")
        meta["round"] = int(round)
        meta["round_label"] = f"round {round}"
        meta["updated_at"] = now
        meta["provenance"] = provenance
        meta["counts"] = _counts(final_set)
        workspace._write_results(
            final_set,
            {
                "workspace": str(dest),
                "round": int(round),
                "imported": True,
                "source": str(results_file),
                "mode": mode,
                "reconciliation": provenance,
                "updated_at": now,
            },
        )

        existing_entries = (
            workspace.log_entries() if mode == "merge" else []
        )
        seen_ids = {
            entry.get("search_id") for entry in existing_entries
        }
        fresh = [
            entry
            for entry in entries
            if entry.get("search_id") not in seen_ids
        ]
        log_entries = existing_entries + fresh
        _write_text_atomic(
            dest / LOG_JSONL,
            "".join(
                json.dumps(entry, ensure_ascii=False) + "\n"
                for entry in log_entries
            ),
        )
        _write_text_atomic(
            dest / LOG_MD, render_log_markdown(log_entries)
        )
        meta["searches"] = len(log_entries)
        workspace._write_meta(meta)
        workspace._papers = list(final_set)
        workspace._meta = meta

    report["total"] = len(final_set)
    report["new"] = new_count
    report["seen"] = seen_count
    return report


# ---------------------------------------------------------------------- #
# Workspace
# ---------------------------------------------------------------------- #


def _counts(papers: Iterable[Paper]) -> dict[str, int]:
    """Record and screening counts for ``workspace.json``."""
    items = list(papers)
    counts: dict[str, int] = {"records": len(items), "screened": 0}
    for label in TRIAGE_LABELS:
        counts[label] = 0
    for paper in items:
        label = paper.triage_label()
        counts[label] += 1
        if label != "unreviewed":
            counts["screened"] += 1
    return counts


def _new_search_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


class Workspace:
    """A directory that owns ``results.json``, its log and its meta.

    Constructing a workspace creates it (``create=True``, the default).  Pass
    ``create=False`` to inspect an existing directory without creating one.
    All mutating methods take an exclusive ``flock`` and reload from disk
    inside the lock, so a CLI run and a long-lived MCP server cannot lose each
    other's records.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        settings: Settings | None = None,
        create: bool = True,
        round: int | None = None,
    ) -> None:
        self.settings = settings if settings is not None else load_settings()
        self.path = resolve_workspace_path(path, self.settings)
        self._papers: list[Paper] | None = None
        self._meta: dict[str, Any] | None = None
        self._results_meta: dict[str, Any] = {}
        if create and not self.exists():
            self.create()
        if round is not None and self.exists():
            self.set_round(round)

    @classmethod
    def default(
        cls, settings: Settings | None = None, *, create: bool = True
    ) -> "Workspace":
        """Open ``RESEARCHER_WORKSPACE`` as the default workspace."""
        settings = settings if settings is not None else load_settings()
        if settings.workspace is None:
            raise ConfigurationError(
                "no default workspace; set RESEARCHER_WORKSPACE or pass a path"
            )
        return cls(settings.workspace, settings=settings, create=create)

    # -- paths -----------------------------------------------------------

    @property
    def meta_path(self) -> Path:
        return self.path / META_FILE

    @property
    def results_path(self) -> Path:
        return self.path / RESULTS_FILE

    @property
    def log_jsonl_path(self) -> Path:
        return self.path / LOG_JSONL

    @property
    def log_md_path(self) -> Path:
        return self.path / LOG_MD

    @property
    def pending_path(self) -> Path:
        return self.path / PENDING_FILE

    # -- lifecycle -------------------------------------------------------

    def exists(self) -> bool:
        return self.meta_path.is_file() and self.results_path.is_file()

    def create(self, topic: str = "") -> "Workspace":
        """Create the directory and its empty stores (idempotent)."""
        self.path.mkdir(parents=True, exist_ok=True)
        if self.meta_path.exists() or self.results_path.exists():
            self.load()
            return self

        now = _now()
        meta: dict[str, Any] = {
            "schema_version": WORKSPACE_SCHEMA_VERSION,
            "kind": "researcher-workspace",
            "path": str(self.path),
            "created_at": now,
            "updated_at": now,
            "topic": topic,
            "round": 1,
            "round_label": "round 1",
            "counts": _counts([]),
            "searches": 0,
            "last_search": None,
            "provenance": {
                "legacy": False,
                "logged_records": 0,
                "unaccounted": 0,
                "not_in_result": 0,
            },
        }
        results_meta = {
            "workspace": str(self.path),
            "round": 1,
            "created_at": now,
            "updated_at": now,
        }
        self._write_results([], results_meta)
        self._write_meta(meta)
        if not self.log_jsonl_path.exists():
            self.log_jsonl_path.write_text("", encoding="utf-8")
        _write_text_atomic(self.log_md_path, render_log_markdown([]))
        self._meta = meta
        self._papers = []
        self._results_meta = results_meta
        return self

    def load(self) -> "Workspace":
        """Read meta and results from disk, reconciling a crash journal."""
        if not self.meta_path.is_file():
            raise WorkspaceError(
                f"not a workspace: {self.path} (missing {META_FILE})"
            )
        meta = _read_json(self.meta_path)
        version = meta.get("schema_version")
        if isinstance(version, int) and version > WORKSPACE_SCHEMA_VERSION:
            raise WorkspaceError(
                f"workspace schema {version} is newer than supported "
                f"{WORKSPACE_SCHEMA_VERSION}; upgrade researcher-python"
            )
        try:
            envelope = export.read_envelope(self.results_path)
        except Exception as exc:  # noqa: BLE001 - reported as a workspace error
            raise WorkspaceError(
                f"could not read {self.results_path}: {exc}", cause=exc
            ) from exc
        self._meta = meta
        self._papers = envelope.papers
        self._results_meta = dict(envelope.meta)
        self._reconcile_pending(meta)
        return self

    def reload(self) -> "Workspace":
        self._papers = None
        self._meta = None
        self._results_meta = {}
        return self.load()

    @property
    def meta(self) -> dict[str, Any]:
        if self._meta is None:
            self.load()
        assert self._meta is not None
        return self._meta

    @property
    def papers(self) -> list[Paper]:
        if self._papers is None:
            self.load()
        assert self._papers is not None
        return self._papers

    @property
    def round(self) -> int:
        return int(self.meta.get("round", 1))

    def log_entries(self) -> list[dict[str, Any]]:
        return _read_log_entries(self.log_jsonl_path)

    def render_log(self) -> str:
        return render_log_markdown(self.log_entries())

    # -- locking / writes ------------------------------------------------

    @contextmanager
    def _locked(self):
        self.path.mkdir(parents=True, exist_ok=True)
        with open(self.path / ".lock", "a+", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _write_meta(self, meta: Mapping[str, Any]) -> None:
        _write_json_atomic(self.meta_path, meta)

    def _write_results(
        self, papers: Iterable[Paper], meta: Mapping[str, Any]
    ) -> None:
        export.to_json(papers, self.results_path, meta=meta)
        self._results_meta = dict(meta)

    def _reconcile_pending(self, meta: dict[str, Any]) -> None:
        """Complete or discard a write that crashed between its two files.

        ``results.json`` is the commit point.  If it carries the pending
        ``search_id``, the log line is appended and the journal removed; if it
        does not, the results write never happened and the journal is dropped.
        """
        pending = self.pending_path
        if not pending.exists():
            return
        try:
            payload = _read_json(pending)
        except WorkspaceError:
            pending.unlink(missing_ok=True)
            return

        search_id = str(payload.get("search_id") or "")
        committed = bool(
            search_id and self._results_meta.get("search_id") == search_id
        )
        if committed:
            entries = _read_log_entries(self.log_jsonl_path)
            if not any(entry.get("search_id") == search_id for entry in entries):
                entry = payload.get("entry")
                if isinstance(entry, dict):
                    append_jsonl(self.log_jsonl_path, entry)
                    entries.append(entry)
                    _write_text_atomic(
                        self.log_md_path, render_log_markdown(entries)
                    )
            meta["last_search"] = payload.get("last_search") or meta.get(
                "last_search"
            )
            meta["updated_at"] = _now()
            self._write_meta(meta)
        pending.unlink(missing_ok=True)


    # -- search ----------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        sources: str | list[str] | tuple[str, ...] | None = None,
        max_results: int | None = None,
        sort: str = "relevance",
        filters: Any | None = None,
        min_score: float | None = None,
        top_ratio: float | None = None,
        field: str | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        min_citations: int | None = None,
        channel: str = "library",
        settings: Settings | None = None,
    ) -> WorkspaceSearch:
        """Run :func:`pipeline.search` and persist it through the workspace."""
        settings = settings if settings is not None else self.settings
        started = time.perf_counter()
        result = pipeline.search(
            query,
            sources=sources,
            max_results=max_results,
            sort=sort,
            settings=settings,
            filters=filters,
            min_score=min_score,
            top_ratio=top_ratio,
            field=field,
            year_from=year_from,
            year_to=year_to,
            min_citations=min_citations,
        )
        duration_ms = (time.perf_counter() - started) * 1000.0

        if filters is not None:
            field_name = getattr(filters, "field", "any") or "any"
            filter_from = getattr(filters, "year_from", None)
            filter_to = getattr(filters, "year_to", None)
        else:
            field_name = field or "any"
            filter_from, filter_to = year_from, year_to

        if sources is None:
            requested = tuple(DEFAULT_SOURCES)
        elif isinstance(sources, str):
            requested = (sources,)
        else:
            requested = tuple(sources)

        params = SearchParams(
            query=query,
            sources=requested,
            max_results=(
                max_results if max_results is not None else settings.max_results
            ),
            sort=sort,
            field=field_name,
            year_from=filter_from,
            year_to=filter_to,
            min_score=min_score,
            top_ratio=top_ratio,
            min_citations=min_citations,
            round=self.round,
            channel=channel,
        )
        return self.record_search(params, result, duration_ms=duration_ms)

    def record_search(
        self,
        params: SearchParams | Mapping[str, Any],
        result: pipeline.PipelineResult,
        *,
        duration_ms: float = 0.0,
        extra: Mapping[str, Any] | None = None,
    ) -> WorkspaceSearch:
        """Merge ``result`` into the workspace and append one provenance row.

        ``extra`` carries retrieval metadata that is not a search parameter —
        a snowball expansion records its direction, core set and link counts
        here.  It is merged into both the log entry and the result envelope,
        so a later reader sees why the records arrived without the search log
        growing another column.
        """
        if isinstance(params, Mapping):
            params = SearchParams.from_dict(params)
        if not isinstance(params, SearchParams):
            raise ConfigurationError(
                "params must be SearchParams or a mapping, "
                f"got {type(params).__name__}"
            )

        with self._locked():
            self.load()
            if params.round is None:
                params = replace(
                    params, round=int((self._meta or {}).get("round", 1))
                )
            prior = list(self._papers or [])
            outcome: MergeOutcome = merge_sets(prior, result.papers)
            screened = [
                paper
                for paper in outcome.seen
                if paper.triage_label() != "unreviewed"
            ]
            search_id = _new_search_id()
            now = _now()
            entry = self._build_entry(
                search_id, now, params, result, outcome, len(screened),
                duration_ms,
            )
            if extra:
                entry.update(dict(extra))

            meta = self._meta or {}
            results_meta = {
                "workspace": str(self.path),
                "round": params.round,
                "search_id": search_id,
                "generated_by": (
                    f"researcher-python {__version__} ({params.channel})"
                ),
                "search": params.to_dict(),
                "outcome": entry["outcome"],
                "by_source": entry["by_source"],
                "errors": entry["errors"],
                "terms": entry["outcome"]["terms"],
                "updated_at": now,
            }
            if extra:
                results_meta.update(dict(extra))

            last_search = {
                "id": search_id,
                "at": now,
                "query": params.query,
                "new": len(outcome.new),
                "seen": len(outcome.seen),
                "screened": len(screened),
                "total": len(outcome.papers),
            }

            # Journal intent first: results.json is the commit point, and a
            # crash between the two is completed on the next load.
            _write_json_atomic(
                self.pending_path,
                {
                    "search_id": search_id,
                    "entry": entry,
                    "last_search": last_search,
                },
            )
            self._write_results(outcome.papers, results_meta)
            append_jsonl(self.log_jsonl_path, entry)
            _write_text_atomic(self.log_md_path, self.render_log())

            meta["updated_at"] = now
            meta["round"] = params.round
            meta["counts"] = _counts(outcome.papers)
            meta["searches"] = int(meta.get("searches", 0)) + 1
            meta["last_search"] = last_search
            provenance = dict(meta.get("provenance") or {})
            if not provenance.get("legacy"):
                provenance.update(
                    {
                        "logged_records": len(outcome.papers),
                        "unaccounted": 0,
                        "not_in_result": 0,
                    }
                )
            meta["provenance"] = provenance
            self._write_meta(meta)
            self.pending_path.unlink(missing_ok=True)

            self._papers = outcome.papers
            self._meta = meta
            self._results_meta = results_meta

        return WorkspaceSearch(
            result=result,
            new=len(outcome.new),
            seen=len(outcome.seen),
            screened=len(screened),
            total=len(outcome.papers),
            round=params.round if params.round is not None else 1,
            search_id=search_id,
            paths={
                "workspace": str(self.path),
                "results": str(self.results_path),
                "log": str(self.log_md_path),
                "jsonl": str(self.log_jsonl_path),
            },
            params=params.to_dict(),
        )

    def _build_entry(
        self,
        search_id: str,
        at: str,
        params: SearchParams,
        result: pipeline.PipelineResult,
        outcome: MergeOutcome,
        screened: int,
        duration_ms: float,
    ) -> dict[str, Any]:
        return {
            "search_id": search_id,
            "at": at,
            "round": params.round,
            "channel": params.channel,
            "package_version": __version__,
            "params": params.to_dict(),
            "outcome": {
                "returned": len(result.papers),
                "kept": len(result.papers),
                "new": len(outcome.new),
                "seen": len(outcome.seen),
                "screened": screened,
                "total": len(outcome.papers),
                "dropped": result.dropped,
                "terms": list(result.terms),
            },
            "by_source": dict(result.by_source),
            "errors": {
                name: {
                    "type": type(error).__name__,
                    "message": _text(error),
                }
                for name, error in result.errors.items()
            },
            "duration_ms": int(duration_ms),
        }


    # -- rounds ----------------------------------------------------------

    def set_round(self, number: int, label: str = "") -> int:
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ConfigurationError(
                f"round must be a positive integer, got {number!r}"
            )
        with self._locked():
            self.load()
            meta = self._meta or {}
            meta["round"] = number
            meta["round_label"] = label or f"round {number}"
            meta["updated_at"] = _now()
            self._write_meta(meta)
            self._meta = meta
        return number

    def start_round(self, label: str = "") -> int:
        """Snapshot the current round, then advance to the next one."""
        with self._locked():
            self.load()
            meta = self._meta or {}
            current = int(meta.get("round", 1))
            snapshot = self.path / "rounds" / f"round-{current}"
            snapshot.mkdir(parents=True, exist_ok=True)
            now = _now()
            export.to_json(
                self.papers,
                snapshot / RESULTS_FILE,
                meta={
                    "workspace": str(self.path),
                    "round": current,
                    "snapshot": True,
                    "generated_at": now,
                },
            )
            entries = [
                entry
                for entry in self.log_entries()
                if int(entry.get("round", 1)) == current
            ]
            _write_text_atomic(snapshot / LOG_MD, render_log_markdown(entries))
            new_round = current + 1
            meta["round"] = new_round
            meta["round_label"] = label or f"round {new_round}"
            meta["updated_at"] = now
            self._write_meta(meta)
            self._meta = meta
        return new_round

    # -- screening -------------------------------------------------------

    def label_many(
        self,
        updates: Iterable[LabelUpdate | Mapping[str, Any]],
        *,
        strict: bool = True,
    ) -> dict[str, Any]:
        """Apply many screening decisions in one locked read/write cycle.

        The batch is validated before any record is touched (see
        :func:`researcher_python.screening.apply_updates`), so a typo in the
        last update cannot leave the set half-labelled.  ``strict=False``
        applies the valid updates and reports the bad ones, which is what a
        long agent-driven batch usually wants.
        """
        prepared = list(updates)
        if not prepared:
            raise ConfigurationError("no label updates to apply")

        with self._locked():
            self.load()
            outcome = apply_updates(self._papers or [], prepared, strict=strict)

            now = _now()
            meta = self._meta or {}
            counts = _counts(self._papers or [])
            meta["updated_at"] = now
            meta["counts"] = counts
            meta["last_label"] = {
                "at": now,
                "labelled": outcome.labelled,
                "set": outcome.labels[0] if len(outcome.labels) == 1 else None,
                "labels": list(outcome.labels),
                "unmatched": list(outcome.unmatched),
            }
            results_meta = dict(self._results_meta)
            results_meta.update(
                {
                    "action": "label",
                    "labelled": outcome.labelled,
                    "updated_at": now,
                }
            )
            self._write_results(self._papers or [], results_meta)
            self._write_meta(meta)
            self._meta = meta

        return {
            "labelled": outcome.labelled,
            "total": len(self._papers or []),
            "summary": _counts(self._papers or []),
            "unmatched": list(outcome.unmatched),
            "invalid": list(outcome.invalid),
            "labels": list(outcome.labels),
        }

    def screen(
        self,
        *,
        ids: Iterable[str] | None = None,
        set_label: str | None = None,
        note: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        """The verb form of :meth:`label` — screening sets the stored label."""
        return self.label(ids=ids, set_label=set_label, note=note, reason=reason)

    def label(
        self,
        *,
        ids: Iterable[str] | None = None,
        set_label: str | None = None,
        note: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        """Apply one triage label / note to the canonical set, in place.

        Kept as the single-record convenience path (and the compatibility
        name); :meth:`label_many` is the batch primitive underneath it.
        """
        if set_label is None and not note:
            raise ConfigurationError("give set_label and/or note to record")
        if set_label is not None and set_label not in TRIAGE_LABELS:
            raise ConfigurationError(
                f"unknown triage label {set_label!r}; expected one of "
                f"{', '.join(TRIAGE_LABELS)}"
            )

        wanted = list(ids or [])
        update = LabelUpdate(
            ids=tuple(wanted),
            label=set_label,
            note=note,
            reason=reason,
            all_records=not wanted,
        )
        result = self.label_many([update])
        return {
            "labelled": result["labelled"],
            "total": result["total"],
            "summary": result["summary"],
        }

    def prescreen(
        self,
        *,
        apply: bool = False,
        criteria: PrescreenCriteria | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Flag obvious exclusions, optionally writing them as ``excluded``.

        ``apply=False`` (the default) is a report: rules are heuristics, and
        "no abstract" is a false positive for exactly the grey literature a
        review often needs.  Applying writes only to records still marked
        ``unreviewed`` and prefixes the reason with ``pre-screen:`` so the
        PRISMA report can separate a rule exclusion from a human one.
        """
        with self._locked():
            self.load()
            report = prescreen(self._papers or [], criteria)
            applied = 0
            if apply and report.hits:
                reason_prefix = "pre-screen: "
                for hit in report.hits:
                    if hit.paper.triage_label() != "unreviewed":
                        continue
                    hit.paper.label_as("excluded", reason_prefix + hit.reason)
                    applied += 1
                now = _now()
                meta = self._meta or {}
                meta["updated_at"] = now
                meta["counts"] = _counts(self._papers or [])
                meta["last_label"] = {
                    "at": now,
                    "labelled": applied,
                    "set": "excluded",
                    "labels": ["excluded"],
                    "unmatched": [],
                    "prescreen": True,
                }
                results_meta = dict(self._results_meta)
                results_meta.update(
                    {
                        "action": "prescreen",
                        "labelled": applied,
                        "updated_at": now,
                    }
                )
                self._write_results(self._papers or [], results_meta)
                self._write_meta(meta)
                self._meta = meta
            payload = report.as_dict(limit=limit)
            payload["applied"] = applied
            payload["summary"] = _counts(self._papers or [])
            return payload

    def expand(
        self,
        *,
        direction: str = "both",
        ids: Iterable[str] | None = None,
        core_label: str = "core",
        limit: int | None = None,
        sources: list[str] | tuple[str, ...] | None = None,
        min_links: int = 1,
        max_results: int | None = None,
        persist: bool = True,
        settings: Settings | None = None,
    ) -> dict[str, Any]:
        """Snowball the core set through the citation graph.

        The core set is the explicit ``ids`` or, more usually, every record
        labelled ``core``.  Network I/O runs *outside* the workspace lock: a
        30-paper expansion is dozens of API calls, and holding the flock for
        that long would block every other writer.

        ``persist=True`` merges the candidates through
        :meth:`record_search`, so the expansion gets a provenance row
        (``channel="snowball"``) and the crash-journal guarantees a search
        already has.  The log row records the direction, core set and bounds —
        enough to reproduce the query — while the per-record ``linked_by``
        evidence is returned to the caller rather than copied into the log.
        """
        settings = settings if settings is not None else self.settings
        if core_label not in TRIAGE_LABELS:
            raise ConfigurationError(
                f"unknown core label {core_label!r}; expected one of "
                f"{', '.join(TRIAGE_LABELS)}"
            )

        # Snapshot the core/known sets, then release the lock for the network.
        with self._locked():
            self.load()
            papers = list(self._papers or [])
            if ids is not None:
                wanted = [ids] if isinstance(ids, str) else list(ids)
                index = index_papers(papers)
                selected: list[Paper] = []
                missing: list[str] = []
                seen: set[int] = set()
                for identifier in wanted:
                    matches = index.get(str(identifier), ())
                    if not matches:
                        missing.append(str(identifier))
                        continue
                    for paper in matches:
                        if id(paper) not in seen:
                            seen.add(id(paper))
                            selected.append(paper)
                if missing:
                    raise ConfigurationError(
                        "no record for core id(s): " + ", ".join(missing)
                    )
            else:
                selected = [
                    paper
                    for paper in papers
                    if paper.triage_label() == core_label
                ]
            if not selected:
                raise ConfigurationError(
                    "no core records to expand; label some records 'core' "
                    "first, or pass ids=[...]"
                )
            known = papers
            round_number = self.round

        result = snowball_expand(
            selected,
            direction=direction,
            known=known,
            limit=limit,
            sources=sources,
            min_links=min_links,
            settings=settings,
        )

        payload = result.as_dict(min_links=min_links, limit=max_results)
        payload["round"] = round_number
        payload["persisted"] = False

        if persist:
            from .citation import CITATION_SOURCES

            queried_sources = tuple(result.by_source) or CITATION_SOURCES
            errors: dict[str, ResearcherError] = {}
            for core_id, per_source in result.errors.items():
                for source, message in per_source.items():
                    errors[f"{core_id}:{source}"] = SourceError(source, message)
            pipeline_result = pipeline.PipelineResult(
                papers=[item.paper for item in result.candidates],
                by_source=dict(result.by_source),
                errors=errors,
                terms=[],
                dropped=0,
            )
            params = SearchParams(
                query=f"expand:{result.direction}",
                sources=queried_sources,
                max_results=limit,
                sort="relevance",
                field="any",
                round=round_number,
                channel="snowball",
            )
            outcome = self.record_search(
                params,
                pipeline_result,
                extra={
                    "expansion": {
                        "direction": result.direction,
                        "core": list(result.core),
                        "min_links": min_links,
                        "skipped": list(result.skipped),
                        "truncated_sources": dict(result.truncated),
                    }
                },
            )
            payload.update(
                {
                    "persisted": True,
                    "search_id": outcome.search_id,
                    "new": outcome.new,
                    "seen": outcome.seen,
                    "total": outcome.total,
                    "workspace": str(self.path),
                }
            )

        return payload

    # -- reporting -------------------------------------------------------

    def prisma(self) -> dict[str, Any]:
        """PRISMA-style counts derived from the log and the labels.

        Pure work lives in :func:`researcher_python.prisma.prisma_report`; this
        method only supplies the workspace's records, log and provenance.
        """
        self.load()
        return prisma_report(
            self.papers,
            self.log_entries(),
            provenance=self.meta.get("provenance") or {},
        )

    def status(self) -> dict[str, Any]:
        """A JSON-able status card — paths, counts, round and provenance."""
        files = {
            "meta": str(self.meta_path),
            "results": str(self.results_path),
            "log": str(self.log_md_path),
            "jsonl": str(self.log_jsonl_path),
        }
        if not self.exists():
            return {
                "path": str(self.path),
                "exists": False,
                "files": files,
            }
        self.load()
        meta = self.meta
        return {
            "path": str(self.path),
            "exists": True,
            "schema_version": meta.get("schema_version", 0),
            "topic": meta.get("topic", ""),
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
            "round": meta.get("round", 1),
            "round_label": meta.get("round_label", ""),
            "counts": meta.get("counts", _counts(self.papers)),
            "searches": meta.get("searches", 0),
            "last_search": meta.get("last_search"),
            "last_label": meta.get("last_label"),
            "provenance": meta.get("provenance", {}),
            "log_rows": len(self.log_entries()),
            "files": files,
        }

    def export_papers(self, path: str | os.PathLike[str], *, meta: bool = True) -> Path:
        """Write the current set as a standalone JSON result file."""
        target = Path(path)
        papers = self.papers  # ensures results.json (and its meta) is loaded
        payload = dict(self._results_meta) if meta else None
        export.to_json(papers, target, meta=payload)
        return target


# ---------------------------------------------------------------------- #
# Convenience helpers used by the CLI / MCP
# ---------------------------------------------------------------------- #


def log_entry_for(
    params: SearchParams,
    result: pipeline.PipelineResult,
    *,
    outcome: MergeOutcome | None = None,
    screened: int = 0,
    search_id: str = "",
    at: str = "",
    duration_ms: float = 0.0,
) -> dict[str, Any]:
    """Build the same log entry a workspace would, without writing it.

    This is what a bare ``--search-log`` uses, so a standalone log and a
    workspace log are the same shape.
    """
    outcome = outcome or MergeOutcome(papers=list(result.papers), new=list(result.papers))
    return {
        "search_id": search_id or _new_search_id(),
        "at": at or _now(),
        "round": params.round if params.round is not None else 1,
        "channel": params.channel,
        "package_version": __version__,
        "params": params.to_dict(),
        "outcome": {
            "returned": len(result.papers),
            "kept": len(result.papers),
            "new": len(outcome.new),
            "seen": len(outcome.seen),
            "screened": screened,
            "total": len(outcome.papers),
            "dropped": result.dropped,
            "terms": list(result.terms),
        },
        "by_source": dict(result.by_source),
        "errors": {
            name: {"type": type(error).__name__, "message": _text(error)}
            for name, error in result.errors.items()
        },
        "duration_ms": int(duration_ms),
    }
