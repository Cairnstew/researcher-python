"""Shared machinery for every source adapter.

An adapter is a subclass of :class:`Source` with a ``name``, a
:class:`Capabilities` record, and implementations of the operations it
supports.  Three rules keep the five adapters honest:

* **Lazy imports.**  ``arxiv``, ``habanero`` and ``Bio`` are only imported
  inside the constructor (see :func:`require_dependency`), so importing
  :mod:`researcher_python` never fails because one optional wheel is missing —
  only constructing that specific source does, and it says which one.
* **No default network.**  Constructors may prepare clients but do not fetch;
  every byte moves inside ``search``/``get``/``references``/``citations``.
* **Declare before you refuse.**  An operation the source does not implement
  raises :class:`~researcher_python.errors.UnsupportedOperation` from
  :meth:`Source.require_support`, and ``capabilities`` says the same thing
  up front so the pipeline can filter without paying the exception.
"""

from __future__ import annotations

import abc
import importlib
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..errors import SourceUnavailableError, UnsupportedOperation
from ..models import Paper
from ..work_types import (
    DEFAULT_SEARCH_EXCLUDE_TYPES,
    normalize_work_type,
    resolve_types,
)

__all__ = [
    "Capabilities",
    "RateGate",
    "SEARCH_FIELDS",
    "SearchFilters",
    "Source",
    "apply_filters",
    "matches_filters",
    "parse_retry_after",
    "require_dependency",
    "squash_text",
]


def squash_text(value: object) -> str:
    """Coerce to ``str`` and collapse every run of whitespace to one space.

    Every source wraps its titles and abstracts differently — arXiv and
    Semantic Scholar hard-wrap at 80 columns, Crossref uses ``&#x2010;`` hyphens
    and doubled spaces, PubMed keeps ``<i>`` tags inside ``ArticleTitle``.
    Collapsing in the parser (not the exporter) means :func:`models.merge` and
    :func:`models.title_key` compare like with like.  Returns ``""`` for
    ``None`` and for anything whose ``str()`` is blank.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return " ".join(value.split())


#: Which part of a record a query is allowed to match.  ``any`` is the
#: default free-text behaviour; ``title``/``abstract`` are what a systematic
#: review actually needs ("title only" is how you exclude a passing mention).
SEARCH_FIELDS = ("any", "title", "abstract")


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Narrowing applied on top of a query: publication window + field scope.

    Every source takes the same optional ``filters=`` argument, so a caller
    does not learn five different spellings of "between 2019 and 2024".

    ``year_from``/``year_to`` are inclusive.  ``year_to`` may also be the
    string ``"now"``, which means the current year — the natural reading of
    ``--to now`` and one the shell does not have to compute.

    Adapters pass what their API supports down to the server and filter the
    rest locally (see :func:`matches_filters`).  A record whose year is
    unknown is **kept** by a year filter: dropping it would silently hide the
    very grey literature that has no date, so it is the caller's job to see
    and exclude it.
    """

    year_from: int | None = None
    year_to: int | None = None
    field: str = "any"
    #: Allow-list of canonical work types (see ``work_types``).  Empty means
    #: every type; an unknown type on a record is kept, because it cannot be
    #: proved to be outside the list.
    types: tuple[str, ...] = ()
    #: Deny-list of canonical work types.  ``None`` means "the search default"
    #: (:data:`DEFAULT_SEARCH_EXCLUDE_TYPES`), unless ``types`` is non-empty —
    #: an explicit allow-list is unambiguous and wins.  Pass ``()`` to disable
    #: the default exclusions explicitly.
    exclude_types: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.field not in SEARCH_FIELDS:
            raise ValueError(
                f"unknown search field {self.field!r}; expected one of "
                f"{', '.join(SEARCH_FIELDS)}"
            )
        for name in ("year_from", "year_to"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ValueError(f"{name} must be an int or None, got {value!r}")
                if not 1000 <= value <= 2999:
                    raise ValueError(f"{name} {value} is not a plausible year")

        types = resolve_types(self.types)
        if self.exclude_types is None:
            excluded = () if types else DEFAULT_SEARCH_EXCLUDE_TYPES
        else:
            excluded = resolve_types(self.exclude_types)
        object.__setattr__(self, "types", types)
        object.__setattr__(self, "exclude_types", excluded)

    @property
    def active(self) -> bool:
        """True when this filter would actually remove something."""
        return bool(
            self.field != "any"
            or self.year_from is not None
            or self.year_to is not None
            or self.types
            or self.exclude_types
        )

    def type_ok(self, work_type: str | None) -> bool:
        """Whether a record's native ``work_type`` survives the type policy.

        An unknown/unrecognised type is kept for both an allow-list and a
        deny-list: there is no evidence it is out.  Exclusion wins when a
        caller names the same type in both lists.
        """
        canonical = normalize_work_type(work_type)
        if canonical and canonical in (self.exclude_types or ()):
            return False
        if self.types:
            return not canonical or canonical in self.types
        return True

    def year_ok(self, year: int | None) -> bool:
        """Whether a record's ``year`` falls inside the window (unknown passes)."""
        if year is None:
            return True
        if self.year_from is not None and year < self.year_from:
            return False
        if self.year_to is not None and year > self.year_to:
            return False
        return True


def _query_terms(query: str) -> list[str]:
    """Split a free-text query into the terms a field filter must all contain.

    Boolean operators and the field prefixes an adapter may have added are
    stripped first, so ``ti:(agents AND society)`` filters on ``agents`` and
    ``society`` rather than on the punctuation the API needed.
    """
    text = re.sub(r"\b(?:ti|abs|au|all):", " ", query or "", flags=re.IGNORECASE)
    text = re.sub(r"[()\"]", " ", text)
    stop = {"and", "or", "not", "the", "of", "in", "for", "a", "an"}
    return [t for t in re.findall(r"[\w'-]+", text.lower()) if t not in stop]


def matches_filters(
    paper: Paper, filters: SearchFilters | None, query: str = ""
) -> bool:
    """Whether ``paper`` survives ``filters``, judged on the parsed record.

    ``query`` is needed only for a field filter: every term it contains must
    appear in the chosen field, which is how a source that cannot push the
    scope down to its API approximates one.
    """
    if filters is None or not filters.active:
        return True
    if not filters.year_ok(paper.year):
        return False
    # Work-type policy is enforced by the pipeline after the merge, not here:
    # an adapter that filtered it would make its per-source count disagree with
    # the PRISMA "identified" count, and the pipeline has the complete record
    # (a merge may fill a type one source did not know).  Adapters still push an
    # allow-list down to their API where it is expressible.
    if filters.field == "any":
        return True
    haystack = (
        paper.title if filters.field == "title" else paper.abstract
    ).lower()
    terms = _query_terms(query)
    return all(term in haystack for term in terms)


def apply_filters(
    papers: list[Paper], filters: SearchFilters | None, query: str = ""
) -> list[Paper]:
    """Drop the records ``filters`` exclude, preserving order."""
    if filters is None or not filters.active:
        return papers
    return [p for p in papers if matches_filters(p, filters, query)]


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a source can actually do — asked before any request is made."""

    search: bool = True
    get: bool = True
    references: bool = False
    citations: bool = False
    #: Source exposes a direct open-access PDF URL for a record.
    pdf: bool = False
    #: Source can batch-resolve records by DOI (``works_by_dois``), which the
    #: pipeline uses for the missing-count/abstract backfill.  Off by default:
    #: a source without the method must never be asked for it.
    enrich: bool = False

    def supports(self, operation: str) -> bool:
        return bool(getattr(self, operation, False))


class RateGate:
    """Serialize requests to one source so the minimum interval holds.

    Thread-safe: the lock covers the wait, so two threads sharing a source
    cannot both conclude "it has been long enough" in the same instant.  A
    ``min_interval`` of ``0`` still serializes the bookkeeping, which costs
    nothing measurable and keeps behaviour identical when a library (arXiv's
    ``Client``) already enforces its own delay.
    """

    __slots__ = ("_gate", "_last", "_lock", "min_interval")

    def __init__(self, min_interval: float = 0.0) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> float:
        """Block until it is this source's turn; return seconds actually slept."""
        with self._lock:
            now = time.monotonic()
            delay = self._last + self.min_interval - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._last = now
            return max(0.0, delay)


def parse_retry_after(headers: Any) -> float | None:
    """Seconds from a ``Retry-After`` header, or ``None`` if unusable.

    Only the delta-seconds form is read: the HTTP-date form needs clock
    arithmetic the caller has no business doing, so it — and any garbage —
    becomes ``None`` and the caller falls back to its own default delay.
    """
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
    except AttributeError:  # pragma: no cover - exotic header containers
        return None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def require_dependency(module: str, source: str) -> Any:
    """Import ``module`` or raise :class:`SourceUnavailableError`.

    Used by constructors so a missing third-party wheel fails loudly with the
    source's name, instead of as an opaque ``ModuleNotFoundError`` three
    frames deeper inside someone else's client.

    The return type is :class:`~typing.Any` on purpose: adapters call into the
    library's own classes (``arxiv.Search``, ``Bio.Entrez``), none of which are
    part of this package's public surface.
    """
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise SourceUnavailableError(
            source, f"dependency {module!r} is not installed ({exc})"
        ) from exc


class Source(abc.ABC):
    """Base class for one scholarly source.

    Subclasses set :attr:`name` and :attr:`capabilities`, override
    :attr:`default_interval`, and implement :meth:`search` and :meth:`get`.
    Optional operations stay at the base-class implementations, which raise
    :class:`UnsupportedOperation`.
    """

    #: Registry key, e.g. ``"arxiv"``.  Also the tag in ``Paper.sources``.
    name: str = ""
    #: Declared capabilities; override as a class attribute.
    capabilities: Capabilities = Capabilities()

    def __init__(self, settings: Settings | None = None, **_: object) -> None:
        self.settings = settings if settings is not None else Settings()
        self._gate = RateGate(self.default_interval)

    # -- configuration --------------------------------------------------

    @property
    def default_interval(self) -> float:
        """Minimum seconds between two requests to this source."""
        return 0.0

    def wait(self) -> float:
        """Wait out this source's rate limit.  Call before every request."""
        return self._gate.wait()

    # -- capability checks ----------------------------------------------

    def supports(self, operation: str) -> bool:
        return self.capabilities.supports(operation)

    def require_support(self, operation: str) -> None:
        if not self.supports(operation):
            raise UnsupportedOperation(f"source {self.name!r} has no {operation!r}")

    # -- required operations --------------------------------------------

    @abc.abstractmethod
    def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        filters: SearchFilters | None = None,
    ) -> list[Paper]:
        """Return up to ``limit`` papers matching ``query``, best first.

        ``filters`` narrows by publication year and field scope.  An adapter
        pushes down whatever its API supports and applies the remainder
        locally with :func:`apply_filters`, so the contract is identical across
        sources even where the API is not.
        """

    @abc.abstractmethod
    def get(self, key: str) -> Paper:
        """Fetch one record by identifier.

        Raises :class:`~researcher_python.errors.NotFoundError` when the
        source knows the identifier format but has no such record.
        """

    # -- optional operations --------------------------------------------

    def references(self, paper: Paper) -> list[Paper]:
        """Works cited by ``paper``."""
        self.require_support("references")
        raise AssertionError("unreachable: require_support raised")

    def citations(self, paper: Paper) -> list[Paper]:
        """Works that cite ``paper``."""
        self.require_support("citations")
        raise AssertionError("unreachable: require_support raised")

    # -- misc ------------------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name!r}>"
