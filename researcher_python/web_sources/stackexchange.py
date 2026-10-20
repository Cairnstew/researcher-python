"""Stack Exchange adapter — Stack Overflow by default.

Pulls structured Q&A records from the Stack Exchange API v2.3
(https://api.stackexchange.com/docs).  The first concrete web source; the
endpoint chosen is ``/search``, which is deliberately limited (``intitle``
only) per the API's own guidance — for real full-text search the API tells
you to use a search engine scoped to the site.

API contract this adapter leans on (all from the shipped docs):

* Every response is a wrapper object: ``items`` + ``has_more`` +
  ``quota_max`` / ``quota_remaining``, and optionally ``backoff`` — "wait
  this many seconds before hitting the same method again".  The base class
  reads the wrapper via ``parse_payload`` and feeds ``backoff`` into the
  throttle.
* ``site`` is a required parameter; the adapter defaults to
  ``stackoverflow`` and accepts any SE site name (``superuser``,
  ``serverfault``, ...) through the ``site=`` constructor keyword.
* Errors are reported *in the wrapper* (``error_id`` / ``error_name`` /
  ``error_message``) whatever the HTTP status; ``throttle_violation`` is a
  quota/rate failure and maps to :class:`RateLimitError` (retried once by
  ``web_search`` per D8), everything else to :class:`SourceError` with the
  API's own message.
* A ``key`` raises the anonymous shared quota (default 300/day observed
  without one) to an application-level quota; both run on one IP bucket, so
  the adapter passes ``settings.stackexchange_key`` when configured.

Dates are integer epoch seconds (SE's native format) and JSON-safe, matching
the house rule that records round-trip through ``json.dumps``.
"""

from __future__ import annotations

from html import unescape
from typing import Any, Mapping

from ..config import Settings
from ..errors import SourceError
from ..sources.base import squash_text
from .base import WebResult, WebSource

__all__ = ["SE_SORTS", "StackExchangeSource", "parse_stackexchange_question"]

#: Sorts accepted by ``/search`` (activity is the API's default; we expose
#: relevance for parity with the scholarly pipeline's ``sort=relevance``).
SE_SORTS: tuple[str, ...] = ("relevance", "activity", "votes", "creation")

_API = "https://api.stackexchange.com/2.3"

#: Fields pulled into typed ``WebResult`` members; everything else on a
#: question object (``answer_count``, ``is_answered``, ``view_count``, ...)
#: stays untouched in ``WebResult.extra``.
_CAPTURED = frozenset(
    {
        "question_id",
        "title",
        "link",
        "tags",
        "score",
        "creation_date",
        "owner",
    }
)


def parse_stackexchange_question(
    item: Mapping[str, Any], source: str = "stackoverflow"
) -> WebResult:
    """Turn one SE question object into a :class:`~researcher_python.web_sources.base.WebResult`.

    Pure and defensive: a missing key becomes a default, never an exception.
    The record's id is the numeric ``question_id`` stringified (ids are
    strings everywhere in the web model); ``created`` is the epoch-seconds
    ``creation_date``; the author is the owner's ``display_name``; the
    remainder of the item (``answer_count``, ``is_answered``, ...) is kept
    verbatim in ``extra``.
    """
    if not isinstance(item, Mapping):
        item = {}
    owner = item.get("owner")

    question_id = item.get("question_id")
    sid = str(question_id) if question_id is not None else ""
    site = source or "stackoverflow"
    url = squash_text(item.get("link")) or (
        f"https://{site}.com/questions/{sid}" if sid else ""
    )

    return WebResult(
        source=site,
        id=sid,
        # SE escapes entities in text fields (``&#39;`` for an apostrophe);
        # unescape so titles/authors carry real characters, not HTML codes.
        title=unescape(squash_text(item.get("title"))),
        url=url,
        score=item.get("score") if isinstance(item.get("score"), (int, float)) else None,
        created=item.get("creation_date")
        if isinstance(item.get("creation_date"), int)
        else None,
        author=unescape(squash_text(owner.get("display_name"))) if isinstance(owner, Mapping) else "",
        tags=[str(t) for t in (item.get("tags") or []) if isinstance(t, (str, int))],
        extra={k: v for k, v in item.items() if k not in _CAPTURED},
    )


class StackExchangeSource(WebSource):
    """Stack Exchange API: ``/search`` questions on one site."""

    name = "stackoverflow"
    base_url = _API
    endpoint = "/search"
    page_size = 100
    sort_modes = SE_SORTS
    default_sort = "relevance"
    default_interval = 1.0

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        site: str = "stackoverflow",
        timeout: float = 30.0,
        **kwargs: object,
    ) -> None:
        super().__init__(settings, timeout=timeout, **kwargs)
        self.site = site or "stackoverflow"

    def query_params(
        self, query: str, sort: str, page: int, pagesize: int
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "order": "desc",
            "sort": sort,
            "intitle": query,
            "page": page,
            "pagesize": pagesize,
            "filter": "default",
            "site": self.site,
        }
        if self.settings.stackexchange_key:
            params["key"] = self.settings.stackexchange_key
        return params

    def parse_payload(self, payload: Any) -> tuple[list[WebResult], bool, dict[str, Any]]:
        if not isinstance(payload, Mapping):
            raise SourceError(self.name, "Stack Exchange returned an unexpected payload")
        meta = self._wrapper_meta(payload)
        items = [
            dict(item)
            for item in (payload.get("items") or [])
            if isinstance(item, Mapping)
        ]
        records = [parse_stackexchange_question(item, source=self.site) for item in items]
        return records, bool(meta.get("has_more")), meta

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<StackExchangeSource site={self.site!r}>"