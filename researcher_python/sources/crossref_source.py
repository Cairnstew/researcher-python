"""Crossref adapter.

Crossref is the DOI authority: ``cr.works()`` resolves a DOI, ``cr.works(query=)``
free-texts against title/author/container, and every work object carries its
own ``reference`` array — which is why :attr:`CrossrefSource.capabilities`
advertises ``references=True`` with ``citations=False``.  Crossref exposes the
*incoming* side of the graph for free and the outgoing side only as an
``is-referenced-by-count``, so citations are refused up front (see
:class:`~researcher_python.errors.UnsupportedOperation`) rather than faked from
a number.

Three things drive the implementation:

* **DOI case (D3).**  DOIs are case-insensitive, so
  :func:`~researcher_python.ids.normalize_doi` lowercases the key stored on
  the :class:`~researcher_python.models.Paper`.  The string actually sent to
  the API comes from :func:`wire_doi`, which strips the resolver prefix but
  keeps the caller's casing — we do not "correct" mixed-case DOIs on the wire.
* **Errors surface as ``httpx.HTTPStatusError``.**  ``habanero`` is built on
  httpx and (with ``warn=False``) lets ``raise_for_status()`` through on both
  the id and the query path, so :func:`map_http_error` maps that one type onto
  the package hierarchy: 404 -> ``NotFoundError``, 429 -> ``RateLimitError``
  (carrying ``Retry-After``), 5xx -> ``SourceError`` (D8).
* **Rate limits are aggressive.**  Crossref returns 429 even to an identified
  client on a shared IP, so :attr:`CrossrefSource.default_interval` is a
  conservative 0.5s and ``Settings.email`` is passed as ``mailto`` to join the
  polite pool.

JATS markup in ``abstract`` is stripped by :func:`strip_jats`: a regex over
tags rather than an XML parse, because Crossref abstracts routinely embed
MathML with undeclared ``mml:`` prefixes that a strict parser rejects.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from typing import Any

from ..config import Settings
from ..errors import (
    NotFoundError,
    RateLimitError,
    ResearcherError,
    SourceError,
)
from ..ids import (
    is_arxiv_id,
    is_doi,
    is_pmid,
    normalize_arxiv_id,
    normalize_doi,
    normalize_orcid,
    wire_doi,
)
from ..models import Author, Paper
from .base import (
    Capabilities,
    SearchFilters,
    Source,
    apply_filters,
    parse_retry_after,
    require_dependency,
    squash_text,
)

__all__ = [
    "CrossrefSource",
    "map_http_error",
    "parse_crossref_reference",
    "parse_crossref_work",
    "strip_jats",
    "wire_doi",
]

#: Crossref's documented ceiling for ``rows`` / ``offset`` combined.
_PAGE_SIZE = 100
_MAX_ROWS = 1000
#: ``10.48550/arXiv.<id>`` is the DataCite DOI form of an arXiv preprint.
_ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)
#: Anything shaped ``<...>`` — JATS elements, never literal prose.
_TAG = re.compile(r"<[^>]*>")
#: Block elements become a space; inline ones are deleted.  Replacing *every*
#: tag with a space would render ``emphasis</jats:italic>.`` as ``emphasis .``,
#: while deleting *every* tag would glue ``</jats:p><jats:p>`` into one word.
_BLOCK_TAG = re.compile(
    r"</?\s*(?:jats:)?(?:p|sec|title|list-item|caption)\b[^>]*>", re.IGNORECASE
)
#: Date keys, most specific first; every one uses ``{"date-parts": [[...]]}``.
_DATE_KEYS = ("published", "published-print", "published-online", "issued", "created")


# ---------------------------------------------------------------------- #
# Pure helpers (D4: no network)
# ---------------------------------------------------------------------- #


def strip_jats(value: Any) -> str:
    """Remove JATS/XML markup from a Crossref ``abstract`` and collapse spaces.

    Tags are replaced rather than deleted so adjacent runs of prose do not glue
    together, then entities such as ``&#x2010;`` are decoded and whitespace is
    normalised by :func:`squash_text`.  Unescaping happens *after* tag removal
    so literal ``&lt;foo&gt;`` in the source survives as visible text.
    """
    if value is None:
        return ""
    text = str(value)
    text = _BLOCK_TAG.sub(" ", text)
    text = _TAG.sub("", text)
    return squash_text(html.unescape(text))


def _first(value: Any) -> str:
    """Crossref wraps most string fields in a one-element list."""
    if isinstance(value, list):
        return squash_text(value[0]) if value else ""
    return squash_text(value)


def _int(value: Any) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _date_parts(value: Any) -> list[int]:
    """``{"date-parts": [[2016, 2, 11]]}`` -> ``[2016, 2, 11]`` (or ``[]``)."""
    if not isinstance(value, Mapping):
        return []
    parts = value.get("date-parts")
    if not isinstance(parts, list) or not parts:
        return []
    first = parts[0]
    if not isinstance(first, (list, tuple)):
        return []
    out: list[int] = []
    for item in first:
        parsed = _int(item)
        if parsed is not None:
            out.append(parsed)
    return out


def _iso(parts: list[int]) -> str:
    if not parts:
        return ""
    if len(parts) >= 3:
        return f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"
    if len(parts) == 2:
        return f"{parts[0]:04d}-{parts[1]:02d}"
    return f"{parts[0]:04d}"


def _published(work: Mapping[str, Any]) -> tuple[str, int | None]:
    for key in _DATE_KEYS:
        parts = _date_parts(work.get(key))
        if parts:
            return _iso(parts), parts[0]
    return "", None


def _authors(work: Mapping[str, Any]) -> list[Author]:
    raw = work.get("author")
    if not isinstance(raw, list):
        return []
    authors: list[Author] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        name = squash_text(entry.get("name"))
        if not name:
            name = squash_text(f"{squash_text(entry.get('given'))} {squash_text(entry.get('family'))}")
        if not name:
            continue
        affiliations: list[str] = []
        for aff in entry.get("affiliation") or ():
            text = squash_text(aff.get("name") if isinstance(aff, Mapping) else aff)
            if text:
                affiliations.append(text)
        authors.append(
            Author(
                name=name,
                orcid=normalize_orcid(entry.get("ORCID")),
                affiliations=affiliations,
            )
        )
    return authors


def parse_crossref_work(message: Mapping[str, Any]) -> Paper:
    """Turn one Crossref work ``message`` into a :class:`~researcher_python.models.Paper`.

    Pure: ``message`` is the decoded JSON body Crossref already handed us, so
    fixtures exercise every branch without a socket.
    """
    work = dict(message)
    doi = normalize_doi(work.get("DOI"))
    published, year = _published(work)

    license_url = ""
    for lic in work.get("license") or ():
        if not isinstance(lic, Mapping):
            continue
        url = lic.get("URL")
        # Only a string is a URL; squash_text would happily render 42 as "42".
        if isinstance(url, str) and url.strip():
            license_url = squash_text(url)
            break

    categories = [squash_text(s) for s in (work.get("subject") or ()) if squash_text(s)]

    arxiv_id = ""
    match = _ARXIV_DOI.match(doi) if doi else None
    if match:
        arxiv_id = normalize_arxiv_id(match.group(1))

    paper = Paper(
        sources=["crossref"],
        ids={"crossref": doi} if doi else {},
        doi=doi,
        arxiv_id=arxiv_id,
        url=squash_text(work.get("URL")),
        # ``pdf_url`` stays empty: Crossref ``link`` entries are text-mining
        # URLs behind the publisher's authentication, not open PDFs.
        pdf_url="",
        title=_first(work.get("title")),
        abstract=strip_jats(work.get("abstract")),
        authors=_authors(work),
        year=year,
        published=published,
        venue=_first(work.get("container-title")),
        volume=squash_text(work.get("volume")),
        issue=squash_text(work.get("issue")),
        pages=squash_text(work.get("page")) or squash_text(work.get("article-number")),
        publisher=squash_text(work.get("publisher")),
        language=squash_text(work.get("language")),
        work_type=squash_text(work.get("type")),
        categories=categories,
        citation_count=_int(work.get("is-referenced-by-count")),
        reference_count=_int(work.get("reference-count")),
        is_open_access=False,
        oa_status="",
        license=license_url,
    )
    if doi:
        paper.ids.setdefault("doi", doi)
    return paper


def parse_crossref_reference(entry: Mapping[str, Any]) -> Paper | None:
    """One element of a work's ``reference`` array, or ``None`` without a DOI.

    Only DOI-bearing entries are returned (per the adapter's contract): the
    rest are unstructured strings with nothing to resolve, and returning a
    stub with an empty id would poison :func:`researcher_python.models.merge`.
    """
    if not isinstance(entry, Mapping):
        return None
    ref = dict(entry)
    doi = normalize_doi(ref.get("DOI"))
    if not doi:
        return None

    author = squash_text(ref.get("author"))
    paper = Paper(
        sources=["crossref"],
        ids={"crossref": doi},
        doi=doi,
        title=squash_text(ref.get("article-title")) or squash_text(ref.get("unstructured")),
        venue=squash_text(ref.get("journal-title")) or squash_text(ref.get("volume-title")),
        volume=squash_text(ref.get("volume")),
        pages=squash_text(ref.get("first-page")),
        publisher=squash_text(ref.get("publisher")),
        authors=[Author(name=author)] if author else [],
        year=_int(ref.get("year")),
        work_type="reference",
    )
    paper.ids.setdefault("doi", doi)
    return paper


def map_http_error(source: str, exc: Any) -> ResearcherError:
    """Map one Crossref HTTP failure onto the package's error hierarchy.

    ``exc`` is an ``httpx.HTTPStatusError`` in production; the mapping only
    reads ``.response.status_code``, ``.response.headers`` and
    ``.response.text``, so tests can pass any object with those attributes.
    """
    response = getattr(exc, "response", None)
    status = _int(getattr(response, "status_code", 0)) or 0
    detail = _response_text(response)
    return _map_status(
        source, status, detail, parse_retry_after(getattr(response, "headers", None)), exc
    )


def _map_status(
    source: str,
    status: int,
    detail: str,
    retry_after: float | None,
    cause: BaseException | None = None,
) -> ResearcherError:
    if status == 404:
        return NotFoundError(source, detail or "Crossref has no such record")
    if status == 429:
        message = "Crossref rate limit exceeded"
        if retry_after is not None:
            message += f" (Retry-After: {retry_after:g}s)"
        return RateLimitError(
            source,
            f"{message}; set RESEARCHER_EMAIL to join Crossref's polite pool",
            retry_after=retry_after,
            cause=cause,
        )
    # D8: a 5xx is a transport/API failure, not an adapter problem —
    # SourceUnavailableError is reserved for unconstructable adapters (D5).
    return SourceError(source, detail or f"Crossref returned HTTP {status}", cause=cause)


def _response_text(response: Any) -> str:
    text = getattr(response, "text", "")
    return squash_text(text) if isinstance(text, str) else ""




# ---------------------------------------------------------------------- #
# Adapter
# ---------------------------------------------------------------------- #


class CrossrefSource(Source):
    """The Crossref REST API (``api.crossref.org``), driven through habanero."""

    name = "crossref"
    capabilities = Capabilities(
        search=True,
        get=True,
        references=True,
        citations=False,  # Crossref exposes only an inbound count.
        pdf=False,  # ``link`` targets are text-mining, not open-access.
    )

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        timeout: float = 30.0,
        **_: object,
    ) -> None:
        super().__init__(settings)
        self._httpx = require_dependency("httpx", self.name)
        habanero = require_dependency("habanero", self.name)
        self._exceptions = require_dependency("habanero.exceptions", self.name)
        options: dict[str, Any] = {
            "ua_string": "researcher-python/0.1",
            "timeout": float(timeout),
        }
        if self.settings.email:
            options["mailto"] = self.settings.email
        self._cr = habanero.Crossref(**options)

    @property
    def default_interval(self) -> float:
        # Crossref 429s quickly on shared IPs even with mailto; 0.5s is the
        # spacing at which probes stayed out of trouble.
        return 0.5

    # -- transport -------------------------------------------------------

    def _call(self, **kwargs: Any) -> Any:
        """One ``cr.works(...)`` with Crossref's failure modes mapped.

        ``warn=False`` is load-bearing: habanero prints instead of raising
        when ``should_warn`` is true, so without it a 404 would arrive as a
        warning on stderr rather than an exception we can translate.
        """
        self.wait()
        kwargs.setdefault("warn", False)
        try:
            return self._cr.works(**kwargs)
        except self._httpx.HTTPStatusError as exc:
            raise map_http_error(self.name, exc) from exc
        except self._exceptions.RequestError as exc:
            # Only reachable for a 2xx whose body is not JSON.
            raise SourceError(
                self.name, f"Crossref returned an unusable response: {exc}", cause=exc
            ) from exc
        except self._exceptions.Error as exc:
            raise SourceError(self.name, f"Crossref request failed: {exc}", cause=exc) from exc
        except RuntimeError as exc:
            # habanero wraps transport-level httpx errors in RuntimeError.
            raise SourceError(self.name, f"Crossref request failed: {exc}", cause=exc) from exc

    def _payload(self, **kwargs: Any) -> dict[str, Any]:
        """Unwrap ``{"message": {...}}`` from whatever shape Crossref returned."""
        result = self._call(**kwargs)
        message = result.get("message") if isinstance(result, Mapping) else None
        if not isinstance(message, Mapping):
            raise SourceError(self.name, "Crossref returned an unexpected payload")
        return dict(message)

    # -- identity --------------------------------------------------------

    @staticmethod
    def _hint(key: str) -> str:
        if is_arxiv_id(key):
            return f"Crossref looks up DOIs only; use the 'arxiv' source (got {key!r})"
        if is_pmid(key):
            return f"Crossref looks up DOIs only; use the 'pubmed' source (got {key!r})"
        if is_doi(key):  # pragma: no cover - wire_doi would have accepted it
            return f"could not read DOI {key!r}"
        return f"Crossref looks up DOIs only, got {key!r}"

    def _require_work(self, paper: Paper) -> dict[str, Any]:
        """Re-fetch the Crossref record for ``paper`` by DOI."""
        doi = wire_doi(paper.doi) or wire_doi(paper.id_for("crossref"))
        if not doi:
            raise NotFoundError(
                self.name, f"record has no DOI to resolve against Crossref: {paper.title!r}"
            )
        return self._payload(ids=doi)

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

        extra: dict[str, Any] = {}
        if filters is not None and filters.field != "any":
            # habanero only forwards kwargs whose name starts with ``query_``
            # (``filter_dict``), then renames ``query_`` -> ``query.`` and
            # ``_`` -> ``-``.  Passing ``query.title`` directly is silently
            # dropped, which turns a field-scoped search into an unscoped one.
            extra["query_title" if filters.field == "title" else "query_abstract"] = text
        else:
            extra["query"] = text
        if filters is not None:
            filter_parts: dict[str, Any] = {}
            if filters.year_from is not None:
                filter_parts["from-pub-date"] = f"{filters.year_from}-01-01"
            if filters.year_to is not None:
                filter_parts["until-pub-date"] = f"{filters.year_to}-12-31"
            if filter_parts:
                # habanero's ``works(filter=...)`` expects a dict and renders it
                # to Crossref's ``name:value`` syntax itself; a string raises
                # "'str' object has no attribute 'items'".
                extra["filter"] = filter_parts

        collected: list[Paper] = []
        offset = 0
        while len(collected) < want and offset < _MAX_ROWS:
            rows = min(_PAGE_SIZE, want - len(collected), _MAX_ROWS - offset)
            if rows <= 0:
                break
            message = self._payload(
                **extra, limit=rows, offset=offset, sort="score", order="desc"
            )
            raw = message.get("items")
            items = [dict(i) for i in (raw or ()) if isinstance(i, Mapping)]
            if not items:
                break
            collected.extend(parse_crossref_work(item) for item in items)
            offset += len(items)
            if len(items) < rows:
                break
        return apply_filters(collected, filters, query)[:want]

    def get(self, key: str) -> Paper:
        self.require_support("get")
        doi = wire_doi(key)
        if not doi:
            raise NotFoundError(self.name, self._hint(key))
        return parse_crossref_work(self._payload(ids=doi))

    def references(self, paper: Paper) -> list[Paper]:
        self.require_support("references")
        work = self._require_work(paper)
        out: list[Paper] = []
        for entry in work.get("reference") or ():
            stub = parse_crossref_reference(entry)
            if stub is not None:
                out.append(stub)
        return out

    # ``citations`` is intentionally not overridden: the base class raises
    # UnsupportedOperation because capabilities.citations is False.
