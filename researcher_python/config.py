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
    #: Optional Semantic Scholar API key (2 rps keyless -> 10 rps with key).
    semantic_scholar_api_key: str = ""
    #: Optional OpenAlex API key.  Anonymous *search* on OpenAlex is served by a
    #: cluster that pauses periodically (HTTP 503 "Anonymous search is paused");
    #: a free key removes both that outage mode and the rate limit.
    openalex_api_key: str = ""
    #: Optional NCBI E-utilities API key (3 rps -> 10 rps with key).
    ncbi_api_key: str = ""
    #: Where :func:`researcher_python.download.save_pdf` writes files.
    output_dir: Path = Path("downloads")
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
    "RESEARCHER_OUTPUT_DIR": "output_dir",
    "RESEARCHER_LOG_LEVEL": "log_level",
    "RESEARCHER_MAX_RESULTS": "max_results",
    "RESEARCHER_RETRIES": "retries",
    "RESEARCHER_TIMEOUT": "timeout",
}

_INT_FIELDS = frozenset({"max_results", "retries"})
_FLOAT_FIELDS = frozenset({"timeout"})


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from ``env`` (default: ``os.environ``).

    When ``env`` is ``None`` a ``.env`` file in the working directory is loaded
    first (non-destructively: existing environment variables win), then the
    process environment is read.  Passing an explicit mapping skips dotenv
    entirely, which is what the tests do.
    """
    if env is None:
        _load_dotenv()
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
        elif field_name == "output_dir":
            values[field_name] = Path(raw)
        else:
            values[field_name] = raw

    if "log_level" in values:
        values["log_level"] = str(values["log_level"]).upper()
    return Settings(**values)  # type: ignore[arg-type]


def _load_dotenv() -> None:
    """Load ``.env`` if python-dotenv is importable; never fail if it is not.

    dotenv is a convenience, not a dependency of the core — a missing package
    must not stop a caller who exported the variables by hand.
    """
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - optional convenience
        return
    load_dotenv(Path(".env"), override=False)
