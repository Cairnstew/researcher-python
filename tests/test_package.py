"""Package-level smoke tests (offline, no network)."""

from __future__ import annotations

import re

import researcher_python


def test_version_is_semver_shaped() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", researcher_python.__version__)


def test_version_matches_pyproject() -> None:
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text()
    assert f'version = "{researcher_python.__version__}"' in text


def test_fresh_interpreter_import_configures_no_logging() -> None:
    """Importing the package must never configure logging.

    Asserted in a subprocess: pytest's own logging plugin attaches handlers to
    the root logger for the duration of the session, so an in-process check
    would be testing pytest, not this package.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(repo_root), env.get("PYTHONPATH", "")) if p
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import logging, researcher_python; "
            "assert not logging.getLogger().handlers, 'import configured root logger'",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_setup_logging_is_idempotent() -> None:
    """Repeated setup_logging() replaces handlers instead of stacking them."""
    import logging

    from researcher_python.logging_config import setup_logging

    logger = setup_logging("INFO")
    setup_logging("INFO")
    setup_logging("DEBUG")

    handlers = [
        h
        for h in logger.handlers
        if not isinstance(h, logging.NullHandler)
    ]
    assert len(handlers) == 1
    assert logger.level == logging.DEBUG
