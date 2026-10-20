"""Web source registry and the top-level ``web_search`` entry point.

Web sources (Q&A sites, forums, issue trackers) are a separate family from
the scholarly :mod:`researcher_python.sources` registry: they return
:class:`~researcher_python.web_sources.base.WebResult` records, not
:class:`~researcher_python.models.Paper`, and never pretend to be papers.
The registry is deliberately eager and tiny (adapters only need ``requests``,
which is a hard dependency of the package), so adding one is a one-line
addition here.

``web_search`` fans a query out to *one* source for now; a multi-source fan-out
across several web sites (Stack Overflow + Super User, say) is the same shape
as :func:`researcher_python.pipeline.search` and can be added on top of this
registry later.  Retry policy matches the scholarly pipeline (D8): the
adapter never loops, ``web_search`` retries :class:`RateLimitError` up to
``settings.retries`` extra times.
"""

from __future__ import annotations

import time
from typing import Any

from .. import _retry
from ..config import Settings
from ..errors import ConfigurationError
from .base import WebResult, WebSearchResult, WebSource
from .format import (
    render_web_bibtex,
    render_web_csl_json,
    render_web_ris,
    web_bibtex_entry,
    web_bibtex_key,
    web_csl_item,
    web_ris_entry,
    web_to_bibtex,
    web_to_csl_json,
    web_to_ris,
)
from .reddit import RedditSource
from .stackexchange import StackExchangeSource

__all__ = [
    "DEFAULT_WEB_SOURCES",
    "WEB_SOURCES",
    "WebResult",
    "WebSearchResult",
    "WebSource",
    "create_web_source",
    "render_web_bibtex",
    "render_web_csl_json",
    "render_web_ris",
    "web_bibtex_entry",
    "web_bibtex_key",
    "web_csl_item",
    "web_ris_entry",
    "web_search",
    "web_source_names",
    "web_to_bibtex",
    "web_to_csl_json",
    "web_to_ris",
]

#: Registry key -> adapter class.  Kept as plain values (no lazy path+
#: classname table like the scholarly registry) because every web adapter
#: imports nothing heavier than ``requests``.
WEB_SOURCES: dict[str, type[WebSource]] = {
    # Defaults-first order: web_source_names() mirrors the dict, so the
    # default source stays first (asserted by the registry tests).
    "stackoverflow": StackExchangeSource,
    "reddit": RedditSource,
}

#: Sources used when none are named.
DEFAULT_WEB_SOURCES: tuple[str, ...] = ("stackoverflow",)

#: Indirection so tests can stub the wait instead of sleeping.
_sleep = time.sleep


def web_source_names() -> list[str]:
    """Every registered web source key, defaults first."""
    registered = list(WEB_SOURCES)
    return [n for n in DEFAULT_WEB_SOURCES if n in registered] + [
        n for n in registered if n not in DEFAULT_WEB_SOURCES
    ]


def create_web_source(
    name: str, settings: Settings | None = None, **kwargs: object
) -> WebSource:
    """Instantiate the adapter registered under ``name``.

    Raises :class:`~researcher_python.errors.ConfigurationError` for an unknown
    key; extra ``kwargs`` go to the adapter's constructor (e.g.
    ``site="superuser"``).
    """
    cls = WEB_SOURCES.get(name)
    if cls is None:
        known = ", ".join(web_source_names())
        raise ConfigurationError(f"unknown web source {name!r}; known sources: {known}")
    return cls(settings=settings, **kwargs)  # type: ignore[arg-type]


def web_search(
    query: str,
    *,
    source: str = "stackoverflow",
    max_results: int | None = None,
    sort: str | None = None,
    settings: Settings | None = None,
    **source_kwargs: object,
) -> WebSearchResult:
    """Search one web source and return its structured records.

    ``source`` must be a :data:`WEB_SOURCES` key (default ``stackoverflow``);
    per-source keyword arguments such as ``site=`` pass through to the
    adapter.  ``sort`` must be accepted by that source.  A
    :class:`~researcher_python.errors.RateLimitError` from the source is
    retried ``settings.retries`` extra times (D8); other errors raise.
    """
    settings = settings if settings is not None else Settings()
    src = create_web_source(source, settings, **source_kwargs)
    return _retry.with_retry(
        lambda: src.search(query, max_results=max_results, sort=sort),
        settings,
        sleep=_sleep,
    )