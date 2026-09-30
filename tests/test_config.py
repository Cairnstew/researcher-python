"""Tests for :mod:`researcher_python.config` — defaults and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from researcher_python.config import ENV_KEYS, Settings, load_settings
from researcher_python.errors import ConfigurationError


# -- defaults --------------------------------------------------------------


def test_defaults() -> None:
    settings = Settings()
    assert settings.retries == 1
    assert settings.max_results == 50
    assert settings.timeout == 30.0
    assert settings.email == ""


@pytest.mark.parametrize("value", [1, 2, 50, 1000])
def test_valid_max_results_are_accepted(value: int) -> None:
    assert Settings(max_results=value).max_results == value


@pytest.mark.parametrize("value", [0, -1, -100])
def test_invalid_max_results_are_rejected(value: int) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        Settings(max_results=value)
    assert "max_results" in str(excinfo.value)
    assert "at least 1" in str(excinfo.value)
    assert "RESEARCHER_MAX_RESULTS" in str(excinfo.value)


def test_non_integer_max_results_are_rejected() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        Settings(max_results="10")  # type: ignore[arg-type]
    assert "max_results" in str(excinfo.value)
    assert "must be an integer" in str(excinfo.value)


@pytest.mark.parametrize("value", [0.5, 1, 30.0, 120])
def test_valid_timeouts_are_accepted(value: float) -> None:
    assert Settings(timeout=value).timeout == value


@pytest.mark.parametrize("value", [0, 0.0, -1, -30.5])
def test_invalid_timeouts_are_rejected(value: float) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        Settings(timeout=value)
    assert "timeout" in str(excinfo.value)
    assert "greater than 0" in str(excinfo.value)


@pytest.mark.parametrize("value", ["30", None, True])
def test_non_numeric_timeouts_are_rejected(value: object) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        Settings(timeout=value)  # type: ignore[arg-type]
    assert "timeout" in str(excinfo.value)
    assert "must be a number" in str(excinfo.value)


@pytest.mark.parametrize(
    "env",
    [
        {"RESEARCHER_MAX_RESULTS": "0"},
        {"RESEARCHER_MAX_RESULTS": "-5"},
    ],
)
def test_invalid_max_results_from_env_are_rejected(env: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(env)
    assert "max_results" in str(excinfo.value)


def test_valid_max_results_from_env_are_accepted() -> None:
    assert load_settings({"RESEARCHER_MAX_RESULTS": "12"}).max_results == 12


def test_empty_env_keeps_the_defaults() -> None:
    settings = load_settings({})
    assert settings.retries == 1
    assert settings.max_results == 50


# -- retries ---------------------------------------------------------------


def test_retries_env_name_follows_the_convention() -> None:
    assert ENV_KEYS["RESEARCHER_RETRIES"] == "retries"
    assert ENV_KEYS["RESEARCHER_MAX_RESULTS"] == "max_results"


@pytest.mark.parametrize("raw, expected", [("0", 0), ("1", 1), ("3", 3)])
def test_retries_from_env(raw: str, expected: int) -> None:
    assert load_settings({"RESEARCHER_RETRIES": raw}).retries == expected


def test_retries_from_env_is_parsed_as_an_int() -> None:
    # Not a bare string: the field keeps its declared type.
    value = load_settings({"RESEARCHER_RETRIES": "4"}).retries
    assert isinstance(value, int)


@pytest.mark.parametrize("bad", ["-1", "-10"])
def test_negative_retries_from_env_is_rejected(bad: str) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        load_settings({"RESEARCHER_RETRIES": bad})
    assert "non-negative" in str(excinfo.value)


def test_non_integer_retries_from_env_is_rejected() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        load_settings({"RESEARCHER_RETRIES": "soon"})
    assert "RESEARCHER_RETRIES" in str(excinfo.value)


def test_negative_retries_on_direct_construction_is_rejected() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        Settings(retries=-1)
    assert "non-negative" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["2", 2.0, True, None])
def test_non_int_retries_on_direct_construction_is_rejected(bad: object) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        Settings(retries=bad)  # type: ignore[arg-type]
    assert "must be an integer" in str(excinfo.value)


def test_zero_retries_is_allowed() -> None:
    assert Settings(retries=0).retries == 0


# -- unrelated settings still work ----------------------------------------


def test_other_settings_are_unaffected() -> None:
    settings = load_settings(
        {
            "RESEARCHER_EMAIL": "a@example.com",
            "RESEARCHER_MAX_RESULTS": "7",
            "RESEARCHER_LOG_LEVEL": "debug",
            "RESEARCHER_OUTPUT_DIR": "out",
        }
    )
    assert settings.email == "a@example.com"
    assert settings.max_results == 7
    assert settings.log_level == "DEBUG"  # normalised to upper case
    assert str(settings.output_dir) == "out"
    assert settings.retries == 1  # untouched by the others


def test_max_results_from_env_still_rejects_non_integers() -> None:
    with pytest.raises(ConfigurationError):
        load_settings({"RESEARCHER_MAX_RESULTS": "lots"})


# -- environment mapping ---------------------------------------------------


@pytest.mark.parametrize(
    ("env_name", "field", "raw", "expected"),
    [
        ("RESEARCHER_EMAIL", "email", "a@example.com", "a@example.com"),
        ("RESEARCHER_SEMANTIC_SCHOLAR_API_KEY", "semantic_scholar_api_key", "s2key", "s2key"),
        ("RESEARCHER_OPENALEX_API_KEY", "openalex_api_key", "oakey", "oakey"),
        ("RESEARCHER_NCBI_API_KEY", "ncbi_api_key", "nbkey", "nbkey"),
        ("RESEARCHER_OUTPUT_DIR", "output_dir", "out/dir", "out/dir"),
        ("RESEARCHER_LOG_LEVEL", "log_level", "debug", "DEBUG"),
        ("RESEARCHER_MAX_RESULTS", "max_results", "12", 12),
        ("RESEARCHER_RETRIES", "retries", "4", 4),
    ],
)
def test_every_env_key_maps_to_its_field(
    env_name: str, field: str, raw: str, expected: object
) -> None:
    assert ENV_KEYS[env_name] == field
    settings = load_settings({env_name: raw})
    value = getattr(settings, field)
    if field == "output_dir":
        assert str(value) == expected
    else:
        assert value == expected


def test_unrelated_env_vars_are_ignored() -> None:
    settings = load_settings({"NOT_OURS": "ignored"})
    assert settings.email == ""
    assert settings.max_results == 50


def test_empty_string_env_vars_fall_back_to_defaults() -> None:
    assert load_settings({"RESEARCHER_MAX_RESULTS": ""}).max_results == 50


# -- .env handling ---------------------------------------------------------


def test_dotenv_file_is_read_when_env_is_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "RESEARCHER_EMAIL=fromfile@example.com\nRESEARCHER_MAX_RESULTS=7\n"
    )
    monkeypatch.chdir(tmp_path)
    for name in ("RESEARCHER_EMAIL", "RESEARCHER_MAX_RESULTS"):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings()  # env=None -> load .env first

    assert settings.email == "fromfile@example.com"
    assert settings.max_results == 7


def test_real_environment_wins_over_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("RESEARCHER_EMAIL=fromfile@example.com\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RESEARCHER_EMAIL", "fromos@example.com")

    assert load_settings().email == "fromos@example.com"


# -- require() -------------------------------------------------------------


def test_require_returns_the_value_when_configured() -> None:
    assert Settings(email="a@example.com").require("email") == "a@example.com"
    assert Settings(log_level="INFO").require("log_level") == "INFO"


@pytest.mark.parametrize(
    ("field", "env_name"),
    [
        ("email", "RESEARCHER_EMAIL"),
        ("semantic_scholar_api_key", "RESEARCHER_SEMANTIC_SCHOLAR_API_KEY"),
        ("openalex_api_key", "RESEARCHER_OPENALEX_API_KEY"),
        ("ncbi_api_key", "RESEARCHER_NCBI_API_KEY"),
    ],
)
def test_require_names_the_env_var_to_export(field: str, env_name: str) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        Settings().require(field)
    message = str(excinfo.value)
    assert f"setting {field!r} is required" in message
    assert f"export {env_name} first" in message
    # The env var name comes from ENV_KEYS, not a hardcoded literal.
    assert ENV_KEYS[env_name] == field


# -- timeout from the environment ------------------------------------------


def test_timeout_env_name_follows_the_convention() -> None:
    assert ENV_KEYS["RESEARCHER_TIMEOUT"] == "timeout"


def test_timeout_from_env_is_a_float() -> None:
    assert load_settings({"RESEARCHER_TIMEOUT": "45.5"}).timeout == 45.5
    assert load_settings({"RESEARCHER_TIMEOUT": "12"}).timeout == 12.0
    assert isinstance(load_settings({"RESEARCHER_TIMEOUT": "12"}).timeout, float)


def test_timeout_unset_falls_back_to_the_default() -> None:
    assert load_settings({}).timeout == 30.0


def test_non_numeric_timeout_from_env_is_rejected() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        load_settings({"RESEARCHER_TIMEOUT": "soon"})
    assert "RESEARCHER_TIMEOUT" in str(excinfo.value)
    assert "must be a number" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["0", "-5", "-0.5"])
def test_non_positive_timeout_from_env_is_rejected(bad: str) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        load_settings({"RESEARCHER_TIMEOUT": bad})
    assert "greater than 0" in str(excinfo.value)
    assert "RESEARCHER_TIMEOUT" in str(excinfo.value)
