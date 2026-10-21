"""Build a research handoff bundle for a downstream writing tool.

A workspace is the right home for *research state*: it holds every record, the
screening labels, the search log and the PRISMA counts.  A paper should not
read that state directly, for two reasons:

* ``results.json`` contains ``excluded`` and ``unreviewed`` records, which do
  not belong in a manuscript's bibliography;
* a matrix row is keyed by ``Paper.primary_id()`` (a DOI/arXiv id), while the
  bibliography is keyed by a generated BibTeX key.  The two need a map.

This module writes one stable, versioned directory that resolves both.  The
builder is pure local work: no source is constructed, no network is used, and
the only imports are the model/format/export/workspace modules already used by
the CLI.

Layout::

    handoff/
      handoff.json          # schema, source, counts, screening, file index
      papers.json           # included records, export envelope schema 1
      references.bib        # included records only, unique keys
      citation-map.json     # id/title -> citation key
      matrix.md             # literature matrix with a cite_key column
      synthesis.md          # dimension x triage-label counts
      prisma.md             # screening flow (workspace sources only)
      README.md             # human summary + regeneration command
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__, export, matrix
from . import format as bib
from .config import Settings, load_settings
from .errors import ConfigurationError, ResearcherError, WorkspaceError
from .models import TRIAGE_LABELS, Paper
from .prisma import render_prisma_markdown
from .workspace import Workspace

__all__ = [
    "DEFAULT_LABELS",
    "HANDOFF_SCHEMA_VERSION",
    "build_handoff",
]

#: The labels a manuscript normally includes.  ``excluded`` and ``unreviewed``
#: are deliberately absent: a bibliography is not a search dump.
DEFAULT_LABELS: tuple[str, ...] = ("core", "general")

#: Bump only when the on-disk bundle shape changes incompatibly.
HANDOFF_SCHEMA_VERSION = 1

#: Human-readable matrix columns; ``cite_key`` is prepended separately.
_MATRIX_COLUMNS: tuple[str, ...] = (
    "label",
    "authors",
    "year",
    "title",
    "venue",
    "type",
    "doi",
    "open_access",
    "notes",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _squash(value: Any) -> str:
    return " ".join(str(value or "").split())


def _atomic_text(path: Path, text: str) -> None:
    """Write text via a sibling temp file, then ``os.replace``."""
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


def _resolve_source(
    source: str | os.PathLike[str], settings: Settings
) -> tuple[Path, list[Paper], Workspace | None]:
    """Resolve a workspace directory or a saved result file."""
    raw = Path(source).expanduser()
    candidates = [raw]
    if not raw.is_absolute() and settings.home is not None:
        candidates.append(Path(settings.home).expanduser() / raw)
    resolved = next((path for path in candidates if path.exists()), None)
    if resolved is None:
        raise ConfigurationError(f"no such workspace or result file: {source}")

    resolved = resolved.resolve()
    if resolved.is_dir():
        workspace = Workspace(resolved, settings=settings, create=False)
        if not workspace.exists():
            raise ConfigurationError(
                f"{resolved} is not a workspace (missing workspace.json or results.json)"
            )
        return resolved, workspace.papers, workspace

    if resolved.is_file():
        suffix = resolved.suffix.lower()
        try:
            papers = (
                export.from_jsonl(resolved)
                if suffix == ".jsonl"
                else export.from_json(resolved)
            )
        except (OSError, ResearcherError) as exc:
            raise ConfigurationError(f"cannot read {resolved}: {exc}") from exc
        return resolved, papers, None

    raise ConfigurationError(f"{resolved} is neither a directory nor a file")


def _md_cell(value: Any) -> str:
    return _squash(value).replace("|", "\\|")


def _render_matrix(
    pairs: Sequence[tuple[Paper, str, str]],
) -> str:
    """The literature matrix with the citation key as the first column."""
    header = ["cite_key", *_MATRIX_COLUMNS]
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    for paper, key, _entry in pairs:
        row = matrix.matrix_row(paper, _MATRIX_COLUMNS)
        cells = [key, *(row[column] for column in _MATRIX_COLUMNS)]
        lines.append("| " + " | ".join(_md_cell(cell) for cell in cells) + " |")
    return "\n".join(lines) + "\n"


def _screen_note(screening: dict[str, Any]) -> str:
    if screening.get("ready"):
        return "complete"
    return f"incomplete — {_squash(screening.get('message'))}"


def _readme(
    *,
    source: Path,
    source_kind: str,
    topic: str,
    generated_at: str,
    counts: dict[str, int],
    screening: dict[str, Any],
    labels: Sequence[str],
) -> str:
    count_lines = "\n".join(f"- {name}: {counts.get(name, 0)}" for name in TRIAGE_LABELS)
    return f"""\
# Research handoff

Generated by researcher-python {__version__} at {generated_at}.

- Source: `{source}` ({source_kind})
- Topic: {topic or "(not set)"}
- Included labels: {", ".join(labels)}
- Screening: {_screen_note(screening)}

## Counts

{count_lines}
- included: {counts.get("included", 0)}
- records: {counts.get("records", 0)}

## Files

- `papers.json` — included records (versioned export envelope)
- `references.bib` — included records only, unique BibTeX keys
- `citation-map.json` — id/title to citation key
- `matrix.md` — literature matrix with `cite_key`
- `synthesis.md` — dimension cross-tab
- `prisma.md` — screening flow (workspace sources only)

This directory is generated. Do not edit it by hand; re-run:

```console
$ researcher-python handoff {source} --out {source.parent / "handoff"}
```
"""


def build_handoff(
    source: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    *,
    settings: Settings | None = None,
    labels: Sequence[str] | None = None,
    require_screening: bool = True,
    include_prisma: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Write a handoff bundle and return a compact receipt.

    ``source`` is a workspace directory or a saved JSON/JSONL result file.
    ``labels`` defaults to ``core`` + ``general``.  With
    ``require_screening=True`` an unscreened set is refused: a manuscript built
    from a search dump is not a screened paper.
    """
    settings = settings if settings is not None else load_settings()
    chosen_labels = tuple(labels or DEFAULT_LABELS)
    unknown = [name for name in chosen_labels if name not in TRIAGE_LABELS]
    if unknown:
        raise ConfigurationError(
            f"unknown label(s) {', '.join(unknown)}; expected one of "
            f"{', '.join(TRIAGE_LABELS)}"
        )

    source_path, papers_all, workspace = _resolve_source(source, settings)
    screening = matrix.screening_report(papers_all)
    if require_screening and not screening["ready"]:
        raise ConfigurationError(
            f"screening incomplete: {_squash(screening.get('message'))}. "
            "Screen the set first (screen/prescreen), or pass --allow-unscreened."
        )

    included = [paper for paper in papers_all if paper.triage_label() in chosen_labels]
    if not included:
        raise ConfigurationError(
            f"no records labelled {', '.join(chosen_labels)}; screen the set first"
        )

    out = Path(out_dir).expanduser().resolve()
    manifest_path = out / "handoff.json"
    if manifest_path.exists() and not force:
        raise ConfigurationError(f"{manifest_path} already exists (pass --force to overwrite)")
    out.mkdir(parents=True, exist_ok=True)

    generated_at = _now()
    pairs = bib.bibtex_pairs(included)

    # Bibliography and citation map come from the same pass, so a collision
    # suffix cannot differ between them.
    bibliography = "\n".join(entry for _paper, _key, entry in pairs)
    _atomic_text(out / "references.bib", bibliography)

    entries: list[dict[str, Any]] = []
    by_id: dict[str, str] = {}
    by_title: dict[str, str] = {}
    for paper, key, _entry in pairs:
        identifier = paper.primary_id()
        title_key = paper.title_key()
        entries.append(
            {
                "id": identifier,
                "title": _squash(paper.title),
                "key": key,
                "year": paper.year,
                "label": paper.triage_label(),
            }
        )
        if identifier:
            by_id[identifier] = key
        if title_key:
            by_title[title_key] = key

    citation_map = {
        "schema_version": 1,
        "generated_at": generated_at,
        "entries": entries,
        "by_id": by_id,
        "by_title": by_title,
    }
    _atomic_text(
        out / "citation-map.json",
        json.dumps(citation_map, ensure_ascii=False, indent=2) + "\n",
    )

    export.to_json(
        included,
        out / "papers.json",
        meta={
            "source": str(source_path),
            "labels": list(chosen_labels),
            "generated_by": f"researcher-python {__version__}",
        },
    )

    _atomic_text(out / "matrix.md", _render_matrix(pairs))
    _atomic_text(
        out / "synthesis.md",
        matrix.render_synthesis_markdown(
            papers_all, dimension="venue", require_screening=False
        ),
    )

    if include_prisma:
        if workspace is not None:
            prisma_text = render_prisma_markdown(workspace.prisma())
        else:
            prisma_text = (
                "# PRISMA-style screening flow\n\n"
                "Not available: the source was a result file, not a workspace "
                "with a search log.\n"
            )
        _atomic_text(out / "prisma.md", prisma_text)

    counts = {name: 0 for name in TRIAGE_LABELS}
    for paper in papers_all:
        counts[paper.triage_label()] += 1
    counts["records"] = len(papers_all)
    counts["included"] = len(included)

    topic = _squash((workspace.meta.get("topic") if workspace is not None else "") or "")
    round_number = int((workspace.meta.get("round") if workspace is not None else 1) or 1)

    files = {
        "papers": "papers.json",
        "bibliography": "references.bib",
        "citation_map": "citation-map.json",
        "matrix": "matrix.md",
        "synthesis": "synthesis.md",
        **({"prisma": "prisma.md"} if include_prisma else {}),
        "readme": "README.md",
    }
    manifest = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "generated_at": generated_at,
        "source": str(source_path),
        "source_kind": "workspace" if workspace is not None else "file",
        "topic": topic,
        "round": round_number,
        "labels": list(chosen_labels),
        "counts": counts,
        "screening": {
            "ready": bool(screening.get("ready")),
            "message": _squash(screening.get("message")),
        },
        "files": files,
        "tool": {"name": "researcher-python", "version": __version__},
    }
    _atomic_text(out / "README.md", _readme(
        source=source_path,
        source_kind=manifest["source_kind"],
        topic=topic,
        generated_at=generated_at,
        counts=counts,
        screening=screening,
        labels=chosen_labels,
    ))
    # The manifest is the commit point: readers see a complete bundle or none.
    _atomic_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )

    return {
        "ok": True,
        "out_dir": str(out),
        "source": str(source_path),
        "source_kind": manifest["source_kind"],
        "counts": counts,
        "screening": manifest["screening"],
        "labels": list(chosen_labels),
        "files": files,
        "next": f"paper-handler research import {out} --paper NAME",
    }
