"""arXiv adapter.

Query handling is deliberately *not* rewritten.  The arXiv API's
``search_query`` is its own fielded language (``au:``, ``ti:``, ``cat:``,
boolean ``AND``/``OR``/``ANDNOT``), and the endpoint accepts bare free text
verbatim — ``transformer`` and ``all:"deep learning"`` both return results.
Prefixing a user's terms with ``all:`` would break every query that already
uses field prefixes, so this adapter passes the string straight through and
lets arXiv do the parsing.

arXiv rate-limits by citation policy (one request every 3 seconds); the
``arxiv.Client`` already enforces that between pages, so :attr:`ArxivSource.default_interval`
stays at ``0`` and the rate gate only serialises calls made concurrently from
this process.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..errors import NotFoundError, SourceError
from ..ids import is_arxiv_id, normalize_arxiv_id, normalize_doi
from ..models import Author, Paper
from .base import Capabilities, Source, require_dependency, squash_text

__all__ = ["ArxivSource", "parse_arxiv_result"]

#: arXiv caps ``max_results`` per page and its client pages internally.
_DEFAULT_PAGE_SIZE = 100


def parse_arxiv_result(result: Any) -> Paper:
    """Turn one ``arxiv.Result`` into a :class:`~researcher_python.models.Paper`.

    Pure and offline: every field is read off the object arXiv already parsed,
    so tests can pass a stand-in with the same attributes.
    """
    entry_id = squash_text(getattr(result, "entry_id", ""))

    url = ""
    pdf_url = squash_text(getattr(result, "pdf_url", ""))
    for link in getattr(result, "links", None) or ():
        href = squash_text(getattr(link, "href", ""))
        if not href:
            continue
        if squash_text(getattr(link, "rel", "")) == "alternate":
            url = href
        elif not pdf_url and squash_text(getattr(link, "title", "")) == "pdf":
            pdf_url = href
    if not url:
        url = entry_id

    published_dt = getattr(result, "published", None)
    published = ""
    year: int | None = None
    if published_dt is not None:
        try:
            published = published_dt.date().isoformat()
            year = int(published_dt.year)
        except (AttributeError, TypeError, ValueError):  # pragma: no cover
            published, year = squash_text(published_dt), None

    raw_doi = normalize_doi(getattr(result, "doi", ""))
    arxiv_id = normalize_arxiv_id(getattr(result, "entry_id", ""), keep_version=False)

    categories = [squash_text(c) for c in (getattr(result, "categories", None) or ())]
    categories = [c for c in categories if c]
    primary = squash_text(getattr(result, "primary_category", ""))
    if primary and primary in categories:
        categories.remove(primary)
        categories.insert(0, primary)

    authors = [
        Author(name=squash_text(getattr(a, "name", "")))
        for a in (getattr(result, "authors", None) or ())
        if squash_text(getattr(a, "name", ""))
    ]

    paper = Paper(
        sources=["arxiv"],
        ids={"arxiv": arxiv_id} if arxiv_id else {},
        doi=raw_doi,
        arxiv_id=arxiv_id,
        url=url,
        pdf_url=pdf_url,
        title=squash_text(getattr(result, "title", "")),
        abstract=squash_text(getattr(result, "summary", "")),
        authors=authors,
        year=year,
        published=published,
        venue=squash_text(getattr(result, "journal_ref", "")),
        categories=categories,
        work_type="preprint",
        is_open_access=True,
    )
    if raw_doi:
        paper.ids.setdefault("doi", raw_doi)
    return paper


class ArxivSource(Source):
    """The arXiv API (``export.arxiv.org/api/query``)."""

    name = "arxiv"
    capabilities = Capabilities(search=True, get=True, references=False, citations=False, pdf=True)

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        page_size: int = _DEFAULT_PAGE_SIZE,
        delay_seconds: float = 3.0,
        **_: object,
    ) -> None:
        super().__init__(settings)
        self._arxiv = require_dependency("arxiv", self.name)
        self._page_size = max(1, min(int(page_size), _DEFAULT_PAGE_SIZE))
        # arXiv's stated policy is 3s between requests; the client honours it.
        self._client = self._arxiv.Client(
            page_size=self._page_size,
            delay_seconds=float(delay_seconds),
            num_retries=3,
        )

    @property
    def default_interval(self) -> float:
        return 0.0

    # -- helpers ---------------------------------------------------------

    def _limit(self, limit: int | None) -> int:
        want = self.settings.max_results if limit is None else int(limit)
        return max(0, want)

    def _search(self, **kwargs: Any) -> list[Any]:
        """Run one ``Search`` and return its raw ``Result`` objects."""
        self.wait()
        search = self._arxiv.Search(
            sort_by=self._arxiv.SortCriterion.Relevance,
            sort_order=self._arxiv.SortOrder.Descending,
            **kwargs,
        )
        try:
            return list(self._client.results(search))
        except self._arxiv.ArxivError as exc:
            raise SourceError(self.name, f"arXiv request failed: {exc}", cause=exc) from exc

    # -- operations ------------------------------------------------------

    def search(self, query: str, *, limit: int | None = None) -> list[Paper]:
        want = self._limit(limit)
        if want <= 0 or not (query or "").strip():
            return []
        results = self._search(query=query.strip(), max_results=want)
        return [parse_arxiv_result(r) for r in results[:want]]

    def get(self, key: str) -> Paper:
        self.require_support("get")
        if not is_arxiv_id(key):
            raise NotFoundError(
                self.name,
                f"arXiv looks up arXiv ids only, got {key!r} "
                "(try the 'crossref' source for a DOI)",
            )
        bare = normalize_arxiv_id(key)
        results = self._search(id_list=[bare], max_results=1)
        if not results:
            raise NotFoundError(self.name, f"no arXiv record for {bare!r}")
        return parse_arxiv_result(results[0])
