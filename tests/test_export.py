"""Offline tests for :mod:`researcher_python.export` (tmp_path + StringIO, no network)."""

from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path

import pytest

import researcher_python.export as export
from researcher_python.errors import ExportError
from researcher_python.export import (
    SCHEMA_VERSION,
    from_json,
    from_jsonl,
    to_json,
    to_jsonl,
)
from researcher_python.models import Author, Paper


def sample_papers() -> list[Paper]:
    """Three records: fully populated, minimal (missing optionals), id-only."""
    full = Paper(
        sources=["openalex", "crossref"],
        ids={"openalex": "W1", "doi": "10.1/full"},
        doi="10.1/full",
        arxiv_id="1706.03762",
        url="https://doi.org/10.1/full",
        pdf_url="https://example.org/x.pdf",
        title="Ünïcödé title with emoji \u2728 and “curly quotes”",
        abstract="An abstract with <b>markup</b> in it.",
        authors=[
            Author(
                name="Ada Lovelace",
                author_id="A1",
                orcid="0000-0002-1825-0097",
                affiliations=["Analytical Engine Institute"],
            ),
            Author(name="Alan Turing"),
        ],
        year=2021,
        published="2021-04-15",
        venue="Journal of Testing Things",
        volume="12",
        issue="4",
        pages="101-112",
        publisher="American Physical Society (APS)",
        language="eng",
        work_type="journal-article",
        categories=["Physics"],
        keywords=["testing"],
        citation_count=1234,
        reference_count=56,
        is_open_access=True,
        oa_status="gold",
        license="https://example.org/licence",
    )
    minimal = Paper(title="Minimal record")
    third = Paper(
        sources=["pubmed"],
        ids={"pubmed": "22470460"},
        pmid="22470460",
        title="Third record",
        year=2012,
    )
    return [full, minimal, third]


def as_dicts(papers: list[Paper]) -> list[dict]:
    return [p.to_dict() for p in papers]


# -- round trips -----------------------------------------------------------


def test_json_round_trip_preserves_papers(tmp_path: Path) -> None:
    original = sample_papers()
    path = tmp_path / "papers.json"

    to_json(original, path)
    loaded = from_json(path)

    assert as_dicts(loaded) == as_dicts(original)
    assert loaded[0].authors[0].affiliations == ["Analytical Engine Institute"]
    assert loaded[0].title.startswith("Ünïcödé")
    assert loaded[1].title == "Minimal record"  # missing optionals survive


def test_jsonl_round_trip_preserves_papers(tmp_path: Path) -> None:
    original = sample_papers()
    path = tmp_path / "papers.jsonl"

    to_jsonl(original, path)
    loaded = from_jsonl(path)

    assert as_dicts(loaded) == as_dicts(original)


def test_jsonl_has_one_object_per_line_and_no_blank_line(tmp_path: Path) -> None:
    path = tmp_path / "papers.jsonl"
    to_jsonl(sample_papers(), path)

    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert not text.endswith("\n\n")  # no trailing blank line
    assert len(text.splitlines()) == 3
    for line in text.splitlines():
        assert isinstance(json.loads(line), dict)


def test_json_writes_utf8_without_escaping(tmp_path: Path) -> None:
    path = tmp_path / "papers.json"
    to_json(sample_papers(), path)

    text = path.read_text(encoding="utf-8")
    assert "Ünïcödé" in text  # ensure_ascii=False
    assert "\\u00dc" not in text


# -- envelope --------------------------------------------------------------


def test_envelope_fields(tmp_path: Path) -> None:
    path = tmp_path / "papers.json"
    to_json(sample_papers(), path, meta={"query": "aspirin", "sources": ["openalex"]})

    envelope = json.loads(path.read_text(encoding="utf-8"))

    assert envelope["schema_version"] == SCHEMA_VERSION == 1
    assert envelope["count"] == 3
    assert len(envelope["papers"]) == 3
    assert envelope["meta"] == {"query": "aspirin", "sources": ["openalex"]}
    assert list(envelope)[:4] == ["schema_version", "generated_at", "count", "meta"]

    timestamp = envelope["generated_at"]
    assert timestamp.endswith("Z")
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None  # really UTC


def test_meta_is_omitted_when_none(tmp_path: Path) -> None:
    path = tmp_path / "papers.json"
    to_json(sample_papers(), path)

    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert "meta" not in envelope
    assert list(envelope) == ["schema_version", "generated_at", "count", "papers"]


def test_non_mapping_meta_is_rejected() -> None:
    with pytest.raises(ExportError) as excinfo:
        to_json(
            sample_papers(),
            io.StringIO(),
            meta="not a mapping",  # type: ignore[arg-type]
        )
    assert "meta must be a mapping" in str(excinfo.value)


# -- path vs file object ---------------------------------------------------


def test_path_and_file_object_variants(tmp_path: Path) -> None:
    original = sample_papers()
    path = tmp_path / "papers.json"

    # path -> write, file object -> read
    to_json(original, path)
    with open(path, encoding="utf-8") as handle:
        assert as_dicts(from_json(handle)) == as_dicts(original)

    # file object -> write, path -> read
    buffer = io.StringIO()
    to_json(original, buffer)
    assert not buffer.closed  # a caller's stream is never closed for them
    buffer.seek(0)
    assert as_dicts(from_json(buffer)) == as_dicts(original)

    # and the JSONL equivalents
    buffer = io.StringIO()
    to_jsonl(original, buffer)
    buffer.seek(0)
    assert as_dicts(from_jsonl(buffer)) == as_dicts(original)

    jsonl_path = tmp_path / "papers.jsonl"
    to_jsonl(original, jsonl_path)
    with open(jsonl_path, encoding="utf-8") as handle:
        assert as_dicts(from_jsonl(handle)) == as_dicts(original)


def test_parent_directories_are_created(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "nested" / "dir" / "papers.json"
    to_json(sample_papers(), path)

    assert path.exists()
    assert len(from_json(path)) == 3


def test_unsupported_dest_and_src_types_are_rejected() -> None:
    with pytest.raises(ExportError) as excinfo:
        to_json(sample_papers(), 42)  # type: ignore[arg-type]
    assert "dest must be a path or a text file object" in str(excinfo.value)

    with pytest.raises(ExportError) as excinfo:
        from_json(42)  # type: ignore[arg-type]
    assert "src must be a path or a text file object" in str(excinfo.value)


# -- atomic writes ---------------------------------------------------------


def test_failed_write_leaves_no_temp_file_and_no_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "papers.json"

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(export.json, "dump", boom)

    with pytest.raises(RuntimeError):
        to_json(sample_papers(), path)

    assert not path.exists()  # never half-written
    assert list(tmp_path.glob("*.tmp")) == []


def test_replaced_destination_failure_cleans_up_the_temp_file(
    tmp_path: Path,
) -> None:
    # os.replace() onto an existing directory fails with an OSError.
    target = tmp_path / "a-directory"
    target.mkdir()

    with pytest.raises(ExportError) as excinfo:
        to_json(sample_papers(), target)

    assert isinstance(excinfo.value.__cause__, OSError)
    assert list(tmp_path.glob("*.tmp")) == []
    assert target.is_dir()  # untouched


# -- read errors -----------------------------------------------------------


def test_missing_file_is_an_export_error(tmp_path: Path) -> None:
    with pytest.raises(ExportError) as excinfo:
        from_json(tmp_path / "nope.json")
    assert isinstance(excinfo.value.__cause__, FileNotFoundError)


def test_invalid_json_is_an_export_error() -> None:
    with pytest.raises(ExportError) as excinfo:
        from_json(io.StringIO("{this is not json"))
    assert "invalid JSON" in str(excinfo.value)


def test_wrong_top_level_shape_is_rejected() -> None:
    with pytest.raises(ExportError) as excinfo:
        from_json(io.StringIO("[1, 2, 3]"))
    assert "expected a JSON object at the top level" in str(excinfo.value)


def test_missing_papers_array_is_rejected() -> None:
    with pytest.raises(ExportError) as excinfo:
        from_json(io.StringIO('{"schema_version": 1, "count": 0}'))
    assert "missing the 'papers' array" in str(excinfo.value)


def test_papers_must_be_an_array() -> None:
    with pytest.raises(ExportError) as excinfo:
        from_json(io.StringIO('{"schema_version": 1, "papers": {"a": 1}}'))
    assert "'papers' must be an array" in str(excinfo.value)


def test_rejected_paper_entry_names_its_index() -> None:
    good = sample_papers()[2].to_dict()
    payload = json.dumps(
        {"schema_version": SCHEMA_VERSION, "count": 2, "papers": [good, "not an object"]}
    )

    with pytest.raises(ExportError) as excinfo:
        from_json(io.StringIO(payload))

    assert "index 1" in str(excinfo.value)
    assert "not a JSON object" in str(excinfo.value)


# -- schema_version --------------------------------------------------------


def test_missing_schema_version_is_rejected() -> None:
    with pytest.raises(ExportError) as excinfo:
        from_json(io.StringIO('{"papers": []}'))
    assert "missing 'schema_version'" in str(excinfo.value)


def test_future_schema_version_is_rejected() -> None:
    with pytest.raises(ExportError) as excinfo:
        from_json(io.StringIO('{"schema_version": 99, "papers": []}'))
    message = str(excinfo.value)
    assert "99" in message
    assert "newer than the supported" in message


def test_lower_schema_version_is_accepted() -> None:
    # Version 1 is the only one that has ever existed; a lower one still loads.
    loaded = from_json(
        io.StringIO('{"schema_version": 0, "count": 1, "papers": [{"title": "old"}]}')
    )
    assert [p.title for p in loaded] == ["old"]


def test_non_integer_schema_version_is_rejected() -> None:
    with pytest.raises(ExportError) as excinfo:
        from_json(io.StringIO('{"schema_version": "one", "papers": []}'))
    assert "'schema_version' must be an integer" in str(excinfo.value)


# -- JSONL line errors -----------------------------------------------------


def test_jsonl_invalid_line_names_the_line_number() -> None:
    handle = io.StringIO('{"title": "ok"}\nthis is not json\n')

    with pytest.raises(ExportError) as excinfo:
        from_jsonl(handle)

    assert "line 2" in str(excinfo.value)
    assert "invalid JSON" in str(excinfo.value)


def test_jsonl_non_object_line_names_the_line_number() -> None:
    with pytest.raises(ExportError) as excinfo:
        from_jsonl(io.StringIO('{"title": "ok"}\n42\n'))
    assert "line 2" in str(excinfo.value)
    assert "not a JSON object" in str(excinfo.value)


def test_jsonl_skips_blank_lines() -> None:
    handle = io.StringIO('\n{"title": "a"}\n\n{"title": "b"}\n')
    assert [p.title for p in from_jsonl(handle)] == ["a", "b"]
