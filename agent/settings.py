from enum import StrEnum
from functools import lru_cache

from pydantic import AliasChoices, Field, HttpUrl, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelProvider(StrEnum):
    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="agent/.env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        protected_namespaces=("settings_",),
    )

    model_provider: ModelProvider = Field(
        validation_alias=AliasChoices("MODEL_PROVIDER", "AGENT_MODEL_PROVIDER")
    )
    model_name: str = Field(
        min_length=1,
        validation_alias=AliasChoices("MODEL_NAME", "AGENT_MODEL_NAME"),
    )
    model_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    model_max_tokens: int = Field(default=4096, ge=1)

    openai_api_key: SecretStr | None = None
    openai_base_url: HttpUrl | None = None

    azure_openai_api_key: SecretStr | None = None
    azure_openai_endpoint: HttpUrl | None = None
    azure_openai_api_version: str = "2024-10-21"

    google_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None

    ollama_base_url: HttpUrl = HttpUrl("http://localhost:11434")

    @model_validator(mode="after")
    def validate_selected_provider(self) -> "Settings":
        required_fields = {
            ModelProvider.OPENAI: ("openai_api_key",),
            ModelProvider.AZURE_OPENAI: (
                "azure_openai_api_key",
                "azure_openai_endpoint",
            ),
            ModelProvider.GEMINI: ("google_api_key",),
            ModelProvider.ANTHROPIC: ("anthropic_api_key",),
            ModelProvider.OLLAMA: (),
        }
        missing = [
            field_name
            for field_name in   [self.model_provider]
            if getattr(self, field_name) is None
        ]
        if missing:
            names = ", ".join(missing)
            raise ValueError(
                f"Missing configuration for {self.model_provider.value}: {names}"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()