from importlib import import_module
from typing import Any

from pydantic import BaseModel

from agent.settings import ModelProvider, Settings


class ProviderDependencyError(RuntimeError):
    pass


class ChatModelFactory:
    _provider_classes = {
        ModelProvider.OPENAI: ("langchain_openai", "ChatOpenAI", "openai"),
        ModelProvider.AZURE_OPENAI: (
            "langchain_openai",
            "AzureChatOpenAI",
            "azure",
        ),
        ModelProvider.GEMINI: (
            "langchain_google_genai",
            "ChatGoogleGenerativeAI",
            "gemini",
        ),
        ModelProvider.ANTHROPIC: (
            "langchain_anthropic",
            "ChatAnthropic",
            "anthropic",
        ),
        ModelProvider.OLLAMA: ("langchain_ollama", "ChatOllama", "ollama"),
    }

    @classmethod
    def create(cls, settings: Settings) -> Any:
        module_name, class_name, dependency_group = cls._provider_classes[
            settings.model_provider
        ]
        try:
            model_class = getattr(import_module(module_name), class_name)
        except (ImportError, AttributeError) as exc:
            raise ProviderDependencyError(
                f"Provider '{settings.model_provider.value}' is unavailable. "
                f"Install the '{dependency_group}' dependency group."
            ) from exc

        return model_class(**cls._model_arguments(settings))

    @staticmethod
    def with_structured_output(
        model: Any,
        schema: type[BaseModel],
        provider: ModelProvider,
    ) -> Any:
        if provider is ModelProvider.OLLAMA:
            return model.with_structured_output(schema, method="json_schema")
        return model.with_structured_output(schema)

    @staticmethod
    def _model_arguments(settings: Settings) -> dict[str, Any]:
        common = {
            "model": settings.model_name,
            "temperature": settings.model_temperature,
        }
        if settings.model_provider is ModelProvider.OPENAI:
            return {
                **common,
                "api_key": settings.openai_api_key,
                "base_url": settings.openai_base_url,
                "max_tokens": settings.model_max_tokens,
            }
        if settings.model_provider is ModelProvider.AZURE_OPENAI:
            return {
                "azure_deployment": settings.model_name,
                "api_key": settings.azure_openai_api_key,
                "azure_endpoint": settings.azure_openai_endpoint,
                "api_version": settings.azure_openai_api_version,
                "temperature": settings.model_temperature,
                "max_tokens": settings.model_max_tokens,
            }
        if settings.model_provider is ModelProvider.GEMINI:
            return {
                **common,
                "google_api_key": settings.google_api_key,
                "max_output_tokens": settings.model_max_tokens,
            }
        if settings.model_provider is ModelProvider.ANTHROPIC:
            return {
                **common,
                "api_key": settings.anthropic_api_key,
                "max_tokens": settings.model_max_tokens,
            }
        return {
            **common,
            "base_url": str(settings.ollama_base_url).rstrip("/"),
            "num_predict": settings.model_max_tokens,
        }