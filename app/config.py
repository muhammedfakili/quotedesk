"""Application settings, loaded from environment / .env file."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "fixtures"
DATA_DIR = PROJECT_ROOT / "data"

# CrewAI sends anonymous telemetry and offers cloud tracing by default. A back-office
# tool that reads customer emails should not phone home, so both are off before
# crewai is imported anywhere.
for _var, _value in (("CREWAI_DISABLE_TELEMETRY", "true"), ("OTEL_SDK_DISABLED", "true"),
                     ("CREWAI_TRACING_ENABLED", "false")):
    os.environ.setdefault(_var, _value)

# Gemini Flash-Lite is free (about 500 requests a day) and fast; Claude is the paid
# upgrade; NVIDIA's hosted models are free for development.
DEFAULT_MODELS = {
    "gemini": "gemini-3.5-flash-lite",
    "anthropic": "claude-sonnet-5-5",
    "nvidia": "nvidia/nemotron-3-super-120b-a12b",
}

Provider = Literal["gemini", "anthropic", "nvidia"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    llm_provider: Provider = "gemini"
    google_api_key: str | None = None
    anthropic_api_key: str | None = None
    nvidia_api_key: str | None = None
    # Leave unset to take the default model for the selected provider.
    llm_model: str | None = None

    # Protects the HTTP API. Unset = open (fine on localhost; set it before exposing the API).
    app_api_key: str | None = None

    # Where the runtime state lives: SQLite database, mock inbox/outbox folders, quote PDFs.
    data_dir: Path = DATA_DIR
    # Embedding model for semantic search (local ONNX, ~65 MB, downloaded once) and where it and the
    # catalogue's embeddings are cached. Separate from data_dir so a Docker image can ship them.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    model_cache: Path = DATA_DIR / "models"

    # Our own mailbox: replies go out from here, and it is never a valid recipient.
    company_email: str = "quotes@northgate-supply.example"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "quotedesk.db"

    @property
    def catalogue_db(self) -> Path:
        return self.model_cache / "catalogue-embeddings.db"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    @property
    def quotes_dir(self) -> Path:
        return self.data_dir / "quotes"

    @property
    def models_dir(self) -> Path:
        return self.model_cache

    def api_key(self, provider: Provider) -> str | None:
        return {
            "gemini": self.google_api_key,
            "anthropic": self.anthropic_api_key,
            "nvidia": self.nvidia_api_key,
        }[provider]

    @property
    def llm_model_resolved(self) -> str:
        """`llm_model` if set, else the default model for the provider."""
        return self.llm_model or DEFAULT_MODELS[self.llm_provider]


settings = Settings()
