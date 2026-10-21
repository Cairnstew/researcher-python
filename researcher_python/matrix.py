"""Literature and synthesis matrices — the Week 3 lab deliverable as data.

The module's literature-study lab asks for two artefacts, and CW2 marks the
literature review at 30%:

* a **Literature Matrix** — one row per paper, columns for author, year,
  title, venue, method, findings…;
* a **Synthesis Matrix** — the cross-tab that shows which papers support which
  theme, which is where a "research gap" actually becomes visible.

Neither is a bibliography, so neither belongs in :mod:`researcher_python.format`
(which renders *citations*).  This module renders **tabular** output instead,
as CSV (spreadsheet / ``pandas``) or Markdown (paste-ready into a report), and
it reads the reader-owned triage state on
:class:`~researcher_python.models.Paper` so screening decisions travel with the
records through ``to_json``/``from_json``.

Every column is a named extractor over ``Paper``, so a caller can add one
without touching this module's logic:

    >>> import researcher_python as rp
    >>> rp.matrix_rows(papers)                       # doctest: +SKIP
    >>> rp.render_matrix_csv(papers, columns=("key", "year", "title"))
"""

from __future__ import annotations

import csv
import io
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from typing import TextIO

from .errors import ExportError
from .export import _atomic_text, _label
from .format import _squash, bibtex_key, bibtex_pairs
from .models import TRIAGE_LABELS, Paper
from .work_types import normalize_work_type

__all__ = [
    "LITERATURE_COLUMNS",
    "SYNTHESIS_DIMENSIONS",
    "column_names",
    "matrix_row",
    "matrix_rows",
    "render_matrix_csv",
    "render_matrix_markdown",
    "render_synthesis_csv",
    "render_synthesis_markdown",
    "screen_summary",
    "screening_report",
    "synthesis_matrix",
    "to_matrix_csv",
    "to_matrix_markdown",
    "to_synthesis_csv",
    "to_synthesis_markdown",
]


def _key(paper: Paper) -> str:
    """A stable row key: the best identifier, else the citation key."""
    return paper.primary_id() or _squash(paper.title)[:80]


def _authors(paper: Paper) -> str:
    """Author names joined with ``; `` — unambiguous inside a CSV cell."""
    return "; ".join(_squash(a.name) for a in paper.authors if _squash(a.name))


def _year(paper: Paper) -> str:
    return str(paper.year) if paper.year is not None else ""


def _triage_label(paper: Paper) -> str:
    return paper.triage_label()


def _type(paper: Paper) -> str:
    return _squash(paper.work_type)


def _citations(paper: Paper) -> str:
    return str(paper.citation_count) if paper.citation_count is not None else ""


def _citations_per_year(paper: Paper) -> str:
    """Citations normalised by age — the *standing* signal that actually works.

    Raw citation counts reward age: a 1992 paper with 400 citations and a 2023
    paper with 400 are not comparable, which is lab03's explicit caution.  In
    practice this ratio was far more useful for triage than the venue name (a
    venue allow-list cannot anticipate every field's good journals, while this
    is field-agnostic).  Two decimal places; empty when either input is
    unknown, so a gap stays visible rather than reading as zero.
    """
    if paper.citation_count is None or paper.year is None:
        return ""
    age = max(1, date.today().year - paper.year + 1)
    return f"{paper.citation_count / age:.2f}"


def _open_access(paper: Paper) -> str:
    return "yes" if paper.is_open_access else "no"


def _relevance(paper: Paper) -> str:
    """The stamped topical score, to three places; empty when unscored.

    Included so a Literature Matrix records *why* a row survived the search
    (``min_score``) alongside what the reader decided about it — the score and
    the screening judgement travel in one artefact.
    """
    return "" if paper.relevance is None else f"{paper.relevance:.3f}"


#: Every column this module knows how to fill, name -> extractor.
COLUMNS: dict[str, Callable[[Paper], str]] = {
    "key": _key,
    "cite_key": bibtex_key,
    "label": lambda p: p.triage_label(),
    "authors": _authors,
    "year": _year,
    "title": lambda p: _squash(p.title),
    "venue": lambda p: _squash(p.venue),
    "publisher": lambda p: _squash(p.publisher),
    "type": _type,
    "doi": lambda p: p.doi,
    "url": lambda p: _squash(p.url),
    "abstract": lambda p: _squash(p.abstract),
    "keywords": lambda p: "; ".join(p.keywords),
    "categories": lambda p: "; ".join(p.categories),
    "citations": _citations,
    "citations_per_year": _citations_per_year,
    "references": lambda p: str(p.reference_count) if p.reference_count is not None else "",
    "open_access": _open_access,
    "score": _relevance,
    "exclusion_reason": lambda p: _squash(p.exclusion_reason),
    "notes": lambda p: _squash(p.notes),
}

#: The default Literature Matrix: identification, provenance and the reader's
#: own words.  Deliberately not "methodology"/"findings" — those live in the
#: free-text ``notes`` column, because this package does not read papers for
#: you (see :mod:`researcher_python.extract` for the text it *can* pull).
#:
#: ``score`` is available but *not* default: most records in a hand-built or
#: ``get``-sourced set were never scored, and a column that is empty for every
#: row is noise.  Pass ``columns=("key", "label", "score", ...)`` — or
#: ``--columns key,label,score,title`` — when the set came from a scored
#: ``search`` and you want the score that admitted each row recorded beside the
#: screening decision made about it.
LITERATURE_COLUMNS: tuple[str, ...] = (
    "key",
    "label",
    "authors",
    "year",
    "title",
    "venue",
    "type",
    "doi",
    "open_access",
    "abstract",
    "notes",
)

#: Dimensions a Synthesis Matrix can cross-tab rows against.
SYNTHESIS_DIMENSIONS: tuple[str, ...] = (
    "label",
    "year",
    "type",
    "venue",
    "open_access",
)


def column_names() -> list[str]:
    """Every available column name, in a stable order."""
    return list(COLUMNS)


def _resolve(columns: Sequence[str] | None) -> tuple[str, ...]:
    if columns is None:
        return LITERATURE_COLUMNS
    unknown = [c for c in columns if c not in COLUMNS]
    if unknown:
        raise ExportError(
            f"unknown matrix column(s) {', '.join(unknown)}; "
            f"available: {', '.join(COLUMNS)}"
        )
    return tuple(columns)


def matrix_row(paper: Paper, columns: Sequence[str] | None = None) -> dict[str, str]:
    """One Literature Matrix row as ``{column: text}``."""
    return {name: COLUMNS[name](paper) for name in _resolve(columns)}


def matrix_rows(
    papers: Iterable[Paper],
    columns: Sequence[str] | None = None,
    *,
    cite_keys: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """Every row, in the order given (the caller's screening order is kept).

    ``cite_key`` is made unique across the set by the same single pass that
    renders ``references.bib`` (:func:`researcher_python.format.bibtex_pairs`),
    so a matrix key and a citation key cannot disagree about a collision
    suffix.  Pass ``cite_keys`` to reuse a map you already hold.
    """
    names = _resolve(columns)
    items = list(papers)
    # Key by object identity for the generated case: two records with the same
    # title and no identifier share a title_key, so an id->key map would lose
    # one of them (and hand both rows the second key).
    generated = (
        {id(paper): key for paper, key, _entry in bibtex_pairs(items)}
        if cite_keys is None and "cite_key" in names
        else {}
    )
    mapping = dict(cite_keys) if cite_keys is not None else {}
    rows: list[dict[str, str]] = []
    for paper in items:
        row = {name: COLUMNS[name](paper) for name in names}
        if "cite_key" in row:
            if cite_keys is None:
                row["cite_key"] = generated.get(id(paper), row["cite_key"])
            else:
                identifier = paper.primary_id() or paper.title_key()
                row["cite_key"] = mapping.get(identifier, row["cite_key"])
        rows.append(row)
    return rows


# ---------------------------------------------------------------------- #
# Literature matrix rendering
# ---------------------------------------------------------------------- #


def render_matrix_csv(
    papers: Iterable[Paper],
    columns: Sequence[str] | None = None,
    *,
    require_screening: bool = False,
    cite_keys: Mapping[str, str] | None = None,
) -> str:
    """The Literature Matrix as RFC-4180 CSV with a header row.

    Built through :mod:`csv`, so embedded commas, quotes and newlines in
    abstracts and notes are quoted rather than corrupting the row.

    ``require_screening=True`` refuses to render a set whose rows are all
    ``unreviewed`` (see :func:`screening_report`) — the guard that stops an
    unscreened search dump being handed in as the lab's Literature Matrix.
    """
    papers = list(papers)
    _gate(papers, require_screening)
    names = _resolve(columns)
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=list(names), lineterminator="\n", quoting=csv.QUOTE_MINIMAL
    )
    writer.writeheader()
    for row in matrix_rows(papers, names, cite_keys=cite_keys):
        writer.writerow(row)
    return buffer.getvalue()


def _md_cell(value: str) -> str:
    """Make one cell safe inside a Markdown table cell."""
    text = _squash(value)
    return text.replace("\\", "\\\\").replace("|", "\\|") if text else ""


def render_matrix_markdown(
    papers: Iterable[Paper],
    columns: Sequence[str] | None = None,
    *,
    require_screening: bool = False,
    cite_keys: Mapping[str, str] | None = None,
) -> str:
    """The Literature Matrix as a GitHub-flavoured Markdown table.

    ``require_screening`` behaves as in :func:`render_matrix_csv`.
    """
    papers = list(papers)
    _gate(papers, require_screening)
    names = _resolve(columns)
    rows = matrix_rows(papers, names, cite_keys=cite_keys)
    lines = [
        "| " + " | ".join(names) + " |",
        "| " + " | ".join("---" for _ in names) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_md_cell(row[name]) for name in names) + " |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- #
# Synthesis matrix
# ---------------------------------------------------------------------- #


def _normalise_venue(venue: str) -> str:
    """Collapse the formatting variants of one venue into a single key.

    Sources spell the same journal several ways — ``"AI and Ethics"``,
    ``"AI and ethics"``, ``"AI and Ethics (2026), 6, Article 281"`` — so
    cross-tabbing the raw string fragments one venue across several rows and
    hides the counts a synthesis matrix exists to show.  Normalising case and
    stripping the volume/article tail makes the rows comparable.

    The *displayed* value stays the first-seen original, so the table still
    shows the source's own spelling.
    """
    text = _squash(venue)
    if not text:
        return text
    # "Nature Machine Intelligence (2026), 6, Article 281" -> "Nature Machine Intelligence"
    text = re.sub(r"\s*\(\d{4}\)\s*,\s*\d+\s*,\s*Article\s*\d+", "", text)
    text = re.sub(r"\s*\(\d{4}\)\s*,?\s*Vol\.?\s*\d+", "", text, flags=re.IGNORECASE)
    return text.strip(" ,.")


def _venue_key(venue: str) -> str:
    """A comparable key for a venue (see :func:`_normalise_venue`)."""
    return _normalise_venue(venue).casefold()


#: Spellings of one work type collapsed to a single canonical label.  The
#: sources disagree ("Journal Article", "article", "journal-article"), so a
#: raw cross-tab splits one type across several rows — the same defect as
#: venue spelling, and just as destructive to the counts.
_TYPE_ALIASES: dict[str, str] = {
    "article": "journal-article",
    "journal article": "journal-article",
    "journal-article": "journal-article",
    "research-article": "journal-article",
    "proceedings-article": "conference-paper",
    "conference-paper": "conference-paper",
    "conference paper": "conference-paper",
    "proceedings paper": "conference-paper",
    "posted-content": "preprint",
    "preprint": "preprint",
    "book-chapter": "book-chapter",
    "book chapter": "book-chapter",
    "book": "book",
    "monograph": "book",
    "review": "review",
    "review-article": "review",
    "dataset": "dataset",
}


def _type_key(value: str) -> str:
    """Canonical key for a work type, so one type is one row.

    Uses the same vocabulary as the search filter: ``Journal Article``,
    ``journal-article`` and ``article`` are one synthesis row.
    """
    return normalize_work_type(value) or _squash(value).casefold() or "(unknown)"


def _dimension_values(paper: Paper, dimension: str) -> list[str]:
    """The value(s) one record occupies on a synthesis dimension.

    A dimension can be multi-valued (``venue`` is a list for one record split
    across ``categories``), so this returns a list and callers union them.
    """
    if dimension not in COLUMNS:
        raise ExportError(
            f"unknown synthesis dimension {dimension!r}; "
            f"available: {', '.join(SYNTHESIS_DIMENSIONS)}"
        )
    raw = COLUMNS[dimension](paper)
    if dimension == "label":
        return [raw]
    if not raw:
        return ["(unknown)"]
    return [part.strip() for part in raw.split("; ") if part.strip()]


def synthesis_matrix(
    papers: Iterable[Paper], dimension: str = "year"
) -> list[dict[str, str]]:
    """Cross-tab ``dimension`` against triage label: one row per dimension value.

    ``papers`` are counted once per (value, label) pair, so a record that
    carries two keywords contributes to both rows — a cross-tab, not a
    partition.

    ``venue`` and ``type`` are *normalised* before counting, because sources
    spell one journal (or one work type) several ways and raw-string rows
    fragment the counts — the exact rows a synthesis matrix exists to show.
    The label shown is the first-seen original spelling for a venue and the
    canonical type name for a type.
    """
    if dimension not in COLUMNS:
        raise ExportError(
            f"unknown synthesis dimension {dimension!r}; "
            f"available: {', '.join(SYNTHESIS_DIMENSIONS)}"
        )
    counts: dict[str, dict[str, int]] = {}
    display: dict[str, str] = {}
    for paper in papers:
        bucket = paper.triage_label()
        for value in _dimension_values(paper, dimension):
            if dimension == "venue":
                key, shown = _venue_key(value), _normalise_venue(value)
            elif dimension == "type":
                # Display the canonical name, not the first-seen raw spelling:
                # "article" and "journal-article" are one row, so the row must
                # say which one rather than inheriting whichever arrived first.
                key = _type_key(value)
                shown = key
            else:
                key = shown = value
            display.setdefault(key, shown)
            counts.setdefault(key, {}).setdefault(bucket, 0)
            counts[key][bucket] += 1

    rows: list[dict[str, str]] = []
    for key in sorted(counts):
        row: dict[str, str] = {dimension: display.get(key, key)}
        for label in TRIAGE_LABELS:
            row[label] = str(counts[key].get(label, 0))
        row["total"] = str(sum(counts[key].values()))
        rows.append(row)
    return rows


def _synthesis_columns(dimension: str) -> tuple[str, ...]:
    return (dimension, *TRIAGE_LABELS, "total")


def render_synthesis_csv(
    papers: Iterable[Paper],
    dimension: str = "year",
    *,
    require_screening: bool = False,
) -> str:
    """The Synthesis Matrix as CSV (dimension × triage-label counts).

    ``require_screening`` behaves as in :func:`render_matrix_csv` — and matters
    more here, because a Synthesis Matrix cross-tabs *by* triage label, so an
    unscreened set collapses to a single all-``unreviewed`` column.
    """
    papers = list(papers)
    _gate(papers, require_screening)
    rows = synthesis_matrix(papers, dimension)
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=list(_synthesis_columns(dimension)), lineterminator="\n"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def render_synthesis_markdown(
    papers: Iterable[Paper],
    dimension: str = "year",
    *,
    require_screening: bool = False,
) -> str:
    """The Synthesis Matrix as a Markdown table.

    ``require_screening`` behaves as in :func:`render_matrix_csv`.
    """
    papers = list(papers)
    _gate(papers, require_screening)
    names = _synthesis_columns(dimension)
    rows = synthesis_matrix(papers, dimension)
    lines = [
        "| " + " | ".join(names) + " |",
        "| " + " | ".join("---" for _ in names) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_md_cell(row[n]) for n in names) + " |")
    return "\n".join(lines) + "\n"


def screen_summary(papers: Iterable[Paper]) -> dict[str, int]:
    """Counts per triage label, plus ``total`` — the numbers a screening
    appendix reports.

    Excluded records are counted *and* broken out, so a reader can show
    "N retrieved, M screened out for reason R" without re-running the search.
    """
    counts: dict[str, int] = {label: 0 for label in TRIAGE_LABELS}
    total = 0
    for paper in papers:
        counts[paper.triage_label()] += 1
        total += 1
    counts["total"] = total
    return counts


def screening_report(papers: Iterable[Paper]) -> dict[str, object]:
    """Is this a *screened* set, or just a raw search dump?

    A Literature Matrix is defined by its triage decisions — the Week 3 lab's
    "skim into core / general / not-relevant" step — so a matrix whose rows
    are all ``unreviewed`` is not a literature matrix, it is a search result
    with a header row.  This reports the difference explicitly, so the
    artefact can say so itself instead of a reader (or a marker) having to
    notice.

    Returns the :func:`screen_summary` counts plus:

    ``screened``
        records carrying a real triage decision (``core``, ``general`` or
        ``excluded``).
    ``ready``
        ``True`` only when the set is non-empty and *every* record is
        triaged.  Empty is never ready: "no records" is not a screened set.
    ``message``
        an actionable sentence naming what is missing and the command that
        fixes it — empty when ``ready``.

    Pure and offline; takes any iterable of records.
    """
    counts = screen_summary(papers)
    total = int(counts["total"])
    unreviewed = int(counts["unreviewed"])
    screened = total - unreviewed
    ready = total > 0 and unreviewed == 0

    if ready:
        message = ""
    elif total == 0:
        message = (
            "no records to screen — widen the search (--query, --max, or drop "
            "--min-score) before building a matrix"
        )
    else:
        message = (
            f"screening incomplete: {unreviewed} of {total} records are still "
            f"'unreviewed'. A matrix records screening decisions, so label "
            f"them first: researcher-python label FILE --set "
            f"core|general|excluded"
        )

    return {
        **counts,
        "screened": screened,
        "ready": ready,
        "message": message,
    }


def _gate(papers: Iterable[Paper], require_screening: bool) -> dict[str, object]:
    """Return the screening report, raising when the caller demands readiness."""
    report = screening_report(papers)
    if require_screening and not report["ready"]:
        raise ExportError(str(report["message"]))
    return report


# ---------------------------------------------------------------------- #
# Writers
# ---------------------------------------------------------------------- #


def _write(text: str, dest: str | os.PathLike[str] | TextIO) -> None:
    try:
        with _atomic_text(dest) as handle:
            handle.write(text)
    except OSError as exc:
        raise ExportError(f"could not write {_label(dest)}: {exc}", cause=exc) from exc


def to_matrix_csv(
    papers: Iterable[Paper],
    dest: str | os.PathLike[str] | TextIO,
    columns: Sequence[str] | None = None,
    *,
    require_screening: bool = False,
    cite_keys: Mapping[str, str] | None = None,
) -> None:
    """Write :func:`render_matrix_csv` to a path or an open file object."""
    _write(
        render_matrix_csv(
            papers, columns, require_screening=require_screening, cite_keys=cite_keys
        ),
        dest,
    )


def to_matrix_markdown(
    papers: Iterable[Paper],
    dest: str | os.PathLike[str] | TextIO,
    columns: Sequence[str] | None = None,
    *,
    require_screening: bool = False,
    cite_keys: Mapping[str, str] | None = None,
) -> None:
    """Write :func:`render_matrix_markdown` to a path or an open file object."""
    _write(
        render_matrix_markdown(
            papers,
            columns,
            require_screening=require_screening,
            cite_keys=cite_keys,
        ),
        dest,
    )


def to_synthesis_csv(
    papers: Iterable[Paper],
    dest: str | os.PathLike[str] | TextIO,
    dimension: str = "year",
    *,
    require_screening: bool = False,
) -> None:
    """Write :func:`render_synthesis_csv` to a path or an open file object."""
    _write(
        render_synthesis_csv(papers, dimension, require_screening=require_screening),
        dest,
    )


def to_synthesis_markdown(
    papers: Iterable[Paper],
    dest: str | os.PathLike[str] | TextIO,
    dimension: str = "year",
    *,
    require_screening: bool = False,
) -> None:
    """Write :func:`render_synthesis_markdown` to a path or an open file object."""
    _write(
        render_synthesis_markdown(
            papers, dimension, require_screening=require_screening
        ),
        dest,
    )