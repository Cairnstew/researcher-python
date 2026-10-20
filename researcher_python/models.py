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

from .ids import arxiv_id_from_doi, normalize_doi, normalize_title
from .work_types import normalize_work_type

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
    #: Where the current ``citation_count`` came from: a source name, or
    #: ``openalex`` when the missing-count backfill supplied it.  Empty when the
    #: count is unknown, so a mixed-provenance count is visible rather than
    #: looking like every other number.
    "citation_count_source",
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
class PaperVersion:
    """One version of a work: a preprint, an accepted manuscript, or the
    published record.

    ``Paper`` keeps the bibliographic view a citation needs (usually the
    published version) while ``versions`` preserves the other identities, so a
    preprint is not lost when its journal version is merged in.  Version
    entries are *evidence*, not guesses: each one comes from a shared arXiv id,
    a Crossref relation, an OpenAlex location, or a Semantic Scholar external
    id.  A title+year match alone never creates one.
    """

    kind: str = "unknown"  # preprint | accepted | published | unknown
    doi: str = ""
    arxiv_id: str = ""
    pmid: str = ""
    pmcid: str = ""
    openalex_id: str = ""
    semantic_scholar_id: str = ""
    url: str = ""
    pdf_url: str = ""
    venue: str = ""
    year: int | None = None
    published: str = ""
    work_type: str = ""
    source: str = ""
    #: The assertion that produced this entry: ``is-preprint-of``,
    #: ``is-version-of``, ``location``, ``external-id``, ...
    relation: str = ""
    asserted_by: str = ""
    is_published: bool | None = None

    def key(self) -> tuple[str, str]:
        """Stable identity for deduplicating version entries."""
        if self.doi:
            return ("doi", normalize_doi(self.doi))
        if self.arxiv_id:
            return ("arxiv", self.arxiv_id)
        if self.openalex_id:
            return ("openalex", self.openalex_id)
        if self.semantic_scholar_id:
            return ("semantic_scholar", self.semantic_scholar_id)
        return (self.kind, "|".join((self.source, self.venue, str(self.year or ""))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "pmid": self.pmid,
            "pmcid": self.pmcid,
            "openalex_id": self.openalex_id,
            "semantic_scholar_id": self.semantic_scholar_id,
            "url": self.url,
            "pdf_url": self.pdf_url,
            "venue": self.venue,
            "year": self.year,
            "published": self.published,
            "work_type": self.work_type,
            "source": self.source,
            "relation": self.relation,
            "asserted_by": self.asserted_by,
            "is_published": self.is_published,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PaperVersion":
        return cls(
            kind=str(data.get("kind") or "unknown"),
            doi=normalize_doi(data.get("doi")),
            arxiv_id=str(data.get("arxiv_id") or ""),
            pmid=str(data.get("pmid") or ""),
            pmcid=str(data.get("pmcid") or ""),
            openalex_id=str(data.get("openalex_id") or ""),
            semantic_scholar_id=str(data.get("semantic_scholar_id") or ""),
            url=str(data.get("url") or ""),
            pdf_url=str(data.get("pdf_url") or ""),
            venue=str(data.get("venue") or ""),
            year=_as_int(data.get("year")),
            published=str(data.get("published") or ""),
            work_type=str(data.get("work_type") or ""),
            source=str(data.get("source") or ""),
            relation=str(data.get("relation") or ""),
            asserted_by=str(data.get("asserted_by") or ""),
            is_published=(
                bool(data["is_published"])
                if isinstance(data.get("is_published"), bool)
                else None
            ),
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
    #: Which source supplied :attr:`citation_count` (empty when unknown).
    citation_count_source: str = ""
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

    #: Explicit versions of this work: a preprint, accepted manuscript or the
    #: published record, each with the identifier that proves it.  Usually one
    #: entry (the canonical record) or two (preprint + published).  Populated
    #: only from explicit links, never from a title guess.
    versions: list[PaperVersion] = field(default_factory=list)

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

        An arXiv DOI (``10.48550/arXiv.X``) contributes the bare ``arxiv:X``
        key too, and every explicit :attr:`versions` entry contributes its own
        ids.  That is what lets a published record merge with a preprint that
        carries only the arXiv id, and what keeps a workspace round merging a
        record whose canonical DOI was promoted.
        """
        keys: list[str] = []

        def add(prefix: str, value: str | None) -> None:
            text = str(value or "").strip()
            if text and f"{prefix}{text}" not in keys:
                keys.append(f"{prefix}{text}")

        add("doi:", normalize_doi(self.doi))
        add("arxiv:", self.arxiv_id)
        add("arxiv:", arxiv_id_from_doi(self.doi))
        add("pmid:", self.pmid)
        add("pmcid:", self.pmcid)
        add("isbn:", self.isbn)
        for version in self.versions:
            add("doi:", normalize_doi(version.doi))
            add("arxiv:", arxiv_id_from_doi(version.doi))
            add("arxiv:", version.arxiv_id)
            add("pmid:", version.pmid)
            add("pmcid:", version.pmcid)
        return keys

    def preferred_version(self) -> PaperVersion | None:
        """The highest-ranked explicit version, or ``None`` if none is known."""
        if not self.versions:
            return None
        return max(self.versions, key=_version_rank)

    def as_version(self, kind: str | None = None) -> PaperVersion:
        """A :class:`PaperVersion` snapshot of this record."""
        return _paper_version(self, kind)

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
            "versions": [v.to_dict() for v in self.versions],
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
        raw_versions = data.get("versions") or []
        if isinstance(raw_versions, Iterable):
            paper.versions = [
                v if isinstance(v, PaperVersion) else PaperVersion.from_dict(v)
                for v in raw_versions
                if isinstance(v, (PaperVersion, Mapping))
            ]
        return paper

    def copy(self) -> Paper:
        clone = Paper.from_dict(self.to_dict())
        return clone


# ---------------------------------------------------------------------- #
# Merging
# ---------------------------------------------------------------------- #

_VERSION_RANKS = {"unknown": 0, "preprint": 1, "accepted": 2, "published": 3}

_PUBLISHED_KINDS = {
    "article",
    "review",
    "book",
    "book-chapter",
    "proceedings-article",
    "thesis",
    "report",
    "editorial",
    "letter",
    "erratum",
    "peer-review",
    "paratext",
    "other",
}

#: Fields the published version owns when a version pair is linked.  ``url``
#: and ``pdf_url`` are deliberately absent: a preprint PDF is often the only
#: open copy, so the generic fill-gaps merge keeps it.
_BIBLIOGRAPHIC_FIELDS = (
    "title",
    "doi",
    "year",
    "published",
    "venue",
    "volume",
    "issue",
    "pages",
    "publisher",
    "language",
    "work_type",
)


def _version_rank(version: PaperVersion) -> tuple[int, int, str]:
    return (
        _VERSION_RANKS.get(version.kind, 0),
        version.year or 0,
        version.doi or version.arxiv_id or version.openalex_id or "",
    )


def _version_kind(paper: Paper) -> str:
    """Coarse version kind from a record's own type, without guessing."""
    canonical = normalize_work_type(paper.work_type)
    if canonical == "preprint":
        return "preprint"
    if canonical in _PUBLISHED_KINDS:
        return "published"
    return "unknown"


def _paper_version(paper: Paper, kind: str | None = None) -> PaperVersion:
    return PaperVersion(
        kind=kind or _version_kind(paper),
        doi=normalize_doi(paper.doi),
        arxiv_id=paper.arxiv_id,
        pmid=paper.pmid,
        pmcid=paper.pmcid,
        openalex_id=paper.id_for("openalex"),
        semantic_scholar_id=paper.id_for("semantic_scholar"),
        url=paper.url,
        pdf_url=paper.pdf_url,
        venue=paper.venue,
        year=paper.year,
        published=paper.published,
        work_type=paper.work_type,
        source=paper.source,
    )


def _version_refs(paper: Paper) -> set[str]:
    """Identifiers asserted by explicit version entries (never the own ids)."""
    refs: set[str] = set()
    for version in paper.versions:
        if version.doi:
            refs.add(f"doi:{normalize_doi(version.doi)}")
            derived = arxiv_id_from_doi(version.doi)
            if derived:
                refs.add(f"arxiv:{derived}")
        if version.arxiv_id:
            refs.add(f"arxiv:{version.arxiv_id}")
        if version.pmid:
            refs.add(f"pmid:{version.pmid}")
    return refs


def _own_keys(paper: Paper) -> set[str]:
    return set(paper.merge_keys())


def _is_version_pair(target: Paper, other: Paper) -> bool:
    """True when the two records describe different versions of one work.

    Evidence is explicit: one side names the other in ``versions`` (a Crossref
    relation, an OpenAlex location, a Semantic Scholar external id), or one is
    a preprint and the other a published/accepted record.  Same-kind
    duplicates are ordinary merges, not versions.
    """
    if _version_refs(target) & _own_keys(other):
        return True
    if _version_refs(other) & _own_keys(target):
        return True
    a, b = _version_kind(target), _version_kind(other)
    return {a, b} == {"preprint", "published"} or {a, b} == {"preprint", "accepted"}


def _union_versions(
    target: Paper, other: Paper, *, pair: bool
) -> None:
    """Deduplicate version entries, adding a self entry when a pair is linked."""
    entries = list(target.versions) + list(other.versions)
    if pair:
        entries = [_paper_version(target), _paper_version(other), *entries]
    ordered: list[PaperVersion] = []
    seen: set[tuple[str, str]] = set()
    for version in entries:
        key = version.key()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(version)
    target.versions = ordered


def _promote_published(target: Paper, other: Paper) -> None:
    """Move the higher-ranked version's bibliographic fields onto ``target``.

    Reader-owned state, source provenance, result order and the preprint's
    open PDF are untouched; the displaced values remain in ``versions``.  This
    is what makes a citation render the journal record while the preprint is
    still discoverable by its arXiv id.
    """
    if _version_rank(_paper_version(other)) <= _version_rank(_paper_version(target)):
        return
    for name in _BIBLIOGRAPHIC_FIELDS:
        value = getattr(other, name)
        if value:
            setattr(target, name, value)
    if other.doi and normalize_doi(other.doi):
        target.ids["doi"] = normalize_doi(other.doi)
    for key in ("openalex", "semantic_scholar", "pubmed", "pmc"):
        value = other.ids.get(key)
        if value:
            target.ids.setdefault(key, value)



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

    pair = _is_version_pair(target, other)
    _union_versions(target, other, pair=pair)
    if pair:
        _promote_published(target, other)

    for name in _STRING_FIELDS:
        if not getattr(target, name):
            setattr(target, name, getattr(other, name) or "")

    if target.year is None and other.year is not None:
        target.year = other.year

    for name in ("citation_count", "reference_count"):
        mine, theirs = getattr(target, name), getattr(other, name)
        if mine is None:
            setattr(target, name, theirs)
            if name == "citation_count" and theirs is not None:
                target.citation_count_source = other.citation_count_source
        elif theirs is not None and theirs > mine:
            setattr(target, name, theirs)
            if name == "citation_count":
                target.citation_count_source = other.citation_count_source

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


def _cross_version(a: Paper, b: Paper) -> bool:
    """True when two records have known, different version kinds."""
    ka, kb = _version_kind(a), _version_kind(b)
    return ka != "unknown" and kb != "unknown" and ka != kb


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
                if not _years_compatible(candidate, paper):
                    continue
                # A preprint and its published version often share a title but
                # differ in year.  Once both are known versions, only explicit
                # evidence (a shared id/relation) may merge them; a title guess
                # silently loses a version.
                if _cross_version(candidate, paper):
                    continue
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
    "PaperVersion",
    "dedupe",
    "merge",
    "merge_sets",
    "sort_papers",
]
