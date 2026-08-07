"""Representación estructurada de un documento parseado (previa a extracción)."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from kipu_knowledge.domain.enums import DocumentTypeCode, SectionType


class ParsedSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_type: SectionType
    label_raw: str | None = None  # "Artículo 1.-", "VISTOS:", etc.
    order_index: int
    text_raw: str
    text_normalized: str


class ParsedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publication_code: str
    source_series: str  # "NL"
    canonical_url: str | None = None
    # URL del PDF que la captura declara para este dispositivo. Es una afirmación
    # de la fuente, no una verificación nuestra: nadie ha comprobado que responda.
    pdf_url: str | None = None
    title_raw: str  # sumilla
    document_type_raw: str
    document_type_code: DocumentTypeCode
    number_raw: str
    number_normalized: str
    issue_place_raw: str | None = None
    issued_on: date | None = None
    published_on: date | None = None
    # Frase de la captura que declara la fecha de publicación, con su rango en el
    # texto del artefacto (no en una sección: aparece en la página del buscador,
    # fuera del dispositivo). Es la evidencia de la fecha que la ley usa para
    # fijar el inicio de efectos, así que tiene que poder citarse literalmente.
    published_on_phrase: str | None = None
    published_on_char_start: int | None = None
    published_on_char_end: int | None = None
    sections: list[ParsedSection] = Field(default_factory=list)

    def articles(self) -> list[ParsedSection]:
        return [s for s in self.sections if s.section_type == SectionType.ARTICLE]

    def sections_of(self, section_type: SectionType) -> list[ParsedSection]:
        return [s for s in self.sections if s.section_type == section_type]
