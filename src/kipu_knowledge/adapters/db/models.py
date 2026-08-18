"""Modelo relacional canónico (PostgreSQL como fuente operativa de verdad).

Convenciones:
- PK: UUID4 en texto (portable entre PostgreSQL y SQLite para pruebas).
- Enums como VARCHAR con CHECK (native_enum=False) para portabilidad y migraciones simples.
- Temporalidad doble: validez del hecho (valid_from/valid_to) y registro del sistema
  (recorded_at/superseded_at). Las correcciones se registran con supersede, nunca
  sobrescribiendo (regla de inmutabilidad).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from kipu_knowledge.domain import enums as e


def _uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


def _enum(enum_cls: type, name: str) -> SAEnum:
    return SAEnum(enum_cls, name=name, native_enum=False, length=48, validate_strings=True)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON}


class PKMixin:
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)


# ---------------------------------------------------------------------------
# Fuente, corridas de captura y publicaciones
# ---------------------------------------------------------------------------


class SourceSystem(PKMixin, Base):
    __tablename__ = "source_system"

    name: Mapped[str] = mapped_column(String(200), unique=True)
    base_url: Mapped[str | None] = mapped_column(String(500))
    source_family: Mapped[str] = mapped_column(String(100), index=True)
    policy_status: Mapped[str] = mapped_column(String(50), default="DOCUMENTED")
    authority: Mapped[e.SourceAuthority] = mapped_column(
        _enum(e.SourceAuthority, "source_authority"), default=e.SourceAuthority.MIRROR
    )


class CrawlRun(PKMixin, Base):
    __tablename__ = "crawl_run"

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="RUNNING")
    crawler_version: Mapped[str] = mapped_column(String(100))
    parameters: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_summary: Mapped[str | None] = mapped_column(Text)


class CrawlItem(PKMixin, Base):
    """Un dispositivo visto en el índice de una fecha, y qué se hizo con él.

    Existe para que el descubrimiento automático no tenga zonas mudas. Sin esta
    tabla, "el sistema ingirió 19 de 32" es indistinguible de "ese día se
    publicaron 19": lo que no se ingiere no deja rastro en ninguna otra parte
    del modelo. Aquí queda cada código con lo que el catálogo declaraba, el
    veredicto de relevancia con su regla, el estado final y —si falló— el error
    literal, además del artefacto del listado cuyos bytes lo declararon.

    No es evidencia de ningún hecho del documento: la sumilla la escribe el
    buscador, no la norma. Es la bitácora del recolector.
    """

    __tablename__ = "crawl_item"
    __table_args__ = (
        UniqueConstraint("crawl_run_id", "source_series", "publication_code"),
        Index("ix_crawl_item_status", "status"),
    )

    crawl_run_id: Mapped[str] = mapped_column(ForeignKey("crawl_run.id"), index=True)
    source_series: Mapped[str] = mapped_column(String(20))
    publication_code: Mapped[str] = mapped_column(String(50), index=True)
    canonical_url: Mapped[str | None] = mapped_column(String(500))
    # Bytes del listado que declararon este dispositivo: la captura del índice es
    # la constancia de qué dijo la fuente que se publicó ese día.
    listing_artifact_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_version.id")
    )
    issuer_raw: Mapped[str | None] = mapped_column(String(500))
    document_type_raw: Mapped[str | None] = mapped_column(String(200))
    number_raw: Mapped[str | None] = mapped_column(String(200))
    summary_raw: Mapped[str | None] = mapped_column(Text)
    listed_date_raw: Mapped[str | None] = mapped_column(String(100))
    relevance: Mapped[e.Relevance] = mapped_column(
        _enum(e.Relevance, "relevance"), default=e.Relevance.UNDECIDED
    )
    relevance_rule: Mapped[str | None] = mapped_column(String(100))
    relevance_rationale: Mapped[str | None] = mapped_column(Text)
    status: Mapped[e.CrawlItemStatus] = mapped_column(
        _enum(e.CrawlItemStatus, "crawl_item_status"), default=e.CrawlItemStatus.DISCOVERED
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    # Qué salió de procesarlo, en texto ("eventos=1 asignaciones=1 tareas=0; PDF …").
    outcome_detail: Mapped[str | None] = mapped_column(Text)
    # Eventos de personal extraídos. Cero en un dispositivo que el filtro llamó
    # relevante señala un hueco del extractor, no un documento vacío: el
    # documento está capturado y su texto íntegro, pero nada se afirmó de él.
    # Sin esta cuenta ese hueco no se ve desde ninguna parte.
    events_extracted: Mapped[int | None] = mapped_column(Integer)
    publication_item_id: Mapped[str | None] = mapped_column(ForeignKey("publication_item.id"))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PublicationIssue(PKMixin, Base):
    __tablename__ = "publication_issue"
    __table_args__ = (UniqueConstraint("source_system_id", "issue_code"),)

    source_system_id: Mapped[str] = mapped_column(ForeignKey("source_system.id"), index=True)
    family: Mapped[str] = mapped_column(String(100))
    issue_code: Mapped[str] = mapped_column(String(100))  # p.ej. "NL20260806"
    publication_date: Mapped[date | None] = mapped_column(Date, index=True)
    ordinary_or_extraordinary: Mapped[str | None] = mapped_column(String(30))


class PublicationItem(PKMixin, Base):
    """Un acto tal como lo publica UN sistema fuente concreto.

    La unicidad incluye el sistema fuente porque el mismo acto aparece en varios
    sitios con identificadores distintos: El Peruano lo llama 2540861-1 y el
    portal del ministerio 8450966. Cada uno es una publicación propia, con su
    landing, su PDF y sus capturas; lo que las une es el `LegalDocument`, vía
    `document_source`.
    """

    __tablename__ = "publication_item"
    __table_args__ = (UniqueConstraint("source_system_id", "source_series", "publication_code"),)

    source_system_id: Mapped[str | None] = mapped_column(ForeignKey("source_system.id"), index=True)
    issue_id: Mapped[str | None] = mapped_column(ForeignKey("publication_issue.id"))
    source_series: Mapped[str] = mapped_column(String(20))
    publication_code: Mapped[str] = mapped_column(String(50), index=True)
    canonical_url: Mapped[str | None] = mapped_column(String(500))
    # URL del ARCHIVO PDF, la que declara el payload de la captura. Puntero a la
    # fuente, no evidencia: la evidencia es el artefacto del CAS. Ojo: la ruta
    # derivable …/<código>/pdf NO sirve aquí, devuelve el visor HTML.
    pdf_url: Mapped[str | None] = mapped_column(String(500))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    artifacts: Mapped[list[Artifact]] = relationship(back_populates="publication_item")


# ---------------------------------------------------------------------------
# Artefactos: evidencia binaria inmutable
# ---------------------------------------------------------------------------


class Artifact(PKMixin, Base):
    __tablename__ = "artifact"
    __table_args__ = (UniqueConstraint("publication_item_id", "representation_type"),)

    publication_item_id: Mapped[str] = mapped_column(ForeignKey("publication_item.id"), index=True)
    representation_type: Mapped[e.RepresentationType] = mapped_column(
        _enum(e.RepresentationType, "representation_type")
    )
    media_type: Mapped[str | None] = mapped_column(String(100))

    publication_item: Mapped[PublicationItem] = relationship(back_populates="artifacts")
    versions: Mapped[list[ArtifactVersion]] = relationship(back_populates="artifact")


class ArtifactVersion(PKMixin, Base):
    __tablename__ = "artifact_version"
    __table_args__ = (
        UniqueConstraint("artifact_id", "sha256"),
        CheckConstraint("length(sha256) = 64", name="ck_artifact_version_sha256_len"),
    )

    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifact.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    byte_length: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    http_status: Mapped[int | None] = mapped_column(Integer)
    etag: Mapped[str | None] = mapped_column(String(300))
    last_modified: Mapped[str | None] = mapped_column(String(100))
    response_headers: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    requested_url: Mapped[str | None] = mapped_column(String(500))
    final_url: Mapped[str | None] = mapped_column(String(500))
    object_key: Mapped[str] = mapped_column(String(300), nullable=False)
    crawler_version: Mapped[str | None] = mapped_column(String(100))
    previous_version_id: Mapped[str | None] = mapped_column(ForeignKey("artifact_version.id"))

    artifact: Mapped[Artifact] = relationship(back_populates="versions")


# ---------------------------------------------------------------------------
# Documento legal y su estructura
# ---------------------------------------------------------------------------


class LegalDocument(PKMixin, Base):
    __tablename__ = "legal_document"

    publication_item_id: Mapped[str] = mapped_column(ForeignKey("publication_item.id"), index=True)
    document_type_raw: Mapped[str] = mapped_column(String(200))
    document_type_code: Mapped[e.DocumentTypeCode] = mapped_column(
        _enum(e.DocumentTypeCode, "document_type_code")
    )
    number_raw: Mapped[str] = mapped_column(String(200))
    number_normalized: Mapped[str] = mapped_column(String(200), index=True)
    title_raw: Mapped[str] = mapped_column(Text)
    issue_place_raw: Mapped[str | None] = mapped_column(String(200))
    issued_on: Mapped[date | None] = mapped_column(Date)
    published_on: Mapped[date | None] = mapped_column(Date, index=True)
    issuer_mention_id: Mapped[str | None] = mapped_column(
        ForeignKey("organization_mention.id", use_alter=True)
    )
    parsed_from_artifact_version_id: Mapped[str] = mapped_column(ForeignKey("artifact_version.id"))

    sections: Mapped[list[DocumentSection]] = relationship(
        back_populates="legal_document", order_by="DocumentSection.order_index"
    )


class DocumentSource(PKMixin, Base):
    """Dónde más aparece publicado este mismo acto.

    `legal_document.publication_item_id` sigue apuntando a la publicación de la
    que se extrajo, que es siempre la autoritativa. Esta tabla añade las demás
    sin duplicar el documento: dos publicaciones del mismo acto son un solo
    LegalDocument, o el grafo acabaría afirmando dos designaciones donde hubo una.

    `matched_by` deja escrito por qué se afirma que son el mismo acto. Hoy solo
    se registra por decisión explícita de un operador (`kipu link-source`): no
    hay emparejamiento automático entre fuentes, y no lo habrá sin evidencia que
    lo respalde.
    """

    __tablename__ = "document_source"
    __table_args__ = (UniqueConstraint("legal_document_id", "publication_item_id"),)

    legal_document_id: Mapped[str] = mapped_column(ForeignKey("legal_document.id"), index=True)
    publication_item_id: Mapped[str] = mapped_column(ForeignKey("publication_item.id"), index=True)
    role: Mapped[e.DocumentSourceRole] = mapped_column(
        _enum(e.DocumentSourceRole, "document_source_role")
    )
    matched_by: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocumentSection(PKMixin, Base):
    __tablename__ = "document_section"
    __table_args__ = (
        UniqueConstraint("legal_document_id", "order_index"),
        Index("ix_document_section_type", "legal_document_id", "section_type"),
    )

    legal_document_id: Mapped[str] = mapped_column(ForeignKey("legal_document.id"), index=True)
    section_type: Mapped[e.SectionType] = mapped_column(_enum(e.SectionType, "section_type"))
    label_raw: Mapped[str | None] = mapped_column(String(200))
    order_index: Mapped[int] = mapped_column(Integer)
    text_raw: Mapped[str] = mapped_column(Text)
    text_normalized: Mapped[str] = mapped_column(Text)
    page_from: Mapped[int | None] = mapped_column(Integer)
    page_to: Mapped[int | None] = mapped_column(Integer)

    legal_document: Mapped[LegalDocument] = relationship(back_populates="sections")


class EvidenceSpan(PKMixin, Base):
    __tablename__ = "evidence_span"
    __table_args__ = (
        CheckConstraint("length(quoted_text) > 0", name="ck_evidence_span_quote_nonempty"),
    )

    document_section_id: Mapped[str | None] = mapped_column(ForeignKey("document_section.id"))
    artifact_version_id: Mapped[str] = mapped_column(ForeignKey("artifact_version.id"), index=True)
    article_label: Mapped[str | None] = mapped_column(String(200))
    page_number: Mapped[int | None] = mapped_column(Integer)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    quoted_text: Mapped[str] = mapped_column(Text, nullable=False)
    quoted_text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    locator_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class DocumentReference(PKMixin, Base):
    __tablename__ = "document_reference"

    source_document_id: Mapped[str] = mapped_column(ForeignKey("legal_document.id"), index=True)
    reference_type: Mapped[e.ReferenceType] = mapped_column(
        _enum(e.ReferenceType, "reference_type")
    )
    target_document_id: Mapped[str | None] = mapped_column(ForeignKey("legal_document.id"))
    target_number_raw: Mapped[str] = mapped_column(String(300))
    target_doc_kind_raw: Mapped[str | None] = mapped_column(String(200))
    evidence_span_id: Mapped[str] = mapped_column(ForeignKey("evidence_span.id"))


# ---------------------------------------------------------------------------
# Corridas de extracción y afirmaciones con procedencia
# ---------------------------------------------------------------------------


class ExtractionRun(PKMixin, Base):
    __tablename__ = "extraction_run"

    artifact_version_id: Mapped[str] = mapped_column(ForeignKey("artifact_version.id"), index=True)
    parser_version: Mapped[str] = mapped_column(String(100))
    extractor_version: Mapped[str] = mapped_column(String(100))
    ontology_version: Mapped[str] = mapped_column(String(50))
    model_provider: Mapped[str | None] = mapped_column(String(100))
    model_name: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[e.ExtractionStatus] = mapped_column(
        _enum(e.ExtractionStatus, "extraction_status"), default=e.ExtractionStatus.RUNNING
    )


class Assertion(PKMixin, Base):
    __tablename__ = "assertion"
    __table_args__ = (
        # Regla 9 reforzada: ninguna afirmación (ni siquiera candidata) sin evidencia.
        CheckConstraint("evidence_span_id IS NOT NULL", name="ck_assertion_requires_evidence"),
        Index("ix_assertion_subject", "subject_type", "subject_id"),
    )

    extraction_run_id: Mapped[str] = mapped_column(ForeignKey("extraction_run.id"), index=True)
    subject_type: Mapped[str] = mapped_column(String(50))
    subject_id: Mapped[str | None] = mapped_column(String(32))
    predicate: Mapped[str] = mapped_column(String(100), index=True)
    object_type: Mapped[str | None] = mapped_column(String(50))
    object_id: Mapped[str | None] = mapped_column(String(32))
    object_value_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    confidence: Mapped[float] = mapped_column(Float)
    evidence_span_id: Mapped[str] = mapped_column(ForeignKey("evidence_span.id"), nullable=False)
    review_status: Mapped[e.ReviewStatus] = mapped_column(
        _enum(e.ReviewStatus, "review_status"), default=e.ReviewStatus.CANDIDATE, index=True
    )
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_id: Mapped[str | None] = mapped_column(ForeignKey("assertion.id"))


# ---------------------------------------------------------------------------
# Menciones y entidades canónicas
# ---------------------------------------------------------------------------


class Person(PKMixin, Base):
    __tablename__ = "person"

    preferred_name: Mapped[str] = mapped_column(String(300), index=True)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE")
    # Duplicado absorbido por otra persona tras revisión humana. La fila nunca se
    # borra (regla 3): queda como registro de que ese identificador existió.
    merged_into_person_id: Mapped[str | None] = mapped_column(ForeignKey("person.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PersonMention(PKMixin, Base):
    __tablename__ = "person_mention"

    legal_document_id: Mapped[str] = mapped_column(ForeignKey("legal_document.id"), index=True)
    text_raw: Mapped[str] = mapped_column(String(400))
    text_normalized: Mapped[str] = mapped_column(String(400), index=True)
    # Cargo declarado junto al nombre en el documento (p.ej. la capacidad con que
    # firma). Señal corroborante independiente del nombre para la resolución de
    # identidad; NULL cuando el documento no la expresa.
    # 1000 y no 400: el contexto de rol conserva lo que la fuente escribió, y una
    # celda con notas al pie legítimas superaba los 400 caracteres y abortaba la
    # ingesta del documento entero (RM 197-2026-PCM, 2541397-1). Ancho de sobra
    # convierte ese fallo fatal en una mención registrada que la revisión ve.
    role_context_raw: Mapped[str | None] = mapped_column(String(1000))
    role_context_normalized: Mapped[str | None] = mapped_column(String(1000), index=True)
    evidence_span_id: Mapped[str] = mapped_column(ForeignKey("evidence_span.id"))
    canonical_person_id: Mapped[str | None] = mapped_column(ForeignKey("person.id"), index=True)
    resolution_status: Mapped[e.ResolutionStatus] = mapped_column(
        _enum(e.ResolutionStatus, "resolution_status"), default=e.ResolutionStatus.UNRESOLVED
    )
    # Precedente que produjo la vinculación, cuando resolution_status es
    # PRECEDENT_LINKED: la cadena mención → precedente → decisión humana es auditable.
    # Sin FK declarada: identity_precedent apunta de vuelta a person_mention y el
    # ciclo impediría el CREATE TABLE ordenado en SQLite.
    identity_precedent_id: Mapped[str | None] = mapped_column(String(32), index=True)


class PersonIdentifier(PKMixin, Base):
    """Identificador de persona tal como lo declara un documento.

    A diferencia del nombre, un DNI o carné de extranjería identifica de forma
    unívoca: dos menciones que declaran el mismo identificador son la misma
    persona por afirmación de la fuente, no por inferencia del sistema. Va anclado
    a la mención y a su EvidenceSpan, nunca a la persona canónica directamente:
    lo que consta es que *este documento* lo afirmó.
    """

    __tablename__ = "person_identifier"
    __table_args__ = (
        UniqueConstraint("person_mention_id", "scheme", "value_normalized"),
        Index("ix_person_identifier_lookup", "scheme", "value_normalized"),
    )

    person_mention_id: Mapped[str] = mapped_column(ForeignKey("person_mention.id"), index=True)
    scheme: Mapped[e.IdentifierScheme] = mapped_column(
        _enum(e.IdentifierScheme, "identifier_scheme")
    )
    value_raw: Mapped[str] = mapped_column(String(60))
    value_normalized: Mapped[str] = mapped_column(String(60))
    evidence_span_id: Mapped[str] = mapped_column(ForeignKey("evidence_span.id"))


class IdentityPrecedent(PKMixin, Base):
    """Decisión humana de identidad reutilizable en ingestas posteriores.

    Materializa una resolución ya tomada por un revisor. Tiene dos alcances:

    - Por cargo (``role_context`` con valor): "el nombre N, apareciendo con el
      cargo C, es la persona P". Es el alcance por defecto y el más conservador,
      porque el cargo aporta una segunda señal además del nombre.
    - Alias (``role_context`` NULL): "la grafía N es la persona P, aparezca con
      el cargo que aparezca". Sirve para el caso corriente de un mismo nombre
      escrito con y sin segundo nombre o con inicial ("ELMER CUBA BUSTINZA"
      frente a "ELMER RAFAEL CUBA BUSTINZA"), donde atar la decisión a un cargo
      obligaría a re-decidir cada vez que la persona cambia de puesto.

    Ninguno debilita la regla 13, que prohíbe que *el sistema* infiera identidad
    por parecido de nombres: aquí la afirmación la aporta un humano y queda
    trazable hasta la ReviewDecision que la originó. El alias exige además que
    la grafía sea discriminante —que no corresponda ya a más de una persona—
    para no vincular homónimos futuros en silencio (ver ReviewService). Ambos
    alcances pueden revocarse, tras lo cual dejan de aplicar.
    """

    __tablename__ = "identity_precedent"
    __table_args__ = (Index("ix_identity_precedent_key", "name_normalized", "role_context"),)

    subject_type: Mapped[str] = mapped_column(String(30), default="person")
    name_normalized: Mapped[str] = mapped_column(String(400))
    # NULL = alias sin restricción de contexto (ver docstring).
    role_context: Mapped[str | None] = mapped_column(String(400))
    person_id: Mapped[str] = mapped_column(ForeignKey("person.id"), index=True)
    # Cuál fue la mención que motivó la decisión. Anulable porque re-extraer el
    # documento retira sus menciones y crea otras: el reproceso reapunta este
    # campo a la equivalente, y si ya no existe lo deja NULL y abre revisión.
    # Lo que el precedente afirma no depende de este puntero (ver docstring).
    source_person_mention_id: Mapped[str | None] = mapped_column(ForeignKey("person_mention.id"))
    review_decision_id: Mapped[str] = mapped_column(ForeignKey("review_decision.id"), index=True)
    reviewer: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(Text)


class Organization(PKMixin, Base):
    __tablename__ = "organization"

    preferred_name: Mapped[str] = mapped_column(String(400), index=True)
    name_normalized: Mapped[str] = mapped_column(String(400), index=True)
    acronym: Mapped[str | None] = mapped_column(String(50))
    organization_type: Mapped[str] = mapped_column(String(50), default="PUBLIC_ENTITY")
    public_private_category: Mapped[str] = mapped_column(String(30), default="PUBLIC")
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    # Duplicado absorbido por otra organización tras revisión humana (espejo de
    # `Person.merged_into_person_id`). La fila nunca se borra (regla 3): queda
    # apuntando a la superviviente para que su identificador siga resolviendo.
    merged_into_organization_id: Mapped[str | None] = mapped_column(ForeignKey("organization.id"))
    # Entidad de la que esta organización depende (un programa nacional adscrito
    # a su ministerio). Solo lo escribe la adscripción curada del catálogo
    # (domain/state_entities.py) o una decisión humana; nunca se infiere del texto.
    parent_organization_id: Mapped[str | None] = mapped_column(ForeignKey("organization.id"))
    # Ficha del nombre ANTERIOR de la misma cartera (MIDAGRI → MINAGRI →
    # Ministerio de Agricultura): la sucesión que "¿cuántos ministros tuvo X?"
    # necesita recorrer sin colapsar las épocas. Distinta de la fusión (misma
    # entidad, grafía duplicada) y de la adscripción. Solo la escribe la
    # sincronización del catálogo o una decisión humana; nunca se infiere.
    predecessor_organization_id: Mapped[str | None] = mapped_column(ForeignKey("organization.id"))


class OrganizationMention(PKMixin, Base):
    __tablename__ = "organization_mention"

    legal_document_id: Mapped[str | None] = mapped_column(ForeignKey("legal_document.id"))
    text_raw: Mapped[str] = mapped_column(String(500))
    text_normalized: Mapped[str] = mapped_column(String(500), index=True)
    canonical_organization_id: Mapped[str | None] = mapped_column(ForeignKey("organization.id"))
    resolution_status: Mapped[e.ResolutionStatus] = mapped_column(
        _enum(e.ResolutionStatus, "resolution_status"), default=e.ResolutionStatus.UNRESOLVED
    )
    evidence_span_id: Mapped[str | None] = mapped_column(ForeignKey("evidence_span.id"))


class OrganizationalUnit(PKMixin, Base):
    __tablename__ = "organizational_unit"
    __table_args__ = (UniqueConstraint("organization_id", "parent_unit_id", "name_normalized"),)

    organization_id: Mapped[str] = mapped_column(ForeignKey("organization.id"), index=True)
    parent_unit_id: Mapped[str | None] = mapped_column(ForeignKey("organizational_unit.id"))
    preferred_name: Mapped[str] = mapped_column(String(400))
    name_normalized: Mapped[str] = mapped_column(String(400), index=True)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)


class Position(PKMixin, Base):
    __tablename__ = "position"
    __table_args__ = (
        Index(
            "ix_position_identity", "organization_id", "organizational_unit_id", "label_normalized"
        ),
    )

    # organization_id es nullable a propósito: hay puestos cuyo órgano no puede
    # determinarse de forma segura desde el texto (p.ej. Superintendente SUNAT);
    # se conserva la ruta cruda y se crea una tarea de revisión (decisión conservadora).
    organization_id: Mapped[str | None] = mapped_column(ForeignKey("organization.id"), index=True)
    organizational_unit_id: Mapped[str | None] = mapped_column(ForeignKey("organizational_unit.id"))
    preferred_label: Mapped[str] = mapped_column(String(500))
    label_normalized: Mapped[str] = mapped_column(String(500), index=True)
    position_type: Mapped[str] = mapped_column(String(50), default="UNKNOWN")
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)


class PositionSlot(PKMixin, Base):
    __tablename__ = "position_slot"
    __table_args__ = (UniqueConstraint("position_id", "external_scheme", "external_code"),)

    position_id: Mapped[str] = mapped_column(ForeignKey("position.id"), index=True)
    external_scheme: Mapped[str] = mapped_column(String(100))  # "CAP_PROVISIONAL"
    external_code: Mapped[str] = mapped_column(String(100))  # "007"
    source_document_id: Mapped[str | None] = mapped_column(ForeignKey("legal_document.id"))


# ---------------------------------------------------------------------------
# Eventos de personal, participantes, asignaciones y mandatos
# ---------------------------------------------------------------------------


class Mandate(PKMixin, Base):
    __tablename__ = "mandate"

    mandate_type: Mapped[e.MandateType] = mapped_column(_enum(e.MandateType, "mandate_type"))
    label: Mapped[str] = mapped_column(String(400))
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    end_condition_text: Mapped[str | None] = mapped_column(Text)


class PersonnelEvent(PKMixin, Base):
    __tablename__ = "personnel_event"

    legal_document_id: Mapped[str] = mapped_column(ForeignKey("legal_document.id"), index=True)
    event_type: Mapped[e.EventType] = mapped_column(_enum(e.EventType, "event_type"), index=True)
    assignment_effect: Mapped[e.AssignmentEffect] = mapped_column(
        _enum(e.AssignmentEffect, "assignment_effect")
    )
    legal_verb_raw: Mapped[str] = mapped_column(String(200))
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_from_status: Mapped[e.DateStatus] = mapped_column(
        _enum(e.DateStatus, "date_status"), default=e.DateStatus.NOT_STATED
    )
    effective_to: Mapped[date | None] = mapped_column(Date)
    effective_to_status: Mapped[e.DateStatus] = mapped_column(
        _enum(e.DateStatus, "date_status_to"), default=e.DateStatus.NOT_STATED
    )
    end_condition_text: Mapped[str | None] = mapped_column(Text)
    collective_event_id: Mapped[str | None] = mapped_column(ForeignKey("personnel_event.id"))
    evidence_span_id: Mapped[str] = mapped_column(ForeignKey("evidence_span.id"), nullable=False)
    # Fecha en que el acto produce efectos jurídicos cuando el documento no la
    # expresa pero una norma la fija (ver domain/legal_effect.py). Va aparte de
    # effective_from a propósito: aquella dice lo que el documento dice —y sigue
    # NOT_STATED—, esta dice lo que la ley determina, con su fundamento citado.
    legal_effect_from: Mapped[date | None] = mapped_column(Date)
    legal_effect_basis_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class EventParticipant(PKMixin, Base):
    __tablename__ = "event_participant"

    event_id: Mapped[str] = mapped_column(ForeignKey("personnel_event.id"), index=True)
    participant_type: Mapped[str] = mapped_column(String(30))  # PERSON | ORGANIZATION
    person_mention_id: Mapped[str | None] = mapped_column(ForeignKey("person_mention.id"))
    organization_mention_id: Mapped[str | None] = mapped_column(
        ForeignKey("organization_mention.id")
    )
    role_in_event: Mapped[e.ParticipantRole] = mapped_column(
        _enum(e.ParticipantRole, "participant_role")
    )
    confidence: Mapped[float] = mapped_column(Float, default=1.0)


class RoleAssignment(PKMixin, Base):
    __tablename__ = "role_assignment"

    person_id: Mapped[str | None] = mapped_column(ForeignKey("person.id"), index=True)
    person_mention_id: Mapped[str] = mapped_column(ForeignKey("person_mention.id"))
    position_id: Mapped[str | None] = mapped_column(ForeignKey("position.id"), index=True)
    position_label_raw: Mapped[str | None] = mapped_column(String(600))
    organization_id: Mapped[str | None] = mapped_column(ForeignKey("organization.id"))
    organization_path_raw: Mapped[str | None] = mapped_column(Text)
    assignment_kind: Mapped[e.AssignmentKind] = mapped_column(
        _enum(e.AssignmentKind, "assignment_kind"), default=e.AssignmentKind.UNKNOWN
    )
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_from_status: Mapped[e.DateStatus] = mapped_column(
        _enum(e.DateStatus, "assignment_from_status"), default=e.DateStatus.NOT_STATED
    )
    valid_to: Mapped[date | None] = mapped_column(Date)
    valid_to_status: Mapped[e.DateStatus] = mapped_column(
        _enum(e.DateStatus, "assignment_to_status"), default=e.DateStatus.NOT_STATED
    )
    end_condition_text: Mapped[str | None] = mapped_column(Text)
    # Proyección en la asignación de la fecha que la norma determina para el
    # evento que la inicia o la concluye. Solo se rellena cuando la fuente no
    # expresó la fecha correspondiente; nunca sustituye a una declarada.
    legal_effect_from: Mapped[date | None] = mapped_column(Date)
    legal_effect_to: Mapped[date | None] = mapped_column(Date)
    start_event_id: Mapped[str | None] = mapped_column(ForeignKey("personnel_event.id"))
    end_event_id: Mapped[str | None] = mapped_column(ForeignKey("personnel_event.id"))
    mandate_id: Mapped[str | None] = mapped_column(ForeignKey("mandate.id"))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Signatory(PKMixin, Base):
    __tablename__ = "signatory"
    __table_args__ = (UniqueConstraint("legal_document_id", "signature_order"),)

    legal_document_id: Mapped[str] = mapped_column(ForeignKey("legal_document.id"), index=True)
    person_mention_id: Mapped[str] = mapped_column(ForeignKey("person_mention.id"))
    capacity_raw: Mapped[str | None] = mapped_column(String(400))
    organization_mention_id: Mapped[str | None] = mapped_column(
        ForeignKey("organization_mention.id")
    )
    signature_order: Mapped[int] = mapped_column(Integer)


# ---------------------------------------------------------------------------
# Contexto web atribuido: prensa, redes sociales y otras fuentes
# (docs/web-context-design.md). Capa separada del registro funcional: nada de
# aquí crea ni modifica eventos, asignaciones ni fechas de efectos.
# ---------------------------------------------------------------------------


class WebDocument(PKMixin, Base):
    """Una página web de contexto según su propia captura.

    Análogo de `legal_document` para fuentes sin peso jurídico: cuelga de un
    `publication_item` (serie WEB, código derivado de la URL canónica) cuyo
    `source_system` tiene autoridad PRESS/SOCIAL_MEDIA/OTHER_WEB, y todos sus
    metadatos salen de los bytes capturados (JSON-LD schema.org, OpenGraph),
    nunca de lo que el buscador o el extractor crean saber.
    """

    __tablename__ = "web_document"

    publication_item_id: Mapped[str] = mapped_column(
        ForeignKey("publication_item.id"), unique=True, index=True
    )
    kind: Mapped[e.WebDocumentKind] = mapped_column(_enum(e.WebDocumentKind, "web_document_kind"))
    # Nulo en posts y perfiles: una publicación social no tiene titular y
    # fabricarle uno sería contenido nuestro, no de la fuente.
    headline_raw: Mapped[str | None] = mapped_column(String(1000))
    published_at_raw: Mapped[str | None] = mapped_column(String(100))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    modified_at_raw: Mapped[str | None] = mapped_column(String(100))
    author_raw: Mapped[str | None] = mapped_column(String(400))
    # Handle de la cuenta que publica, en posts y perfiles. Distinto del autor:
    # la cuenta es atribución del documento; el byline, un dato dentro de él.
    account_raw: Mapped[str | None] = mapped_column(String(300))
    section_raw: Mapped[str | None] = mapped_column(String(200))
    language: Mapped[str | None] = mapped_column(String(10))
    # Cuánto del cuerpo entregó el servidor (muros de pago): el sistema no debe
    # citar como completo lo que no lo es.
    body_scope: Mapped[e.WebBodyScope] = mapped_column(_enum(e.WebBodyScope, "web_body_scope"))
    parsed_from_artifact_version_id: Mapped[str] = mapped_column(ForeignKey("artifact_version.id"))


class WebPersonMention(PKMixin, Base):
    """Aparición de una persona en un documento de contexto, con su evidencia.

    Mismo patrón que `person_mention`, con una regla más dura: la vinculación
    automática exige nombre + señal corroborante en el mismo documento (norma
    ya ingerida que involucra a la persona, o cargo+organización de una
    asignación vigente). Nombre solo abre tarea WEB_MENTION_RESOLUTION: el
    corpus curado de identidades no se contamina con homónimos periodísticos.
    """

    __tablename__ = "web_person_mention"

    web_document_id: Mapped[str] = mapped_column(ForeignKey("web_document.id"), index=True)
    text_raw: Mapped[str] = mapped_column(String(400))
    text_normalized: Mapped[str] = mapped_column(String(400), index=True)
    # Cargo con que la fuente nombra a la persona ("el superintendente de la
    # Sunat, …"); señal corroborante independiente del nombre, como en el corpus.
    role_context_raw: Mapped[str | None] = mapped_column(String(1000))
    role_context_normalized: Mapped[str | None] = mapped_column(String(1000), index=True)
    evidence_span_id: Mapped[str] = mapped_column(ForeignKey("evidence_span.id"))
    canonical_person_id: Mapped[str | None] = mapped_column(ForeignKey("person.id"), index=True)
    resolution_status: Mapped[e.ResolutionStatus] = mapped_column(
        _enum(e.ResolutionStatus, "resolution_status"), default=e.ResolutionStatus.UNRESOLVED
    )
    # La señal literal que sostuvo la vinculación automática ("cita RS
    # 027-2026-EF ya ingerida", "cargo+org coincide con asignación vigente").
    # Nula mientras no hay vinculación; auditable siempre que la haya.
    matched_by: Mapped[str | None] = mapped_column(Text)
    identity_precedent_id: Mapped[str | None] = mapped_column(
        ForeignKey("identity_precedent.id"), index=True
    )


class WebReference(PKMixin, Base):
    """Norma citada por un documento de contexto, anclada a su cita textual.

    Análoga a `document_reference`. Es la única vinculación automática total
    que la política permite sobre contexto, por ser mecánica y verificable: o
    el número normalizado coincide con un `legal_document` ingerido o no. Con
    `target_document_id` resuelto, el documento pasa de "habla de la persona"
    a "cita el mismo acto que respalda su asignación".
    """

    __tablename__ = "web_reference"

    web_document_id: Mapped[str] = mapped_column(ForeignKey("web_document.id"), index=True)
    reference_type: Mapped[e.ReferenceType] = mapped_column(
        _enum(e.ReferenceType, "reference_type")
    )
    target_document_id: Mapped[str | None] = mapped_column(ForeignKey("legal_document.id"))
    target_number_raw: Mapped[str] = mapped_column(String(300))
    target_doc_kind_raw: Mapped[str | None] = mapped_column(String(200))
    evidence_span_id: Mapped[str] = mapped_column(ForeignKey("evidence_span.id"))


# ---------------------------------------------------------------------------
# Revisión humana y gobernanza de ontología
# ---------------------------------------------------------------------------


class ReviewTask(PKMixin, Base):
    __tablename__ = "review_task"

    task_type: Mapped[e.ReviewTaskType] = mapped_column(_enum(e.ReviewTaskType, "review_task_type"))
    target_type: Mapped[str] = mapped_column(String(50))
    target_id: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=3)  # 1 = máxima
    status: Mapped[e.ReviewTaskStatus] = mapped_column(
        _enum(e.ReviewTaskStatus, "review_task_status"),
        default=e.ReviewTaskStatus.PENDING,
        index=True,
    )
    assigned_to: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewDecision(PKMixin, Base):
    __tablename__ = "review_decision"

    review_task_id: Mapped[str] = mapped_column(ForeignKey("review_task.id"), index=True)
    action: Mapped[e.DecisionAction] = mapped_column(_enum(e.DecisionAction, "decision_action"))
    reviewer: Mapped[str | None] = mapped_column(String(200))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OntologyRelease(PKMixin, Base):
    __tablename__ = "ontology_release"

    version: Mapped[str] = mapped_column(String(50), unique=True)
    git_commit: Mapped[str | None] = mapped_column(String(64))
    released_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(30), default="STABLE")
