"""Contratos (puertos) del núcleo. Los adaptadores concretos los implementan.

El código de dominio y aplicación depende solo de estos protocolos, nunca de
FastAPI, MinIO, un proveedor LLM ni otra infraestructura concreta.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from kipu_knowledge.domain.extraction_models import ExtractionResult
from kipu_knowledge.domain.parsed import ParsedDocument


@dataclass(frozen=True)
class SourceReference:
    """Identificación de un dispositivo dentro de una fuente."""

    source_family: str  # "EL_PERUANO_NL"
    source_series: str  # "NL"
    publication_code: str  # "2540861-1"
    canonical_url: str | None = None


@dataclass(frozen=True)
class CaptureRecord:
    """Metadatos completos de una captura (antes de cualquier parsing)."""

    requested_url: str | None
    final_url: str | None
    http_status: int | None
    content_type: str | None
    byte_length: int
    captured_at: datetime
    etag: str | None = None
    last_modified: str | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    crawler_version: str = ""
    retries: int = 0
    error_summary: str | None = None


@dataclass(frozen=True)
class FetchResult:
    reference: SourceReference
    content: bytes
    capture: CaptureRecord


@dataclass(frozen=True)
class StoredObject:
    sha256: str
    object_key: str
    byte_length: int
    already_existed: bool


@runtime_checkable
class SourceAdapter(Protocol):
    """Adaptador de una fuente de publicaciones (p. ej. El Peruano)."""

    source_family: str

    def discover(self, publication_date: date) -> Iterable[SourceReference]:
        """Enumera dispositivos publicados en una fecha (interfaz preparada; el MVP
        puede devolver una colección vacía si la fuente no soporta descubrimiento)."""
        ...

    def fetch(self, reference: SourceReference) -> FetchResult: ...

    def parse_source_reference(self, url_or_code: str) -> SourceReference: ...


@runtime_checkable
class ArtifactStore(Protocol):
    """Almacenamiento inmutable direccionado por contenido (sha256/ab/cd/<hash>)."""

    def put_immutable(self, content: bytes) -> StoredObject: ...

    def get(self, object_key: str) -> bytes: ...

    def exists_by_hash(self, sha256: str) -> bool: ...


@runtime_checkable
class DocumentParser(Protocol):
    def parse(self, content: bytes, reference: SourceReference) -> ParsedDocument: ...


@runtime_checkable
class StructuredExtractor(Protocol):
    extractor_version: str

    def extract(self, document: ParsedDocument) -> ExtractionResult: ...


@dataclass(frozen=True)
class MatchProposal:
    """Propuesta de vinculación mención -> entidad canónica. Nunca es un auto-merge."""

    entity_id: str
    entity_label: str
    score: float
    rationale: str


@runtime_checkable
class EntityResolver(Protocol):
    def propose_matches(
        self, mention_text_normalized: str, context: dict[str, Any]
    ) -> Sequence[MatchProposal]: ...


@runtime_checkable
class AssertionRepository(Protocol):
    def save_candidates(self, assertion_ids: Sequence[str]) -> None: ...

    def accept(self, assertion_id: str, reviewer: str | None = None) -> None: ...

    def reject(self, assertion_id: str, reviewer: str | None = None) -> None: ...

    def supersede(self, assertion_id: str, replacement_id: str) -> None: ...


@runtime_checkable
class KnowledgeProjection(Protocol):
    def rebuild(self, publication_code: str | None = None) -> None: ...

    def export_rdf(self, publication_code: str, destination: Path | None = None) -> str: ...

    def export_jsonld(self, publication_code: str, destination: Path | None = None) -> str: ...


@runtime_checkable
class SearchBackend(Protocol):
    """Interfaz de búsqueda; el MVP usa PostgreSQL FTS, OpenSearch puede sustituirla."""

    def index_document(self, document_id: str) -> None: ...

    def search(self, query: str, limit: int = 20) -> Sequence[dict[str, Any]]: ...
