from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from agent.llm import ChatModelFactory, ProviderDependencyError
from agent.settings import ModelProvider, Settings


class StructuredResponse(BaseModel):
    answer: str


class FakeChatModel:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.structured_call: tuple[type[BaseModel], dict[str, object]] | None = None

    def with_structured_output(
        self, schema: type[BaseModel], **kwargs: object
    ) -> "FakeChatModel":
        self.structured_call = (schema, kwargs)
        return self


def _settings(provider: ModelProvider) -> Settings:
    provider_config = {
        ModelProvider.OPENAI: {"openai_api_key": "test-key"},
        ModelProvider.AZURE_OPENAI: {
            "azure_openai_api_key": "test-key",
            "azure_openai_endpoint": "https://example.openai.azure.com",
        },
        ModelProvider.GEMINI: {"google_api_key": "test-key"},
        ModelProvider.ANTHROPIC: {"anthropic_api_key": "test-key"},
        ModelProvider.OLLAMA: {},
    }
    return Settings(
        _env_file=None,
        model_provider=provider,
        model_name="test-model",
        **provider_config[provider],
    )


@pytest.mark.parametrize("provider", list(ModelProvider))
def test_factory_constructs_each_provider(monkeypatch: pytest.MonkeyPatch, provider: ModelProvider) -> None:
    module_name, class_name, _ = ChatModelFactory._provider_classes[provider]
    fake_module = SimpleNamespace(**{class_name: FakeChatModel})
    monkeypatch.setattr("agent.llm.import_module", lambda name: fake_module)

    model = ChatModelFactory.create(_settings(provider))

    assert model.kwargs.get("model", model.kwargs.get("azure_deployment")) == "test-model"
    assert model.kwargs["temperature"] == 0.0


def test_factory_reports_missing_optional_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_import(module_name: str) -> None:
        raise ImportError(module_name)

    monkeypatch.setattr("agent.llm.import_module", fail_import)

    with pytest.raises(ProviderDependencyError, match="Install the 'anthropic'"):
        ChatModelFactory.create(_settings(ModelProvider.ANTHROPIC))


@pytest.mark.parametrize("provider", list(ModelProvider))
def test_structured_output_uses_provider_capability(provider: ModelProvider) -> None:
    model = FakeChatModel()

    result = ChatModelFactory.with_structured_output(model, StructuredResponse, provider)

    assert result is model
    assert model.structured_call is not None
    _, options = model.structured_call
    expected_options = {"method": "json_schema"} if provider is ModelProvider.OLLAMA else {}
    assert options == expected_options