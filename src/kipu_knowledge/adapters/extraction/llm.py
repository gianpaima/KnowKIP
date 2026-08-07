"""Interfaz para un extractor LLM opcional. Desactivado por defecto.

Requisitos si se habilita (docs/adr/0005 y sección de extracción):
- La salida DEBE validar contra ExtractionResult (Pydantic / JSON Schema).
- Se registran proveedor, modelo, versión de prompt y parámetros en ExtractionRun.
- Ningún hecho sin evidence span; el modelo debe abstenerse si falta evidencia.
- El resultado se guarda siempre como CANDIDATE; identidades, fechas inferidas y
  conflictos requieren revisión humana.

El MVP no incluye implementación de proveedor: esta clase falla explícitamente
si se activa sin configurar, y sirve como punto de extensión tipado.
"""

from __future__ import annotations

from kipu_knowledge.config import get_settings
from kipu_knowledge.domain.extraction_models import ExtractionResult
from kipu_knowledge.domain.parsed import ParsedDocument


class LlmExtractorNotConfigured(RuntimeError):
    pass


class LlmExtractor:
    """Punto de extensión: implementar `_call_provider` en una subclase concreta."""

    extractor_version = "extractor-llm/0.0-disabled"

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.llm_extractor_enabled:
            raise LlmExtractorNotConfigured(
                "LLM_EXTRACTOR_ENABLED=false: el extractor LLM está desactivado por defecto"
            )
        if not settings.llm_provider or not settings.llm_model:
            raise LlmExtractorNotConfigured(
                "Configura LLM_PROVIDER y LLM_MODEL para habilitar el extractor LLM"
            )
        self.provider = settings.llm_provider
        self.model = settings.llm_model

    def extract(self, document: ParsedDocument) -> ExtractionResult:
        raise NotImplementedError(
            "No hay proveedor LLM implementado en el MVP; ver docs/architecture.md"
        )
