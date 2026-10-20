"""Fan one query out across sources, then merge, dedupe and sort.

The pipeline is the only layer that knows about *all five* sources.  It has
three jobs:

* **Isolate failure.**  Each source runs in its own worker; a source that is
  unconfigured, unwritten, rate-limited or broken contributes an entry to
  :attr:`PipelineResult.errors` and never aborts the others.  Constructing the
  source happens *inside* the worker, so ``ConfigurationError`` for a missing
  ``RESEARCHER_EMAIL`` is a per-source error rather than a run-ending one.
* **Rank fairly.**  Results are interleaved round-robin before
  :func:`models.dedupe` so one chatty source cannot occupy the whole list —
  which is what :func:`models.sort_papers` documents for ``relevance``.
* **Retry, once, here.**  Adapters never loop on failure (D8), so a
  ``RateLimitError`` is retried ``settings.retries`` *extra* times (default
  1, ``0`` disables retrying) with ``min(Retry-After or 2, 10)`` seconds
  between attempts.  :data:`_sleep` is a module-level indirection so tests
  never wait.
* **Score once, locally.**  Round-robin preserves each source's *order* but
  cannot compare it across sources, so every merged record is also scored
  against the query text by :mod:`researcher_python.relevance`.  The score is
  stamped on :attr:`~researcher_python.models.Paper.relevance` — available to
  ``sort="score"``, to the ``min_score`` floor, and to the Literature Matrix —
  and is counted in :attr:`PipelineResult.dropped` when filtered out.

``max_results`` is applied *per source* before merging; after dedupe the list
is deliberately not truncated, so a merge of five sources can exceed it.  A
``min_score`` floor is the opposite lever: it is applied *after* the merge, so
it is the way to cut a merged set back to the genuinely relevant records.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import _retry, relevance
from .config import Settings
from .errors import (
    ConfigurationError,
    NotFoundError,
    ResearcherError,
    SourceError,
    UnsupportedOperation,
)
from .ids import is_arxiv_id, is_doi, is_isbn, is_pmid
from .models import SORT_MODES, Paper, dedupe, sort_papers
from .sources import DEFAULT_SOURCES, create_source, source_names
from .sources.base import SearchFilters

__all__ = ["PipelineResult", "get", "search"]

#: Upper bound on concurrent sources; each source still has its own RateGate.
_MAX_WORKERS = 8
#: Used as the ``source`` of an error that spans several sources.
_PIPELINE = "pipeline"

#: Indirection so tests can stub the wait instead of sleeping.
_sleep = time.sleep


@dataclass(slots=True)
class PipelineResult:
    """Outcome of one :func:`search` call."""

    #: Deduped, sorted papers.  Can exceed ``max_results`` after a merge.
    papers: list[Paper] = field(default_factory=list)
    #: Source name -> the error that stopped it.  Absent sources succeeded.
    errors: dict[str, ResearcherError] = field(default_factory=dict)
    #: Source name -> raw results it returned *before* dedupe (0 on failure).
    by_source: dict[str, int] = field(default_factory=dict)
    #: Records dropped by a ``min_score`` floor.  Reported so a thin result set
    #: is visibly *filtered* rather than mysteriously small.
    dropped: int = 0
    #: The query terms each score was computed against (see
    #: :func:`researcher_python.relevance.query_terms`).
    terms: list[str] = field(default_factory=list)
    #: Why the result is empty, when the cause is guessable (a field-scoped
    #: AND match, or a citation floor).  Empty when there is nothing to explain.
    hint: str = ""


# ---------------------------------------------------------------------- #
# search
# ---------------------------------------------------------------------- #


def search(
    query: str,
    sources: str | Sequence[str] | None = None,
    max_results: int | None = None,
    sort: str = "relevance",
    settings: Settings | None = None,
    filters: SearchFilters | None = None,
    min_score: float | None = None,
    top_ratio: float | None = None,
    *,
    field: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    min_citations: int | None = None,
) -> PipelineResult:
    """Query several sources concurrently and return one merged result.

    ``filters`` (:class:`~researcher_python.sources.base.SearchFilters`) narrows
    every source by publication year and field scope, so a systematic search is
    stated once and applied uniformly — and can be written down and repeated.

    ``field``/``year_from``/``year_to``/``min_citations`` are **convenience
    keywords** for the common case, mirroring the CLI flags and the MCP tool.
    ``field``/``year_from``/``year_to`` build the :class:`SearchFilters`;
    passing them *as well as* an explicit ``filters`` is a caller mistake and
    raises rather than silently letting one win.

    ``min_citations`` drops records with fewer citations than the floor.  It is
    a crude *standing* proxy — a decade-old paper with 40 citations and a
    two-year-old paper with 40 are not comparable, which is why the Literature
    Matrix reports ``citations_per_year`` alongside it (lab03's caution about
    citation counts and age).  A record with an unknown citation count is
    **kept**, like an unknown year under a date filter, so grey literature is
    never hidden.

    ``min_score`` is a *local* relevance floor (0.0–1.0) applied after the
    merge, using :mod:`researcher_python.relevance`.  It exists because a
    source's own ranking is not comparable across sources, so a merged set
    otherwise contains whatever each source's weakest hits happened to be.
    ``None`` (the default) applies no floor and changes nothing.

    ``top_ratio`` is a *relative* floor (0.0–1.0): keep records scoring at
    least ``top_ratio × the best score``.  Prefer it for cutting a result set
    to its top tier — an absolute floor does not scale with query length, so on
    a six-term query a low ``min_score`` can remove nothing at all.

    Whatever any floor discards is counted in
    :attr:`PipelineResult.dropped` — never silently.

    ``ConfigurationError`` is raised up front — for an unknown source name, an
    unknown ``sort``, an unknown search field, or an out-of-range floor —
    because those are caller mistakes, not source failures.  Everything a
    *source* does wrong lands in :attr:`PipelineResult.errors`.
    """
    settings = settings if settings is not None else Settings()
    names = _validate_sources(sources)
    if sort not in SORT_MODES:
        raise ConfigurationError(
            f"unknown sort {sort!r}; expected one of {', '.join(SORT_MODES)}"
        )
    filters = _resolve_filters(filters, field, year_from, year_to)
    if min_citations is not None and (
        not isinstance(min_citations, int)
        or isinstance(min_citations, bool)
        or min_citations < 0
    ):
        raise ConfigurationError(
            f"min_citations must be a non-negative int, got {min_citations!r}"
        )
    if min_score is not None and not 0.0 <= float(min_score) <= 1.0:
        raise ConfigurationError(
            f"min_score must be between 0.0 and 1.0, got {min_score!r}"
        )
    if top_ratio is not None and not 0.0 < float(top_ratio) <= 1.0:
        raise ConfigurationError(
            f"top_ratio must be greater than 0.0 and at most 1.0, got {top_ratio!r}"
        )

    limit = settings.max_results if max_results is None else max(0, int(max_results))
    result = PipelineResult()
    if limit == 0 or not (query or "").strip() or not names:
        return result

    groups: dict[str, list[Paper]] = {}
    workers = max(1, min(_MAX_WORKERS, len(names)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            name: pool.submit(_search_one, name, query, limit, settings, filters)
            for name in names
        }
        for name in names:
            try:
                papers, error = futures[name].result()
            except Exception as exc:  # pragma: no cover - _search_one never raises
                papers, error = [], SourceError(
                    name, f"{type(exc).__name__}: {exc}", cause=exc
                )
            groups[name] = papers
            result.by_source[name] = len(papers)
            if error is not None:
                result.errors[name] = error

    merged = dedupe(_round_robin([groups[name] for name in names]))

    # Score every merged record against the query the caller actually typed.
    # Done unconditionally (it is pure string work) so a result file always
    # carries the score, and so `sort="score"` and `min_score` need no extra
    # pass by the caller.
    result.terms = relevance.query_terms(query)
    relevance.score_papers(merged, query, _score_fields(filters))

    kept, result.dropped = relevance.filter_by_score(merged, min_score, top_ratio)
    if min_citations is not None:
        kept, low_cited = _filter_min_citations(kept, min_citations)
        result.dropped += low_cited
    result.papers = sort_papers(kept, sort, query=query if sort == "score" else None)
    result.hint = _zero_result_hint(result, query, filters, min_citations)
    return result


def _filter_min_citations(
    papers: Iterable[Paper], minimum: int
) -> tuple[list[Paper], int]:
    """Split on the citation floor; an unknown count is **kept**.

    Unknown is kept for the same reason an unknown year survives a date filter:
    the alternative hides exactly the grey literature a reader most needs to
    see and judge.
    """
    items = list(papers)
    kept = [
        p for p in items if p.citation_count is None or p.citation_count >= minimum
    ]
    return kept, len(items) - len(kept)


def _zero_result_hint(
    result: PipelineResult,
    query: str,
    filters: SearchFilters | None,
    min_citations: int | None,
) -> str:
    """Explain a *silently* empty result set, because the cause is guessable.

    A field-scoped search is an AND: ``field="title"`` means every query term
    must appear in the title, so a long keyword set legitimately matches
    nothing while returning no error.  That is exactly the trap lab01 describes
    — the title-match search is "tightly controlled" and "maybe too restricted"
    — so say so rather than leaving "0 papers" to mean nine different things.
    """
    if result.papers:
        return ""
    attempted = list(result.by_source)
    if attempted and len(result.errors) == len(attempted):
        # Every source failed: a transport/credential problem, not a query one.
        return ""
    field = getattr(filters, "field", "any") if filters else "any"
    terms = len(result.terms)
    if field in ("title", "abstract") and terms > 2:
        return (
            f"0 kept: field={field!r} requires ALL {terms} query terms in the "
            f"{field} (AND match). Try fewer terms, or field='any' to relax it "
            f"— a restrictive match returns fewer hits but more exact ones "
            f"(see lab01's title-match vs relaxed-search trade-off)."
        )
    if min_citations is not None:
        return (
            f"0 kept: every retrieved record had fewer than {min_citations} "
            f"citations. Lower min_citations, or drop it — citations relate to "
            f"a work's age, so a floor hides recent work."
        )
    return (
        "0 kept: the sources returned records but every one was filtered out "
        "by the relevance floors. Lower min_score/top_ratio."
    )


def _resolve_filters(
    filters: SearchFilters | None,
    field: str | None,
    year_from: int | None,
    year_to: int | None,
) -> SearchFilters | None:
    """Build a :class:`SearchFilters` from the convenience keywords.

    ``filters`` and the keywords are two spellings of the same request, so
    giving both is ambiguous — raise instead of silently letting one win
    (that kind of silent precedence is how a search stops being reproducible).
    """
    if filters is not None:
        if field is not None or year_from is not None or year_to is not None:
            raise ConfigurationError(
                "pass either filters=SearchFilters(...) or the convenience "
                "keywords (field=/year_from=/year_to=), not both"
            )
        return filters
    if field is None and year_from is None and year_to is None:
        return None
    try:
        return SearchFilters(
            year_from=year_from, year_to=year_to, field=field or "any"
        )
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc


def _score_fields(filters: SearchFilters | None) -> list[str] | None:
    """Which fields the relevance score should read, given the search scope.

    A search scoped to ``title`` returns only title-matching records; scoring
    those on title+abstract+keywords makes every one of them saturate at the
    same score, so the score stops carrying any ranking information and a
    relative floor can never cut anything.  Scoring on the scoped field keeps
    the distinctions the caller asked for.  ``any`` (or no scope) scores on
    every field, as before.
    """
    if filters is None:
        return None
    field = getattr(filters, "field", "any") or "any"
    if field == "title":
        return ["title"]
    if field == "abstract":
        return ["abstract", "title"]
    return None


def _search_one(
    name: str,
    query: str,
    limit: int,
    settings: Settings,
    filters: SearchFilters | None = None,
) -> tuple[list[Paper], ResearcherError | None]:
    """Build and exercise one source; never raises, always reports."""
    try:
        source = create_source(name, settings)
    except ResearcherError as exc:
        # ConfigurationError (missing email), SourceUnavailableError, ...
        return [], exc
    except Exception as exc:
        return [], SourceError(name, f"{type(exc).__name__}: {exc}", cause=exc)

    if not source.supports("search"):
        return [], UnsupportedOperation(f"source {name!r} has no 'search'")

    try:
        return (
            list(
                _with_retry(
                    lambda: source.search(query, limit=limit, filters=filters), settings
                )
            ),
            None,
        )
    except ResearcherError as exc:
        return [], exc
    except Exception as exc:
        return [], SourceError(name, f"{type(exc).__name__}: {exc}", cause=exc)


def _with_retry(operation, settings: Settings):
    """Run ``operation``, retrying only :class:`RateLimitError` (D8).

    ``settings.retries`` counts *extra* attempts after the first, so
    ``retries=0`` means a rate-limited source fails on its first call.  The
    loop lives in :mod:`researcher_python._retry` so the citation fan-out shares
    it; ``_sleep`` stays here as this module's test seam.
    """
    return _retry.with_retry(operation, settings, sleep=_sleep)


def _round_robin(groups: Iterable[list[Paper]]) -> list[Paper]:
    """Interleave sources so no single source dominates the merged order."""
    materialised = [list(g) for g in groups]
    out: list[Paper] = []
    for index in range(max((len(g) for g in materialised), default=0)):
        for group in materialised:
            if index < len(group):
                out.append(group[index])
    return out


# ---------------------------------------------------------------------- #
# get
# ---------------------------------------------------------------------- #


def get(
    identifier: str,
    source: str | None = None,
    settings: Settings | None = None,
) -> Paper:
    """Resolve one identifier, trying candidate sources in order.

    With ``source`` only that source is used.  Otherwise the identifier's type
    picks the candidates: DOI -> openalex, crossref, semantic_scholar;
    arXiv id -> arxiv, openalex; PMID -> pubmed; ISBN -> the book-capable
    sources (openalex, crossref); anything else -> the default source list.

    A :class:`~researcher_python.errors.NotFoundError` moves to the next
    candidate, as does any other failure — but the failures are remembered, so
    an exhausted list reports *why* every source said no instead of a bare
    "not found".
    """
    settings = settings if settings is not None else Settings()
    candidates = _candidates(identifier, source)

    failures: dict[str, ResearcherError] = {}
    only_not_found = True

    for name in candidates:
        try:
            candidate = create_source(name, settings)
        except ResearcherError as exc:
            failures[name] = exc
            only_not_found = False
            continue
        except Exception as exc:
            failures[name] = SourceError(name, f"{type(exc).__name__}: {exc}", cause=exc)
            only_not_found = False
            continue

        if not candidate.supports("get"):
            failures[name] = UnsupportedOperation(f"source {name!r} has no 'get'")
            only_not_found = False
            continue

        try:
            return candidate.get(identifier)
        except NotFoundError as exc:
            failures[name] = exc
        except ResearcherError as exc:
            failures[name] = exc
            only_not_found = False
        except Exception as exc:
            failures[name] = SourceError(name, f"{type(exc).__name__}: {exc}", cause=exc)
            only_not_found = False

    message = _aggregate(failures, only_not_found)
    if failures and only_not_found:
        raise NotFoundError(_PIPELINE, message)
    raise SourceError(_PIPELINE, message)


def _candidates(identifier: str, source: str | None) -> list[str]:
    if source is not None:
        names = [source]
        _validate_sources(names)
        return names

    text = str(identifier or "").strip()
    if is_doi(text):
        routed = ["openalex", "crossref", "semantic_scholar"]
    elif is_arxiv_id(text):
        routed = ["arxiv", "openalex"]
    elif is_pmid(text):
        routed = ["pubmed"]
    elif is_isbn(text):
        # Books carry an ISBN rather than a DOI; the two metadata sources that
        # index monographs and textbook records.
        routed = ["openalex", "crossref"]
    else:
        routed = list(DEFAULT_SOURCES)
    return _validate_sources(routed)


def _aggregate(failures: dict[str, ResearcherError], only_not_found: bool) -> str:
    joined = "; ".join(f"{name}: {exc}" for name, exc in failures.items())
    head = "no source has this record" if only_not_found else "every source failed"
    return f"{head} — {joined}" if joined else head


# ---------------------------------------------------------------------- #
# validation
# ---------------------------------------------------------------------- #


def _validate_sources(sources: str | Sequence[str] | None) -> list[str]:
    """Resolve and check source names; unknown ones raise ``ConfigurationError``."""
    if sources is None:
        return list(DEFAULT_SOURCES)
    requested = [sources] if isinstance(sources, str) else list(sources)
    known = source_names()
    resolved: list[str] = []
    for name in requested:
        if not isinstance(name, str) or name not in known:
            raise ConfigurationError(
                f"unknown source {name!r}; known sources: {', '.join(known)}"
            )
        if name not in resolved:
            resolved.append(name)
    return resolved
