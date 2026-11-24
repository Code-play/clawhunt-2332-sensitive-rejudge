"""Configuration is read from the environment only."""

from __future__ import annotations

import pytest

from ipsensitive.config import DEFAULT_DATABASE_URL, Settings, load_settings


def test_defaults_are_safe_for_local_development():
    settings = load_settings({})
    assert settings.database_url == DEFAULT_DATABASE_URL
    assert settings.model_mode == "recorded"
    assert settings.min_model_confidence == 0.5
    assert settings.on_model_error == "fail"
    assert settings.prompt_path.name == "ip.sensitive.v1.md"


def test_environment_overrides_every_setting():
    settings = load_settings(
        {
            "DATABASE_URL": "postgresql+psycopg://user:pass@db:5432/x",
            "IP_SENSITIVE_MODEL_MODE": "http",
            "IP_SENSITIVE_MODEL_BASE_URL": "http://gateway.local/v1",
            "IP_SENSITIVE_MODEL_API_KEY": "secret",
            "IP_SENSITIVE_MODEL_NAME": "model-x",
            "IP_SENSITIVE_MODEL_TIMEOUT_SECONDS": "12.5",
            "IP_SENSITIVE_ON_MODEL_ERROR": "rules_only",
            "IP_SENSITIVE_MIN_MODEL_CONFIDENCE": "0.6",
        }
    )
    assert settings.database_url == "postgresql+psycopg://user:pass@db:5432/x"
    assert settings.model_mode == "http"
    assert settings.model_base_url == "http://gateway.local/v1"
    assert settings.model_name == "model-x"
    assert settings.model_timeout_seconds == 12.5
    assert settings.on_model_error == "rules_only"
    assert settings.min_model_confidence == 0.6


@pytest.mark.parametrize(
    "overrides",
    [
        {"model_mode": "grpc"},
        {"on_model_error": "ignore"},
        {"min_model_confidence": 1.5},
        {"model_mode": "http"},
    ],
)
def test_invalid_configuration_is_rejected(overrides):
    with pytest.raises(ValueError):
        Settings(**overrides)


def test_settings_are_immutable():
    settings = load_settings({})
    with pytest.raises(Exception):
        settings.model_mode = "http"  # type: ignore[misc]
