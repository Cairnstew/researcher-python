"""Offline tests for web record bibliography rendering (``web_sources/format.py``).

Pure functions pinned against hand-built records: no network, no clock.  The
expected dates are UTC conversions of the fixture epoch seconds (verified by
hand: 1305010204 -> 2011-05-10, 1589039100 -> 2020-05-09).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from researcher_python.errors import ExportError
from researcher_python.web_sources.base import WebResult
from researcher_python.web_sources.format import (
    web_bibtex_entry,
    web_bibtex_key,
    web_csl_id,
    web_csl_item,
    web_ris_entry,
    render_web_bibtex,
    render_web_csl_json,
    render_web_ris,
    web_to_bibtex,
    web_to_csl_json,
    web_to_ris,
)


def question() -> WebResult:
    """The familiar fixture question (2011, Stack Overflow, tags)."""
    return WebResult(
        source="stackoverflow",
        id="5946236",
        title="How to merge dicts, collecting values from matching keys?",
        url="https://stackoverflow.com/questions/5946236/how-to-merge-dicts",
        score=165,
        created=1305010204,
        author="Salil",
        tags=["python", "dictionary", "merge"],
    )


def sparse() -> WebResult:
    return WebResult(source="stackoverflow", id="7", title="A question")


# ---------------------------------------------------------------------- #
# BibTeX
# ---------------------------------------------------------------------- #


def test_web_bibtex_key_is_deterministic_and_readable() -> None:
    assert web_bibtex_key(question()) == "salil2011merge"


def test_web_bibtex_key_falls_back_to_the_source_slug() -> None:
    record = WebResult(source="serverfault", id="1", title="vimrc tips")
    assert web_bibtex_key(record) == "serverfaultndvimrc"


def test_web_bibtex_entry_shape() -> None:
    text = web_bibtex_entry(question())
    assert text.startswith("@misc{salil2011merge,")
    assert "  author = {Salil}," in text
    assert "  title = {How to merge dicts, collecting values from matching keys?}," in text
    assert "  howpublished = {Stack Overflow}," in text
    assert "  year = {2011}," in text
    assert "  url = {https://stackoverflow.com/questions/5946236/how-to-merge-dicts}," in text
    assert "note" not in text
    assert text.endswith("}\n")


def test_web_bibtex_entry_accepts_an_explicit_key() -> None:
    text = web_bibtex_entry(question(), key="custom-key")
    assert text.startswith("@misc{custom-key,")


def test_web_bibtex_entry_accessed_note_is_opt_in() -> None:
    text = web_bibtex_entry(question(), accessed=(2026, 10, 6))
    assert "  note = {Accessed: 2026-10-06}," in text
    # And the scalar form parses too.
    assert "Accessed: 2026-10-06" in web_bibtex_entry(question(), accessed="2026-10-06")


def test_web_bibtex_entry_sparse_record_is_still_valid() -> None:
    text = web_bibtex_entry(sparse())
    assert "@misc{" in text
    assert "  title = {A question}," in text
    assert "  author" not in text
    assert "  year" not in text


# ---------------------------------------------------------------------- #
# RIS
# ---------------------------------------------------------------------- #


def test_web_ris_entry_shape() -> None:
    text = web_ris_entry(question())
    assert "TY  - GEN" in text
    assert "TI  - How to merge dicts, collecting values from matching keys?" in text
    assert "AU  - Salil" in text
    assert "PY  - 2011" in text
    assert "DA  - 2011/05/10/" in text
    assert "T2  - Stack Overflow" in text
    assert "UR  - https://stackoverflow.com/questions/5946236/how-to-merge-dicts" in text
    assert "KW  - python" in text
    assert "KW  - dictionary" in text
    assert "KW  - merge" in text
    assert text.strip().endswith("ER  -")


def test_web_ris_entry_accessed_note_is_opt_in() -> None:
    assert "N1  - Accessed: 2026-10-06" in web_ris_entry(question(), accessed=(2026, 10, 6))
    assert "N1" not in web_ris_entry(question())


def test_web_ris_entry_sparse_record() -> None:
    text = web_ris_entry(sparse())
    assert "TY  - GEN" in text
    assert "PY" not in text
    assert "AU" not in text


# ---------------------------------------------------------------------- #
# CSL-JSON
# ---------------------------------------------------------------------- #


def test_web_csl_item_shape() -> None:
    item = web_csl_item(question())
    assert item["id"] == "stackoverflow-5946236"
    assert item["type"] == "webpage"
    assert item["title"] == "How to merge dicts, collecting values from matching keys?"
    assert item["author"] == [{"literal": "Salil"}]  # usernames are literal
    assert item["issued"] == {"date-parts": [[2011, 5, 10]]}
    assert item["container-title"] == "Stack Overflow"
    assert item["URL"] == "https://stackoverflow.com/questions/5946236/how-to-merge-dicts"
    assert item["keyword"] == "python, dictionary, merge"
    assert "accessed" not in item


def test_web_csl_item_accessed_is_opt_in() -> None:
    item = web_csl_item(question(), accessed=(2026, 10, 6))
    assert item["accessed"] == {"date-parts": [[2026, 10, 6]]}


def test_web_csl_item_sparse_record_has_no_optional_keys() -> None:
    item = web_csl_item(sparse())
    assert item["id"] == "stackoverflow-7"
    assert item["type"] == "webpage"
    assert set(item) == {"id", "type", "title", "container-title"}


def test_web_csl_id_is_source_prefixed_and_falls_back_to_the_key() -> None:
    assert web_csl_id(question()) == "stackoverflow-5946236"
    assert web_csl_id(WebResult(id="7")) == "7"
    record = WebResult(source="serverfault", id="", title="orphan")
    assert web_csl_id(record) == web_bibtex_key(record)
    assert web_csl_item(record)["id"] == web_bibtex_key(record)


# ---------------------------------------------------------------------- #
# Whole-document rendering / determinism
# ---------------------------------------------------------------------- #


def test_render_web_bibtex_makes_keys_unique_and_keeps_order() -> None:
    a = question()
    b = WebResult(
        source="stackoverflow",
        id="99",
        title="Merge dicts: a reprise",
        url="https://stackoverflow.com/questions/99",
        created=1589039100,
        author="Salil",
    )
    rendered = render_web_bibtex([a, b])
    # Different years give different bases, so no suffix is needed for b.
    assert "salil2011merge," in rendered
    assert "salil2020merge,\n" in rendered
    # Two identical records collide on the key and get a/b suffixes.
    dupes = render_web_bibtex([a, a])
    assert dupes.count("@misc{salil2011merge,") == 1
    assert "@misc{salil2011mergea," in dupes


def test_render_web_bibtex_is_a_pure_function() -> None:
    assert render_web_bibtex([question()]) == render_web_bibtex([question()])


def test_render_web_csl_json_round_trips() -> None:
    rendered = render_web_csl_json([question()])
    items = json.loads(rendered)
    assert items == [web_csl_item(question())]
    assert rendered.endswith("\n")


def test_writers_write_atomically_to_a_path(tmp_path: Path) -> None:
    dest = tmp_path / "so.bib"
    web_to_bibtex([question()], dest)
    assert dest.read_text() == render_web_bibtex([question()])

    ris = tmp_path / "so.ris"
    web_to_ris([question()], ris)
    assert ris.read_text() == render_web_ris([question()])

    csl = tmp_path / "so.json"
    web_to_csl_json([question()], csl)
    assert csl.read_text() == render_web_csl_json([question()])


def test_writers_accept_file_objects(tmp_path: Path) -> None:
    dest = tmp_path / "out.txt"
    with dest.open("w") as handle:
        web_to_bibtex([question()], handle)
    assert "@misc{" in dest.read_text()


def test_writer_failure_is_an_export_error(tmp_path: Path) -> None:
    # _atomic_text creates missing parent *dirs*, so provoke a real OSError:
    # a path whose parent component is a regular file.
    blocker = tmp_path / "afile"
    blocker.write_text("")
    with pytest.raises(ExportError):
        web_to_bibtex([question()], blocker / "x.bib")


def test_epoch_zero_and_bogus_dates_stay_sparse() -> None:
    record = WebResult(id="1", title="t", created=0)
    assert "issued" not in web_csl_item(record)
    assert "year" not in web_bibtex_entry(record)
    assert "PY" not in web_ris_entry(record)