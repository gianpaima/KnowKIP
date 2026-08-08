"""Representación estructurada de un documento parseado (previa a extracción)."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from kipu_knowledge.domain.enums import DocumentTypeCode, SectionType

# Separador con que una fila de tabla se rinde en una sola cadena. Vive en el
# dominio porque lo escribe el parser y lo lee el extractor: es la forma de la
# sección, no un detalle de ninguno de los dos. Solo se interpone entre celdas,
# de modo que el texto de cada una sigue siendo literal y citable.
TABLE_CELL_SEPARATOR = " | "


class ParsedSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_type: SectionType
    label_raw: str | None = None  # "Artículo 1.-", "VISTOS:", etc.
    order_index: int
    text_raw: str
    text_normalized: str

    def cells(self) -> list[str]:
        """Celdas de una fila de tabla, en orden, con su texto tal cual."""
        return [cell.strip() for cell in self.text_raw.split(TABLE_CELL_SEPARATOR)]

    def cell_span(self, index: int) -> tuple[int, int]:
        """Rango de la celda `index` dentro del texto de la fila.

        Permite citar la celda concreta —el documento de identidad, el nombre—
        en vez de la fila entera, sin perder que la evidencia es esta fila.
        """
        start = 0
        for cell in self.text_raw.split(TABLE_CELL_SEPARATOR)[:index]:
            start += len(cell) + len(TABLE_CELL_SEPARATOR)
        raw = self.text_raw.split(TABLE_CELL_SEPARATOR)[index]
        lead = len(raw) - len(raw.lstrip())
        return start + lead, start + len(raw.rstrip())


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
