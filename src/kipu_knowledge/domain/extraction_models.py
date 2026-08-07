"""Modelos Pydantic estrictos que describen la salida del extractor.

Estos modelos son el contrato entre extracción (determinista o LLM opcional) y
persistencia: nada llega a las tablas canónicas sin pasar por esta validación.
Cada hecho lleva su evidencia textual exacta (regla 9).
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kipu_knowledge.domain.enums import (
    ArticleClass,
    AssignmentEffect,
    AssignmentKind,
    DateStatus,
    EventType,
    IdentifierScheme,
    ParticipantRole,
    ReferenceType,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class EvidenceRef(StrictModel):
    """Localización exacta de la evidencia dentro de una sección segmentada."""

    section_index: int = Field(ge=0, description="Índice de la sección en el documento parseado")
    article_label: str | None = None
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    quoted_text: str = Field(min_length=1)

    @model_validator(mode="after")
    def _span_consistent(self) -> EvidenceRef:
        if self.char_end <= self.char_start:
            raise ValueError("char_end debe ser mayor que char_start")
        if len(self.quoted_text) != self.char_end - self.char_start:
            raise ValueError("quoted_text no coincide con el rango declarado")
        return self


class ExtractedDate(StrictModel):
    """Una fecha con su estatus epistemológico. Nunca se rellena por defecto."""

    value: date | None = None
    status: DateStatus = DateStatus.NOT_STATED
    source_phrase: str | None = None

    @model_validator(mode="after")
    def _value_requires_explicit_basis(self) -> ExtractedDate:
        if self.value is not None and self.status == DateStatus.NOT_STATED:
            raise ValueError("una fecha con valor no puede tener status NOT_STATED")
        if self.value is not None and not self.source_phrase:
            raise ValueError("una fecha con valor requiere la frase fuente que la sustenta")
        return self


class ExtractedIdentifier(StrictModel):
    """Documento de identidad declarado por la fuente junto a un nombre.

    Solo se emite cuando la atribución al nombre es inequívoca (ver
    ``patterns.identifiers_for_name``); ante duda no se extrae nada.
    """

    scheme: IdentifierScheme
    value_raw: str = Field(min_length=1)
    evidence: EvidenceRef


class ExtractedPersonMention(StrictModel):
    text_raw: str = Field(min_length=1)
    evidence: EvidenceRef
    identifiers: list[ExtractedIdentifier] = Field(default_factory=list)


class ExtractedOrgPath(StrictModel):
    """Descomposición heurística de la ruta organizacional. Siempre conserva el raw."""

    path_raw: str = Field(min_length=1)
    organization_name: str | None = None
    unit_chain: list[str] = Field(default_factory=list)  # de más específica a más general


class ExtractedPositionSlot(StrictModel):
    external_scheme: str  # p.ej. "CAP_PROVISIONAL"
    external_code: str
    source_phrase: str


class ExtractedAssignment(StrictModel):
    person: ExtractedPersonMention | None = None
    position_label_raw: str | None = None
    org_path: ExtractedOrgPath | None = None
    assignment_kind: AssignmentKind = AssignmentKind.UNKNOWN
    position_slot: ExtractedPositionSlot | None = None
    valid_from: ExtractedDate = Field(default_factory=ExtractedDate)
    valid_to: ExtractedDate = Field(default_factory=ExtractedDate)
    end_condition_text: str | None = None
    completes_predecessor_period: bool = False

    @model_validator(mode="after")
    def _needs_position_info(self) -> ExtractedAssignment:
        if not self.position_label_raw and not self.org_path:
            raise ValueError("una asignación requiere al menos etiqueta de puesto u org path")
        return self


class ExtractedParticipant(StrictModel):
    role: ParticipantRole
    person: ExtractedPersonMention | None = None
    organization_name: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.95)
    # Datos declarados por el considerando cuando el participante es un
    # candidato de recital: permiten corroborar mecánicamente contra el
    # artículo resolutivo (mismo puesto, mismo instrumento) sin inferir.
    encargo_position_raw: str | None = None
    substantive_role_raw: str | None = None
    cited_document_number_raw: str | None = None


class ExtractedEvent(StrictModel):
    event_type: EventType
    assignment_effect: AssignmentEffect
    legal_verb_raw: str = Field(min_length=1)
    article_label: str | None = None
    participants: list[ExtractedParticipant] = Field(default_factory=list)
    assignments: list[ExtractedAssignment] = Field(default_factory=list)
    effective_from: ExtractedDate = Field(default_factory=ExtractedDate)
    effective_to: ExtractedDate = Field(default_factory=ExtractedDate)
    end_condition_text: str | None = None
    is_collective: bool = False
    mandate_hint: str | None = None  # frase que sugiere mandato/periodo institucional
    prior_document_number_raw: str | None = None  # p.ej. encargo dispuesto por otra RS
    evidence: EvidenceRef
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _no_invented_dates(self) -> ExtractedEvent:
        # Regla 12: nunca fecha efectiva sin frase fuente (lo garantiza ExtractedDate),
        # y un evento START sin fecha explícita debe quedar NOT_STATED, no derivado.
        if self.effective_from.status == DateStatus.INFERRED:
            raise ValueError("el extractor determinista no puede producir fechas INFERRED")
        return self


class ArticleClassification(StrictModel):
    article_label: str
    article_class: ArticleClass
    evidence: EvidenceRef
    confidence: float = Field(ge=0.0, le=1.0, default=0.9)


class ExtractedReference(StrictModel):
    reference_type: ReferenceType
    target_number_raw: str
    target_doc_kind_raw: str | None = None  # "Resolución Suprema", "Ley", ...
    evidence: EvidenceRef


class ExtractedSignatory(StrictModel):
    person: ExtractedPersonMention
    capacity_raw: str | None = None
    signature_order: int = Field(ge=1)


class ExtractionResult(StrictModel):
    """Salida completa del extractor para un documento."""

    events: list[ExtractedEvent] = Field(default_factory=list)
    article_classifications: list[ArticleClassification] = Field(default_factory=list)
    references: list[ExtractedReference] = Field(default_factory=list)
    signatories: list[ExtractedSignatory] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
