"""Configuración de la aplicación (variables de entorno / .env)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://kipu:kipu@localhost:5433/kipu"

    artifact_store: str = "fs"  # fs | minio
    artifact_fs_root: Path = Path("var/artifacts")

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "kipu"
    minio_secret_key: str = ""
    minio_bucket: str = "kipu-artifacts"
    minio_secure: bool = False

    live_source_enabled: bool = False
    crawler_user_agent: str = "KipuKnowledge/0.1 (+contacto: configurar-email-real)"
    crawler_rate_limit_seconds: float = 2.0
    crawler_max_retries: int = 3
    # Tope del recorrido del índice diario. No es una cuota de resultados: con 20
    # por página cubre ~500 dispositivos, muy por encima de una edición real. Está
    # para que un enlace de paginación cíclico no se convierta en un bucle contra
    # la fuente. Si se alcanza, el descubrimiento falla en vez de truncar.
    crawler_max_listing_pages: int = 25

    llm_extractor_enabled: bool = False
    llm_provider: str = ""
    llm_model: str = ""
    # Clave del proveedor LLM. Solo en .env (gitignored); jamás en el repo.
    anthropic_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
