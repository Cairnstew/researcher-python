"""Registry behaviour: resolution, unknown keys, and unwritten adapters (D5)."""

from __future__ import annotations

import pytest

from researcher_python.config import Settings
from researcher_python.errors import ConfigurationError, SourceUnavailableError
from researcher_python.sources import (
    DEFAULT_SOURCES,
    _REGISTRY,
    create_source,
    get_source_class,
    source_names,
)


def test_every_registered_source_resolves() -> None:
    """All five adapters exist now, so iterate the whole registry."""
    assert len(_REGISTRY) >= len(DEFAULT_SOURCES)
    for name, (module_path, class_name) in _REGISTRY.items():
        cls = get_source_class(name)
        assert cls.__name__ == class_name
        assert cls.name == name
        assert cls.__module__ == module_path


def test_every_default_source_is_a_real_adapter() -> None:
    for name in DEFAULT_SOURCES:
        cls = get_source_class(name)
        assert cls.__module__.startswith("researcher_python.sources.")
        assert cls.name == name


def test_source_names_defaults_first() -> None:
    names = source_names()
    assert names[:5] == [
        "openalex",
        "semantic_scholar",
        "arxiv",
        "crossref",
        "pubmed",
    ]
    assert set(names) == set(_REGISTRY)


def test_unknown_name_raises_configuration_error() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        get_source_class("definitely_not_a_source")
    assert "definitely_not_a_source" in str(excinfo.value)
    assert "openalex" in str(excinfo.value)  # lists the known sources


def test_registered_but_missing_adapter_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        _REGISTRY,
        "ghost",
        ("researcher_python.sources.ghost_source", "GhostSource"),
    )
    with pytest.raises(SourceUnavailableError) as excinfo:
        get_source_class("ghost")
    assert excinfo.value.source == "ghost"
    assert "not implemented yet" in str(excinfo.value)


def test_missing_third_party_import_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dependency missing *inside* a loaded adapter must surface as-is."""
    import researcher_python.sources as sources_pkg

    def boom(_path: str) -> object:
        raise ModuleNotFoundError("No module named 'arxiv'", name="arxiv")

    monkeypatch.setattr(sources_pkg.importlib, "import_module", boom)
    with pytest.raises(ModuleNotFoundError) as excinfo:
        get_source_class("arxiv")
    assert excinfo.value.name == "arxiv"
    assert not isinstance(excinfo.value, SourceUnavailableError)


def test_create_source_passes_settings_through() -> None:
    settings = Settings(email="a@example.com", max_results=7)
    source = create_source("openalex", settings)
    assert source.settings is settings
    assert source.settings.max_results == 7


def test_create_source_without_settings_builds_defaults() -> None:
    source = create_source("arxiv")
    assert isinstance(source.settings, Settings)
