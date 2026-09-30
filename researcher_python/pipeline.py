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

``max_results`` is applied *per source* before merging; after dedupe the list
is deliberately not truncated, so a merge of five sources can exceed it.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .config import Settings
from .errors import (
    ConfigurationError,
    NotFoundError,
    RateLimitError,
    ResearcherError,
    SourceError,
    UnsupportedOperation,
)
from .ids import is_arxiv_id, is_doi, is_pmid
from .models import SORT_MODES, Paper, dedupe, sort_papers
from .sources import DEFAULT_SOURCES, create_source, source_names

__all__ = ["PipelineResult", "get", "search"]

#: ``Retry-After``/default sleep for a rate-limited source, capped at this.
_MAX_SLEEP = 10.0
_DEFAULT_RETRY_AFTER = 2.0
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


# ---------------------------------------------------------------------- #
# search
# ---------------------------------------------------------------------- #


def search(
    query: str,
    sources: str | Sequence[str] | None = None,
    max_results: int | None = None,
    sort: str = "relevance",
    settings: Settings | None = None,
) -> PipelineResult:
    """Query several sources concurrently and return one merged result.

    ``ConfigurationError`` is raised up front — for an unknown source name or
    an unknown ``sort`` — because those are caller mistakes, not source
    failures.  Everything a *source* does wrong lands in
    :attr:`PipelineResult.errors` instead.
    """
    settings = settings if settings is not None else Settings()
    names = _validate_sources(sources)
    if sort not in SORT_MODES:
        raise ConfigurationError(
            f"unknown sort {sort!r}; expected one of {', '.join(SORT_MODES)}"
        )

    limit = settings.max_results if max_results is None else max(0, int(max_results))
    result = PipelineResult()
    if limit == 0 or not (query or "").strip() or not names:
        return result

    groups: dict[str, list[Paper]] = {}
    workers = max(1, min(_MAX_WORKERS, len(names)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            name: pool.submit(_search_one, name, query, limit, settings)
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
    result.papers = sort_papers(merged, sort)
    return result


def _search_one(
    name: str, query: str, limit: int, settings: Settings
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
        return list(_with_retry(lambda: source.search(query, limit=limit), settings)), None
    except ResearcherError as exc:
        return [], exc
    except Exception as exc:
        return [], SourceError(name, f"{type(exc).__name__}: {exc}", cause=exc)


def _with_retry(operation, settings: Settings):
    """Run ``operation``, retrying only :class:`RateLimitError` (D8).

    ``settings.retries`` counts *extra* attempts after the first, so
    ``retries=0`` means a rate-limited source fails on its first call.
    """
    attempts = 1 + int(settings.retries)
    for attempt in range(attempts):
        try:
            return operation()
        except RateLimitError as exc:
            if attempt + 1 >= attempts:
                raise
            _sleep(min(_retry_after(exc) or _DEFAULT_RETRY_AFTER, _MAX_SLEEP))
    raise AssertionError("unreachable: loop always returns or raises")  # pragma: no cover


def _retry_after(exc: ResearcherError) -> float | None:
    value = getattr(exc, "retry_after", None)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


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
    arXiv id -> arxiv, openalex; PMID -> pubmed; anything else -> the default
    source list.

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
