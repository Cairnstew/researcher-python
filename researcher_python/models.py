"""The unified record model.

Every adapter in :mod:`researcher_python.sources` produces :class:`Paper`
objects, so the pipeline, the exporter, and the CLI only ever see one shape.
Plain dataclasses (not pydantic) keep the package dependency-light and make
fixtures trivially constructible in tests.

Everything here round-trips through ``json.dumps``: no datetimes, no Decimals,
no ``None`` surprises — dates are ISO-ish strings, counts are ``int | None``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .ids import normalize_title

SORT_MODES = ("relevance", "year", "citations", "title")

_STRING_FIELDS = (
    "doi",
    "arxiv_id",
    "pmid",
    "pmcid",
    "url",
    "pdf_url",
    "title",
    "abstract",
    "published",
    "venue",
    "volume",
    "issue",
    "pages",
    "publisher",
    "language",
    "work_type",
    "oa_status",
    "license",
)

_INT_FIELDS = ("year", "citation_count", "reference_count")


@dataclass(slots=True)
class Author:
    """One author on a :class:`Paper`.

    ``author_id`` is source-local (an OpenAlex ``A...`` id, a Semantic Scholar
    author id) and is deliberately *not* globally resolved — two sources that
    disagree about who wrote what must not be merged on a shared id.
    """

    name: str
    author_id: str = ""
    orcid: str = ""
    affiliations: list[str] = field(default_factory=list)

    def copy(self) -> Author:
        return Author(self.name, self.author_id, self.orcid, list(self.affiliations))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "author_id": self.author_id,
            "orcid": self.orcid,
            "affiliations": list(self.affiliations),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Author:
        return cls(
            name=str(data.get("name") or ""),
            author_id=str(data.get("author_id") or ""),
            orcid=str(data.get("orcid") or ""),
            affiliations=[str(x) for x in (data.get("affiliations") or [])],
        )


@dataclass(slots=True)
class Paper:
    """One scholarly work, normalised across every source that knows it."""

    # -- identity -------------------------------------------------------
    sources: list[str] = field(default_factory=list)
    ids: dict[str, str] = field(default_factory=dict)
    doi: str = ""
    arxiv_id: str = ""
    pmid: str = ""
    pmcid: str = ""
    url: str = ""
    pdf_url: str = ""

    # -- bibliographic --------------------------------------------------
    title: str = ""
    abstract: str = ""
    authors: list[Author] = field(default_factory=list)
    year: int | None = None
    published: str = ""
    venue: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    publisher: str = ""
    language: str = ""
    work_type: str = ""
    categories: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    # -- metrics / access -----------------------------------------------
    citation_count: int | None = None
    reference_count: int | None = None
    is_open_access: bool = False
    oa_status: str = ""
    license: str = ""

    # -- helpers ---------------------------------------------------------

    @property
    def source(self) -> str:
        """The first source that contributed this record."""
        return self.sources[0] if self.sources else ""

    def id_for(self, source: str) -> str:
        """This source's own record id, so a pipeline can re-query it."""
        return self.ids.get(source, "")

    def primary_id(self) -> str:
        """Best human-facing identifier: DOI, then arXiv id, then PMID."""
        return self.doi or self.arxiv_id or self.pmid or self.pmcid

    def merge_keys(self) -> list[str]:
        """Stable identifier keys, most authoritative first.

        Two records sharing *any* of these describe the same work.  Title is
        deliberately excluded — see :func:`title_key`.
        """
        keys: list[str] = []
        for prefix, value in (
            ("doi:", self.doi),
            ("arxiv:", self.arxiv_id),
            ("pmid:", self.pmid),
            ("pmcid:", self.pmcid),
        ):
            if value and f"{prefix}{value}" not in keys:
                keys.append(f"{prefix}{value}")
        return keys

    def title_key(self) -> str | None:
        """Folded title, used as a *fallback* match when no id lines up."""
        folded = normalize_title(self.title)
        return folded or None

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serialisable copy — the only shape the exporter emits."""
        return {
            "sources": list(self.sources),
            "ids": dict(self.ids),
            **{name: getattr(self, name) for name in _STRING_FIELDS},
            "authors": [a.to_dict() for a in self.authors],
            "year": self.year,
            "categories": list(self.categories),
            "keywords": list(self.keywords),
            "citation_count": self.citation_count,
            "reference_count": self.reference_count,
            "is_open_access": self.is_open_access,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Paper:
        """Rebuild a record from :meth:`to_dict` output.

        Unknown keys are ignored so an older reader survives a newer writer.
        """
        paper = cls()
        paper.sources = [str(x) for x in (data.get("sources") or [])]
        raw_ids = data.get("ids") or {}
        if isinstance(raw_ids, Mapping):
            paper.ids = {str(k): str(v) for k, v in raw_ids.items() if v}
        for name in _STRING_FIELDS:
            value = data.get(name)
            if isinstance(value, str):
                setattr(paper, name, value)
        raw_authors = data.get("authors") or []
        if isinstance(raw_authors, Iterable):
            paper.authors = [
                a if isinstance(a, Author) else Author.from_dict(a)
                for a in raw_authors
                if isinstance(a, (Author, Mapping))
            ]
        paper.year = _as_int(data.get("year"))
        paper.citation_count = _as_int(data.get("citation_count"))
        paper.reference_count = _as_int(data.get("reference_count"))
        paper.is_open_access = bool(data.get("is_open_access") or False)
        paper.categories = [str(x) for x in (data.get("categories") or [])]
        paper.keywords = [str(x) for x in (data.get("keywords") or [])]
        return paper

    def copy(self) -> Paper:
        clone = Paper.from_dict(self.to_dict())
        return clone


# ---------------------------------------------------------------------- #
# Merging
# ---------------------------------------------------------------------- #


def _union(dest: list[str], extra: Iterable[str]) -> None:
    """Append ``extra`` members to ``dest`` that are not already present."""
    seen = set(dest)
    for item in extra:
        if item and item not in seen:
            dest.append(item)
            seen.add(item)


def _merge_authors(target: list[Author], other: list[Author]) -> None:
    """Merge ``other``'s author list into ``target``'s, in place.

    The longer list wins outright — Crossref routinely truncates long author
    lists, and keeping the short one would silently drop people.  Field-level
    gaps (ORCID, affiliation) are then filled by exact name match; unmatched
    authors are *not* appended, because source-specific name formats
    (``Smith, J.`` vs ``John Smith``) would otherwise duplicate everyone.
    """
    if not other:
        return
    if not target or len(other) > len(target):
        target[:] = [a.copy() for a in other]

    index = {normalize_title(a.name): a for a in target}
    for author in other:
        match = index.get(normalize_title(author.name))
        if match is None:
            continue
        if not match.author_id:
            match.author_id = author.author_id
        if not match.orcid:
            match.orcid = author.orcid
        _union(match.affiliations, author.affiliations)


def merge(target: Paper, other: Paper) -> Paper:
    """Fold ``other`` into ``target`` in place and return ``target``.

    Policy: fill empty scalars, union lists, take the larger of the counts.
    Nothing already present on ``target`` is ever overwritten, so the first
    record seen (and therefore the source that ranked it highest) wins.
    """
    if target is other:
        return target

    _union(target.sources, other.sources)
    for key, value in other.ids.items():
        if value:
            target.ids.setdefault(key, value)

    for name in _STRING_FIELDS:
        if not getattr(target, name):
            setattr(target, name, getattr(other, name) or "")

    if target.year is None and other.year is not None:
        target.year = other.year

    for name in ("citation_count", "reference_count"):
        mine, theirs = getattr(target, name), getattr(other, name)
        if mine is None:
            setattr(target, name, theirs)
        elif theirs is not None:
            setattr(target, name, max(mine, theirs))

    target.is_open_access = target.is_open_access or other.is_open_access

    _union(target.categories, other.categories)
    _union(target.keywords, other.keywords)
    _merge_authors(target.authors, other.authors)
    return target


def _years_compatible(a: Paper, b: Paper) -> bool:
    """A shared year, or a year missing on at least one side."""
    if a.year is None or b.year is None:
        return True
    return a.year == b.year


def _index_title(index: dict[str, list[Paper]], key: str | None, onto: Paper) -> None:
    if not key:
        return
    bucket = index.setdefault(key, [])
    if onto not in bucket:
        bucket.append(onto)


def dedupe(papers: Iterable[Paper]) -> list[Paper]:
    """Collapse records that describe the same work, preserving first-seen order.

    Matching runs in two passes per record:

    1. **Stable ids** — any shared ``doi``/``arxiv``/``pmid``/``pmcid`` is an
       unconditional match, even when the two sources disagree about the year
       (a preprint and its published version legitimately do).
    2. **Title** — only when no id matched, and only when the years are
       compatible.  Conservative on purpose: a duplicate is visible, a false
       merge silently loses a paper.

    The canonical record is the first one seen, so source ranking and search
    relevance survive the collapse.
    """
    buckets: dict[str, Paper] = {}
    by_title: dict[str, list[Paper]] = {}
    order: list[Paper] = []

    for paper in papers:
        keys = paper.merge_keys()
        target = next((buckets[k] for k in keys if k in buckets), None)

        if target is None:
            incoming_title = paper.title_key()
            if incoming_title:
                for candidate in by_title.get(incoming_title, ()):
                    if _years_compatible(candidate, paper):
                        target = candidate
                        break

        if target is None:
            target = paper
            order.append(paper)
        elif target is not paper:
            incoming_title = paper.title_key()
            merge(target, paper)
            _index_title(by_title, incoming_title, target)

        for key in keys:
            buckets[key] = target
        _index_title(by_title, target.title_key(), target)

    return order


def sort_papers(papers: Iterable[Paper], mode: str = "relevance") -> list[Paper]:
    """Sort by ``mode``; unknown modes raise ``ValueError``.

    ``relevance`` is the default and is *not* a sort: it keeps the order the
    pipeline produced (round-robin across sources, then first-seen wins), so
    each source's own ranking is preserved.
    """
    items = list(papers)
    if mode == "relevance":
        return items
    if mode == "year":
        return sorted(items, key=lambda p: (p.year is None, -(p.year or 0)))
    if mode == "citations":
        return sorted(
            items, key=lambda p: (p.citation_count is None, -(p.citation_count or 0))
        )
    if mode == "title":
        return sorted(items, key=lambda p: normalize_title(p.title))
    raise ValueError(f"unknown sort {mode!r}; expected one of {', '.join(SORT_MODES)}")


# ---------------------------------------------------------------------- #
# Coercion helpers
# ---------------------------------------------------------------------- #

_INT_RE = re.compile(r"-?\d+")


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        match = _INT_RE.search(value)
        if match:
            return int(match.group())
    return None


__all__ = [
    "SORT_MODES",
    "Author",
    "Paper",
    "dedupe",
    "merge",
    "sort_papers",
]
