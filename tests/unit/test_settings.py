"""The settings object, read the way a deployment actually writes it.

These go through `monkeypatch.setenv` and not through `Settings(...)` keyword
arguments on purpose: the failure they guard against happens *in the environment
source*, before field validation, so a test that passes the value as an argument
would pass while the real deployment crashed at startup.
"""

import pytest

from src.core.config import Settings


def test_list_settings_accept_a_comma_separated_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`MEDIA_ALLOWED_FORMATS=jpg,png` is how §8.4 writes it and how anyone would.

    Without `NoDecode` on the field, pydantic-settings tries to read it as JSON
    and raises before the validator can split it — and it raises at import time,
    which means the whole service fails to boot over a comma.
    """
    monkeypatch.setenv("MEDIA_ALLOWED_FORMATS", "jpg, png ,webp")
    monkeypatch.setenv("CORS_ORIGINS", "https://admin.lavaliente.gt,https://otra.gt")

    settings = Settings()

    assert settings.media_allowed_formats == ["jpg", "png", "webp"]
    assert settings.cors_origins == ["https://admin.lavaliente.gt", "https://otra.gt"]


def test_list_settings_still_accept_a_json_array(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDIA_ALLOWED_FORMATS", '["jpg", "png"]')

    assert Settings().media_allowed_formats == ["jpg", "png"]


def test_empty_list_setting_is_an_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """An origin list defined but left blank means "no origins", not a crash."""
    monkeypatch.setenv("CORS_ORIGINS", "")

    assert Settings().cors_origins == []


def test_docs_are_not_published_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")

    assert Settings().serve_docs is False


def test_docs_are_published_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "staging")

    assert Settings().serve_docs is True


def test_docs_flag_overrides_the_environment_both_ways(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """So that opening the documentation for an afternoon is a variable, not a deploy."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DOCS_ENABLED", "true")
    assert Settings().serve_docs is True

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DOCS_ENABLED", "false")
    assert Settings().serve_docs is False
