"""ACM Digital Library, as a prefix-scoped view of Crossref.

Every ACM DL article, proceedings paper and book chapter carries a DOI under
the ``10.1145`` prefix, and Crossref indexes all of them.  dl.acm.org itself
offers no open API — its search results are HTML, and scraping them is both
fragile and outside the site's terms — so this adapter reuses Crossref's
registered transport and adds the one thing the DL is actually for: a search
*scoped to ACM*, ranked by Crossref's relevance score within that scope.

That is a real capability, not a shim.  ``sources`` reports ``acm`` as its own
source, ``--source acm`` searches only the Digital Library, and a citation query
for an ACM paper behaves exactly as it does for any other Crossref record.

Not supported: reference/citation edges beyond what Crossref exposes (inbound
count only), and ACM's own editorial filters (ACM Author-izer, OpenTOC).
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..errors import NotFoundError
from ..models import Paper
from .crossref_source import CrossrefSource, parse_crossref_work
from .base import Capabilities, SearchFilters, apply_filters

__all__ = ["ACMSource", "ACM_DOI_PREFIX"]

#: The DOI prefix Crossref reserves for the ACM Digital Library.
ACM_DOI_PREFIX = "10.1145"

_PAGE_SIZE = 100
_MAX_ROWS = 1000


class ACMSource(CrossrefSource):
    """ACM Digital Library search/get, served by Crossref's API.

    Inherits the whole Crossref adapter — polite-pool configuration, error
    mapping, record parsing — and narrows every query with
    ``filter=prefix:10.1145``.  Only ``name`` and ``search`` differ.
    """

    name = "acm"
    capabilities = Capabilities(
        search=True,
        get=True,
        references=True,
        citations=False,
        pdf=False,
    )

    def __init__(self, settings: Settings | None = None, **kwargs: Any) -> None:
        super().__init__(settings, **kwargs)

    def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        filters: SearchFilters | None = None,
    ) -> list[Paper]:
        """Search the Digital Library.

        A record that is not ACM's is filtered out locally as well as
        server-side, so a Crossref-side quirk cannot leak a non-ACM hit into a
        result set that claims to be ACM-only.
        """
        self.require_support("search")
        want = self.settings.max_results if limit is None else max(0, int(limit))
        text = (query or "").strip()
        if want == 0 or not text:
            return []

        extra: dict[str, Any] = {}
        if filters is not None and filters.field != "any":
            # See CrossrefSource.search: habanero wants ``query_title`` /
            # ``query_abstract``, not the dotted Crossref parameter names.
            extra["query_title" if filters.field == "title" else "query_abstract"] = text
        else:
            extra["query"] = text
        # habanero's ``works(filter=...)`` expects a dict; it renders every
        # entry to Crossref's ``name:value`` syntax.  The prefix filter is
        # always present, and the year window is merged into the same dict
        # rather than replacing it (a replacement would drop the ACM scope).
        filter_parts: dict[str, Any] = {"prefix": ACM_DOI_PREFIX}
        if filters is not None:
            if filters.year_from is not None:
                filter_parts["from-pub-date"] = f"{filters.year_from}-01-01"
            if filters.year_to is not None:
                filter_parts["until-pub-date"] = f"{filters.year_to}-12-31"
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
            items = [
                dict(i)
                for i in (message.get("items") or ())
                if isinstance(i, dict)
            ]
            if not items:
                break
            for item in items:
                paper = parse_crossref_work(item)
                # The record is ACM's, not Crossref's: the source tag is what
                # `Paper.sources` and the merged output report.
                paper.sources = [self.name]
                paper.ids[self.name] = paper.doi
                collected.append(paper)
            offset += len(items)
            if len(items) < rows:
                break

        acm_only = [p for p in collected if p.doi.startswith(ACM_DOI_PREFIX)]
        return apply_filters(acm_only, filters, query)[:want]

    def get(self, key: str) -> Paper:
        """Fetch one ACM record, refusing a DOI outside the ACM prefix."""
        if not key.lower().startswith(ACM_DOI_PREFIX):
            raise NotFoundError(
                self.name,
                f"the ACM Digital Library holds DOIs under {ACM_DOI_PREFIX}, got {key!r} "
                "(use the 'crossref' source for any other DOI)",
            )
        return super().get(key)