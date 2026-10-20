"""OpenAIRE — the grey-literature source (theses, reports, deliverables).

The five original sources index the *journal* literature well and the grey
literature badly.  A module whose Week 2-5 packs are WEF white papers, UN
reports, UK DSIT guidance, POSTnotes and doctoral theses needs a source that
aggregates **research repositories** — which is what OpenAIRE is: the European
open scholarly infrastructure, free and unauthenticated, aggregating
institutional repositories, EU-funded project deliverables and, crucially,
**theses and dissertations** (the ProQuest-shaped material the labs cover).

It also carries a ``resulttype`` facet, so ``--type thesis`` is a native
filter rather than a post-hoc guess from a title.

Endpoint: ``https://api.openaire.eu/search/publications`` (JSON), which accepts
``keywords``, ``title``, ``doi``, ``fromDateAccepted``/``toDateAccepted``,
``resulttype`` and ``sortBy``.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..errors import ConfigurationError, NotFoundError, SourceError
from ..ids import normalize_doi, wire_doi, year_from_date
from ..models import Author, Paper
from .base import (
    Capabilities,
    RateGate,
    SearchFilters,
    Source,
    apply_filters,
    parse_retry_after,
    require_dependency,
    squash_text,
)

__all__ = ["OpenAIRESource", "RESULT_TYPES", "parse_openaire_result"]

_API = "https://api.openaire.eu/search"
_PAGE_SIZE = 25
_MAX_PAGES = 20

#: OpenAIRE's own ``resulttype`` values, mapped onto :class:`Paper.work_type`.
RESULT_TYPES: dict[str, str] = {
    "publication": "publication",
    "dataset": "dataset",
    "other": "report",
    "software": "software",
    "unknown": "publication",
}

#: ``resulttype`` values a reader of this module actually asks for.  OpenAIRE
#: only has three of its own, so theses are reached through ``other`` and
#: reported honestly in the record's ``work_type``.
_RESULT_TYPE_CHOICES = ("publication", "dataset", "other")


def _first(value: Any) -> Any:
    """OpenAIRE wraps most fields in a single-key object; unwrap one level."""
    if isinstance(value, dict):
        for key in ("$", "content", "value"):
            if key in value:
                return _first(value[key])
        for nested in value.values():
            return _first(nested)
    if isinstance(value, list):
        return _first(value[0]) if value else None
    return value


def _all(value: Any) -> list[Any]:
    """Every element of an OpenAIRE list field, unwrapped."""
    if value is None:
        return []
    if isinstance(value, dict):
        for key in ("$", "content"):
            if key in value:
                value = value[key]
                break
        else:
            return [_first(value)]
    if not isinstance(value, list):
        return [_first(value)] if value else []
    return [_first(item) for item in value]


def _text(value: Any) -> str:
    return squash_text(_first(value))


def _raw_list(value: Any) -> list[Any]:
    """The elements of a list field *without* unwrapping each one.

    ``pid`` is a list of objects carrying both a scheme tag and a value, so
    unwrapping would discard the tag and leave nothing to read the value with.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        for key in ("$", "content"):
            if key in value:
                return _raw_list(value[key])
        return [value]
    return [value]


def parse_openaire_result(result: Any) -> Paper:
    """Turn one OpenAIRE ``result`` object into a :class:`Paper`.

    Pure — a dict in, a ``Paper`` out — so the parser is pinned by an offline
    fixture and never needs the network (D4).
    """
    paper = Paper()
    if not isinstance(result, dict):
        return paper

    entity = result.get("oaf:entity") or result
    record = entity.get("oaf:result") if isinstance(entity, dict) else None
    if not isinstance(record, dict):
        record = result

    titles = [squash_text(t) for t in _all(record.get("title")) if squash_text(t)]
    paper.title = titles[0] if titles else ""

    # A subtitle often arrives as a second title; keep it appended so the
    # record reads the way the title page does.
    if len(titles) > 1 and titles[1].lower() not in paper.title.lower():
        paper.title = f"{paper.title}: {titles[1]}"

    paper.abstract = _text(record.get("description"))

    authors: list[Author] = []
    for creator in _all(record.get("creator")):
        name = squash_text(creator) if not isinstance(creator, dict) else squash_text(
            _first(creator.get("name")) or _first(creator)
        )
        if name:
            # OpenAIRE lists a thesis supervisor or a project body the same way
            # it lists a person; a name with no comma and 3+ words is more
            # often an institution than a person, so it is flagged rather than
            # mangled into "Nations, U." by a bibliography renderer.
            authors.append(
                Author(
                    name=name,
                    organization=(" " not in name or name.count(" ") >= 2)
                    and "," not in name,
                )
            )
    paper.authors = authors

    accepted = _text(record.get("dateofacceptance")) or _text(record.get("dateofacceptance"))
    paper.published = accepted
    parsed_year = year_from_date(accepted)
    paper.year = parsed_year

    publisher = _text(record.get("publisher"))
    if publisher:
        paper.publisher = publisher

    subjects = [squash_text(s) for s in _all(record.get("subject")) if squash_text(s)]
    paper.keywords = subjects
    paper.categories = subjects

    result_type = _text(record.get("resulttype")).lower()
    paper.work_type = RESULT_TYPES.get(result_type, "publication") if result_type else ""

    # `pid` holds several id schemes; the DOI is the one that unifies records.
    doi = ""
    for pid in _raw_list(record.get("pid")):
        if not isinstance(pid, dict):
            continue
        scheme = squash_text(
            _text(pid.get("@classid")) or _text(pid.get("classid"))
        ).lower()
        if "doi" in scheme:
            doi = normalize_doi(_text(pid))
            if doi:
                break
    paper.doi = doi

    access = _text(record.get("bestaccessright")).lower()
    paper.oa_status = access
    paper.is_open_access = "open" in access

    url = _text(record.get("bestaccessright") and record.get("url")) or _text(
        record.get("url")
    )
    paper.url = url

    if doi:
        paper.ids["doi"] = doi
    paper.sources = ["openaire"]
    return paper


class OpenAIRESource(Source):
    """The OpenAIRE Graph API, unauthenticated."""

    name = "openaire"
    capabilities = Capabilities(
        search=True,
        get=True,
        references=False,
        citations=False,
        pdf=False,  # `url` points at a landing page, not a file.
    )

    def __init__(self, settings: Settings | None = None, *, timeout: float = 30.0, **_: object) -> None:
        super().__init__(settings)
        self._httpx = require_dependency("httpx", self.name)
        self._timeout = float(timeout)
        # The API is a shared European service; be a polite client.
        self._gate = RateGate(0.5)

    @property
    def default_interval(self) -> float:
        return 0.5

    # -- transport -------------------------------------------------------

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        self.wait()
        try:
            response = self._httpx.get(
                f"{_API}/publications",
                params={**params, "format": "json"},
                timeout=self._timeout,
                headers={"Accept": "application/json"},
            )
        except self._httpx.HTTPError as exc:
            raise SourceError(self.name, f"OpenAIRE request failed: {exc}", cause=exc) from exc

        if response.status_code == 404:
            raise NotFoundError(self.name, "OpenAIRE has no such record")
        if response.status_code == 429:
            retry = parse_retry_after(response.headers)
            from ..errors import RateLimitError

            raise RateLimitError(self.name, "OpenAIRE rate limit reached", retry_after=retry)
        if response.status_code >= 400:
            raise SourceError(
                self.name, f"OpenAIRE returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(self.name, "OpenAIRE returned a non-JSON body", cause=exc) from exc
        if not isinstance(payload, dict):
            raise SourceError(self.name, "OpenAIRE returned an unexpected payload")
        return payload

    @staticmethod
    def _results(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Pull ``response.results.result`` out, tolerating both nestings."""
        node: Any = payload
        for key in ("response", "results", "result"):
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                return []
        if isinstance(node, list):
            return [r for r in node if isinstance(r, dict)]
        return [node] if isinstance(node, dict) else []

    # -- operations ------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        filters: SearchFilters | None = None,
        result_type: str | None = None,
    ) -> list[Paper]:
        """Search aggregated repositories, theses and project deliverables.

        ``result_type`` is one of ``publication``/``dataset``/``other``; ``other``
        is where theses and reports land, which is why it is the useful one for
        grey literature.
        """
        self.require_support("search")
        want = self.settings.max_results if limit is None else max(0, int(limit))
        text = (query or "").strip()
        if want == 0 or not text:
            return []
        if result_type is not None and result_type not in _RESULT_TYPE_CHOICES:
            raise ConfigurationError(
                f"unknown result type {result_type!r}; expected one of "
                f"{', '.join(_RESULT_TYPE_CHOICES)}"
            )

        params: dict[str, Any] = {
            "keywords": text,
            "sortBy": "RANK",
        }
        if filters is not None and filters.field != "any":
            # The API scopes by title only; an abstract-scoped query is sent as
            # a keyword query and filtered locally instead.
            if filters.field == "title":
                params.pop("keywords")
                params["title"] = text
        if result_type:
            params["resulttype"] = result_type
        if filters is not None:
            if filters.year_from is not None:
                params["fromDateAccepted"] = f"{filters.year_from}-01-01"
            if filters.year_to is not None:
                params["toDateAccepted"] = f"{filters.year_to}-12-31"

        collected: list[Paper] = []
        page = 1
        while len(collected) < want and page <= _MAX_PAGES:
            batch = [
                parse_openaire_result(raw)
                for raw in self._results(self._get({**params, "size": _PAGE_SIZE, "page": page}))
            ]
            if not batch:
                break
            collected.extend(batch)
            if len(batch) < _PAGE_SIZE:
                break
            page += 1
        return apply_filters(collected, filters, query)[:want]

    def get(self, key: str) -> Paper:
        """Look a DOI up in OpenAIRE."""
        doi = wire_doi(key)
        if not doi:
            raise NotFoundError(
                self.name,
                f"OpenAIRE looks up DOIs only, got {key!r}",
            )
        for raw in self._results(self._get({"doi": doi, "size": 1})):
            paper = parse_openaire_result(raw)
            if paper.doi:
                return paper
        raise NotFoundError(self.name, f"no OpenAIRE record for {doi}")