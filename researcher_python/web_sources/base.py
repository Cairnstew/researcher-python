"""Shared machinery for *web* sources (Q&A sites, forums, issue trackers).

The scholarly pipeline (:mod:`researcher_python.sources`) answers one
question: "which *paper* is this?"  Web sources answer a different one —
"what *content* on this site matches?" — so they get their own record model
(:class:`WebResult`) and their own base (:class:`WebSource`), deliberately
separate from :class:`researcher_python.sources.base.Source` and from
``Paper``.  A Reddit thread or a Stack Overflow answer is not a paper, and
forcing it into ``Paper`` would corrupt the scholarly export formats.

What the two families *share* is the error hierarchy (``SourceError``,
``RateLimitError``, ...), the retry policy (D8: adapters never loop, the
wrapper layer retries ``RateLimitError`` via :func:`researcher_python._retry.with_retry`),
and the throttle discipline (``RateGate``/``Retry-After`` ideas).  Reusing
those is the point of the foundation: a web adapter is ~40 lines of parsing
plus a registry entry.

Adding a new web source:

1. Subclass :class:`WebSource`, set ``name`` / ``base_url`` / ``endpoint`` /
   ``page_size`` / ``sort_modes`` / ``default_sort``.
2. Implement the two hooks:

   * ``query_params(query, sort, page, pagesize)`` — the request's query
     string parameters for one page.
   * ``parse_payload(payload)`` — API JSON in, ``(records, has_more, meta)``
     out, where ``meta`` carries wrapper-level keys (quota, cursors, ...).

   The base class runs the page loop, the throttle, transport and error
   classification; an api whose paging model is not page-number-based can
   override :meth:`WebSource.search` wholesale.
3. Register the class in ``researcher_python/web_sources/__init__.py`` with a
   :data:`WEB_SOURCES` entry.  ``web_search`` then reaches it directly.
4. Add offline tests (pure parser + a stubbed ``_get``) and a README row.

Throttling is two-tier: a minimum interval between requests (per source), plus
an *adaptive* cooldown fed by the API's own hints (Stack Exchange's
``backoff`` field, for example).  The source records the last response's
wrapper metadata on ``last_response_meta`` so callers and tests can read back
quota/backoff state.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..config import Settings
from ..errors import (
    ConfigurationError,
    RateLimitError,
    ResearcherError,
    SourceError,
)
from ..sources.base import squash_text

__all__ = [
    "WebResult",
    "WebSearchResult",
    "WebSource",
    "WebThrottle",
    "classify_web_error",
]


# ---------------------------------------------------------------------- #
# Records
# ---------------------------------------------------------------------- #


@dataclass(slots=True)
class WebResult:
    """One structured record from a web source (a question, a thread, ...).

    The fields are the intersection most Q&A / forum / tracker APIs expose;
    everything source-specific stays in :attr:`extra`, so the parser can put
    a site's long tail (``answer_count``, ``is_answered``, ``subreddit``, ...)
    somewhere lossless without contaminating the common shape.  JSON-safe by
    construction: ids are strings, ``created`` is integer epoch seconds.
    """

    source: str = ""
    id: str = ""
    title: str = ""
    url: str = ""
    summary: str = ""
    score: int | float | None = None
    created: int | None = None
    author: str = ""
    tags: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serialisable view of every field, including ``extra``."""
        return {
            "source": self.source,
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "summary": self.summary,
            "score": self.score,
            "created": self.created,
            "author": self.author,
            "tags": list(self.tags),
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WebResult:
        """Rebuild a record from :meth:`to_dict` output.

        Unknown keys are ignored, so an older reader survives a newer writer.
        """
        extra = data.get("extra") or {}
        return cls(
            source=str(data.get("source") or ""),
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            url=str(data.get("url") or ""),
            summary=str(data.get("summary") or ""),
            score=data.get("score"),
            created=_as_epoch(data.get("created")),
            author=str(data.get("author") or ""),
            tags=[str(t) for t in (data.get("tags") or [])],
            extra=dict(extra) if isinstance(extra, Mapping) else {},
        )


@dataclass(slots=True)
class WebSearchResult:
    """Outcome of one :meth:`WebSource.search` call."""

    #: The records, source order, best first.
    results: list[WebResult] = field(default_factory=list)
    #: Registry name of the source that produced them.
    source: str = ""
    #: ``True`` when more records existed than ``max_results`` allowed.
    truncated: bool = False
    #: Number of API pages fetched (one request each).
    pages: int = 0
    #: Wrapper-level quota state from the final page (``max``/``remaining``).
    quota: dict[str, int | None] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "truncated": self.truncated,
            "pages": self.pages,
            "quota": dict(self.quota),
            "results": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------------- #
# Throttle
# ---------------------------------------------------------------------- #


class WebThrottle:
    """Minimum-interval gate plus an adaptive API-hint cooldown.

    ``wait()`` first enforces the source's own minimum interval (the way
    ``RateGate`` does for scholarly sources), then any pending cooldown set
    by :meth:`note_backoff`.  All sleeping goes through this module's
    ``time`` object, so a test can stub ``web_sources.base.time.sleep`` and
    cover both tiers with one hook (unlike ``RateGate``, whose sleep lives in
    its own module — see ``tests/test_openalex_parse.py``).
    """

    __slots__ = ("min_interval", "_cooldown_until", "_last")

    def __init__(self, min_interval: float = 1.0) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self._last = 0.0
        self._cooldown_until = 0.0

    def wait(self) -> float:
        """Block until allowed to send; return seconds actually slept."""
        now = time.monotonic()
        delay = max(0.0, self._last + self.min_interval - now)
        if self._cooldown_until > now:
            delay = max(delay, self._cooldown_until - now)
        if delay > 0:
            time.sleep(delay)
            now = time.monotonic()
        self._last = now
        return max(0.0, delay)

    def note_backoff(self, seconds: float | int | None) -> None:
        """Hold the gate for ``seconds`` *after* the current wait backlog.

        The API told us to back off before hitting the same method again
        (Stack Exchange's ``backoff`` field).  The cooldown is monotonic —
        the longest hint wins.
        """
        if (
            isinstance(seconds, (int, float))
            and not isinstance(seconds, bool)
            and seconds > 0
        ):
            self._cooldown_until = max(
                self._cooldown_until, time.monotonic() + float(seconds)
            )


# ---------------------------------------------------------------------- #
# Error classification
# ---------------------------------------------------------------------- #


def classify_web_error(
    source: str, payload: Any, status: int
) -> ResearcherError:
    """Turn a non-200 response into the right exception.

    The body's ``error_name`` wins when present (Stack Exchange reports all
    method-call errors in the response wrapper, whatever the HTTP code), then
    the HTTP status.  ``throttle_violation`` and 429 → :class:`RateLimitError`
    (so :func:`researcher_python._retry.with_retry` retries it); everything
    else → :class:`~researcher_python.errors.SourceError` carrying the API's
    own message.
    """
    name = ""
    message = ""
    if isinstance(payload, Mapping):
        name = squash_text(payload.get("error_name"))
        message = squash_text(payload.get("error_message")) or message
    message = message or f"{source} returned HTTP {status}"

    if name == "throttle_violation" or status == 429:
        return RateLimitError(source, message or "rate limit exceeded")
    if name == "temporarily_unavailable":
        return SourceError(source, message or "temporarily unavailable")
    return SourceError(source, message)


# ---------------------------------------------------------------------- #
# Base source
# ---------------------------------------------------------------------- #


class WebSource(abc.ABC):
    """Base class for one structured web API.

    Subclasses set the class attributes below and implement
    :meth:`query_params` and :meth:`parse_payload`; the base class owns the
    request, the throttle, error classification and the paging loop.
    """

    #: Registry key, e.g. ``"stackoverflow"``.
    name: str = ""
    #: API root, e.g. ``"https://api.stackexchange.com/2.3"``.
    base_url: str = ""
    #: Path of the search method, e.g. ``"/search"``.
    endpoint: str = ""
    #: Largest page size the API accepts.
    page_size: int = 100
    #: Sort keys this source accepts, ``default_sort`` first if it is a sort.
    sort_modes: tuple[str, ...] = ("relevance",)
    #: Sort used when the caller passes nothing.
    default_sort: str = "relevance"
    #: Minimum seconds between two requests to this source.
    default_interval: float = 1.0

    def __init__(
        self, settings: Settings | None = None, *, timeout: float = 30.0, **_: object
    ) -> None:
        self.settings = settings if settings is not None else Settings()
        self._timeout = float(timeout)
        self._throttle = WebThrottle(self.default_interval)
        #: Wrapper metadata from the most recent successful response
        #: (``quota_*``, ``backoff``, ...); empty until the first request.
        self.last_response_meta: dict[str, Any] = {}
        # Lazy: importing the package must never require requests (house rule).
        import requests  # noqa: PLC0415 - intentional lazy dependency

        self._requests = requests
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "researcher-python/0.1"})

    # -- required hooks --------------------------------------------------

    @abc.abstractmethod
    def query_params(
        self, query: str, sort: str, page: int, pagesize: int
    ) -> dict[str, Any]:
        """Query-string parameters for one page of ``search`` results."""

    @abc.abstractmethod
    def parse_payload(self, payload: Any) -> tuple[list[WebResult], bool, dict[str, Any]]:
        """``(records, has_more, meta)`` from one API response.

        ``meta`` is the wrapper-level metadata (quota, cursors, ...) and is
        stored on :attr:`last_response_meta` by the transport.
        """

    # -- shared helpers ---------------------------------------------------

    def _wrapper_meta(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Pull known wrapper keys out of a payload (``has_more``, ...)."""
        meta: dict[str, Any] = {}
        for key in (
            "has_more",
            "quota_max",
            "quota_remaining",
            "backoff",
            "total",
            "page",
        ):
            if key in payload:
                meta[key] = payload[key]
        return meta

    def _quota(self) -> dict[str, int | None]:
        meta = self.last_response_meta
        return {
            "max": meta.get("quota_max"),
            "remaining": meta.get("quota_remaining"),
        }

    # -- transport --------------------------------------------------------

    def _get(self, endpoint: str, params: Mapping[str, Any]) -> Any:
        """GET ``endpoint`` as JSON, throttled, error-classified.

        A non-200 response raises via :func:`classify_web_error`; a 200
        payload records its wrapper metadata and honours a ``backoff`` hint
        before the *next* request.  This is the only method in the class that
        touches the network.
        """
        self._throttle.wait()
        url = f"{self.base_url}{endpoint}"
        try:
            response = self._session.get(url, params=dict(params), timeout=self._timeout)
        except self._requests.RequestException as exc:
            raise SourceError(
                self.name, f"{self.name} request failed: {exc}", cause=exc
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(
                self.name, f"{self.name} returned non-JSON", cause=exc
            ) from exc

        if response.status_code != 200:
            raise classify_web_error(
                self.name, payload if isinstance(payload, Mapping) else None,
                response.status_code,
            )

        if not isinstance(payload, Mapping):
            raise SourceError(self.name, f"{self.name} returned an unexpected payload")
        meta = self._wrapper_meta(payload)
        self.last_response_meta = meta
        backoff = meta.get("backoff")
        self._throttle.note_backoff(backoff)
        self._after_response(response)
        return payload

    def _after_response(self, response: Any) -> None:
        """Inspect a successful response; default no-op.

        Stack Exchange reports its backoff hint in the body wrapper, which
        ``_get`` already feeds to the throttle.  A source whose rate hints
        live in HTTP *headers* (Reddit's ``X-Ratelimit-*``) overrides this
        to read them and call :meth:`WebThrottle.note_backoff` instead.
        """

    # -- operations -------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        max_results: int | None = None,
        sort: str | None = None,
    ) -> WebSearchResult:
        """Search this source, paging until satisfied.

        ``max_results`` caps the total (default: ``settings.max_results``);
        ``sort`` must be one of :attr:`sort_modes`.  ``truncated`` is set when
        more records existed than were asked for; ``pages`` counts the API
        calls made.
        """
        text = (query or "").strip()
        want = self.settings.max_results if max_results is None else max(0, int(max_results))
        if want == 0 or not text:
            return WebSearchResult(source=self.name, results=[], pages=0)

        order = self.default_sort if sort is None else sort
        if order not in self.sort_modes:
            raise ConfigurationError(
                f"unknown sort {order!r}; expected one of {', '.join(self.sort_modes)}"
            )

        collected: list[WebResult] = []
        page = 1
        pages = 0
        truncated = False
        while len(collected) < want:
            pages += 1
            pagesize = min(self.page_size, want - len(collected))
            payload = self._get(
                self.endpoint, self.query_params(text, order, page, pagesize)
            )
            records, has_more, meta = self.parse_payload(payload)
            if meta:
                self.last_response_meta = meta
            collected.extend(records)
            if len(collected) >= want:
                truncated = bool(has_more)
                break
            if not has_more or not records:
                break
            page += 1

        return WebSearchResult(
            source=self.name,
            results=collected[:want],
            truncated=truncated,
            pages=pages,
            quota=self._quota(),
        )


# ---------------------------------------------------------------------- #
# Coercion helpers
# ---------------------------------------------------------------------- #


def _as_epoch(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None