"""Drift guards: the docs must describe what actually exists.

These tests read ``README.md``, ``.env.example`` and the package surface and
compare them against the live objects, so a flag, env var or capability that
changes without a matching doc edit fails the suite.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

import researcher_python as rp
import researcher_python.download as download
import researcher_python.sources as registry
from researcher_python.cli import build_parser
from researcher_python.config import ENV_KEYS
from researcher_python.errors import NotFoundError
from researcher_python.models import Paper
from researcher_python.sources.base import Capabilities, Source

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
ENV_EXAMPLE = ROOT / ".env.example"


# -- .env.example ----------------------------------------------------------


def test_every_env_key_is_documented_in_env_example() -> None:
    text = ENV_EXAMPLE.read_text()
    missing = [name for name in ENV_KEYS if name not in text]
    assert missing == [], f"undocumented env vars: {missing}"


def test_env_example_declares_every_key_it_documents() -> None:
    declared = set(re.findall(r"^([A-Z0-9_]+)=", ENV_EXAMPLE.read_text(), re.M))
    assert set(ENV_KEYS) <= declared


# -- CLI surface -----------------------------------------------------------


def subcommand_names() -> list[str]:
    parser = build_parser()
    for action in parser._actions:  # noqa: SLF001 - argparse exposes no public view
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices)
    raise AssertionError("parser has no subcommands")


def test_every_subcommand_is_documented_in_the_readme() -> None:
    text = README.read_text()
    missing = [name for name in subcommand_names() if f"researcher_python {name}" not in text]
    assert missing == [], f"subcommands missing from README: {missing}"


def test_readme_shows_both_entry_points() -> None:
    text = README.read_text()
    assert "python -m researcher_python" in text
    assert "researcher-python" in text  # the installed console script


# -- package surface -------------------------------------------------------

REQUIRED_EXPORTS = [
    "__version__",
    "Paper",
    "Author",
    "Settings",
    "load_settings",
    "search",
    "get",
    "PipelineResult",
    "create_source",
    "get_source_class",
    "DEFAULT_SOURCES",
    "to_json",
    "to_jsonl",
    "from_json",
    "from_jsonl",
    "download_pdf",
    "ConfigurationError",
    "DownloadError",
    "ExportError",
    "NoPdfError",
    "NotFoundError",
    "RateLimitError",
    "ResearcherError",
    "SourceError",
    "SourceUnavailableError",
    "UnsupportedOperation",
]


@pytest.mark.parametrize("name", REQUIRED_EXPORTS)
def test_public_api_name_is_re_exported_and_importable(name: str) -> None:
    assert name in rp.__all__
    assert getattr(rp, name) is not None


def test_every_exported_name_exists() -> None:
    missing = [name for name in rp.__all__ if not hasattr(rp, name)]
    assert missing == []


def test_import_does_not_eagerly_load_heavy_adapters() -> None:
    """`import researcher_python` must stay light (no arxiv/habanero/Bio)."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import sys, researcher_python;"
        "print([m for m in ('Bio','habanero','arxiv') if m in sys.modules])"
    )
    # The subprocess gets no PYTHONPATH of its own, so the Nix sandbox
    # checkPhase (which runs before installPhase) cannot resolve the package.
    # Hand it the checkout explicitly, as test_cli.py does for `python -m`.
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(root), env.get("PYTHONPATH", "")) if p
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


# -- capability table ------------------------------------------------------

def test_readme_capability_table_matches_the_code() -> None:
    rows = re.findall(
        r"^\| `(\w+)` \| (.) \| (.) \| (.) \| (.) \| (.) \| ([^|]+) \|",
        README.read_text(),
        re.M,
    )
    assert rows, "no capability rows found in README"

    fields = ("search", "get", "references", "citations", "pdf")
    seen = set()
    for row in rows:
        name, *cells, status = row
        seen.add(name)
        caps = rp.get_source_class(name).capabilities
        for field, cell in zip(fields, cells):
            assert cell in ("✓", "-", "–"), f"{name}: unexpected glyph {cell!r}"
            present = cell in ("✓",)
            assert present is bool(getattr(caps, field)), f"{name}.{field} mismatch"
        verified = "live-verified" in status and "not" not in status
        # crossref / semantic_scholar are the two deferred by D1.
        if name in ("crossref", "semantic_scholar"):
            assert not verified, f"{name} should read as not live-verified"
        else:
            assert verified, f"{name} should read as live-verified"

    assert seen == set(rp.DEFAULT_SOURCES)


def test_readme_lists_the_deferred_adapters_as_limits() -> None:
    limitations = README.read_text().split("## Known limitations", 1)[1]
    assert "crossref" in limitations
    assert "semantic_scholar" in limitations
    assert "Bio.Entrez" in limitations


# -- docs stay free of scaffold language -----------------------------------


@pytest.mark.parametrize(
    "path",
    [README, ENV_EXAMPLE, ROOT / "pyproject.toml", Path(rp.__file__ or "")],
)
def test_docs_do_not_call_the_package_a_scaffold(path: Path) -> None:
    text = path.read_text().lower()
    assert "scaffold" not in text, f"{path.name} still says scaffold"
    assert "purpose tbd" not in text


# -- the README example actually runs --------------------------------------


class DocsFakeSource(Source):
    """Stands in for `openalex` while the README example runs."""

    name = "openalex"
    capabilities = Capabilities(
        search=True, get=True, references=False, citations=False, pdf=True
    )

    def search(self, query: str, *, limit: int | None = None) -> list[Paper]:
        papers = [
            Paper(
                sources=["openalex"],
                title="First example paper",
                year=2020,
                doi="10.1/first",
                pdf_url="https://example.org/first.pdf",
            ),
            Paper(sources=["openalex"], title="Second example paper", year=2021,
                  doi="10.1/second"),
        ]
        return papers if limit is None else papers[:limit]

    def get(self, key: str) -> Paper:
        raise NotFoundError(self.name, "the README example never calls get()")


class FakePdfResponse:
    status_code = 200
    text = ""

    def iter_content(self, chunk_size: int | None = None):  # noqa: ANN201
        yield b"%PDF-1.7\nexample\n%%EOF\n"

    def close(self) -> None:
        pass


def readme_python_examples() -> list[str]:
    return re.findall(r"```python\n(.*?)```", README.read_text(), re.S)


def test_the_readme_has_at_least_one_python_example() -> None:
    assert readme_python_examples(), "README has no ```python block"


def test_readme_python_example_runs_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exec the fenced example against a fake source — no network."""
    monkeypatch.setattr(
        registry, "_REGISTRY", {DocsFakeSource.name: (__name__, "DocsFakeSource")}
    )
    monkeypatch.setattr(download, "_http_get", lambda url, **kw: FakePdfResponse())
    monkeypatch.chdir(tmp_path)

    for block in readme_python_examples():
        exec(compile(block, "README.md", "exec"), {"__name__": "__readme__"})  # noqa: S102

    # The example exported something and it round-trips.
    exported = tmp_path / "papers.json"
    assert exported.exists()
    restored = rp.from_json(exported)
    assert [p.title for p in restored] == ["First example paper", "Second example paper"]
    # ...and it downloaded the first record.
    saved = list((tmp_path / "downloads").glob("*.pdf"))
    assert len(saved) == 1
    assert saved[0].read_bytes().startswith(b"%PDF-")
    capsys.readouterr()  # discard the example's own prints
