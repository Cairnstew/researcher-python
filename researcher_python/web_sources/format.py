"""Render :class:`~researcher_python.web_sources.base.WebResult` records as bibliography files.

A Stack Overflow question — or any future web record — is a *citable work*:
papers cite them, and the standard shapes are CSL ``webpage``, BibTeX
``@misc`` (plain-BibTeX compatible; ``@online`` is biblatex-only) and a RIS
``GEN`` record.  This module is the web source's counterpart to
:mod:`researcher_python.format`, with the same rules:

* **Local and pure.**  Given the same ``WebResult`` list, the same bytes come
  out, in the same order.  Nothing reads a clock or a server.
* **Sparse is valid.**  A record with no author or date still produces a valid
  entry rather than ``author = {}`` noise.
* **Optional ``accessed``.**  A web citation usually records when it was last
  accessed.  Because that date is *not* part of the record, every renderer
  takes it as an explicit argument and omits it by default — so the default
  output stays byte-deterministic (D13-style), and a caller who wants
  ``Accessed: ...`` opt in with one argument.

Conventions chosen for web records (deliberately distinct from papers):
author names are **usernames**, so CSL renders them as ``{"literal": ...}``
(never a fake family/given split); the site slug maps to a display name for
``container-title`` / ``howpublished`` / ``T2`` (``stackoverflow`` →
``Stack Overflow``); the CSL id is ``<source>-<id>`` (``stackoverflow-5946236``)
because a web id, unlike a DOI, is only unique under its source; dates come
from the epoch-seconds ``created`` field, in UTC.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import unicodedata
from collections.abc import Iterable
from typing import Any, TextIO

from ..errors import ExportError
from ..export import _atomic_text, _label
from .base import WebResult

__all__ = [
    "web_date",
    "web_bibtex_entry",
    "web_bibtex_key",
    "web_csl_item",
    "web_ris_entry",
    "render_web_bibtex",
    "render_web_csl_json",
    "render_web_ris",
    "web_to_bibtex",
    "web_to_csl_json",
    "web_to_ris",
]

#: Title words ignored when picking the key's distinguishing word.  The
#: scholarly formatter's set plus the interrogatives and question openers
#: that dominate Q&A titles — without them every "How to X?" record would
#: collapse onto the key ``{author}{year}how``.
_KEY_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "based", "by", "for", "from",
        "in", "into", "is", "new", "novel", "of", "on", "or", "study", "the",
        "to", "toward", "towards", "using", "via", "with",
        "can", "could", "do", "does", "did", "get", "got", "how", "my",
        "should", "what", "when", "where", "which", "who", "why", "will",
        "would", "your",
    }
)

#: Common Stack Exchange site slugs -> display names used as the container
#: title / howpublished / RIS T2.  Unknown sites fall back to the slug.
_SITE_TITLES = {
    "reddit": "Reddit",
    "stackoverflow": "Stack Overflow",
    "superuser": "Super User",
    "serverfault": "Server Fault",
    "askubuntu": "Ask Ubuntu",
    "mathoverflow": "MathOverflow",
    "stackapps": "Stack Apps",
}

_TITLE_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_KEY_STRIP = re.compile(r"[^a-z0-9]+")

#: CSL item type for a Q&A / thread record.  ``webpage`` is the mapping
#: citation managers use for Stack Overflow; interpreters wanting ``post``
#: can remap the type at render time.
CSL_WEB_TYPE = "webpage"


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #


def _squash(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _fold(value: Any) -> str:
    text = _squash(value)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _brace_safe(value: Any) -> str:
    return _squash(value).replace("{", "").replace("}", "")


def _family(name: str) -> str:
    """Best-effort family word from a display name or username."""
    text = _squash(name)
    if not text:
        return ""
    if "," in text:
        return text.split(",", 1)[0].strip()
    parts = text.split()
    return parts[-1] if parts else ""


def _date_parts(epoch: int | None) -> tuple[int, int, int] | None:
    """UTC ``(year, month, day)`` from epoch seconds, or ``None``.

    ``None``, zero and clearly bogus timestamps (before 1970) yield ``None``
    so a sparse record stays sparse.
    """
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)) or epoch <= 0:
        return None
    date = _dt.datetime.fromtimestamp(float(epoch), tz=_dt.timezone.utc).date()
    return date.year, date.month, date.day


def _site_title(source: str) -> str:
    return _SITE_TITLES.get(source, source)


def _iso_date(value: str | tuple[int, int, int]) -> str:
    if isinstance(value, tuple):
        return f"{value[0]:04d}-{value[1]:02d}-{value[2]:02d}"
    return _squash(value)


def _accessed_note(accessed: str | tuple[int, int, int] | None) -> str:
    return "" if accessed is None else f"Accessed: {_iso_date(accessed)}"


def web_date(result: WebResult) -> str:
    """UTC ``YYYY-MM-DD`` for a record's ``created`` epoch, or ``""`` if unknown.

    The display counterpart to the private date helpers, for callers rendering
    a table: a :class:`WebResult` has no ``date`` field (only the epoch
    ``created``), so a terminal view needs this rather than reaching in here.
    Sparse records stay sparse — an unknown timestamp renders as an empty cell
    rather than a fake date.
    """
    parts = _date_parts(result.created)
    return _iso_date(parts) if parts else ""


# ---------------------------------------------------------------------- #
# BibTeX
# ---------------------------------------------------------------------- #


def web_bibtex_key(result: WebResult) -> str:
    """A stable, ASCII citation key: ``family/username + year + title-word``.

    ``family`` is derived from the author's display name (user names included);
    the source slug stands in when no author is recorded.  Uniqueness across a
    document is :func:`render_web_bibtex`'s job, not this function's.
    """
    family = _KEY_STRIP.sub(
        "", _fold(_family(result.author) if result.author else "").lower()
    ) or _KEY_STRIP.sub("", _fold(result.source).lower())
    date = _date_parts(result.created)
    year = str(date[0]) if date else "nd"

    word = ""
    for token in _TITLE_SPLIT.split(_fold(result.title)):
        if token and token.lower() not in _KEY_STOPWORDS:
            word = token
            break

    key = _KEY_STRIP.sub("", f"{family}{year}{word}".lower())
    return key or "untitled"


def web_bibtex_entry(
    result: WebResult,
    key: str | None = None,
    *,
    accessed: str | tuple[int, int, int] | None = None,
) -> str:
    """One ``@misc{key, ...}`` entry, with a trailing newline.

    ``howpublished`` carries the site display name, ``url`` the question URL;
    an optional ``note = {Accessed: ...}`` is emitted only when ``accessed`` is
    given, keeping the default output deterministic.
    """
    fields: list[tuple[str, str]] = []
    if author := _brace_safe(result.author):
        fields.append(("author", author))
    if title := _brace_safe(result.title):
        fields.append(("title", title))
    if site := _site_title(result.source):
        fields.append(("howpublished", site))
    date = _date_parts(result.created)
    if date:
        fields.append(("year", str(date[0])))
    if result.url:
        fields.append(("url", _brace_safe(result.url)))
    if note := _accessed_note(accessed):
        fields.append(("note", note))

    lines = [f"@misc{{{key or web_bibtex_key(result)},"]
    for name, value in fields:
        lines.append(f"  {name} = {{{value}}},")
    lines.append("}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- #
# RIS
# ---------------------------------------------------------------------- #


def web_ris_entry(
    result: WebResult,
    *,
    accessed: str | tuple[int, int, int] | None = None,
) -> str:
    """One RIS record for a web source, ``TY  - GEN`` through ``ER  - ``."""
    lines = ["TY  - GEN"]
    if title := _squash(result.title):
        lines.append(f"TI  - {title}")
    if author := _squash(result.author):
        lines.append(f"AU  - {author}")
    date = _date_parts(result.created)
    if date:
        lines.append(f"PY  - {date[0]}")
        lines.append(f"DA  - {date[0]:04d}/{date[1]:02d}/{date[2]:02d}/")
    if site := _site_title(result.source):
        lines.append(f"T2  - {site}")
    if result.url:
        lines.append(f"UR  - {result.url}")
    for tag in result.tags:
        if text := _squash(tag):
            lines.append(f"KW  - {text}")
    if note := _accessed_note(accessed):
        lines.append(f"N1  - {note}")

    lines.append("ER  - ")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- #
# CSL-JSON
# ---------------------------------------------------------------------- #


def web_csl_item(
    result: WebResult,
    *,
    accessed: str | tuple[int, int, int] | None = None,
) -> dict[str, Any]:
    """One canonical CSL-JSON item (``type: webpage``).

    The author is a username, so CSL renders it as ``{"literal": ...}`` — the
    spec's escape hatch for names that must not be split into family/given.
    ``issued`` comes from ``created`` (UTC date-parts); ``accessed`` is added
    only when given.
    """
    item: dict[str, Any] = {
        "id": web_csl_id(result),
        "type": CSL_WEB_TYPE,
    }
    if result.title:
        item["title"] = _squash(result.title)
    if result.author:
        item["author"] = [{"literal": _squash(result.author)}]
    date = _date_parts(result.created)
    if date:
        item["issued"] = {"date-parts": [[date[0], date[1], date[2]]]}
    if site := _site_title(result.source):
        item["container-title"] = site
    if result.url:
        item["URL"] = _squash(result.url)
    if result.tags:
        item["keyword"] = ", ".join(_squash(t) for t in result.tags)
    if accessed is not None:
        year, month, day = _accessed_parts(accessed)
        item["accessed"] = {"date-parts": [[year, month, day]]}
    return item


def web_csl_id(result: WebResult) -> str:
    """CSL id for a web record: ``<source>-<id>`` (ids are source-local)."""
    if result.source and result.id:
        return f"{result.source}-{result.id}"
    if result.id:
        return result.id
    return web_bibtex_key(result)


def _accessed_parts(accessed: str | tuple[int, int, int]) -> tuple[int, int, int]:
    if isinstance(accessed, tuple):
        return int(accessed[0]), int(accessed[1]), int(accessed[2])
    date = _dt.date.fromisoformat(_squash(accessed))
    return date.year, date.month, date.day


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


def render_web_bibtex(
    results: Iterable[WebResult],
    *,
    accessed: str | tuple[int, int, int] | None = None,
) -> str:
    """All entries as one BibTeX string, keys made unique, input order kept."""
    used: set[str] = set()
    entries = [
        web_bibtex_entry(result, _unique_key(web_bibtex_key(result), used), accessed=accessed)
        for result in results
    ]
    return "\n".join(entries)


def render_web_ris(
    results: Iterable[WebResult],
    *,
    accessed: str | tuple[int, int, int] | None = None,
) -> str:
    """All records as one RIS string, input order kept."""
    return "\n".join(web_ris_entry(result, accessed=accessed) for result in results)


def render_web_csl_json(
    results: Iterable[WebResult],
    *,
    accessed: str | tuple[int, int, int] | None = None,
) -> str:
    """A JSON array of CSL items, pretty-printed, with a trailing newline."""
    items = [web_csl_item(result, accessed=accessed) for result in results]
    return json.dumps(items, ensure_ascii=False, indent=2) + "\n"


# ---------------------------------------------------------------------- #
# Writing
# ---------------------------------------------------------------------- #


def web_to_bibtex(
    results: Iterable[WebResult],
    dest: str | os.PathLike[str] | TextIO,
    *,
    accessed: str | tuple[int, int, int] | None = None,
) -> None:
    """Write :func:`render_web_bibtex` to a path or an open file object."""
    _write(render_web_bibtex(results, accessed=accessed), dest)


def web_to_ris(
    results: Iterable[WebResult],
    dest: str | os.PathLike[str] | TextIO,
    *,
    accessed: str | tuple[int, int, int] | None = None,
) -> None:
    """Write :func:`render_web_ris` to a path or an open file object."""
    _write(render_web_ris(results, accessed=accessed), dest)


def web_to_csl_json(
    results: Iterable[WebResult],
    dest: str | os.PathLike[str] | TextIO,
    *,
    accessed: str | tuple[int, int, int] | None = None,
) -> None:
    """Write :func:`render_web_csl_json` to a path or an open file object."""
    _write(render_web_csl_json(results, accessed=accessed), dest)


def _write(text: str, dest: str | os.PathLike[str] | TextIO) -> None:
    try:
        with _atomic_text(dest) as handle:
            handle.write(text)
    except OSError as exc:
        raise ExportError(f"could not write {_label(dest)}: {exc}", cause=exc) from exc