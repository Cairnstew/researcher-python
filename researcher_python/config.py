"""Runtime settings, read from the environment.

The settings object is frozen and passed explicitly to :class:`Pipeline`, so a
test (or an embedding application) can construct one with plain keyword
arguments and never touch ``os.environ``.  Only :func:`load_settings` reads the
process environment — and it does so through an optional ``env`` mapping,
defaulting to ``os.environ`` plus ``.env`` via python-dotenv.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError

__all__ = ["Settings", "load_settings"]


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable configuration shared by every source and the pipeline."""

    #: Polite-pool identifier for OpenAlex / Crossref / Unpaywall.
    email: str = ""
    #: Optional Semantic Scholar API key.  S2's documented rate limit is ~1
    #: request/second, *cumulative across all endpoints* — search, get,
    #: references and citations share one gate per adapter instance
    #: (``semantic_scholar_source._INTERVAL`` = 1.1s).  The key is sent as
    #: ``x-api-key`` so calls are attributed rather than drawing on the shared
    #: anonymous pool that keyless traffic hits.
    semantic_scholar_api_key: str = ""
    #: Optional OpenAlex API key.  Anonymous *search* on OpenAlex is served by a
    #: cluster that pauses periodically (HTTP 503 "Anonymous search is paused");
    #: a free key removes both that outage mode and the rate limit.
    openalex_api_key: str = ""
    #: Optional NCBI E-utilities API key (3 rps -> 10 rps with key).
    ncbi_api_key: str = ""
    #: Optional Stack Exchange API key.  Without one the anonymous IP bucket
    #: is shared (quota observed as 300/day); a key raises the application
    #: quota.  Sent as the ``key`` request parameter, never logged.
    stackexchange_key: str = ""
    #: Optional Reddit OAuth app (client) credentials for the web-source
    #: framework.  Reddit's anonymous JSON endpoints are closed (HTTP 403 to
    #: keyless data consumers), so ``web_search(source="reddit")`` requires a
    #: registered app these two identifiers.  Sent as HTTP Basic auth on the
    #: ``/api/v1/access_token`` POST, never logged.
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    #: Where :func:`researcher_python.download.save_pdf` writes files.
    output_dir: Path = Path("downloads")
    #: Root for *named* workspaces (``RESEARCHER_HOME``).  A relative
    #: :class:`researcher_python.workspace.Workspace` path resolves under this
    #: directory, so a globally launched MCP server does not depend on the
    #: process cwd.  ``None`` falls back to cwd for backwards compatibility.
    home: Path | None = None
    #: Default workspace when a tool omits one (``RESEARCHER_WORKSPACE``).
    workspace: Path | None = None
    #: Explicit ``.env`` path (``RESEARCHER_ENV_FILE``), so a server started
    #: from an arbitrary directory still finds credentials.
    env_file: Path | None = None
    #: ``DEBUG``/``INFO``/``WARNING``/... used by ``setup_logging``.
    log_level: str = "INFO"

    #: Hard ceiling on records one ``search`` may ask a source for.  Sources
    #: use it to bound page counts; the pipeline slices the final list.
    max_results: int = 50

    #: Per-request socket timeout, in seconds, for the HTTP-backed sources.
    timeout: float = 30.0

    #: Extra attempts after the first, allowed *only* for a rate-limited
    #: source (see :func:`researcher_python.pipeline.search`).  ``0`` disables
    #: retrying; adapters themselves never loop (D8).
    retries: int = 1

    def __post_init__(self) -> None:
        # Frozen dataclass: validate on construction so a bad value is refused
        # whether it came from the environment or a direct keyword.
        if isinstance(self.retries, bool) or not isinstance(self.retries, int):
            raise ConfigurationError(
                f"setting 'retries' must be an integer, got {self.retries!r}"
            )
        if self.retries < 0:
            raise ConfigurationError(
                f"setting 'retries' must be a non-negative integer, got "
                f"{self.retries} (set RESEARCHER_RETRIES to 0 or more)"
            )

        if isinstance(self.max_results, bool) or not isinstance(self.max_results, int):
            raise ConfigurationError(
                f"setting 'max_results' must be an integer, got {self.max_results!r}"
            )
        if self.max_results < 1:
            raise ConfigurationError(
                f"setting 'max_results' must be at least 1, got "
                f"{self.max_results} (set RESEARCHER_MAX_RESULTS to 1 or more)"
            )

        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)):
            raise ConfigurationError(
                f"setting 'timeout' must be a number, got {self.timeout!r}"
            )
        if self.timeout <= 0:
            raise ConfigurationError(
                f"setting 'timeout' must be greater than 0, got {self.timeout} "
                f"(set RESEARCHER_TIMEOUT to a positive value)"
            )

    def require(self, name: str) -> str:
        """Return ``self.<name>`` or raise :class:`ConfigurationError`.

        Used by adapters that cannot work without a credential, so the failure
        names the setting instead of surfacing as a 401 from the API.
        """
        value = getattr(self, name)
        if value in (None, ""):
            env_name = {
                "email": "RESEARCHER_EMAIL",
                "semantic_scholar_api_key": "RESEARCHER_SEMANTIC_SCHOLAR_API_KEY",
                "openalex_api_key": "RESEARCHER_OPENALEX_API_KEY",
                "ncbi_api_key": "RESEARCHER_NCBI_API_KEY",
                "stackexchange_key": "RESEARCHER_STACKEXCHANGE_KEY",
                "reddit_client_id": "RESEARCHER_REDDIT_CLIENT_ID",
                "reddit_client_secret": "RESEARCHER_REDDIT_CLIENT_SECRET",
            }.get(name, name)
            raise ConfigurationError(
                f"setting {name!r} is required; export {env_name} first"
            )
        return str(value)


#: Environment variable names read by :func:`load_settings`.
ENV_KEYS = {
    "RESEARCHER_EMAIL": "email",
    "RESEARCHER_SEMANTIC_SCHOLAR_API_KEY": "semantic_scholar_api_key",
    "RESEARCHER_OPENALEX_API_KEY": "openalex_api_key",
    "RESEARCHER_NCBI_API_KEY": "ncbi_api_key",
    "RESEARCHER_STACKEXCHANGE_KEY": "stackexchange_key",
    "RESEARCHER_REDDIT_CLIENT_ID": "reddit_client_id",
    "RESEARCHER_REDDIT_CLIENT_SECRET": "reddit_client_secret",
    "RESEARCHER_OUTPUT_DIR": "output_dir",
    "RESEARCHER_HOME": "home",
    "RESEARCHER_WORKSPACE": "workspace",
    "RESEARCHER_ENV_FILE": "env_file",
    "RESEARCHER_LOG_LEVEL": "log_level",
    "RESEARCHER_MAX_RESULTS": "max_results",
    "RESEARCHER_RETRIES": "retries",
    "RESEARCHER_TIMEOUT": "timeout",
}

_INT_FIELDS = frozenset({"max_results", "retries"})
_FLOAT_FIELDS = frozenset({"timeout"})
_PATH_FIELDS = frozenset({"output_dir", "home", "workspace", "env_file"})


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from ``env`` (default: ``os.environ``).

    When ``env`` is ``None`` one ``.env`` is loaded first, non-destructively
    (existing environment variables win): ``RESEARCHER_ENV_FILE`` if set, else
    ``$RESEARCHER_HOME/.env``, else ``~/.config/researcher-python/.env``, else
    ``./.env``.  Then the process environment is read.  Passing an explicit
    mapping skips dotenv entirely, which is what the tests do.
    """
    if env is None:
        # The env-file search itself must read the process environment first:
        # a globally registered MCP server has no useful cwd, so
        # RESEARCHER_ENV_FILE / RESEARCHER_HOME decide where .env lives.
        _load_dotenv(
            env_file=os.environ.get("RESEARCHER_ENV_FILE"),
            home=os.environ.get("RESEARCHER_HOME"),
        )
        env = os.environ

    values: dict[str, object] = {}
    for env_name, field_name in ENV_KEYS.items():
        raw = env.get(env_name)
        if raw is None or raw == "":
            continue
        if field_name in _INT_FIELDS:
            try:
                values[field_name] = int(raw)
            except ValueError as exc:
                raise ConfigurationError(
                    f"{env_name} must be an integer, got {raw!r}"
                ) from exc
        elif field_name in _FLOAT_FIELDS:
            try:
                values[field_name] = float(raw)
            except ValueError as exc:
                raise ConfigurationError(
                    f"{env_name} must be a number, got {raw!r}"
                ) from exc
        elif field_name in _PATH_FIELDS:
            values[field_name] = Path(raw).expanduser()
        else:
            values[field_name] = raw

    if "log_level" in values:
        values["log_level"] = str(values["log_level"]).upper()
    return Settings(**values)  # type: ignore[arg-type]


def _load_dotenv(
    *, env_file: str | None = None, home: str | None = None
) -> None:
    """Load ``.env`` from the first location that makes sense.

    Order:

    1. an explicit ``RESEARCHER_ENV_FILE`` (an absolute path is expected; a
       missing explicit file is a configuration error, not silently ignored);
    2. ``$RESEARCHER_HOME/.env``;
    3. ``~/.config/researcher-python/.env`` — the stable global location;
    4. ``./.env`` — the historical behaviour for a repo-local run.

    dotenv is a convenience, not a dependency of the core — a missing package
    must not stop a caller who exported the variables by hand.
    """
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - optional convenience
        return

    if env_file:
        path = Path(env_file).expanduser()
        if not path.is_file():
            raise ConfigurationError(
                f"RESEARCHER_ENV_FILE points at {path}, which does not exist"
            )
        load_dotenv(path, override=False)
        return

    candidates: list[Path] = []
    if home:
        candidates.append(Path(home).expanduser() / ".env")
    candidates.append(Path.home() / ".config" / "researcher-python" / ".env")
    candidates.append(Path(".env"))
    for candidate in candidates:
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return
