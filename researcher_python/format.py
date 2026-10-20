"""Render :class:`~researcher_python.models.Paper` records as bibliography files.

Three formats, all produced **locally from the model** with no network and no
third-party service:

* **BibTeX** (:func:`to_bibtex`) — one ``@type{key, ...}`` entry per paper.
* **RIS** (:func:`to_ris`) — the tagged line format EndNote/Zotero import.
* **CSL-JSON** (:func:`to_csl_json`) — the canonical Citation Style Language
  item shape, which any CSL processor (citeproc) can turn into a styled
  citation.  Authors use the standard ``{"family", "given"}`` keys.

Everything is a pure function of the records: given the same ``Paper`` list, the
same bytes come out, in the same order.  That is deliberate — a bibliography is
something a caller diffs and commits, so nothing here may depend on a clock, a
server, or dict iteration order.

Only fields the record actually carries are emitted, so a sparse ``Paper``
produces a sparse (still valid) entry rather than ``author = {}`` noise.  Every
writer takes a path *or* an already-open text file object; a path is written
atomically (a sibling temp file published with :func:`os.replace`), matching
:mod:`researcher_python.export`.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Iterable
from typing import Any, TextIO

from .errors import ExportError
from .export import _atomic_text, _label
from .models import Author, Paper

__all__ = [
    "bibtex_entry",
    "bibtex_key",
    "csl_item",
    "harvard_author_sort_key",
    "harvard_citation",
    "harvard_citation_map",
    "harvard_entry",
    "render_bibtex",
    "render_csl_json",
    "render_harvard",
    "render_ris",
    "ris_entry",
    "to_bibtex",
    "to_csl_json",
    "to_harvard",
    "to_ris",
]

#: Title words ignored when picking the key's distinguishing word.
_KEY_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "based", "by", "for", "from",
        "in", "into", "is", "new", "novel", "of", "on", "or", "study", "the",
        "to", "toward", "towards", "using", "via", "with",
    }
)

#: ``work_type`` substring -> BibTeX entry type.  Order matters: more specific
#: first, so ``proceedings-article`` is an ``inproceedings``, not an ``article``.
_BIBTEX_TYPES: tuple[tuple[str, str], ...] = (
    ("proceedings", "inproceedings"),
    ("conference", "inproceedings"),
    ("chapter", "incollection"),
    ("monograph", "book"),
    ("book", "book"),
    ("thesis", "phdthesis"),
    ("dissertation", "phdthesis"),
    ("report", "techreport"),
    ("preprint", "misc"),
    ("posted-content", "misc"),
    ("dataset", "misc"),
    ("review", "article"),
    ("journal", "article"),
    ("article", "article"),
)

#: ``work_type`` substring -> RIS reference type, same ordering rule.
_RIS_TYPES: tuple[tuple[str, str], ...] = (
    ("proceedings", "CPAPER"),
    ("conference", "CPAPER"),
    ("chapter", "CHAP"),
    ("monograph", "BOOK"),
    ("book", "BOOK"),
    ("thesis", "THES"),
    ("dissertation", "THES"),
    ("report", "RPRT"),
    ("preprint", "GEN"),
    ("posted-content", "GEN"),
    ("dataset", "DATA"),
    ("review", "JOUR"),
    ("journal", "JOUR"),
    ("article", "JOUR"),
)

#: ``work_type`` substring -> CSL item type, same ordering rule.
_CSL_TYPES: tuple[tuple[str, str], ...] = (
    ("proceedings", "paper-conference"),
    ("conference", "paper-conference"),
    ("chapter", "chapter"),
    ("monograph", "book"),
    ("book", "book"),
    ("thesis", "thesis"),
    ("dissertation", "thesis"),
    ("report", "report"),
    ("dataset", "dataset"),
    ("preprint", "article"),
    ("posted-content", "article"),
    ("review", "article-journal"),
    ("journal", "article-journal"),
    ("article", "article-journal"),
)

_TITLE_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_KEY_STRIP = re.compile(r"[^a-z0-9]+")
_PAGE_SPLIT = re.compile(r"\s*[-\u2013\u2014]\s*")


def _squash(value: Any) -> str:
    """Collapse whitespace and coerce to ``str`` (``""`` for ``None``)."""
    if value is None:
        return ""
    return " ".join(str(value).split())


def _fold(value: Any) -> str:
    """ASCII-fold ``value`` (a BibTeX key may not carry accents)."""
    text = _squash(value)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _brace_safe(value: Any) -> str:
    """A field value with braces removed, so it cannot unbalance an entry."""
    return _squash(value).replace("{", "").replace("}", "")


def _entry_type(work_type: str, table: tuple[tuple[str, str], ...], default: str) -> str:
    needle = _squash(work_type).lower()
    for token, mapped in table:
        if token in needle:
            return mapped
    return default


def _family(name: str) -> str:
    """Best-effort family name from a display name (``"Family, Given"`` kept)."""
    text = _squash(name)
    if not text:
        return ""
    if "," in text:
        return text.split(",", 1)[0].strip()
    parts = text.split()
    return parts[-1] if parts else ""


def _given(name: str) -> str:
    """Best-effort given name, the complement of :func:`_family`."""
    text = _squash(name)
    if not text:
        return ""
    if "," in text:
        return text.split(",", 1)[1].strip()
    parts = text.split()
    return " ".join(parts[:-1]) if len(parts) > 1 else ""


def _best_id(paper: Paper) -> str:
    return paper.doi or paper.arxiv_id or paper.pmid or paper.pmcid or ""


# ---------------------------------------------------------------------- #
# BibTeX
# ---------------------------------------------------------------------- #


def bibtex_key(paper: Paper) -> str:
    """A stable, ASCII citation key: ``family + year + title-word``.

    Deterministic for a given record; uniqueness across a document is
    :func:`render_bibtex`'s job, not this function's.
    """
    family = _KEY_STRIP.sub(
        "", _fold(_family(paper.authors[0].name) if paper.authors else "").lower()
    )
    year = str(paper.year) if paper.year is not None else "nd"

    word = ""
    for token in _TITLE_SPLIT.split(_fold(paper.title)):
        if token and token.lower() not in _KEY_STOPWORDS:
            word = token
            break

    key = _KEY_STRIP.sub("", f"{family}{year}{word}".lower())
    return key or "untitled"


def _bibtex_author(author: Author) -> str:
    name = _brace_safe(author.name)
    if not name:
        return ""
    if "," in name:
        return name
    family, given = _family(name), _given(name)
    return f"{family}, {given}" if given else family


def _bibtex_fields(paper: Paper, entry: str) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []

    authors = " and ".join(a for a in (_bibtex_author(x) for x in paper.authors) if a)
    if authors:
        fields.append(("author", authors))

    if title := _brace_safe(paper.title):
        fields.append(("title", title))

    venue = _brace_safe(paper.venue)
    if venue:
        if entry == "article":
            fields.append(("journal", venue))
        elif entry == "inproceedings":
            fields.append(("booktitle", venue))

    if paper.year is not None:
        fields.append(("year", str(paper.year)))
    if paper.volume:
        fields.append(("volume", _brace_safe(paper.volume)))
    if paper.issue:
        fields.append(("number", _brace_safe(paper.issue)))
    if paper.pages:
        fields.append(("pages", _brace_safe(paper.pages).replace("--", "-")))
    if paper.publisher:
        fields.append(("publisher", _brace_safe(paper.publisher)))
    if paper.doi:
        fields.append(("doi", _brace_safe(paper.doi)))
    if paper.arxiv_id:
        fields.append(("eprint", _brace_safe(paper.arxiv_id)))
        fields.append(("archivePrefix", "arXiv"))
    if paper.url:
        fields.append(("url", _brace_safe(paper.url)))
    if paper.keywords:
        fields.append(("keywords", ", ".join(_brace_safe(k) for k in paper.keywords)))
    return fields


def bibtex_entry(paper: Paper, key: str | None = None) -> str:
    """One ``@type{key, ...}`` BibTeX entry, with a trailing newline."""
    entry = _entry_type(paper.work_type, _BIBTEX_TYPES, "misc")
    lines = [f"@{entry}{{{key or bibtex_key(paper)},"]
    for name, value in _bibtex_fields(paper, entry):
        lines.append(f"  {name} = {{{value}}},")
    lines.append("}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- #
# RIS
# ---------------------------------------------------------------------- #


def _ris_author(author: Author) -> str:
    name = _squash(author.name)
    if not name:
        return ""
    if "," in name:
        return name
    family, given = _family(name), _given(name)
    return f"{family}, {given}" if given else family


def ris_entry(paper: Paper) -> str:
    """One RIS record, ``TY  - ...`` through a closing ``ER  - `` line."""
    kind = _entry_type(paper.work_type, _RIS_TYPES, "GEN")
    lines = [f"TY  - {kind}"]

    if title := _squash(paper.title):
        lines.append(f"TI  - {title}")
    for author in paper.authors:
        if name := _ris_author(author):
            lines.append(f"AU  - {name}")
    if paper.year is not None:
        lines.append(f"PY  - {paper.year}")
    if venue := _squash(paper.venue):
        # JOUR journals use JO; books/proceedings use T2.
        lines.append(f"{'JO' if kind == 'JOUR' else 'T2'}  - {venue}")
    if paper.volume:
        lines.append(f"VL  - {_squash(paper.volume)}")
    if paper.issue:
        lines.append(f"IS  - {_squash(paper.issue)}")
    if pages := _squash(paper.pages):
        parts = [part for part in _PAGE_SPLIT.split(pages) if part]
        if parts:
            lines.append(f"SP  - {parts[0]}")
            if len(parts) > 1:
                lines.append(f"EP  - {parts[-1]}")
    if paper.publisher:
        lines.append(f"PB  - {_squash(paper.publisher)}")
    if paper.doi:
        lines.append(f"DO  - {_squash(paper.doi)}")
    if paper.arxiv_id:
        lines.append(f"AN  - {_squash(paper.arxiv_id)}")
    if paper.url:
        lines.append(f"UR  - {_squash(paper.url)}")
    if paper.abstract:
        lines.append(f"AB  - {_squash(paper.abstract)}")
    for keyword in paper.keywords:
        if text := _squash(keyword):
            lines.append(f"KW  - {text}")

    lines.append("ER  - ")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- #
# CSL-JSON
# ---------------------------------------------------------------------- #


def _csl_author(author: Author) -> dict[str, str]:
    """Canonical CSL name object: ``{"family", "given"}`` (not ``full``)."""
    name = _squash(author.name)
    if not name:
        return {}
    if author.organization:
        return {"literal": name}
    given = _given(name)
    return {"family": _family(name), "given": given} if given else {"literal": name}


def csl_item(paper: Paper) -> dict[str, Any]:
    """One canonical CSL-JSON item.

    Uses the standard ``author`` shape (``{"family", "given"}``) and
    ``issued.date-parts``, so any citeproc implementation renders it without a
    bespoke mapping.
    """
    item: dict[str, Any] = {
        "id": _best_id(paper) or bibtex_key(paper),
        "type": _entry_type(paper.work_type, _CSL_TYPES, "document"),
    }
    if paper.title:
        item["title"] = _squash(paper.title)
    authors = [name for name in (_csl_author(a) for a in paper.authors) if name]
    if authors:
        item["author"] = authors
    if paper.year is not None:
        item["issued"] = {"date-parts": [[paper.year]]}
    elif paper.published:
        item["issued"] = {"literal": _squash(paper.published)}
    if paper.venue:
        item["container-title"] = _squash(paper.venue)
    if paper.volume:
        item["volume"] = _squash(paper.volume)
    if paper.issue:
        item["issue"] = _squash(paper.issue)
    if paper.pages:
        item["page"] = _squash(paper.pages)
    if paper.publisher:
        item["publisher"] = _squash(paper.publisher)
    if paper.doi:
        item["DOI"] = _squash(paper.doi)
    if paper.url:
        item["URL"] = _squash(paper.url)
    if paper.abstract:
        item["abstract"] = _squash(paper.abstract)
    if paper.keywords:
        item["keyword"] = ", ".join(_squash(k) for k in paper.keywords)
    if paper.language:
        item["language"] = _squash(paper.language)
    if paper.license:
        item["license"] = _squash(paper.license)
    return item


# ---------------------------------------------------------------------- #
# Whole-document rendering
# ---------------------------------------------------------------------- #


def _unique_key(base: str, used: set[str]) -> str:
    """``base``, then ``basea``/``baseb``/... so a document never repeats a key."""
    if base not in used:
        used.add(base)
        return base
    for index in range(26):
        candidate = f"{base}{chr(ord('a') + index)}"
        if candidate not in used:
            used.add(candidate)
            return candidate
    counter = 1
    while f"{base}{counter}" in used:
        counter += 1
    chosen = f"{base}{counter}"
    used.add(chosen)
    return chosen


def render_bibtex(papers: Iterable[Paper]) -> str:
    """All entries as one BibTeX string, keys made unique, input order kept."""
    used: set[str] = set()
    entries = [
        bibtex_entry(paper, _unique_key(bibtex_key(paper), used)) for paper in papers
    ]
    return "\n".join(entries)


def render_ris(papers: Iterable[Paper]) -> str:
    """All records as one RIS string, input order kept."""
    return "\n".join(ris_entry(paper) for paper in papers)


def render_csl_json(papers: Iterable[Paper]) -> str:
    """A JSON array of CSL items, pretty-printed, with a trailing newline."""
    items = [csl_item(paper) for paper in papers]
    return json.dumps(items, ensure_ascii=False, indent=2) + "\n"


# ---------------------------------------------------------------------- #
# Writing
# ---------------------------------------------------------------------- #


def to_bibtex(papers: Iterable[Paper], dest: str | os.PathLike[str] | TextIO) -> None:
    """Write :func:`render_bibtex` to a path or an open file object."""
    _write(render_bibtex(papers), dest)


def to_ris(papers: Iterable[Paper], dest: str | os.PathLike[str] | TextIO) -> None:
    """Write :func:`render_ris` to a path or an open file object."""
    _write(render_ris(papers), dest)


def to_csl_json(papers: Iterable[Paper], dest: str | os.PathLike[str] | TextIO) -> None:
    """Write :func:`render_csl_json` to a path or an open file object."""
    _write(render_csl_json(papers), dest)


def _write(text: str, dest: str | os.PathLike[str] | TextIO) -> None:
    try:
        with _atomic_text(dest) as handle:
            handle.write(text)
    except OSError as exc:
        raise ExportError(f"could not write {_label(dest)}: {exc}", cause=exc) from exc


# ---------------------------------------------------------------------- #
# Harvard (Cite Them Right)
# ---------------------------------------------------------------------- #
#
# GCU's coursework briefs for MMI226823 mandate **Harvard** referencing, so
# this is the format that actually has to be right for the assessed work — not
# BibTeX.  It follows Cite Them Right (4th ed.):
#
#   Author, A.A. and Author, B.B. (2023) *Title of article*. Journal Name,
#   12(3), pp. 45-67. doi: 10.1000/xyz.
#
#   Author, A.A. (2023) *Title of report*. Publisher. Available at: URL
#   (Accessed: 22 October 2026).
#
# Four or more authors collapse to the first three plus ``et al.`` in both the
# reference list and the in-text citation.
#
# **Honest limitation:** Cite Them Right wants an edition and a place of
# publication for books and ``(Place: Publisher)`` for reports.  :class:`Paper`
# carries neither, so those segments are omitted rather than invented — a
# missing place is a visible gap the writer fills in, a fabricated one is a
# silent error in a submitted reference list.

#: Author names Harvard never abbreviates (organisations, `et al.` expansions).
_HARVARD_SUFFIXES = ("jr", "jr.", "sr", "sr.", "ii", "iii", "iv")


def _initials(given: str) -> str:
    """``"John Ronald"`` -> ``"J.R."``; ``"Jean-Paul"`` -> ``"J.-P."``.

    Cite Them Right writes initials unspaced and dot-terminated.  A hyphenated
    forename keeps its hyphen (``J.-P.``), and a name that is only an initial
    (``"J."``) is not doubled.
    """
    if not given:
        return ""
    out: list[str] = []
    for token in _squash(given).split():
        if not token:
            continue
        pieces = [p for p in token.split("-") if p]
        for piece in pieces:
            out.append(f"{piece[0].upper()}.")
        if len(pieces) > 1:
            # Re-join so "Jean-Paul" -> "J.-P." rather than "J.P.".
            out[-len(pieces):] = ["-".join(out[-len(pieces):])]
    return "".join(out)


def _harvard_name(author: Author) -> str:
    """``"Family, F.M."``, or the literal name when it cannot be split.

    An organisation name ("ACM", "OECD") has no given part, so it is kept
    whole — Harvard cites a corporate author by its full name, and forcing a
    family/given split would render ``ACM, A.`` .
    """
    raw = _squash(author.name)
    if not raw:
        return ""
    if author.organization:
        # A body is cited by its full name; splitting it invents initials.
        return raw
    family, given = _family(raw), _given(raw)
    if not family:
        return raw
    if not given:
        return family
    return f"{family}, {_initials(given)}"


def _harvard_authors(paper: Paper) -> str:
    """Author string, truncated to three names plus ``et al.``."""
    names = [n for n in (_harvard_name(a) for a in paper.authors) if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) <= 3:
        return ", ".join(names[:-1]) + " and " + names[-1]
    # Cite Them Right: three names joined with "and", then "et al.".
    return ", ".join(names[:2]) + " and " + names[2] + " et al."


def _harvard_lead(paper: Paper) -> str:
    """``"Author, A.A. (2023) "`` — or just the year when there is no author."""
    year = paper.year if paper.year is not None else "n.d."
    authors = _harvard_authors(paper)
    return f"{authors} ({year}) " if authors else f"({year}) "


def _harvard_pages(pages: str) -> str:
    """``"45-67"`` -> ``"pp. 45-67"``; an electronic locator keeps its own form."""
    text = _squash(pages)
    if not text:
        return ""
    if not re.search(r"\d", text):
        return f"p. {text}"
    return f"pp. {text}"


def _harvard_url(paper: Paper, accessed: str) -> str:
    """``Available at: URL`` plus an access date when one is given."""
    url = _squash(paper.url) or _squash(paper.pdf_url)
    if not url:
        return ""
    tail = f" (Accessed: {accessed})" if accessed else ""
    return f"Available at: {url}{tail}"


def _harvard_kind(work_type: str) -> str:
    """Classify a record into the Harvard entry shapes we can render."""
    needle = _squash(work_type).lower()
    if "proceedings" in needle or "conference" in needle:
        return "conference"
    if "chapter" in needle:
        return "chapter"
    if "monograph" in needle or "book" in needle:
        return "book"
    if "thesis" in needle or "dissertation" in needle:
        return "thesis"
    if "report" in needle or "posted-content" in needle or "preprint" in needle:
        return "report"
    if "dataset" in needle:
        return "dataset"
    return "article"


def harvard_entry(paper: Paper, accessed: str = "") -> str:
    """One Harvard reference-list entry, without a trailing newline.

    ``accessed`` is an explicit argument (never read from a clock) so the same
    record always renders to the same bytes.
    """
    lead = _harvard_lead(paper)
    title = _squash(paper.title)
    venue = _squash(paper.venue)
    kind = _harvard_kind(paper.work_type)

    parts: list[str] = []
    if title:
        # Cite Them Right italicises the container-of-a-whole-work only for
        # stand-alone titles; an article title stays roman.
        parts.append(title)

    if kind == "article" and venue:
        locator = venue
        if paper.volume:
            locator += f", {paper.volume}"
        if paper.issue:
            locator += f"({paper.issue})"
        pages = _harvard_pages(paper.pages)
        parts.append(f"{locator}{', ' + pages if pages else ''}")
    elif kind == "conference":
        where = venue or _squash(paper.publisher)
        pages = _harvard_pages(paper.pages)
        tail = f", {pages}" if pages else ""
        # No container recorded: emit the pages alone rather than a dangling
        # "In:" with nothing after it.
        parts.append(f"In: {where}{tail}" if where else (pages or ""))
    elif kind == "chapter":
        pages = _harvard_pages(paper.pages)
        tail = f", {pages}" if pages else ""
        parts.append(f"In: {venue}{tail}" if venue else (pages or ""))
    elif kind == "thesis":
        institution = venue or _squash(paper.publisher)
        parts.append(f"[Doctoral thesis, {institution}]" if institution else "[Doctoral thesis]")
    elif kind == "dataset":
        if venue:
            parts.append(venue)
    elif venue:
        parts.append(venue)

    if paper.publisher and kind in {"book", "report"}:
        parts.append(_squash(paper.publisher))

    body = ". ".join(p for p in parts if p)
    # Single trailing period: join with ". " but never double it up.
    entry = f"{lead}{body}".rstrip(". ")
    entry = entry + "."

    tail = []
    if paper.doi:
        tail.append(f"doi: {paper.doi}.")
    url = _harvard_url(paper, accessed)
    if url:
        tail.append(f"{url}{'.' if not url.endswith('.') else ''}")
    if tail:
        entry = entry.rstrip(".") + ". " + " ".join(tail)
    return entry


def harvard_author_sort_key(paper: Paper) -> tuple[str, int, str]:
    """Alphabetisation key: first author's family name, then year, then title.

    Harvard reference lists are ordered alphabetically by author surname, which
    is the one thing a BibTeX dump cannot give you.
    """
    families = [
        _fold(_family(_squash(a.name))) for a in paper.authors if _squash(a.name)
    ]
    lead = families[0] if families else ""
    return (lead.lower(), paper.year if paper.year is not None else 0, _fold(paper.title).lower())


def render_harvard(papers: Iterable[Paper], accessed: str = "") -> str:
    """A Harvard reference list: one entry per line, alphabetical, blank-line separated."""
    ordered = sorted(papers, key=harvard_author_sort_key)
    return "\n\n".join(harvard_entry(p, accessed=accessed) for p in ordered)


def harvard_citation(paper: Paper) -> str:
    """The in-text citation for one record, e.g. ``(Floridi et al., 2018)``.

    Author-count follows Cite Them Right: one name, ``A and B``, ``A, B and C``,
    then ``A et al.`` from four up.  Missing year renders ``n.d.``; a record
    with no author cites a shortened title instead.
    """
    year = str(paper.year) if paper.year is not None else "n.d."
    names = [
        n
        for n in (
            _squash(a.name) if a.organization else _squash(_family(_squash(a.name)))
            for a in paper.authors
        )
        if n
    ]
    if not names:
        title = _squash(paper.title)
        short = title.split(":", 1)[0] if ":" in title else title
        words = short.split()
        return f"('{_squash(' '.join(words[:4]))}', {year})"
    # Surnames keep their source capitalisation — "(Floridi and Cowls, 2018)".
    if len(names) == 1:
        return f"({names[0]}, {year})"
    if len(names) == 2:
        return f"({names[0]} and {names[1]}, {year})"
    if len(names) == 3:
        return f"({names[0]}, {names[1]} and {names[2]}, {year})"
    return f"({names[0]} et al., {year})"


def harvard_citation_map(papers: Iterable[Paper]) -> dict[str, str]:
    """``{stable id -> "(Author, Year)"}`` for every record.

    Keyed on :meth:`Paper.primary_id` when there is one, else the
    :func:`bibtex_key`, so a matrix or a draft can hold ids and resolve the
    human-facing form later without re-searching.
    """
    out: dict[str, str] = {}
    for paper in papers:
        key = paper.primary_id() or bibtex_key(paper)
        out[key] = harvard_citation(paper)
    return out


def to_harvard(
    papers: Iterable[Paper],
    dest: str | os.PathLike[str] | TextIO,
    accessed: str = "",
) -> None:
    """Write :func:`render_harvard` to a path or an open file object."""
    _write(render_harvard(papers, accessed=accessed), dest)
