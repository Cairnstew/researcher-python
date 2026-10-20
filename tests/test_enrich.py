"""OpenAlex missing-count/abstract backfill."""

from __future__ import annotations

from researcher_python.enrich import EnrichmentReport, citation_coverage, enrich_papers
from researcher_python.models import Paper


class FakeOpenAlex:
    capabilities = type("Caps", (), {"supports": lambda self, op: op == "enrich"})()

    def __init__(self, works: list[Paper]) -> None:
        self.works = works
        self.seen: list[str] = []

    def supports(self, operation: str) -> bool:
        return operation == "enrich"

    def works_by_dois(self, dois):
        self.seen.extend(dois)
        return list(self.works)


def _paper(doi: str, **kwargs) -> Paper:
    return Paper(doi=doi, title=doi, **kwargs)


def test_fills_only_missing_values_and_records_provenance() -> None:
    target = _paper("10.1/a")
    existing = _paper("10.1/b", citation_count=99, citation_count_source="crossref")
    enriched = [
        Paper(doi="10.1/a", citation_count=10, abstract="abstract", work_type="article"),
        Paper(doi="10.1/b", citation_count=1, abstract="other", work_type="review"),
    ]
    source = FakeOpenAlex(enriched)

    report = enrich_papers([target, existing], source)

    assert report.ok is True
    assert report.requested == 2
    assert report.matched == 2
    assert target.citation_count == 10
    assert target.citation_count_source == "openalex"
    assert target.abstract == "abstract"
    assert target.work_type == "article"
    assert existing.citation_count == 99  # never overwritten
    assert existing.citation_count_source == "crossref"
    assert existing.abstract == "other"
    assert report.citation_filled == 1
    assert report.abstract_filled == 2


def test_arxiv_only_record_is_addressed_by_arxiv_doi() -> None:
    target = Paper(arxiv_id="2001.00001", title="preprint")
    source = FakeOpenAlex(
        [Paper(doi="10.48550/arxiv.2001.00001", citation_count=7)]
    )

    enrich_papers([target], source)

    assert source.seen == ["10.48550/arxiv.2001.00001"]
    assert target.citation_count == 7


def test_unmatched_records_are_counted_missing_not_altered() -> None:
    target = _paper("10.1/unknown")
    report = enrich_papers([target], FakeOpenAlex([]))

    assert report.matched == 0
    assert report.missing == 1
    assert target.citation_count is None


def test_no_source_is_a_skip_with_the_original_coverage() -> None:
    target = _paper("10.1/a", citation_count=1)
    report = enrich_papers([target], None)

    assert report.ok is False
    assert report.skipped_reason
    assert report.coverage_before == report.coverage_after == 1.0


def test_a_failing_source_never_fails_the_search() -> None:
    class Boom(FakeOpenAlex):
        def works_by_dois(self, dois):
            from researcher_python.errors import SourceError

            raise SourceError("openalex", "503")

    report = enrich_papers([_paper("10.1/a")], Boom([]))

    assert report.ok is False
    assert report.errors and "503" in report.errors[0]


def test_coverage_math_and_record_count() -> None:
    papers = [_paper("10.1/a", citation_count=2), _paper("10.1/b")]
    assert citation_coverage(papers) == (0.5, 0.0)
    report = EnrichmentReport(coverage_before=0.5, coverage_after=1.0)
    assert report.to_dict()["coverage_after"] == 1.0
