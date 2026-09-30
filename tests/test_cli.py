"""CLI tests (offline — no network, no subprocess)."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from researcher_python import __version__
import researcher_python.cli as cli
import researcher_python.download as download
import researcher_python.sources as registry
from researcher_python.cli import build_parser, main
from researcher_python.config import Settings
from researcher_python.errors import (
    ConfigurationError,
    NotFoundError,
    SourceError,
    SourceUnavailableError,
)
from researcher_python.models import Author, Paper
from researcher_python.sources.base import Capabilities, Source

FAKE_KEY = "SUPERSECRETVALUE-DO-NOT-PRINT"


# -- pre-existing behaviour (must keep working) ----------------------------


def test_parser_exposes_expected_commands() -> None:
    parser = build_parser()
    args = parser.parse_args(["info"])
    assert args.command == "info"


def test_info_command_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["info"]) == 0
    out = capsys.readouterr().out
    assert f"researcher-python {__version__}" in out
    assert "researcher_python" in out


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "usage:" in out


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_log_level_still_configures_the_package_logger() -> None:
    assert main(["--log-level", "WARNING", "info"]) == 0
    assert logging.getLogger("researcher_python").level == logging.WARNING


def test_usage_error_exits_two() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--not-a-flag"])
    assert exc.value.code == 2


def test_python_m_entry_point_runs_the_cli_without_installing() -> None:
    """`python -m researcher_python` must behave exactly like the console script."""
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)

    proc = subprocess.run(
        [sys.executable, "-m", "researcher_python", "sources", "--format", "json"],
        capture_output=True,
        text=True,
        cwd=root,
        env=env,
        timeout=180,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)  # valid JSON on stdout
    assert isinstance(payload, list) and payload
    assert {"name", "default", "capabilities", "status"} <= set(payload[0])


# -- fakes -----------------------------------------------------------------


class ReadySource(Source):
    name = "ready"
    capabilities = Capabilities(
        search=True, get=True, references=True, citations=True, pdf=True
    )

    def search(self, query: str, *, limit: int | None = None) -> list[Paper]:
        return []

    def get(self, key: str) -> Paper:
        raise NotImplementedError


class NeedsConfigSource(Source):
    name = "needsconfig"

    def __init__(self, settings: Settings | None = None, **_: object) -> None:
        raise ConfigurationError(
            "setting 'email' is required; export RESEARCHER_EMAIL first"
        )

    def search(self, query: str, *, limit: int | None = None) -> list[Paper]:
        return []

    def get(self, key: str) -> Paper:
        raise NotImplementedError


class UnavailableSource(Source):
    name = "unavailable"

    def __init__(self, settings: Settings | None = None, **_: object) -> None:
        raise SourceUnavailableError(
            "unavailable", "dependency 'nope' is not installed"
        )

    def search(self, query: str, *, limit: int | None = None) -> list[Paper]:
        return []

    def get(self, key: str) -> Paper:
        raise NotImplementedError


def register_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the registry wholesale so only these three rows appear."""
    module = __name__
    monkeypatch.setattr(
        registry,
        "_REGISTRY",
        {
            "ready": (module, "ReadySource"),
            "needsconfig": (module, "NeedsConfigSource"),
            "unavailable": (module, "UnavailableSource"),
        },
    )


def stub_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings | None = None) -> None:
    value = settings if settings is not None else Settings()
    monkeypatch.setattr(cli, "load_settings", lambda: value)


# -- sources ---------------------------------------------------------------


def test_sources_table_reports_every_status(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    register_fakes(monkeypatch)
    stub_settings(monkeypatch)

    assert main(["sources"]) == 0

    out = capsys.readouterr().out
    assert "NAME" in out and "DEFAULT" in out and "CAPS" in out and "STATUS" in out
    assert "S G R C P" in out  # capability letters for the ready row
    assert "needs config (RESEARCHER_EMAIL)" in out
    assert "unavailable (dependency 'nope' is not installed)" in out
    assert "ready" in out
    # Defaults come from DEFAULT_SOURCES; none of the fakes are in it.
    assert re.search(r"^ready\s+no\s", out, re.MULTILINE)


def test_sources_json_carries_the_same_fields(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    register_fakes(monkeypatch)
    stub_settings(monkeypatch)

    assert main(["sources", "--format", "json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert [entry["name"] for entry in payload] == [
        "ready",
        "needsconfig",
        "unavailable",
    ]
    first = payload[0]
    assert set(first) == {"name", "default", "capabilities", "status"}
    assert first["default"] is False
    assert first["status"] == "ready"
    assert first["capabilities"] == {
        "search": True,
        "get": True,
        "references": True,
        "citations": True,
        "pdf": True,
    }
    assert payload[1]["status"].startswith("needs config")
    assert payload[2]["status"].startswith("unavailable")


def test_sources_makes_no_network_calls(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constructing every real adapter must not issue a single request."""
    import httpx
    import requests

    stub_settings(monkeypatch)  # real registry — all five adapters

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the sources command must never touch the network")

    monkeypatch.setattr(requests.sessions.Session, "request", explode)
    monkeypatch.setattr(httpx.Client, "request", explode)
    monkeypatch.setattr(httpx.Client, "send", explode)

    assert main(["sources"]) == 0
    out = capsys.readouterr().out
    for name in ("openalex", "semantic_scholar", "arxiv", "crossref", "pubmed"):
        assert name in out


# -- info ------------------------------------------------------------------


def test_info_table_prints_settings_and_redacts_keys(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_settings(
        monkeypatch,
        Settings(email="user@example.com", semantic_scholar_api_key=FAKE_KEY),
    )

    assert main(["info"]) == 0

    captured = capsys.readouterr()
    assert FAKE_KEY not in captured.out
    assert FAKE_KEY not in captured.err
    assert f"researcher-python {__version__}" in captured.out
    assert "package:" in captured.out and "researcher_python" in captured.out
    assert re.search(r"^python: \d", captured.out, re.MULTILINE)
    # Email is deliberately printable; keys are only ever "set"/"unset".
    assert "user@example.com" in captured.out
    assert re.search(r"semantic_scholar_api_key\s+set", captured.out)
    assert re.search(r"openalex_api_key\s+unset", captured.out)
    assert re.search(r"ncbi_api_key\s+unset", captured.out)
    assert re.search(r"output_dir\s+downloads", captured.out)
    assert re.search(r"log_level\s+INFO", captured.out)
    assert re.search(r"max_results\s+50", captured.out)
    assert re.search(r"timeout\s+30\.0", captured.out)
    assert re.search(r"retries\s+1", captured.out)


def test_info_json_redacts_keys(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_settings(
        monkeypatch,
        Settings(email="user@example.com", semantic_scholar_api_key=FAKE_KEY),
    )

    assert main(["info", "--format", "json"]) == 0

    captured = capsys.readouterr()
    assert FAKE_KEY not in captured.out
    assert FAKE_KEY not in captured.err

    payload = json.loads(captured.out)
    assert payload["version"] == __version__
    assert payload["settings"]["email"] == "user@example.com"
    assert payload["settings"]["semantic_scholar_api_key"] == "set"
    assert payload["settings"]["openalex_api_key"] == "unset"
    assert payload["settings"]["ncbi_api_key"] == "unset"
    assert payload["settings"]["retries"] == 1
    assert payload["settings"]["timeout"] == 30.0


def test_info_table_marks_unset_email_as_unset(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset email is a word in the table, not an empty cell."""
    stub_settings(monkeypatch, Settings())  # email defaults to ""

    assert main(["info"]) == 0

    captured = capsys.readouterr()
    assert re.search(r"^email\s+unset$", captured.out, re.MULTILINE)


def test_info_json_reports_unset_email_as_null(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset email serialises to JSON null, not an empty string."""
    stub_settings(monkeypatch, Settings())

    assert main(["info", "--format", "json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["settings"]["email"] is None
    # API keys are still only ever set/unset, never a value or null.
    assert payload["settings"]["openalex_api_key"] == "unset"


# -- the shared error handler ----------------------------------------------


def test_configuration_error_exits_two_on_one_stderr_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> Settings:
        raise ConfigurationError(
            "setting 'email' is required; export RESEARCHER_EMAIL first"
        )

    monkeypatch.setattr(cli, "load_settings", boom)

    assert main(["info"]) == 2

    err = capsys.readouterr().err
    assert "RESEARCHER_EMAIL" in err
    assert err.count("\n") == 1  # single line, no trailing noise
    assert "Traceback" not in err


def test_other_researcher_error_exits_one_on_one_stderr_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_settings(monkeypatch)

    def handler(args: object, settings: Settings) -> int:
        raise SourceError("arxiv", "everything\nwent   wrong")

    monkeypatch.setitem(cli._HANDLERS, "info", handler)

    assert main(["info"]) == 1

    err = capsys.readouterr().err
    assert "everything went wrong" in err  # newlines collapsed to one line
    assert err.count("\n") == 1
    assert "Traceback" not in err


def test_debug_log_level_prints_a_traceback(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_settings(monkeypatch)

    def handler(args: object, settings: Settings) -> int:
        raise SourceError("arxiv", "boom")

    monkeypatch.setitem(cli._HANDLERS, "info", handler)

    assert main(["--log-level", "DEBUG", "info"]) == 1

    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "boom" in err


def test_unexpected_exception_is_not_swallowed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_settings(monkeypatch)

    def handler(args: object, settings: Settings) -> int:
        raise ZeroDivisionError("unexpected")

    monkeypatch.setitem(cli._HANDLERS, "info", handler)

    with pytest.raises(ZeroDivisionError):
        main(["info"])


# ---------------------------------------------------------------------- #
# 5b — search / get / download
# ---------------------------------------------------------------------- #

#: Behaviour per registry key, mutated by each test.
BEHAVIOUR: dict[str, Any] = {}


class FakeSource(Source):
    """Configurable adapter; behaviour comes from ``BEHAVIOUR[self.name]``."""

    name = "fake"
    capabilities = Capabilities(
        search=True, get=True, references=False, citations=False, pdf=True
    )

    @property
    def _spec(self) -> dict[str, Any]:
        return BEHAVIOUR.setdefault(self.name, {})

    def _stamp(self, item: Paper) -> Paper:
        out = item.copy()
        out.sources = [self.name]
        return out

    def search(self, query: str, *, limit: int | None = None) -> list[Paper]:
        spec = self._spec
        error = spec.get("search_error")
        if error is not None:
            raise error  # type: ignore[misc]
        results = spec.get("search_results", []) or []
        if limit is not None:
            results = results[:limit]
        return [self._stamp(item) for item in results]  # type: ignore[arg-type]

    def get(self, key: str) -> Paper:
        spec = self._spec
        error = spec.get("get_error")
        if error is not None:
            raise error  # type: ignore[misc]
        per_key = spec.get("get_by_key")
        if per_key is not None:
            if key not in per_key:
                raise NotFoundError(self.name, f"no record for {key}")
            item = per_key[key]
            if isinstance(item, Exception):
                raise item
            return self._stamp(item)
        found = spec.get("get_paper")
        if found is None:
            raise NotFoundError(self.name, f"no record for {key}")
        return self._stamp(found)  # type: ignore[arg-type]


class AlphaSource(FakeSource):
    name = "alpha"


class BetaSource(FakeSource):
    name = "beta"


class BrokenSource(FakeSource):
    name = "broken"


class FakePdfResponse:
    status_code = 200
    text = ""

    def iter_content(self, chunk_size: int | None = None):  # noqa: ANN201
        yield b"%PDF-1.7\nfake body\n%%EOF\n"

    def close(self) -> None:
        pass


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(
        registry,
        "_REGISTRY",
        {
            "alpha": (__name__, "AlphaSource"),
            "beta": (__name__, "BetaSource"),
            "broken": (__name__, "BrokenSource"),
        },
    )
    monkeypatch.setattr(cli, "load_settings", lambda: Settings())
    monkeypatch.setattr(download, "_http_get", lambda url, **kwargs: FakePdfResponse())
    BEHAVIOUR.clear()
    yield
    BEHAVIOUR.clear()


def make_paper(**kwargs: object) -> Paper:
    return Paper(**kwargs)  # type: ignore[arg-type]


# -- search ----------------------------------------------------------------


def test_search_table_shape_and_stderr_summary(
    world: None, capsys: pytest.CaptureFixture[str]
) -> None:
    BEHAVIOUR["alpha"] = {
        "search_results": [
            make_paper(
                year=2021,
                title="A very long title " * 6,
                doi="10.1/x",
                authors=[
                    Author(name="Ada Lovelace"),
                    Author(name="Alan Turing"),
                ],
            ),
            make_paper(year=2020, title="Short title", arxiv_id="1706.03762",
                       authors=[Author(name="Solo Author")]),
        ]
    }
    BEHAVIOUR["beta"] = {
        "search_results": [make_paper(year=2019, title="From beta", pmid="22470460")]
    }

    code = main(["search", "query", "--source", "alpha", "--source", "beta"])
    captured = capsys.readouterr()

    assert code == 0
    for header in ("YEAR", "AUTHOR", "TITLE", "SOURCES", "ID"):
        assert header in captured.out
    assert "Ada Lovelace et al." in captured.out  # >1 author
    assert "Solo Author" in captured.out and "et al." not in captured.out.split("\n")[4]
    assert "..." in captured.out  # long title truncated
    assert "alpha" in captured.out  # sources column
    assert "10.1/x" in captured.out and "22470460" in captured.out
    assert captured.err.strip() == "3 papers from 2 sources (failed: none)"


def test_search_json_envelope_and_meta_errors(
    world: None, capsys: pytest.CaptureFixture[str]
) -> None:
    BEHAVIOUR["alpha"] = {"search_results": [make_paper(title="One", doi="10.1/1")]}
    BEHAVIOUR["broken"] = {"search_error": SourceError("broken", "boom")}

    code = main(
        ["search", "q", "--source", "alpha", "--source", "broken", "--format", "json"]
    )
    captured = capsys.readouterr()

    assert code == 0  # partial failure is still a successful run
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert payload["count"] == 1
    assert payload["meta"]["query"] == "q"
    assert payload["meta"]["sources"] == ["alpha", "broken"]
    assert payload["meta"]["sort"] == "relevance"
    assert payload["meta"]["errors"] == {"broken": "boom"}
    assert "broken: SourceError: boom" in captured.err


def test_search_jsonl_is_one_object_per_line(
    world: None, capsys: pytest.CaptureFixture[str]
) -> None:
    BEHAVIOUR["alpha"] = {
        "search_results": [make_paper(title="A"), make_paper(title="B")]
    }

    assert main(["search", "q", "--source", "alpha", "--format", "jsonl"]) == 0

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["title"] for line in lines] == ["A", "B"]
    assert "schema_version" not in capsys.readouterr().out  # no envelope


def test_search_out_writes_a_file_and_keeps_stdout_empty(
    world: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    BEHAVIOUR["alpha"] = {"search_results": [make_paper(title="One", doi="10.1/1")]}
    dest = tmp_path / "nested" / "results.json"

    code = main(
        ["search", "q", "--source", "alpha", "--format", "json", "--out", str(dest)]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out == ""
    payload = json.loads(dest.read_text())
    assert payload["count"] == 1
    assert payload["meta"]["query"] == "q"
    assert "1 paper from 1 source" in captured.err  # summary is still a diagnostic


def test_search_out_with_table_is_a_usage_error(
    world: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["search", "q", "--out", "results.json"])
    captured = capsys.readouterr()

    assert code == 2
    assert captured.out == ""
    assert "--out" in captured.err
    assert "Traceback" not in captured.err


def test_search_partial_failure_exits_zero_with_error_lines(
    world: None, capsys: pytest.CaptureFixture[str]
) -> None:
    BEHAVIOUR["alpha"] = {"search_results": [make_paper(title="One", doi="10.1/1")]}
    BEHAVIOUR["broken"] = {"search_error": SourceError("broken", "everything failed")}

    code = main(["search", "q", "--source", "alpha", "--source", "broken"])
    captured = capsys.readouterr()

    assert code == 0
    assert "broken: SourceError: everything failed" in captured.err
    assert "(failed: broken)" in captured.err


def test_search_all_sources_failing_exits_one(
    world: None, capsys: pytest.CaptureFixture[str]
) -> None:
    BEHAVIOUR["broken"] = {"search_error": SourceError("broken", "dead")}

    code = main(["search", "q", "--source", "broken"])
    captured = capsys.readouterr()

    assert code == 1
    assert "0 papers from 0 sources (failed: broken)" in captured.err
    assert "Traceback" not in captured.err


def test_search_unknown_source_exits_two(
    world: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["search", "q", "--source", "nope"])
    captured = capsys.readouterr()

    assert code == 2
    assert "'nope'" in captured.err
    assert "known sources" in captured.err


def test_search_invalid_sort_exits_two(world: None) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["search", "q", "--sort", "bogus"])
    assert exc.value.code == 2


def test_search_zero_results_still_exits_zero(
    world: None, capsys: pytest.CaptureFixture[str]
) -> None:
    BEHAVIOUR["alpha"] = {"search_results": []}

    assert main(["search", "q", "--source", "alpha"]) == 0
    assert "0 papers from 1 source (failed: none)" in capsys.readouterr().err


def test_search_summary_pluralises(
    world: None, capsys: pytest.CaptureFixture[str]
) -> None:
    BEHAVIOUR["alpha"] = {"search_results": [make_paper(title="Only one")]}

    assert main(["search", "q", "--source", "alpha"]) == 0
    assert capsys.readouterr().err.strip() == "1 paper from 1 source (failed: none)"


# -- get -------------------------------------------------------------------

GET_PAPER = make_paper(
    title="Resolved record",
    authors=[Author(name="Ada Lovelace"), Author(name="Alan Turing")],
    year=2021,
    venue="Journal of Testing Things",
    doi="10.1/resolved",
    ids={"doi": "10.1/resolved", "arxiv": "1706.03762"},
    url="https://doi.org/10.1/resolved",
    pdf_url="https://example.org/resolved.pdf",
    citation_count=42,
    abstract=" ".join(["word"] * 60),
)


def test_get_table_is_a_detail_view(
    world: None, capsys: pytest.CaptureFixture[str]
) -> None:
    BEHAVIOUR["alpha"] = {"get_paper": GET_PAPER}

    code = main(["get", "10.1/resolved", "--source", "alpha"])
    captured = capsys.readouterr()

    assert code == 0
    out = captured.out
    assert "Resolved record" in out
    assert "Ada Lovelace, Alan Turing" in out
    assert "2021" in out and "Journal of Testing Things" in out
    assert "doi: 10.1/resolved" in out and "arxiv: 1706.03762" in out
    assert "https://example.org/resolved.pdf" in out
    assert "42" in out

    # the abstract is wrapped to ~88 columns, after a labelled block
    tail = out.split("\nabstract\n", 1)[1]
    assert tail
    assert all(len(line) <= 88 for line in tail.splitlines())


def test_get_json_is_a_single_paper_object(
    world: None, capsys: pytest.CaptureFixture[str]
) -> None:
    BEHAVIOUR["alpha"] = {"get_paper": GET_PAPER}

    code = main(["get", "10.1/resolved", "--source", "alpha", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["title"] == "Resolved record"
    assert payload["year"] == 2021
    assert "schema_version" not in payload  # NOT the export envelope
    assert "papers" not in payload


def test_get_not_found_exits_one(
    world: None, capsys: pytest.CaptureFixture[str]
) -> None:
    BEHAVIOUR["alpha"] = {}  # get() raises NotFoundError

    code = main(["get", "10.1/absent", "--source", "alpha"])
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert "no source has this record" in captured.err
    assert "Traceback" not in captured.err


# -- download --------------------------------------------------------------


def test_download_success_prints_path_and_via(
    world: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    BEHAVIOUR["alpha"] = {
        "get_paper": make_paper(
            title="Document", doi="10.1/doc", pdf_url="https://example.org/doc.pdf"
        )
    }

    code = main(
        ["download", "10.1/doc", "--source", "alpha", "--output-dir", str(tmp_path)]
    )
    captured = capsys.readouterr()

    assert code == 0
    paths = captured.out.strip().splitlines()
    assert len(paths) == 1
    saved = Path(paths[0])
    assert saved.exists() and saved.suffix == ".pdf"
    assert saved.read_bytes().startswith(b"%PDF-")
    assert "via paper" in captured.err


def test_download_continues_after_a_failure_and_exits_one(
    world: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    BEHAVIOUR["alpha"] = {
        "get_by_key": {
            "10.1/ok": make_paper(
                title="Good", doi="10.1/ok", pdf_url="https://example.org/ok.pdf"
            ),
            "10.1/bad": NotFoundError("alpha", "no such record"),
        }
    }

    code = main(
        [
            "download",
            "10.1/ok",
            "10.1/bad",
            "--source",
            "alpha",
            "--output-dir",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert code == 1  # one failed
    paths = captured.out.strip().splitlines()
    assert len(paths) == 1 and Path(paths[0]).exists()  # the good one still saved
    assert "10.1/bad: NotFoundError:" in captured.err
    assert "10.1/ok: via paper" in captured.err


def test_download_missing_unpaywall_email_exits_two_immediately(
    world: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No pdf_url and no email -> step 2 (Unpaywall) cannot run.
    BEHAVIOUR["alpha"] = {"get_paper": make_paper(title="No url", doi="10.1/x")}

    code = main(
        [
            "download",
            "10.1/x",
            "10.1/second",
            "--source",
            "alpha",
            "--output-dir",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    assert "RESEARCHER_EMAIL" in captured.err
    assert captured.out == ""  # stopped before downloading anything
    assert "10.1/second" not in captured.err
