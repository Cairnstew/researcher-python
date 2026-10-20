"""Text and quote extraction from PDFs.

The module's labs are not "find the paper" — they are *extract the right
information out of one* (Week 2, `lab02 extracting information`).  This
package can download an open-access PDF (:mod:`researcher_python.download`); it
could not read it, which left the most-assessed step of the workflow with no
tool behind it.  This module closes that loop:

* :func:`extract_text` — the plain text of a file or a page range;
* :func:`find_quotes` — page-anchored passages containing given terms, so a
  note can record **where** an idea came from rather than a paraphrase with no
  provenance.

``pypdf`` is an **optional extra**, imported inside :func:`_reader` only, so
``import researcher_python`` stays lightweight and this module is importable
(its pure helpers are unit-testable) without the wheel installed.  See
:func:`quote_snippets` for the matching logic, which needs no PDF at all.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from typing import Any

from .errors import ExportError, SourceUnavailableError
from .sources.base import require_dependency, squash_text

__all__ = [
    "extract_text",
    "find_quotes",
    "iter_pages",
    "parse_page_spec",
    "quote_snippets",
]


def parse_page_spec(spec: str | None, total: int) -> list[int]:
    """Turn ``"1-5,10"`` into 1-indexed page numbers, clamped to ``total``.

    ``None`` or an empty spec means every page.  Out-of-range and reversed
    ranges are dropped or normalised rather than raising — a reader asking for
    pages 1-5 of a 3-page PDF wants what exists.  A page number below 1 is
    ignored, because page 0 is a mistake, not a request.
    """
    if total <= 0:
        return []
    if not spec or not spec.strip():
        return list(range(1, total + 1))

    wanted: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", chunk)
        if match:
            low, high = int(match.group(1)), int(match.group(2))
            if low > high:
                low, high = high, low
            wanted.update(range(low, high + 1))
        elif chunk.isdigit():
            wanted.add(int(chunk))
        else:
            raise ExportError(
                f"cannot read page selection {spec!r}; expected e.g. '1-5,10'"
            )
    return sorted(p for p in wanted if 1 <= p <= total)


def _reader(path: str | os.PathLike[str]) -> Any:
    """Open a PDF, or raise :class:`SourceUnavailableError` naming the wheel.

    The file is opened here rather than in the caller so a missing path and a
    missing dependency produce two different, actionable errors.
    """
    require_dependency("pypdf", "pdf")
    from pypdf import PdfReader  # noqa: PLC0415 - deliberately lazy

    try:
        return PdfReader(str(path))
    except OSError as exc:
        raise ExportError(f"cannot read {path}: {exc}", cause=exc) from exc
    except Exception as exc:  # pypdf raises its own family for bad files
        raise ExportError(f"{path} is not a readable PDF: {exc}", cause=exc) from exc


def iter_pages(path: str | os.PathLike[str]) -> list[tuple[int, str]]:
    """``[(page_number, text), ...]`` for every page, 1-indexed."""
    reader = _reader(path)
    out: list[tuple[int, str]] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # a single unreadable page must not lose the file
            text = ""
        out.append((index, squash_text(text)))
    return out


def extract_text(
    path: str | os.PathLike[str], pages: str | None = None
) -> str:
    """Plain text of ``path``, optionally restricted to ``pages`` (``"1-5,10"``).

    Pages are separated by a blank line so a reader (or a downstream quote
    search) can tell where one page ended and the next began.
    """
    collected = iter_pages(path)
    selected = set(parse_page_spec(pages, len(collected)))
    return "\n\n".join(text for number, text in collected if number in selected)


# ---------------------------------------------------------------------- #
# Quotes
# ---------------------------------------------------------------------- #


def _split_terms(terms: str | Sequence[str] | None) -> list[str]:
    if not terms:
        return []
    if isinstance(terms, str):
        raw = terms.split(",")
    else:
        raw = list(terms)
    return [squash_text(t).lower() for t in raw if squash_text(t)]


def quote_snippets(
    text: str, terms: str | Sequence[str] | None = None, *, window: int = 240
) -> list[str]:
    """Every passage of ``text`` containing any term, with context.

    Pure and PDF-free, so the matching rules are unit-testable offline.  With
    no terms, the opening of the text is returned (the "what is this about"
    fallback).  Overlapping matches are merged, so a term appearing twice in
    one sentence yields one snippet, not two.
    """
    body = squash_text(text)
    if not body:
        return []

    needles = _split_terms(terms)
    if not needles:
        return [body[:window].strip()]

    spans: list[tuple[int, int]] = []
    lowered = body.lower()
    for needle in needles:
        start = lowered.find(needle)
        while start != -1:
            spans.append((max(0, start - window // 2), min(len(body), start + len(needle) + window // 2)))
            start = lowered.find(needle, start + len(needle))

    if not spans:
        return []
    spans.sort()

    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [body[start:end].strip() for start, end in merged]


def find_quotes(
    path: str | os.PathLike[str],
    terms: str | Sequence[str] | None = None,
    *,
    pages: str | None = None,
    window: int = 240,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Page-anchored quotes from ``path``.

    Each hit is ``{"page", "term", "text"}``.  Page numbers are 1-indexed so a
    note can cite ``p. 14`` and mean it.  ``limit`` caps the total returned,
    filled page by page, so the first pages of a report win over the appendix.
    """
    collected = iter_pages(path)
    selected = set(parse_page_spec(pages, len(collected)))
    needles = _split_terms(terms)

    out: list[dict[str, Any]] = []
    for number, text in collected:
        if number not in selected or not text:
            continue
        snippets = quote_snippets(text, needles or None, window=window)
        for snippet in snippets:
            if len(out) >= limit:
                return out
            matched = next((n for n in needles if n in snippet.lower()), "")
            out.append({"page": number, "term": matched, "text": snippet})
    return out