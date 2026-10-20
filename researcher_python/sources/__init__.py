"""Source registry.

Importing this package never imports ``arxiv``, ``habanero``, or ``Bio`` — the
registry stores the *path* of each adapter module and imports it only when that
adapter is requested.  That is what keeps ``import researcher_python`` working
on a machine where one of the wheels is missing, and what lets the offline test
suite assert capability tables without installing anything.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from ..config import Settings
from ..errors import ConfigurationError, SourceUnavailableError

if TYPE_CHECKING:  # pragma: no cover
    from .base import Source

__all__ = [
    "DEFAULT_SOURCES",
    "create_source",
    "get_source_class",
    "source_names",
]

#: Registry key -> (adapter module path, class name).
_REGISTRY: dict[str, tuple[str, str]] = {
    "arxiv": ("researcher_python.sources.arxiv_source", "ArxivSource"),
    "openalex": ("researcher_python.sources.openalex_source", "OpenAlexSource"),
    "semantic_scholar": (
        "researcher_python.sources.semantic_scholar_source",
        "SemanticScholarSource",
    ),
    "crossref": ("researcher_python.sources.crossref_source", "CrossrefSource"),
    "pubmed": ("researcher_python.sources.pubmed_source", "PubMedSource"),
    # ACM's Digital Library: a prefix-scoped view of Crossref (no DL API exists).
    "acm": ("researcher_python.sources.acm_source", "ACMSource"),
    # Repository/thesis/deliverable aggregator — the grey-literature route.
    "openaire": ("researcher_python.sources.openaire_source", "OpenAIRESource"),
}

#: Order used when the caller names no sources: broad free-text first, then
#: identifier authority, then the biomedical index, then the two specialisms.
#:
#: ``acm`` and ``openaire`` are deliberately *not* here: both are subsets or
#: facets of Crossref's reach, and adding them to every unqualified search
#: would inflate results with records the broad sources already returned.
#: Name them (``--source acm``) when the specialism is the point.
DEFAULT_SOURCES: tuple[str, ...] = (
    "openalex",
    "semantic_scholar",
    "arxiv",
    "crossref",
    "pubmed",
)


def source_names() -> list[str]:
    """Every registered source key, in :data:`DEFAULT_SOURCES` order."""
    registered = list(_REGISTRY)
    ordered = [n for n in DEFAULT_SOURCES if n in _REGISTRY]
    return ordered + [n for n in registered if n not in ordered]


def get_source_class(name: str) -> type[Source]:
    """Import and return the adapter class registered under ``name``.

    Raises :class:`~researcher_python.errors.ConfigurationError` for an unknown
    key (the caller can catch one type and print ``source_names()``), and
    :class:`~researcher_python.errors.SourceUnavailableError` when the adapter
    module itself is registered but not yet written.

    A ``ModuleNotFoundError`` raised *inside* a loaded adapter (a third-party
    wheel like ``arxiv`` missing) is re-raised unchanged rather than reported
    as "not implemented" — the adapter exists, its dependency does not.
    """
    entry = _REGISTRY.get(name)
    if entry is None:
        known = ", ".join(source_names())
        raise ConfigurationError(f"unknown source {name!r}; known sources: {known}")
    module_path, class_name = entry
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        if exc.name == module_path:
            raise SourceUnavailableError(name, "not implemented yet") from exc
        raise
    return getattr(module, class_name)


def create_source(name: str, settings: Settings | None = None, **kwargs: object) -> Source:
    """Instantiate the adapter registered under ``name``.

    Any :class:`~researcher_python.errors.SourceUnavailableError` raised while
    importing or constructing is re-raised unchanged — it already carries the
    source name and the reason (missing dependency, missing credential).
    """
    cls = get_source_class(name)
    return cls(settings=settings, **kwargs)  # type: ignore[arg-type]
