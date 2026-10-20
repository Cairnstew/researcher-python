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
from datetime import date
from typing import Any

from .ids import normalize_title

#: ``relevance`` is source-faithful: it keeps each source's own ranking (see
#: :func:`sort_papers`).  ``score`` is the *local* cross-source ranking from
#: :mod:`researcher_python.relevance`, which is comparable between sources
#: because it is computed from the query text and the record alone.
SORT_MODES = ("relevance", "score", "year", "citations", "title")

#: Screening labels a reader can attach to a record (the Week 3 lab's
#: "skim into core / general / not-relevant" step).  ``unreviewed`` is the
#: starting state and the only value :meth:`Paper.is_excluded` treats as
#: "still in play"; ``excluded`` is the only one it treats as out.
TRIAGE_LABELS = ("unreviewed", "core", "general", "excluded")

_STRING_FIELDS = (
    "doi",
    "arxiv_id",
    "pmid",
    "pmcid",
    "isbn",
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
    # -- reader-owned triage state (never populated by a source) --------
    "label",
    "exclusion_reason",
    "notes",
)

_INT_FIELDS = ("year", "citation_count", "reference_count")

#: Optional numeric fields.  ``relevance`` is stamped by
#: :func:`researcher_python.relevance.score_papers` — ``None`` means "never
#: scored", which is different from ``0.0`` ("scored, matched nothing").
_FLOAT_FIELDS = ("relevance",)


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
    #: True when ``name`` is a body (ACM, OECD, "United Nations") rather than a
    #: person.  No source can be trusted to know this, so it is opt-in: the
    #: family/given split would otherwise render "United Nations" as the
    #: implausible "Nations, U." in every bibliography format.
    organization: bool = False

    def copy(self) -> Author:
        return Author(
            self.name, self.author_id, self.orcid, list(self.affiliations),
            self.organization,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "author_id": self.author_id,
            "orcid": self.orcid,
            "affiliations": list(self.affiliations),
            "organization": self.organization,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Author:
        return cls(
            name=str(data.get("name") or ""),
            author_id=str(data.get("author_id") or ""),
            orcid=str(data.get("orcid") or ""),
            affiliations=[str(x) for x in (data.get("affiliations") or [])],
            organization=bool(data.get("organization") or False),
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
    isbn: str = ""
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

    #: Local topical relevance in ``[0.0, 1.0]``, stamped by
    #: :func:`researcher_python.relevance.score_papers`.  ``None`` means the
    #: record was never scored (a bare ``get``/``references`` result), which is
    #: why a ``min_score`` floor drops it rather than guessing.  Round-trips
    #: through :meth:`to_dict`, so a scored result file stays auditable: the
    #: score that justified a screening decision is saved beside it.
    relevance: float | None = None

    # -- reader-owned triage state ---------------------------------------
    # Not populated by any source: these carry the *reader's* judgement, and
    # survive export/import so a screening session can be resumed.  Merging
    # follows the usual "first seen wins, fill the gaps" rule, so a label set
    # on the canonical record is never clobbered by a fresher source hit.

    #: One of :data:`TRIAGE_LABELS`; ``""`` means unreviewed.
    label: str = ""
    #: Why a record was screened out — kept so exclusions stay auditable.
    exclusion_reason: str = ""
    #: Free-text reading notes, quotes and paraphrases.
    notes: str = ""

    # -- helpers ---------------------------------------------------------

    @property
    def source(self) -> str:
        """The first source that contributed this record."""
        return self.sources[0] if self.sources else ""

    def triage_label(self) -> str:
        """The screening label, normalised: unknown values read as unreviewed."""
        return self.label if self.label in TRIAGE_LABELS else "unreviewed"

    def label_as(self, label: str, reason: str = "") -> "Paper":
        """Set the screening label in place and return ``self`` for chaining.

        An unknown label is a caller mistake and raises :class:`ValueError`
        naming the legal values, rather than silently storing junk that
        :meth:`triage_label` would later hide.
        """
        if label not in TRIAGE_LABELS:
            raise ValueError(
                f"unknown triage label {label!r}; expected one of "
                f"{', '.join(TRIAGE_LABELS)}"
            )
        self.label = label
        self.exclusion_reason = reason if label == "excluded" else ""
        return self

    def note(self, text: str, *, append: bool = True) -> "Paper":
        """Add a timestamped reading note; returns ``self`` for chaining."""
        stamp = date.today().isoformat()
        entry = f"[{stamp}] {str(text).strip()}"
        self.notes = f"{self.notes}\n{entry}" if (append and self.notes) else entry
        return self

    def is_excluded(self) -> bool:
        """True when the reader has screened this record out."""
        return self.triage_label() == "excluded"

    def id_for(self, source: str) -> str:
        """This source's own record id, so a pipeline can re-query it."""
        return self.ids.get(source, "")

    def primary_id(self) -> str:
        """Best human-facing identifier: DOI, then arXiv id, then PMID, then ISBN."""
        return self.doi or self.arxiv_id or self.pmid or self.pmcid or self.isbn

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
            ("isbn:", self.isbn),
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
            "relevance": self.relevance,
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
        paper.relevance = _as_float(data.get("relevance"))
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


@dataclass(slots=True)
class MergeOutcome:
    """Result of merging incoming records into an existing set.

    ``papers`` is the whole set (prior records first, then genuinely new ones),
    so a caller can persist it directly.  ``new`` and ``seen`` are the same
    match decisions exposed separately: a workspace reports "N new, M already
    seen" from them, and a ``seen`` record that carries a triage label is what
    "already screened" means.
    """

    papers: list[Paper] = field(default_factory=list)
    new: list[Paper] = field(default_factory=list)
    seen: list[Paper] = field(default_factory=list)


class PaperIndex:
    """An index of records using the same match rules as :func:`merge_sets`.

    A snowball expansion needs to ask "is this candidate already in the
    workspace?" *before* writing anything.  Reimplementing the id/title+year
    match there would let preview and persist disagree — the exact class of
    bug a preview feature invites.  Both now share this index.

    ``add`` is idempotent: adding the same record twice does not duplicate it
    in the title bucket.
    """

    __slots__ = ("buckets", "by_title")

    def __init__(self, papers: Iterable[Paper] = ()) -> None:
        self.buckets: dict[str, Paper] = {}
        self.by_title: dict[str, list[Paper]] = {}
        for paper in papers:
            self.add(paper)

    def add(self, paper: Paper) -> None:
        for key in paper.merge_keys():
            self.buckets.setdefault(key, paper)
        _index_title(self.by_title, paper.title_key(), paper)

    def find(self, paper: Paper) -> Paper | None:
        """The indexed record this one describes, or ``None``."""
        target = next(
            (self.buckets[key] for key in paper.merge_keys() if key in self.buckets),
            None,
        )
        if target is not None:
            return target
        title = paper.title_key()
        if title:
            for candidate in self.by_title.get(title, ()):
                if _years_compatible(candidate, paper):
                    return candidate
        return None


def merge_sets(
    prior: Iterable[Paper], incoming: Iterable[Paper]
) -> MergeOutcome:
    """Merge ``incoming`` into ``prior`` using :func:`dedupe`'s match rules.

    The prior set keeps its order and identity.  A prior record that receives a
    matching incoming hit is returned in ``seen`` (once, even if several hits
    match it); a record with no match is appended and returned in ``new``.
    :func:`merge` fills gaps only, so reader-owned triage state on the prior
    record is never clobbered.

    Duplicates *within* ``incoming`` collapse into whichever new record was
    seen first and do not count as ``seen`` — they were never in a prior round.
    """
    prior_list = list(prior)
    ordered = list(prior_list)
    prior_ids = {id(paper) for paper in prior_list}

    index = PaperIndex(prior_list)
    new: list[Paper] = []
    seen: dict[int, Paper] = {}

    for paper in incoming:
        target = index.find(paper)

        if target is None:
            target = paper
            ordered.append(paper)
            new.append(paper)
        elif target is not paper:
            merge(target, paper)
            if id(target) in prior_ids:
                seen[id(target)] = target
        elif id(target) in prior_ids:
            # The same object was passed in both sets; still a seen record.
            seen[id(target)] = target

        index.add(target)

    return MergeOutcome(papers=ordered, new=new, seen=list(seen.values()))


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
    relevance survive the collapse.  This is :func:`merge_sets` with an empty
    prior set, so the workspace and a single search share one matcher.
    """
    return merge_sets([], papers).papers


def sort_papers(
    papers: Iterable[Paper], mode: str = "relevance", query: str | None = None
) -> list[Paper]:
    """Sort by ``mode``; unknown modes raise ``ValueError``.

    ``relevance`` is the default and is *not* a sort: it keeps the order the
    pipeline produced (round-robin across sources, then first-seen wins), so
    each source's own ranking is preserved.

    ``score`` is the one mode that reads a field rather than a key: it ranks by
    the local :attr:`Paper.relevance` stamped by
    :mod:`researcher_python.relevance`.  Pass ``query=`` to (re)compute that
    stamp, or rely on a stamp already present from an earlier search.  Records
    with no score tie at the bottom rather than raising, so every mode stays
    usable on any list; :func:`researcher_python.pipeline.search` always scores,
    so a searched result set always has one.  Ties keep input order, so the
    result is deterministic.
    """
    items = list(papers)
    if mode == "relevance":
        return items
    if mode == "score":
        # Imported here: relevance imports this module for Paper, so a
        # module-level import would be circular.
        if query:
            from . import relevance

            items = relevance.score_papers(items, query)
        # Unscored records tie at the bottom (-1.0) instead of first.
        return sorted(
            items, key=lambda p: -(p.relevance if p.relevance is not None else -1.0)
        )
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

_FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _as_float(value: Any) -> float | None:
    """Coerce to ``float``; ``None`` for anything unparseable.

    Mirrors :func:`_as_int` — used for the reader-independent ``relevance``
    stamp, so a hand-edited or older result file degrades to "unscored"
    instead of raising.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = _FLOAT_RE.search(value)
        if match:
            return float(match.group())
    return None


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
    "TRIAGE_LABELS",
    "Author",
    "MergeOutcome",
    "Paper",
    "PaperIndex",
    "dedupe",
    "merge",
    "merge_sets",
    "sort_papers",
]
