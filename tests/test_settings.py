import pytest
from pydantic import ValidationError

from agent.settings import ModelProvider, Settings


@pytest.mark.parametrize(
    ("provider", "provider_config"),
    [
        (ModelProvider.OPENAI, {"openai_api_key": "test-key"}),
        (
            ModelProvider.AZURE_OPENAI,
            {
                "azure_openai_api_key": "test-key",
                "azure_openai_endpoint": "https://example.openai.azure.com",
            },
        ),
        (ModelProvider.GEMINI, {"google_api_key": "test-key"}),
        (ModelProvider.ANTHROPIC, {"anthropic_api_key": "test-key"}),
        (ModelProvider.OLLAMA, {}),
    ],
)
def test_selected_provider_accepts_its_required_configuration(
    provider: ModelProvider, provider_config: dict[str, str]
) -> None:
    settings = Settings(
        _env_file=None,
        model_provider=provider,
        model_name="test-model",
        **provider_config,
    )

    assert settings.model_provider is provider
    assert settings.model_name == "test-model"


@pytest.mark.parametrize(
    "provider",
    [
        ModelProvider.OPENAI,
        ModelProvider.AZURE_OPENAI,
        ModelProvider.GEMINI,
        ModelProvider.ANTHROPIC,
    ],
)
def test_cloud_provider_rejects_missing_credentials(provider: ModelProvider) -> None:
    with pytest.raises(ValidationError, match="Missing configuration"):
        Settings(
            _env_file=None,
            model_provider=provider,
            model_name="test-model",
        )


def test_unselected_provider_credentials_are_not_required() -> None:
    settings = Settings(
        _env_file=None,
        model_provider=ModelProvider.OLLAMA,
        model_name="qwen3:8b",
    )

    assert str(settings.ollama_base_url) == "http://localhost:11434/"