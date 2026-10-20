"""Reddit adapter — OAuth client-credentials search.

Pulls structured link records from Reddit's search listing.  The second
concrete web source, and deliberately shaped like the Stack Exchange adapter:
a :class:`~researcher_python.web_sources.base.WebSource` subclass whose only
job is ``query_params`` + ``parse_payload``, with the base class owning
transport, throttle, error classification and the paging loop.

Why OAuth is mandatory (leaned on the shipped docs and a live probe):

* The anonymous endpoints are closed: ``https://www.reddit.com/search.json``
  and ``https://api.reddit.com/search.json`` both answer HTTP 403 with a
  challenge page (no JSON, no data) to keyless data consumers.
* The supported route is OAuth 2.0 *client credentials* (app-only): register
  an app at https://www.reddit.com/prefs/apps (free), POST
  ``/api/v1/access_token`` with HTTP Basic auth using the app's ``client_id``
  / ``client_secret`` and ``grant_type=client_credentials``, then call
  ``https://oauth.reddit.com/search`` with an ``Authorization: Bearer``
  header.  The token expires after ``expires_in`` seconds (24h) and is
  refreshed lazily here.
* Rate limit hints arrive in response *headers* (``X-Ratelimit-Remaining``,
  ``X-Ratelimit-Used``, ``X-Ratelimit-Reset``), not the body — this is why
  :class:`WebSource` gained the :meth:`WebSource._after_response` hook: the
  adapter reads the headers and feeds the throttle the same way the SE
  adapter feeds its in-body ``backoff``.

Paging is the Listing protocol from the API docs: no page numbers.  Every
page answers with ``after`` (a fullname cursor) / ``before`` / ``dist``, and
the caller passes ``after`` + ``count`` (items already seen) on the next
request.  The adapter maps that onto the base's page loop: ``after`` is
recorded from the previous page's wrapper metadata and the integer ``page``
becomes the ``count`` parameter.  ``sort`` is one of ``relevance``, ``hot``,
``top``, ``new``, ``comments``; ``t`` narrows to a time window (``day`` ...
``all``); ``restrict_sr`` + ``sr`` scope the search to one subreddit (the
``subreddit=`` constructor keyword, mirroring ``site=`` on the SE adapter).

The adapter asks for ``raw_json=1`` so text fields carry real ``<``, ``>``
and ``&`` instead of the legacy HTML-escaped encoding the docs describe;
records are therefore *not* run through ``html.unescape`` (unlike the SE
adapter, whose API always escapes).

Dates are integer epoch seconds (``created_utc``), JSON-safe, matching the
house rule that records round-trip through ``json.dumps``.  The fixture is
constructed from the documented Listing shape rather than captured live —
the anonymous 403 means a live payload needs credentials.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..config import Settings
from ..errors import ConfigurationError, RateLimitError, SourceError
from ..sources.base import squash_text
from .base import WebResult, WebSource

__all__ = ["REDDIT_SORTS", "REDDIT_TIME_RANGES", "RedditSource", "parse_reddit_link"]

#: Sorts accepted by ``/search`` (relevance is the API's default).
REDDIT_SORTS: tuple[str, ...] = ("relevance", "hot", "top", "new", "comments")

#: ``t`` parameter values (default ``all`` = the API default, sent only when
#: something else is asked for).
REDDIT_TIME_RANGES: tuple[str, ...] = ("hour", "day", "week", "month", "year", "all")

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_OAUTH_BASE = "https://oauth.reddit.com"

#: Fields pulled into typed ``WebResult`` members; everything else on a link
#: object (``num_comments``, ``upvote_ratio``, ``is_self``, ...) stays
#: untouched in ``WebResult.extra``.  ``subreddit`` and ``link_flair_text``
#: feed ``tags`` rather than ``extra``.
_CAPTURED = frozenset(
    {
        "id",
        "title",
        "permalink",
        "url",
        "selftext",
        "score",
        "created_utc",
        "author",
        "subreddit",
        "link_flair_text",
    }
)

#: Rate-limit headers Reddit sends on every API response.
_RATE_HEADERS = ("X-Ratelimit-Used", "X-Ratelimit-Remaining", "X-Ratelimit-Reset")


def parse_reddit_link(item: Mapping[str, Any], source: str = "reddit") -> WebResult:
    """Turn one Reddit link object into a :class:`~researcher_python.web_sources.base.WebResult`.

    Pure and defensive: a missing key becomes a default, never an exception.
    The record's id is the base-36 link id stringified; ``created`` is the
    epoch-seconds ``created_utc``; ``tags`` carries the subreddit and (when
    present) the link flair; anything else (``num_comments``,
    ``upvote_ratio``, ``is_self``, ...) is kept verbatim in ``extra``.  The
    citable URL is the reddit thread (``permalink``), not the link target.
    """
    if not isinstance(item, Mapping):
        item = {}

    link_id = item.get("id")
    sid = str(link_id) if link_id is not None else ""
    permalink = squash_text(item.get("permalink"))
    url = (
        f"https://www.reddit.com{permalink}"
        if permalink
        else squash_text(item.get("url"))
    )

    tags: list[str] = []
    subreddit = squash_text(item.get("subreddit"))
    if subreddit:
        tags.append(subreddit)
    flair = squash_text(item.get("link_flair_text"))
    if flair:
        tags.append(flair)

    return WebResult(
        source=source or "reddit",
        id=sid,
        title=squash_text(item.get("title")),
        url=url,
        summary=squash_text(item.get("selftext")),
        score=item.get("score") if isinstance(item.get("score"), (int, float)) else None,
        created=item.get("created_utc")
        if isinstance(item.get("created_utc"), int)
        else None,
        author=squash_text(item.get("author")),
        tags=tags,
        extra={k: v for k, v in item.items() if k not in _CAPTURED},
    )


class RedditSource(WebSource):
    """Reddit OAuth search: links matching a query, optionally one subreddit."""

    name = "reddit"
    base_url = _OAUTH_BASE
    endpoint = "/search"
    page_size = 100
    sort_modes = REDDIT_SORTS
    default_sort = "relevance"
    default_interval = 1.0

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        subreddit: str = "",
        time_range: str = "all",
        timeout: float = 30.0,
        **kwargs: object,
    ) -> None:
        super().__init__(settings, timeout=timeout, **kwargs)
        #: Subreddit scope (``restrict_sr`` + ``sr``); "" searches everywhere.
        self.subreddit = (subreddit or "").strip().lstrip("r/")
        self.time_range = (
            time_range if time_range in REDDIT_TIME_RANGES else "all"
        )
        self._token: str | None = None
        self._token_expires = 0.0
        #: ``after`` cursor for the *next* page of the current search.
        self._next_after: str | None = None
        #: Ratelimit headers from the most recent response.
        self.last_rate_headers: dict[str, str | None] = {}

    # -- auth ---------------------------------------------------------------

    def _ensure_token(self) -> None:
        """Fetch (or reuse) an OAuth client-credentials bearer token.

        Credentials come from ``settings.reddit_client_id`` /
        ``reddit_client_secret``: missing → :class:`ConfigurationError`
        naming the env vars; a 401/403 from the token endpoint means the
        credentials were rejected → :class:`ConfigurationError`; 429 →
        :class:`RateLimitError`; anything else → :class:`SourceError`.
        """
        from time import monotonic

        now = monotonic()
        if self._token and now < self._token_expires:
            return
        client_id = self.settings.require("reddit_client_id")
        client_secret = self.settings.require("reddit_client_secret")

        try:
            response = self._session.post(
                _TOKEN_URL,
                auth=(client_id, client_secret),
                data={"grant_type": "client_credentials"},
                timeout=self._timeout,
            )
        except self._requests.RequestException as exc:
            raise SourceError(
                self.name, f"reddit token request failed: {exc}", cause=exc
            ) from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise SourceError(
                self.name, "reddit token endpoint returned non-JSON", cause=exc
            ) from exc

        if response.status_code != 200:
            if response.status_code in (401, 403):
                raise ConfigurationError(
                    "reddit OAuth rejected the client credentials; check "
                    "RESEARCHER_REDDIT_CLIENT_ID / RESEARCHER_REDDIT_CLIENT_SECRET"
                )
            if response.status_code == 429:
                raise RateLimitError(self.name, "reddit token endpoint rate limited")
            message = (
                squash_text(body.get("message"))
                if isinstance(body, Mapping)
                else ""
            )
            raise SourceError(
                self.name,
                message or f"reddit token endpoint returned HTTP {response.status_code}",
            )

        if not isinstance(body, Mapping):
            raise SourceError(self.name, "reddit token endpoint returned an unexpected payload")
        access = body.get("access_token")
        if not access:
            raise SourceError(self.name, "reddit token response had no access_token")
        self._token = str(access)
        try:
            expires = int(body.get("expires_in") or 86400)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            expires = 86400
        self._token_expires = now + max(0, expires) - 30  # refresh 30s early

    # -- hooks --------------------------------------------------------------

    def _get(self, endpoint: str, params: Mapping[str, Any]) -> Any:
        """Authorise, then let the base transport do the rest."""
        self._ensure_token()
        self._session.headers["Authorization"] = f"Bearer {self._token}"
        return super()._get(endpoint, params)

    def _after_response(self, response: Any) -> None:
        """Feed Reddit's *header* rate hints into the throttle.

        ``X-Ratelimit-Remaining`` is the per-app requests left in the current
        minute window and ``X-Ratelimit-Reset`` how many seconds until it
        rolls over.  When almost exhausted we hold the gate for the reset —
        the same adaptive behaviour the SE adapter gets from its in-body
        ``backoff``, one hook shared.
        """
        headers = getattr(response, "headers", None) or {}
        try:
            self.last_rate_headers = {key: headers.get(key) for key in _RATE_HEADERS}
            remaining = int(headers.get("X-Ratelimit-Remaining", ""))
            reset = float(headers.get("X-Ratelimit-Reset", "") or 0)
        except (TypeError, ValueError):
            return
        if remaining <= 1 and reset > 0:
            self._throttle.note_backoff(reset)

    def _wrapper_meta(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Listing wrapper keys live under ``data``, not at the top level."""
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            return {}
        return {k: data[k] for k in ("after", "before", "dist", "has_more") if k in data}

    def query_params(
        self, query: str, sort: str, page: int, pagesize: int
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "q": query,
            "limit": pagesize,
            "sort": sort,
            "raw_json": 1,
        }
        # Listings page by cursor, not page number: page 1 starts fresh, later
        # pages carry the previous response's ``after`` plus a count so Reddit
        # can keep ``before`` consistent.  ``page == 1`` also clears the
        # cursor left over from a previous search.
        if page == 1:
            self._next_after = None
        if page > 1:
            if self._next_after:
                params["after"] = self._next_after
            params["count"] = (page - 1) * pagesize
        if self.subreddit:
            params["restrict_sr"] = 1
            params["sr"] = self.subreddit
        if self.time_range != "all":
            params["t"] = self.time_range
        return params

    def parse_payload(self, payload: Any) -> tuple[list[WebResult], bool, dict[str, Any]]:
        if not isinstance(payload, Mapping):
            raise SourceError(self.name, "Reddit returned an unexpected payload")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise SourceError(self.name, "Reddit payload had no data object")
        meta = {k: data[k] for k in ("after", "before", "dist") if k in data}
        self._next_after = meta.get("after")

        records: list[WebResult] = []
        for child in data.get("children") or []:
            if not isinstance(child, Mapping):
                continue
            item = child.get("data")
            if isinstance(item, Mapping):
                records.append(parse_reddit_link(item, source=self.name))
        return records, bool(meta.get("after")), meta

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        scoped = f" subreddit={self.subreddit!r}" if self.subreddit else ""
        return f"<RedditSource{scoped}>"