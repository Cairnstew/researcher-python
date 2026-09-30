"""Semantic Scholar adapter (Graph API v1 over plain :mod:`requests`).

Like OpenAlex, Semantic Scholar has no packaged wrapper in nixpkgs, so this
adapter calls the REST API directly with the ``requests`` that is already a
dependency.

The constraints that shape it:

* **An API key is the whole story (D7).**  Unauthenticated traffic from a
  shared IP is throttled hard and often persistently, so the key
  (``RESEARCHER_SEMANTIC_SCHOLAR_API_KEY``) is sent as an ``x-api-key``
  header when configured.  Keyless calls are still made — but a keyless 429
  says *which* env var to set, with the name read from
  :data:`researcher_python.config.ENV_KEYS` rather than hardcoded.
* **Every request names its fields.**  The Graph API returns a default field
  set that omits ``externalIds`` and ``openAccessPdf``, which are exactly the
  parts a unified model needs, so :data:`_FIELDS` is passed explicitly to
  search, get, references and citations alike.
* **Reference and citation entries are wrapped and may be null.**  ``/references``
  answers ``{"data": [{"citedPaper": {...}}, {"citedPaper": null}]}``; the null
  rows are dropped by :func:`unwrap_graph_list`, never turned into empty Papers.
* **Rate limiting is uniform.**  The documented baseline is about one request
  per second with or without a key, so :attr:`SemanticScholarSource.default_interval`
  is 1.1s for both tiers (D8: no retry loops — a 429 is reported, not retried).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from ..config import ENV_KEYS, Settings
from ..errors import NotFoundError, RateLimitError, SourceError
from ..ids import (
    is_arxiv_id,
    is_doi,
    is_pmid,
    normalize_arxiv_id,
    normalize_doi,
    normalize_pmid,
    wire_doi,
)
from ..models import Author, Paper
from .base import Capabilities, Source, parse_retry_after, require_dependency, squash_text

__all__ = ["SemanticScholarSource", "parse_s2_paper", "unwrap_graph_list"]

_API = "https://api.semanticscholar.org/graph/v1"
#: The Graph API caps ``limit`` here for both search and graph endpoints.
_PAGE_SIZE = 100
#: ``offset`` is bounded upstream; paging past this is pointless.
_MAX_OFFSET = 1000
#: Explicit on every call — the API's default set omits externalIds/openAccessPdf.
#: ``paperId`` is the Graph API's own stable id, kept in ``ids`` so a record can
#: be re-queried even when it has no DOI.
_FIELDS = "paperId,title,abstract,year,authors,externalIds,venue,openAccessPdf,citationCount"
#: ~1 request/second baseline for both tiers (keyless and keyed).
_INTERVAL = 1.1


# ---------------------------------------------------------------------- #
# Pure helpers (D4: no network)
# ---------------------------------------------------------------------- #


def _env_var_for(field: str) -> str:
    """Env var that feeds ``field``, read from :data:`config.ENV_KEYS`.

    Never hardcoded: adding a setting updates one mapping, not this adapter.
    """
    for env_name, name in ENV_KEYS.items():
        if name == field:
            return env_name
    return field


def parse_s2_paper(d: Mapping[str, Any]) -> Paper:
    """Turn one Graph API paper object into a :class:`~researcher_python.models.Paper`.

    Pure (D4): ``d`` is already-decoded JSON, so fixtures cover every branch
    without a socket.  Nulls are normal for this API (``abstract``,
    ``externalIds``, ``openAccessPdf`` are routinely absent) and must not raise.
    """
    record = dict(d) if isinstance(d, Mapping) else {}
    raw_ids = record.get("externalIds")
    external = dict(raw_ids) if isinstance(raw_ids, Mapping) else {}

    paper_id = squash_text(record.get("paperId"))
    doi = normalize_doi(external.get("DOI"))
    arxiv_id = normalize_arxiv_id(external.get("ArXiv"))
    pmid = normalize_pmid(external.get("PMID"))
    pmcid = squash_text(external.get("PMCID")).upper()

    oa = record.get("openAccessPdf")
    pdf_url = squash_text(oa.get("url")) if isinstance(oa, Mapping) else ""

    authors: list[Author] = []
    raw_authors = record.get("authors")
    if isinstance(raw_authors, list):
        for entry in raw_authors:
            if not isinstance(entry, Mapping):
                continue
            name = squash_text(entry.get("name"))
            if not name:
                continue
            authors.append(Author(name=name, author_id=str(entry.get("authorId") or "")))

    year = record.get("year")
    if isinstance(year, bool) or not isinstance(year, int):
        year = None
    citation_count = record.get("citationCount")
    if isinstance(citation_count, bool) or not isinstance(citation_count, int):
        citation_count = None

    ids: dict[str, str] = {}
    if paper_id:
        ids["semantic_scholar"] = paper_id
    if arxiv_id:
        ids["arxiv"] = arxiv_id
    if doi:
        ids["doi"] = doi
    if pmid:
        ids["pmid"] = pmid
    if pmcid:
        ids["pmc"] = pmcid

    # A DOI is the canonical landing page; fall back to Semantic Scholar's own
    # paper URL when the record has no DOI (and neither when it has neither).
    if doi:
        url = f"https://doi.org/{doi}"
    elif paper_id:
        url = f"https://www.semanticscholar.org/paper/{paper_id}"
    else:
        url = ""

    return Paper(
        sources=["semantic_scholar"],
        ids=ids,
        doi=doi,
        arxiv_id=arxiv_id,
        pmid=pmid,
        pmcid=pmcid,
        url=url,
        # capabilities.pdf is True: openAccessPdf.url is a direct PDF.
        pdf_url=pdf_url,
        title=squash_text(record.get("title")),
        abstract=squash_text(record.get("abstract")),
        authors=authors,
        year=year,
        venue=squash_text(record.get("venue")),
        citation_count=citation_count,
        # No openAccessPdf means the API found no free copy it trusts.
        is_open_access=bool(pdf_url),
    )


def unwrap_graph_list(payload: Any, wrap_key: str) -> list[dict[str, Any]]:
    """Flatten ``{"data": [{"<wrap_key>": {...}}]}`` and drop null entries.

    ``/references`` and ``/citations`` wrap every row (``citedPaper`` /
    ``citingPaper``) and return ``null`` for rows whose paper is unavailable —
    those are skipped rather than parsed into an empty record.
    """
    if not isinstance(payload, Mapping):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, Mapping):
            continue
        inner = entry.get(wrap_key)
        if isinstance(inner, Mapping):
            out.append(dict(inner))
    return out


# ---------------------------------------------------------------------- #
# Adapter
# ---------------------------------------------------------------------- #


class SemanticScholarSource(Source):
    """The Semantic Scholar Graph API (``api.semanticscholar.org/graph/v1``)."""

    name = "semantic_scholar"
    capabilities = Capabilities(
        search=True,
        get=True,
        references=True,
        citations=True,
        pdf=True,
    )

    def __init__(self, settings: Settings | None = None, **_: object) -> None:
        super().__init__(settings)
        self._requests = require_dependency("requests", self.name)
        self._session = self._requests.Session()
        self._session.headers.update({"User-Agent": "researcher-python/0.1"})

    @property
    def default_interval(self) -> float:
        return _INTERVAL

    # -- transport -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        key = self.settings.semantic_scholar_api_key
        return {"x-api-key": key} if key else {}

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """GET one Graph API path, mapping failures onto the error hierarchy (D8)."""
        self.wait()
        try:
            response = self._session.get(
                f"{_API}{path}",
                params=dict(params or {}),
                headers=self._headers(),
                timeout=float(self.settings.timeout),
            )
        except self._requests.RequestException as exc:
            # timeout / connection errors: transport, never "unavailable adapter".
            raise SourceError(
                self.name, f"Semantic Scholar request failed: {exc}", cause=exc
            ) from exc
        return self._handle(response)

    def _handle(self, response: Any) -> Any:
        status = getattr(response, "status_code", 0)
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = 0

        if status == 200:
            try:
                return response.json()
            except ValueError as exc:
                raise SourceError(
                    self.name, "Semantic Scholar returned non-JSON", cause=exc
                ) from exc

        detail = squash_text(getattr(response, "text", ""))
        if status == 404:
            raise NotFoundError(self.name, detail or "Semantic Scholar has no such paper")
        if status == 429:
            raise RateLimitError(
                self.name,
                self._rate_limit_message(response),
                retry_after=parse_retry_after(getattr(response, "headers", None)),
            )
        # D8: 5xx (and any other unexpected status) is a transport failure.
        raise SourceError(self.name, detail or f"Semantic Scholar returned HTTP {status}")

    def _rate_limit_message(self, response: Any) -> str:
        message = "Semantic Scholar rate limit exceeded"
        delay = parse_retry_after(getattr(response, "headers", None))
        if delay is not None:
            message += f" (Retry-After: {delay:g}s)"
        if not self.settings.semantic_scholar_api_key:
            # D7: name the setting to fix it, straight from ENV_KEYS.
            message += f"; set {_env_var_for('semantic_scholar_api_key')} to raise the quota"
        return message

    def _data(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, Mapping):
            raise SourceError(self.name, "Semantic Scholar returned an unexpected payload")
        raw = payload.get("data")
        return [dict(x) for x in (raw or ()) if isinstance(x, Mapping)]

    # -- identity --------------------------------------------------------

    @staticmethod
    def _hint(key: str) -> str:
        if key[:1] == "W" and key[1:].isdigit():
            return (
                "Semantic Scholar does not take OpenAlex ids; "
                f"use the 'openalex' source (got {key!r})"
            )
        return (
            "Semantic Scholar looks up DOIs, arXiv ids and PMIDs only, "
            f"got {key!r}"
        )

    def _address(self, key: str) -> str:
        """Prefix a caller-supplied identifier with the Graph API's id type (D3)."""
        text = squash_text(key)
        if not text:
            raise NotFoundError(self.name, "empty identifier")
        if is_doi(text):
            # D3: case is preserved exactly as given.
            return f"DOI:{wire_doi(text)}"
        if is_arxiv_id(text):
            return f"ARXIV:{normalize_arxiv_id(text)}"
        if is_pmid(text):
            return f"PMID:{normalize_pmid(text)}"
        raise NotFoundError(self.name, self._hint(text))

    def _paper_address(self, paper: Paper) -> str:
        for candidate in (paper.doi, paper.arxiv_id, paper.pmid):
            if candidate:
                return self._address(candidate)
        raise NotFoundError(
            self.name, f"record has no DOI, arXiv id or PMID: {paper.title!r}"
        )

    # -- operations ------------------------------------------------------

    def search(self, query: str, *, limit: int | None = None) -> list[Paper]:
        self.require_support("search")
        want = self.settings.max_results if limit is None else max(0, int(limit))
        text = (query or "").strip()
        if want == 0 or not text:
            return []

        collected: list[Paper] = []
        offset = 0
        while len(collected) < want and offset < _MAX_OFFSET:
            size = min(_PAGE_SIZE, want - len(collected))
            payload = self._get(
                "/paper/search",
                {"query": text, "limit": size, "offset": offset, "fields": _FIELDS},
            )
            items = self._data(payload)
            if not items:
                break
            collected.extend(parse_s2_paper(item) for item in items)
            offset += len(items)
            if len(items) < size:
                break
        return collected[:want]

    def get(self, key: str) -> Paper:
        self.require_support("get")
        address = self._address(key)
        payload = self._get(f"/paper/{quote(address, safe='')}", {"fields": _FIELDS})
        if not isinstance(payload, Mapping):
            raise SourceError(self.name, "Semantic Scholar returned an unexpected payload")
        return parse_s2_paper(payload)

    def references(self, paper: Paper) -> list[Paper]:
        self.require_support("references")
        return self._graph(paper, "/references", "citedPaper")

    def citations(self, paper: Paper) -> list[Paper]:
        self.require_support("citations")
        return self._graph(paper, "/citations", "citingPaper")

    def _graph(self, paper: Paper, suffix: str, wrap_key: str) -> list[Paper]:
        want = max(0, int(self.settings.max_results))
        if want == 0:
            return []
        address = self._paper_address(paper)
        payload = self._get(
            f"/paper/{quote(address, safe='')}{suffix}",
            {"limit": min(want, _PAGE_SIZE), "fields": _FIELDS},
        )
        return [parse_s2_paper(item) for item in unwrap_graph_list(payload, wrap_key)][:want]
