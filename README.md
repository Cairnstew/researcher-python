# researcher-python

> **Status: scaffold.** The package layout, Nix flake, CLI, and offline test
> suite are in place; the actual purpose is TBD. When it is decided, update
> this README, the `description` in `pyproject.toml`, and the matching strings
> in `nix/default.nix` / `flake.nix`.

## Features

- Thin Python package (`researcher_python`) with a versioned `__init__`.
- Console entry point `researcher-python` plus a repo-root `python cli.py`
  delegator that stays in sync with it.
- Nix flake: `nix build` produces the CLI package, `nix develop` gives a shell
  with Python, the runtime deps, and pytest.
- Offline unit tests (no network) under `tests/`.
- `.env.example` for configuration; `.env` is git-ignored.

## Setup

```sh
nix develop          # Python + requests + python-dotenv + pytest on PATH
```

Or without Nix:

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
```

## Quickstart

```sh
python cli.py info        # package name, version, install path
python cli.py --version
```

## CLI

| Command | Description |
|---------|-------------|
| `researcher-python info` | Print package name, version, and import path. |
| `researcher-python --version` | Print the version and exit. |
| `--log-level DEBUG\|INFO\|WARNING\|ERROR` | Package logger verbosity (default `INFO`). |

## Project layout

```
researcher-python/
├── flake.nix / flake.lock   # package + dev shell
├── nix/default.nix          # buildPythonApplication derivation
├── pyproject.toml           # metadata, console script, pytest config
├── cli.py                   # repo-root delegator -> researcher_python.cli
├── researcher_python/       # the package
│   ├── __init__.py          # __version__, NullHandler
│   ├── cli.py               # argparse entry point
│   └── logging_config.py    # setup_logging()
├── tests/                   # offline unit tests
├── scripts/                 # manual verification scripts (none yet)
└── .opencode/               # project-local opencode commands/skills
```

## Extending

1. Add a module under `researcher_python/` — small and single-purpose.
2. Surface anything user-facing as a subparser in
   `researcher_python/cli.py:build_parser`.
3. Add its third-party dependency to **both** `pyproject.toml`
   (`dependencies`) and `nix/default.nix` (`propagatedBuildInputs`), plus the
   dev shell's `withPackages` list in `flake.nix`.
4. Keep tests offline: recorded/mocked fixtures in `tests/`, live checks in a
   `scripts/verify_live.py`-style script gated on `RUN_LIVE=1`.
5. Never commit `.env`; follow `.env.example` for credential names.

## Tests

```sh
nix develop --command pytest tests/ -q
```

Everything under `tests/` is offline by design.
