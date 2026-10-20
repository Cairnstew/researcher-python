"""Deterministic citation queries: what a work cites, and what cites it.

A *citation query* is either "what does this work reference" (:func:`references`)
or "what cites this work" (:func:`citations`).  Some adapters can answer this
today (see each source's ``Capabilities``); this module is the only place that
knows how to ask more than one and fold the answers together, exactly as
:mod:`researcher_python.pipeline` does for free-text search.

Determinism is the point, so the rules are explicit:

* **Same responses -> same result.**  Records are merged and then ordered by a
  canonical *total* order (``order="canonical"``), so neither the order the
  sources answered in nor the order the caller named them changes the output.
  Ties are impossible: the key ends in a stable id, a folded title, a year and
  a folded first author.  Every other ``order`` appends that same key as its
  final tiebreak.
* **Source precedence is fixed, not arrival order.**  Sources are queried in
  :data:`CITATION_SOURCES` order (caller order is ignored), and that order is
  also what decides which record wins a merge when two sources disagree.
* **Nothing is hidden.**  A source that fails, is unsupported, or was cut off
  shows up in :attr:`CitationResult.errors` / ``truncated`` and never silently
  shrinks the answer; per-record provenance is in :attr:`CitationResult.edges`.
* **Strict mode is reproducible by construction.**  ``strict=True`` turns any
  per-source failure into an exception, so a call that *returns* proves every
  requested source answered and the union is exactly reproducible.

This is deliberately **not** a claim that a live query is byte-reproducible
over time -- the citation graph itself changes.  It is a claim about the local
transformation: given the same per-source responses, the result is a pure
function of that input.

The query is by :class:`~researcher_python.models.Paper`, not by string: each
adapter already knows how to resolve a record by whichever identifier it
carries (see ``Source.references``), so routing lives in the adapters, not here.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any

from . import _retry
from .config import Settings
from .errors import (
    ConfigurationError,
    NotFoundError,
    ResearcherError,
    SourceError,
    UnsupportedOperation,
)
from .ids import normalize_arxiv_id, normalize_doi, normalize_pmid, normalize_title
from .models import Paper, dedupe
from .sources import create_source, get_source_class, source_names

__all__ = ["CITATION_SORT_MODES", "CITATION_SOURCES", "CitationResult", "citations", "references"]

#: Sort modes accepted by ``order=``.  ``canonical`` is the deterministic
#: default; every other mode falls back to it for ties.
CITATION_SORT_MODES: tuple[str, ...] = ("canonical", "year", "citations", "title")

#: The directions this module can ask a source for.
_DIRECTIONS: tuple[str, ...] = ("references", "citations")

#: Fixed priority order for the default fan-out.  Never registry order, and
#: never caller order -- so the merge winner is a function of the source *set*.
CITATION_SOURCES: tuple[str, ...] = (
    "openalex",
    "semantic_scholar",
    "crossref",
    "pubmed",
)

#: Upper bound on concurrent sources; each source still has its own RateGate.
_MAX_WORKERS = 8

#: The ``source`` of an error that spans the whole query.
_QUERY = "citations"

#: Indirection so tests can stub the wait instead of sleeping.
_sleep = time.sleep


@dataclass(slots=True)
class CitationResult:
    """Outcome of one :func:`references`/:func:`citations` call."""

    #: ``"references"`` or ``"citations"``.
    direction: str = ""
    #: Identity of the queried paper, for the audit trail.
    subject: dict[str, str] = field(default_factory=dict)
    #: Deduped records in the requested order -- the actual answer.
    papers: list[Paper] = field(default_factory=list)
    #: Edge key -> sorted sources that asserted it (per-record provenance).
    edges: dict[str, list[str]] = field(default_factory=dict)
    #: Source name -> the error that stopped it.  Absent sources succeeded.
    errors: dict[str, ResearcherError] = field(default_factory=dict)
    #: Source name -> records it contributed *before* dedupe (0 on failure).
    by_source: dict[str, int] = field(default_factory=dict)
    #: Source name -> True when it may have been cut off at ``requested``.
    truncated: dict[str, bool] = field(default_factory=dict)
    #: The sources queried, in canonical precedence order.
    sources: list[str] = field(default_factory=list)
    #: Per-source cap that was applied (``settings.max_results`` by default).
    requested: int = 0
    #: Sort mode used for :attr:`papers`.
    order: str = "canonical"
    #: Whether a per-source failure would have raised.
    strict: bool = False

    def keys(self) -> list[str]:
        """Edge keys of :attr:`papers`, in order (a stable, comparable view)."""
        return [_edge_key(paper) for paper in self.papers]


# ---------------------------------------------------------------------- #
# public API
# ---------------------------------------------------------------------- #


def references(
    paper: Paper | str,
    *,
    sources: Sequence[str] | str | None = None,
    strict: bool = False,
    limit: int | None = None,
    order: str = "canonical",
    settings: Settings | None = None,
) -> CitationResult:
    """Works that ``paper`` cites, merged across sources.

    ``paper`` may be a :class:`~researcher_python.models.Paper` **or** an
    identifier string (DOI / arXiv id / PMID), which is resolved first.

    ``sources`` defaults to every source whose ``Capabilities.references`` is
    true, queried in :data:`CITATION_SOURCES` order.  ``limit`` caps each source
    (default ``settings.max_results``).  ``strict=True`` raises instead of
    recording a source failure.  See the module docstring for the determinism
    contract.
    """
    return _query(
        paper,
        "references",
        sources=sources,
        strict=strict,
        limit=limit,
        order=order,
        settings=settings,
    )


def citations(
    paper: Paper | str,
    *,
    sources: Sequence[str] | str | None = None,
    strict: bool = False,
    limit: int | None = None,
    order: str = "canonical",
    settings: Settings | None = None,
) -> CitationResult:
    """Works that cite ``paper``, merged across sources.

    ``paper`` may be a Paper or an identifier string, as in :func:`references`.
    Same contract, against ``Capabilities.citations``.
    """
    return _query(
        paper,
        "citations",
        sources=sources,
        strict=strict,
        limit=limit,
        order=order,
        settings=settings,
    )


# ---------------------------------------------------------------------- #
# fan-out
# ---------------------------------------------------------------------- #


def _resolve_subject(paper: Paper | str, settings: Settings) -> Paper:
    """Accept an identifier string as the citation subject, not only a Paper.

    ``citations("1806.07358")`` is the obvious library call and used to raise
    ``a citation query needs a Paper, got str`` — a message that says what is
    wrong but not what to do.  Resolving the identifier here matches the CLI
    and the MCP tool, which have always accepted one, so all three faces agree.
    """
    if isinstance(paper, Paper):
        return paper
    if isinstance(paper, str) and paper.strip():
        # Local import: pipeline does not import citation, so this direction is
        # acyclic, but keeping it lazy preserves `import researcher_python`'s
        # light import cost.
        from .pipeline import get

        return get(paper, settings=settings)
    raise ConfigurationError(
        "a citation query needs a Paper or an identifier string "
        f"(DOI / arXiv id / PMID), got {type(paper).__name__}"
    )


def _query(
    paper: Paper | str,
    direction: str,
    *,
    sources: Sequence[str] | str | None,
    strict: bool,
    limit: int | None,
    order: str,
    settings: Settings | None,
) -> CitationResult:
    settings = settings if settings is not None else Settings()

    paper = _resolve_subject(paper, settings)
    if direction not in _DIRECTIONS:
        raise ConfigurationError(f"unknown direction {direction!r}")
    if order not in CITATION_SORT_MODES:
        raise ConfigurationError(
            f"unknown order {order!r}; expected one of {', '.join(CITATION_SORT_MODES)}"
        )
    cap = settings.max_results if limit is None else limit
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise ConfigurationError(f"limit must be a positive integer, got {limit!r}")

    names = _resolve_sources(sources, direction)
    result = CitationResult(
        direction=direction,
        subject=_subject_of(paper),
        sources=list(names),
        requested=cap,
        order=order,
        strict=bool(strict),
    )
    if not names:
        return result

    groups: dict[str, list[Paper]] = {}
    workers = max(1, min(_MAX_WORKERS, len(names)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            name: pool.submit(_citation_one, name, paper, direction, cap, settings)
            for name in names
        }
        for name in names:
            try:
                items, error = futures[name].result()
            except Exception as exc:  # pragma: no cover - _citation_one never raises
                items, error = [], SourceError(name, f"{type(exc).__name__}: {exc}", cause=exc)
            groups[name] = items
            result.by_source[name] = len(items)
            # Conservative: "returned at least the cap" means it may have been
            # cut off -- by us or by the source's own page limit.
            result.truncated[name] = len(items) >= cap
            if error is not None:
                result.errors[name] = error

    merged = dedupe(_round_robin([groups[name] for name in names]))
    result.papers = sort_citations(merged, order)
    result.edges = _edges(result.papers)

    if strict and result.errors:
        raise _strict_error(result)
    return result


def _citation_one(
    name: str,
    paper: Paper,
    direction: str,
    cap: int,
    settings: Settings,
) -> tuple[list[Paper], ResearcherError | None]:
    """Build and exercise one source; never raises, always reports.

    The per-source ``cap`` is applied by handing the adapter a ``Settings``
    whose ``max_results`` is the cap -- that is the knob every adapter already
    reads -- then slicing defensively for an adapter (Crossref) that ignores it.
    """
    try:
        source = create_source(name, replace(settings, max_results=cap))
    except ResearcherError as exc:
        return [], exc
    except Exception as exc:
        return [], SourceError(name, f"{type(exc).__name__}: {exc}", cause=exc)

    if not source.supports(direction):
        return [], UnsupportedOperation(f"source {name!r} has no {direction!r}")

    def operation() -> list[Paper]:
        return list(getattr(source, direction)(paper))

    try:
        raw = _with_retry(operation, settings)
    except ResearcherError as exc:
        return [], exc
    except Exception as exc:
        return [], SourceError(name, f"{type(exc).__name__}: {exc}", cause=exc)
    return raw[:cap], None


def _with_retry(operation, settings: Settings):
    """Retry a rate-limited source (D8), sharing :mod:`researcher_python._retry`."""
    return _retry.with_retry(operation, settings, sleep=_sleep)


# ---------------------------------------------------------------------- #
# ordering (the determinism contract)
# ---------------------------------------------------------------------- #


def _stable_id(paper: Paper) -> str:
    """Best stable identifier, normalised so DOI case cannot reorder records."""
    return (
        normalize_doi(paper.doi)
        or normalize_arxiv_id(paper.arxiv_id)
        or normalize_pmid(paper.pmid)
        or (paper.pmcid or "").strip().lower()
    )


def _canonical_key(paper: Paper) -> tuple[Any, ...]:
    """A total order over records: id-bearing first, then id, title, year, author."""
    stable = _stable_id(paper)
    return (
        0 if stable else 1,
        stable,
        normalize_title(paper.title),
        paper.year if paper.year is not None else -1,
        normalize_title(paper.authors[0].name) if paper.authors else "",
    )


def sort_citations(papers: Iterable[Paper], order: str = "canonical") -> list[Paper]:
    """Sort ``papers`` deterministically; unknown ``order`` raises ``ConfigurationError``.

    Every mode appends :func:`_canonical_key` as its final tiebreak, so the
    result is a function of the record *set*, never of arrival order.
    """
    if order not in CITATION_SORT_MODES:
        raise ConfigurationError(
            f"unknown order {order!r}; expected one of {', '.join(CITATION_SORT_MODES)}"
        )
    items = list(papers)
    if order == "canonical":
        return sorted(items, key=_canonical_key)
    if order == "year":
        return sorted(
            items, key=lambda p: (p.year is None, -(p.year or 0), _canonical_key(p))
        )
    if order == "citations":
        return sorted(
            items,
            key=lambda p: (
                p.citation_count is None,
                -(p.citation_count or 0),
                _canonical_key(p),
            ),
        )
    return sorted(items, key=lambda p: (normalize_title(p.title), _canonical_key(p)))


# ---------------------------------------------------------------------- #
# provenance
# ---------------------------------------------------------------------- #


def _edge_key(paper: Paper) -> str:
    """A stable key for the *target* record an edge points at."""
    stable = _stable_id(paper)
    if stable:
        return stable
    return f"title:{normalize_title(paper.title)}|y:{paper.year!r}"


def _edges(papers: Iterable[Paper]) -> dict[str, list[str]]:
    """Edge key -> sorted sources that asserted the edge (via ``Paper.sources``)."""
    edges: dict[str, list[str]] = {}
    for paper in papers:
        edges[_edge_key(paper)] = sorted(set(paper.sources))
    return edges


def _subject_of(paper: Paper) -> dict[str, str]:
    return {
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
        "pmid": paper.pmid,
        "pmcid": paper.pmcid,
        "title": paper.title,
    }


# ---------------------------------------------------------------------- #
# source resolution
# ---------------------------------------------------------------------- #


def _resolve_sources(
    sources: Sequence[str] | str | None, direction: str
) -> list[str]:
    """Resolve ``sources`` to a validated, canonically ordered list.

    ``None`` means "every source that declares ``direction``", in
    :data:`CITATION_SOURCES` order.  Unknown names raise ``ConfigurationError``
    (a caller mistake).  Caller order is discarded: the merge winner must not
    depend on it.
    """
    if sources is None:
        names = [name for name in CITATION_SOURCES if _declares(name, direction)]
    else:
        requested = [sources] if isinstance(sources, str) else list(sources)
        known = source_names()
        names = []
        for name in requested:
            if not isinstance(name, str) or name not in known:
                raise ConfigurationError(
                    f"unknown source {name!r}; known sources: {', '.join(known)}"
                )
            if name not in names:
                names.append(name)
    return sorted(names, key=_source_rank)


def _source_rank(name: str) -> tuple[int, Any]:
    try:
        return (0, CITATION_SOURCES.index(name))
    except ValueError:
        return (1, name)


def _declares(name: str, direction: str) -> bool:
    """Whether a source's ``Capabilities`` claim ``direction`` (never network)."""
    try:
        capabilities = get_source_class(name).capabilities
    except Exception:
        # Unimportable/absent adapter: not a candidate, and not an error here --
        # if the caller names it explicitly, construction reports the failure.
        return False
    return bool(getattr(capabilities, direction, False))


def _round_robin(groups: Iterable[list[Paper]]) -> list[Paper]:
    """Interleave sources so no single source dominates (same rule as search)."""
    materialised = [list(group) for group in groups]
    out: list[Paper] = []
    for index in range(max((len(group) for group in materialised), default=0)):
        for group in materialised:
            if index < len(group):
                out.append(group[index])
    return out


def _strict_error(result: CitationResult) -> ResearcherError:
    """Aggregate every per-source failure into one exception for ``strict=True``."""
    joined = "; ".join(f"{name}: {exc}" for name, exc in result.errors.items())
    head = f"strict {result.direction} query incomplete"
    message = f"{head} -- {joined}" if joined else head
    if all(isinstance(exc, NotFoundError) for exc in result.errors.values()):
        return NotFoundError(_QUERY, message)
    return SourceError(_QUERY, message)
