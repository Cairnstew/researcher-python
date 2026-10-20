"""Snowballing: expand a core set through the citation graph.

Keyword search asks "what matches these words?".  Snowballing asks a better
recall question: "what do the papers I already trust point at, and what points
at them?"  A record linked by several core papers is a much stronger candidate
than one that merely shares vocabulary with the query, which is why this
module ranks by **how many core papers link to it**, not by a keyword score.

The expansion is deliberately read-only and workspace-agnostic:

* :func:`expand` takes the core records and an optional ``known`` set, calls
  :func:`researcher_python.citation.references` / ``citations``, merges the
  answers with the same matcher the workspace uses, drops records already
  ``known``, and returns ranked candidates.
* :class:`~researcher_python.workspace.Workspace` wraps it to select the core
  set (records labelled ``core``), persist the candidates through
  ``record_search`` and log the expansion as a provenance row.

A core paper with no usable identifier is reported in ``skipped`` rather than
silently dropped — the citation APIs cannot resolve a title, and a caller
needs to know its core set was smaller than it looked.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import citation
from .config import Settings
from .errors import ConfigurationError, ResearcherError
from .ids import normalize_title
from .models import Paper, PaperIndex, merge

__all__ = [
    "DIRECTIONS",
    "Candidate",
    "SnowballResult",
    "expand",
    "normalize_direction",
]

#: The directions a caller can ask for.  The short aliases exist because
#: ``refs``/``cites`` are how people actually say it; the stored value is the
#: full name so the log and the PRISMA report stay uniform.
DIRECTIONS: tuple[str, ...] = ("references", "citations", "both")

_ALIASES = {
    "references": "references",
    "reference": "references",
    "refs": "references",
    "ref": "references",
    "citations": "citations",
    "citation": "citations",
    "cites": "citations",
    "citing": "citations",
    "cited-by": "citations",
    "both": "both",
}


def normalize_direction(direction: str) -> str:
    """Return the canonical direction, or raise naming the legal values."""
    value = " ".join(str(direction or "").split()).casefold()
    if value not in _ALIASES:
        raise ConfigurationError(
            f"unknown snowball direction {direction!r}; expected one of "
            f"{', '.join(DIRECTIONS)}"
        )
    return _ALIASES[value]


@dataclass(slots=True)
class Candidate:
    """One record the core set linked to, with the cores that linked it."""

    paper: Paper
    linked_by: list[str] = field(default_factory=list)

    @property
    def links(self) -> int:
        return len(self.linked_by)


@dataclass(slots=True)
class SnowballResult:
    """Outcome of one expansion — candidates plus the provenance of the query."""

    direction: str = "both"
    core: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    known: int = 0
    fetched: int = 0
    skipped: list[dict[str, str]] = field(default_factory=list)
    by_source: dict[str, int] = field(default_factory=dict)
    #: core id -> source -> message (an error never aborts the other cores).
    errors: dict[str, dict[str, str]] = field(default_factory=dict)
    #: core id -> True when a source may have been cut off at ``limit``.
    truncated: dict[str, bool] = field(default_factory=dict)
    persisted: bool = False
    search_id: str = ""

    def ranked(self, *, min_links: int = 1) -> list[Candidate]:
        """Candidates linked by at least ``min_links`` cores, best first.

        Ties are broken deterministically (standing, then folded title, year,
        id), so the same citation responses always produce the same order —
        the preview and the persisted run agree.
        """
        chosen = [item for item in self.candidates if item.links >= min_links]
        return sorted(
            chosen,
            key=lambda item: (
                -item.links,
                -(item.paper.citation_count if item.paper.citation_count is not None else -1),
                normalize_title(item.paper.title),
                item.paper.year or 0,
                item.paper.primary_id(),
            ),
        )

    def as_dict(self, *, min_links: int = 1, limit: int | None = None) -> dict[str, Any]:
        ranked = self.ranked(min_links=min_links)
        shown = ranked if limit is None else ranked[:limit]
        return {
            "direction": self.direction,
            "core": list(self.core),
            "core_count": len(self.core),
            "fetched": self.fetched,
            "new": len(self.candidates),
            "known": self.known,
            "returned": len(shown),
            "truncated": max(0, len(ranked) - len(shown)),
            "candidates": [
                {
                    "id": item.paper.primary_id(),
                    "title": item.paper.title,
                    "year": item.paper.year,
                    "venue": item.paper.venue,
                    "work_type": item.paper.work_type,
                    "citation_count": item.paper.citation_count,
                    "links": item.links,
                    "linked_by": list(item.linked_by),
                }
                for item in shown
            ],
            "skipped": list(self.skipped),
            "by_source": dict(self.by_source),
            "errors": {key: dict(value) for key, value in self.errors.items()},
            "truncated_sources": dict(self.truncated),
            "persisted": self.persisted,
            "search_id": self.search_id,
        }


def _core_id(paper: Paper) -> str:
    return paper.primary_id() or next(iter(paper.merge_keys()), "") or paper.title


def _call(
    paper: Paper,
    direction: str,
    *,
    sources: Sequence[str] | str | None,
    limit: int | None,
    settings: Settings,
) -> citation.CitationResult:
    func = citation.references if direction == "references" else citation.citations
    return func(paper, sources=sources, limit=limit, settings=settings)


def expand(
    core_papers: Iterable[Paper],
    *,
    direction: str = "both",
    known: Iterable[Paper] = (),
    limit: int | None = None,
    sources: Sequence[str] | str | None = None,
    min_links: int = 1,
    settings: Settings | None = None,
) -> SnowballResult:
    """Expand ``core_papers`` through references, citations, or both.

    ``known`` is the record set a candidate is filtered against (normally the
    workspace's canonical set).  Matching uses
    :class:`~researcher_python.models.PaperIndex`, the same id/title+year rules
    as a merge, so a preview cannot claim a record is new and then merge it
    away on write.

    ``min_links`` is applied by :meth:`SnowballResult.ranked`; the result still
    carries every linked candidate so a caller can report "12 at 1 link, 4 at
    2+" instead of throwing the weak tail away.
    """
    settings = settings if settings is not None else Settings()
    chosen_direction = normalize_direction(direction)
    directions = (
        ("references", "citations")
        if chosen_direction == "both"
        else (chosen_direction,)
    )
    if isinstance(min_links, bool) or not isinstance(min_links, int) or min_links < 1:
        raise ConfigurationError(
            f"min_links must be a positive integer, got {min_links!r}"
        )

    known_index = PaperIndex(known)
    known_seen: set[int] = set()
    pool = PaperIndex()
    candidates: dict[int, Candidate] = {}
    result = SnowballResult(direction=chosen_direction)

    for core in core_papers:
        core_id = _core_id(core)
        if not core.merge_keys():
            result.skipped.append(
                {"id": core_id, "reason": "no identifier (cannot query the graph)"}
            )
            continue
        result.core.append(core_id)

        for one_direction in directions:
            try:
                answer = _call(
                    core,
                    one_direction,
                    sources=sources,
                    limit=limit,
                    settings=settings,
                )
            except ResearcherError as exc:
                result.errors.setdefault(core_id, {})["query"] = " ".join(
                    str(exc).split()
                )
                continue

            result.fetched += len(answer.papers)
            for name, count in answer.by_source.items():
                result.by_source[name] = result.by_source.get(name, 0) + int(count)
            for name, error in answer.errors.items():
                result.errors.setdefault(core_id, {})[name] = " ".join(
                    str(error).split()
                )
            if any(answer.truncated.values()):
                result.truncated[core_id] = True

            for paper in answer.papers:
                existing_known = known_index.find(paper)
                if existing_known is not None:
                    if id(existing_known) not in known_seen:
                        known_seen.add(id(existing_known))
                        result.known += 1
                    continue

                target = pool.find(paper)
                if target is None:
                    candidate = Candidate(paper=paper, linked_by=[core_id])
                    candidates[id(paper)] = candidate
                    pool.add(paper)
                else:
                    candidate = candidates[id(target)]
                    merge(target, paper)
                    pool.add(target)
                    if core_id not in candidate.linked_by:
                        candidate.linked_by.append(core_id)

    result.candidates = list(candidates.values())
    return result
