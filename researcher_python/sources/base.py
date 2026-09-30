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
import threading
import time
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..errors import SourceUnavailableError, UnsupportedOperation
from ..models import Paper

__all__ = [
    "Capabilities",
    "RateGate",
    "Source",
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


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a source can actually do — asked before any request is made."""

    search: bool = True
    get: bool = True
    references: bool = False
    citations: bool = False
    #: Source exposes a direct open-access PDF URL for a record.
    pdf: bool = False

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
    def search(self, query: str, *, limit: int | None = None) -> list[Paper]:
        """Return up to ``limit`` papers matching ``query``, best first."""

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
