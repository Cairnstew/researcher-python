"""OpenAlex adapter.

OpenAlex is the workhorse source: it has a DOI- and PMID-lookup path, a free
text ``search``, citation graphs in both directions, and open-access status —
all over plain JSON, so this adapter uses :mod:`requests` directly instead of
adding a wrapper library that is not packaged.

Two quirks drive the design:

* **Anonymous search pauses.**  The ``search=`` parameter is served by a
  dedicated cluster that intermittently returns ``503 Search temporarily
  unavailable`` with a ``Retry-After`` header while it recovers from load.
  A key (``RESEARCHER_OPENALEX_API_KEY``) removes that mode entirely; without
  one, :meth:`OpenAlexSource._get` honours ``Retry-After`` for a bounded
  number of attempts and then reports the server's own message.
* **``cites:`` and ``referenced_works:`` both mean "cites" as filters**, so a
  paper's *reference list* is read from the work's own ``referenced_works``
  array and resolved with ``filter=openalex:W1|W2|...`` — never from a filter.
  Results therefore come back in OpenAlex' order, not the paper's citation
  order, and :meth:`OpenAlexSource.references` documents that.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote

from ..config import Settings
from ..errors import NotFoundError, RateLimitError, SourceError
from ..ids import is_arxiv_id, is_doi, is_pmid, normalize_arxiv_id, normalize_doi, normalize_orcid, normalize_pmid
from ..models import Author, Paper
from .base import (
    Capabilities,
    SearchFilters,
    Source,
    apply_filters,
    parse_retry_after,
    squash_text,
)

__all__ = ["OpenAlexSource", "reconstruct_abstract", "parse_openalex_work"]

_API = "https://api.openalex.org"
_WORKS = f"{_API}/works"
#: OpenAlex caps ``per-page`` well below this, but 100 keeps every page size
#: inside its documented limits on both the anonymous and keyed tiers.
_PAGE_SIZE = 100
#: Filter values are ``|``-joined; chunking keeps a URL comfortably short.
_FILTER_CHUNK = 50
_MAX_ATTEMPTS = 3
#: Upper bound on a single ``Retry-After`` sleep (seconds).
_MAX_BACKOFF = 30.0
#: Backoff used when the server sends no usable ``Retry-After``.
_DEFAULT_RETRY_SLEEP = 2.0
_ID_RE = re.compile(r"https?://openalex\.org/(W\d+)$")


def _as_mapping(value: Any) -> dict[str, Any]:
    """Coerce a JSON value to a plain dict, or ``{}`` for null / wrong shape."""
    return dict(value) if isinstance(value, Mapping) else {}


def reconstruct_abstract(inverted: Any) -> str:
    """Turn an OpenAlex ``abstract_inverted_index`` back into prose.

    The index maps ``{"word": [positions]}``; positions may be out of order and
    are not guaranteed to be contiguous.  Non-mapping / empty input yields
    ``""`` so a missing abstract never raises.
    """
    if not isinstance(inverted, Mapping) or not inverted:
        return ""
    placed: list[tuple[int, str]] = []
    for word, positions in inverted.items():
        if not isinstance(positions, Iterable) or isinstance(positions, (str, bytes)):
            continue
        for pos in positions:
            try:
                placed.append((int(pos), str(word)))
            except (TypeError, ValueError):  # pragma: no cover - malformed input
                continue
    placed.sort()
    return " ".join(word for _, word in placed)


def _short_id(value: Any) -> str:
    """``https://openalex.org/W2741809807`` -> ``W2741809807``."""
    text = squash_text(value)
    match = _ID_RE.search(text)
    return match.group(1) if match else text.rsplit("/", 1)[-1]


def _pmcid(value: Any) -> str:
    """``https://pmc.ncbi.nlm.nih.gov/articles/PMC123/`` -> ``PMC123``.

    The id is located case-insensitively *in the string as given* and only
    then upper-cased.  Searching an already-uppercased copy instead would
    match the ``PMC`` inside the host (``HTTPS://PMC.NCB...``) and stop at
    the following ``.``, so a URL form would yield ``""``.
    """
    text = squash_text(value)
    match = re.search(r"PMC(\d+)", text, re.IGNORECASE)
    return f"PMC{match.group(1)}" if match else ""


def _arxiv_from_locations(work: Mapping[str, Any]) -> str:
    """Find an arXiv id in the work's locations.

    OpenAlex indexes arXiv as a repository rather than an identifier, so the id
    only ever appears inside a location's landing page URL.
    """
    locations = work.get("locations") or []
    if not isinstance(locations, list):
        return ""
    for location in locations:
        if not isinstance(location, Mapping):
            continue
        for key in ("landing_page_url", "pdf_url"):
            candidate = normalize_arxiv_id(location.get(key))
            if candidate and is_arxiv_id(candidate):
                return candidate
    return ""


def parse_openalex_work(work: Mapping[str, Any]) -> Paper:
    """Turn one OpenAlex work object into a :class:`~researcher_python.models.Paper`."""
    openalex_id = _short_id(work.get("id"))
    raw_ids = _as_mapping(work.get("ids"))

    doi = normalize_doi(raw_ids.get("doi") or work.get("doi"))
    pmid = normalize_pmid(raw_ids.get("pmid"))
    pmcid = _pmcid(raw_ids.get("pmcid") or raw_ids.get("pmcid_id") or "")

    primary_location = _as_mapping(work.get("primary_location"))
    source = _as_mapping(primary_location.get("source"))
    best_oa = _as_mapping(work.get("best_oa_location"))
    open_access = _as_mapping(work.get("open_access"))
    biblio = _as_mapping(work.get("biblio"))

    first_page = squash_text(biblio.get("first_page"))
    last_page = squash_text(biblio.get("last_page"))
    pages = first_page
    if last_page and last_page != first_page:
        pages = f"{first_page}-{last_page}" if first_page else last_page

    authors: list[Author] = []
    for authorship in work.get("authorships") or ():
        if not isinstance(authorship, Mapping):
            continue
        person = authorship.get("author") or {}
        if not isinstance(person, Mapping):
            person = {}
        name = squash_text(person.get("display_name") or authorship.get("raw_author_name"))
        if not name:
            continue
        affiliations = [
            squash_text(inst.get("display_name"))
            for inst in (authorship.get("institutions") or ())
            if isinstance(inst, Mapping) and squash_text(inst.get("display_name"))
        ]
        if not affiliations:
            affiliations = [
                squash_text(x)
                for x in (authorship.get("raw_affiliation_strings") or ())
                if squash_text(x)
            ]
        authors.append(
            Author(
                name=name,
                author_id=_short_id(person.get("id")),
                orcid=normalize_orcid(person.get("orcid")),
                affiliations=affiliations,
            )
        )

    categories = [
        squash_text(topic.get("display_name"))
        for topic in (work.get("topics") or ())
        if isinstance(topic, Mapping) and squash_text(topic.get("display_name"))
    ]
    keywords = [
        squash_text(kw.get("display_name"))
        for kw in (work.get("keywords") or ())
        if isinstance(kw, Mapping) and squash_text(kw.get("display_name"))
    ]

    pdf_url = squash_text(best_oa.get("pdf_url")) or squash_text(open_access.get("oa_url"))

    paper = Paper(
        sources=["openalex"],
        ids={"openalex": openalex_id} if openalex_id else {},
        doi=doi,
        arxiv_id=_arxiv_from_locations(work),
        pmid=pmid,
        pmcid=pmcid,
        url=squash_text(primary_location.get("landing_page_url"))
        or squash_text(raw_ids.get("openalex"))
        or (f"https://openalex.org/{openalex_id}" if openalex_id else ""),
        pdf_url=pdf_url,
        title=squash_text(work.get("display_name") or work.get("title")),
        abstract=reconstruct_abstract(work.get("abstract_inverted_index")),
        authors=authors,
        year=work.get("publication_year") if isinstance(work.get("publication_year"), int) else None,
        published=squash_text(work.get("publication_date")),
        venue=squash_text(source.get("display_name")),
        volume=squash_text(biblio.get("volume")),
        issue=squash_text(biblio.get("issue")),
        pages=pages,
        publisher=squash_text(source.get("host_organization_name")),
        language=squash_text(work.get("language")),
        work_type=squash_text(work.get("type")),
        categories=categories,
        keywords=keywords,
        citation_count=work.get("cited_by_count")
        if isinstance(work.get("cited_by_count"), int)
        else None,
        reference_count=work.get("referenced_works_count")
        if isinstance(work.get("referenced_works_count"), int)
        else None,
        is_open_access=bool(open_access.get("is_oa")),
        oa_status=squash_text(open_access.get("oa_status")),
        license=squash_text(best_oa.get("license")),
    )
    if openalex_id:
        paper.ids.setdefault("openalex", openalex_id)
    if doi:
        paper.ids.setdefault("doi", doi)
    if pmid:
        paper.ids.setdefault("pmid", pmid)
    return paper


class OpenAlexSource(Source):
    """OpenAlex works API."""

    name = "openalex"
    capabilities = Capabilities(search=True, get=True, references=True, citations=True, pdf=True)

    def __init__(self, settings: Settings | None = None, *, timeout: float = 30.0, **_: object) -> None:
        super().__init__(settings)
        # Import lazily so ``import researcher_python`` never needs requests.
        import requests  # noqa: PLC0415 - intentional lazy dependency

        self._requests = requests
        self._timeout = float(timeout)
        self._session = requests.Session()
        agent = "researcher-python/0.1"
        if self.settings.email:
            agent = f"{agent} (mailto:{self.settings.email})"
        self._session.headers.update({"User-Agent": agent})

    @property
    def default_interval(self) -> float:
        return 0.1

    # -- transport -------------------------------------------------------

    def _params(self, extra: Mapping[str, Any]) -> dict[str, Any]:
        params = dict(extra)
        if self.settings.email:
            params.setdefault("mailto", self.settings.email)
        if self.settings.openalex_api_key:
            params.setdefault("api_key", self.settings.openalex_api_key)
        return params

    def _get(self, url: str, params: Mapping[str, Any]) -> Any:
        """GET ``url`` as JSON, retrying OpenAlex's temporary 503s.

        Raises :class:`NotFoundError` for a 404 (OpenAlex puts the reason in
        the body), :class:`RateLimitError` for a 429, and
        :class:`SourceError` for everything else — including a 503 that
        outlives the retry budget, so the API's own "Anonymous search is
        paused ..." text reaches the user instead of a bare status code.
        """
        query = self._params(params)
        last_detail = ""
        for attempt in range(_MAX_ATTEMPTS):
            self.wait()
            try:
                response = self._session.get(url, params=query, timeout=self._timeout)
            except self._requests.RequestException as exc:
                raise SourceError(self.name, f"OpenAlex request failed: {exc}", cause=exc) from exc

            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise SourceError(self.name, "OpenAlex returned non-JSON", cause=exc) from exc

            last_detail = _error_detail(response)
            if response.status_code == 404:
                raise NotFoundError(self.name, last_detail or "not found in OpenAlex")
            if response.status_code == 429 or response.status_code == 503:
                if attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(_retry_delay(response))
                    continue
                if response.status_code == 429:
                    raise RateLimitError(
                        self.name,
                        last_detail or "OpenAlex rate limit exceeded",
                        retry_after=parse_retry_after(getattr(response, "headers", None)),
                    )
                raise SourceError(self.name, last_detail or "OpenAlex unavailable")
            raise SourceError(
                self.name, last_detail or f"OpenAlex returned HTTP {response.status_code}"
            )
        raise SourceError(self.name, last_detail or "OpenAlex unavailable")  # pragma: no cover

    def _works(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        payload = self._get(_WORKS, params)
        results = payload.get("results") if isinstance(payload, Mapping) else None
        return [dict(r) for r in (results or []) if isinstance(r, Mapping)]

    def _filter(self, expression: str, want: int) -> list[Paper]:
        """Resolve one ``filter=`` expression, paging until ``want`` records."""
        collected: list[Paper] = []
        page = 1
        while len(collected) < want:
            batch = self._works(
                {
                    "filter": expression,
                    "per-page": min(_PAGE_SIZE, want - len(collected)),
                    "page": page,
                }
            )
            if not batch:
                break
            collected.extend(parse_openalex_work(w) for w in batch)
            if len(batch) < min(_PAGE_SIZE, want - len(collected) + len(batch)):
                break
            page += 1
        return collected[:want]

    # -- identity --------------------------------------------------------

    def _address(self, key: str) -> str:
        """Map a caller-supplied identifier onto an OpenAlex work address."""
        if not key:
            raise NotFoundError(self.name, "empty identifier")
        if _ID_RE.search(squash_text(key)) or (key[:1] == "W" and key[1:].isdigit()):
            return squash_text(key)
        if is_doi(key):
            # OpenAlex resolves a bare ``10.x/...`` only through the resolver
            # URL — ``/works/10.x/...`` is a 404, ``/works/https://doi.org/10.x/...``
            # is not.
            return f"https://doi.org/{normalize_doi(key)}"
        if is_pmid(key):
            return f"pmid:{normalize_pmid(key)}"
        bare = squash_text(key)
        if bare.upper().startswith("PMC") and bare[3:].isdigit():
            return f"pmcid:{bare.upper()}"
        if is_arxiv_id(key):
            raise NotFoundError(
                self.name,
                "OpenAlex does not index arXiv ids; use the 'arxiv' source "
                f"(got {key!r})",
            )
        raise NotFoundError(self.name, f"unrecognised identifier {key!r}")

    def _fetch(self, key: str) -> dict[str, Any]:
        address = self._address(key)
        payload = self._get(f"{_WORKS}/{quote(address, safe='')}", {})
        if not isinstance(payload, Mapping):
            raise SourceError(self.name, "OpenAlex returned an unexpected payload")
        return dict(payload)

    def _require_work(self, paper: Paper) -> dict[str, Any]:
        """Fetch the OpenAlex record for ``paper`` by whichever id it carries."""
        openalex_id = paper.id_for("openalex")
        if openalex_id:
            return self._fetch(openalex_id)
        for key in (paper.doi, paper.pmid, paper.pmcid):
            if key:
                return self._fetch(key)
        raise NotFoundError(
            self.name, f"record has no OpenAlex addressable id: {paper.title!r}"
        )

    # -- operations ------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        filters: SearchFilters | None = None,
    ) -> list[Paper]:
        self.require_support("search")
        want = self.settings.max_results if limit is None else max(0, int(limit))
        text = (query or "").strip()
        if want == 0 or not text:
            return []

        params: dict[str, Any] = {}
        if filters is not None and filters.field != "any":
            # title.search / abstract.search are OpenAlex filter keys, so the
            # scope is applied by the server rather than after the fact.
            key = "title.search" if filters.field == "title" else "abstract.search"
            params["filter"] = f"{key}:{text}"
        else:
            params["search"] = text
        if filters is not None:
            if filters.year_from is not None:
                params["filter"] = ",".join(
                    x for x in (params.get("filter", ""), f"from_publication_date:{filters.year_from}-01-01") if x
                )
            if filters.year_to is not None:
                params["filter"] = ",".join(
                    x for x in (params.get("filter", ""), f"to_publication_date:{filters.year_to}-12-31") if x
                )

        collected: list[Paper] = []
        page = 1
        while len(collected) < want:
            batch = self._works(
                {**params, "per-page": min(_PAGE_SIZE, want - len(collected)), "page": page}
            )
            if not batch:
                break
            collected.extend(parse_openalex_work(w) for w in batch)
            if len(batch) < min(_PAGE_SIZE, want - len(collected) + len(batch)):
                break
            page += 1
        return apply_filters(collected, filters, query)[:want]

    def get(self, key: str) -> Paper:
        self.require_support("get")
        return parse_openalex_work(self._fetch(key))

    def references(self, paper: Paper) -> list[Paper]:
        self.require_support("references")
        work = self._require_work(paper)
        raw = work.get("referenced_works") or []
        ids = [squash_text(x).rsplit("/", 1)[-1] for x in raw if squash_text(x)]
        ids = [i for i in ids if i]
        if not ids:
            return []
        out: list[Paper] = []
        for start in range(0, len(ids), _FILTER_CHUNK):
            chunk = ids[start:start + _FILTER_CHUNK]
            out.extend(self._filter(f"openalex:{'|'.join(chunk)}", len(chunk)))
        return out

    def citations(self, paper: Paper) -> list[Paper]:
        self.require_support("citations")
        work = self._require_work(paper)
        openalex_id = _short_id(work.get("id"))
        if not openalex_id:
            raise NotFoundError(self.name, "record has no OpenAlex id")
        return self._filter(f"cites:{openalex_id}", self.settings.max_results)


# ---------------------------------------------------------------------- #
# Internal helpers
# ---------------------------------------------------------------------- #


def _error_detail(response: Any) -> str:
    """Pull OpenAlex's own human-readable failure message out of a response."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, Mapping):
        bits = [squash_text(payload.get(k)) for k in ("error", "message")]
        detail = ". ".join(b for b in bits if b)
        if detail:
            return detail
    # A non-JSON body (a proxy's HTML 404, say) says nothing useful about the
    # record — fall back to the status line rather than echoing markup.
    return ""


def _retry_delay(response: Any) -> float:
    """Seconds to sleep before retrying: ``Retry-After``, else a short backoff."""
    parsed = parse_retry_after(getattr(response, "headers", None))
    seconds = _DEFAULT_RETRY_SLEEP if parsed is None else parsed
    return max(0.0, min(seconds, _MAX_BACKOFF))
