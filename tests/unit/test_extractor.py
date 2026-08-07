"""Pruebas unitarias del extractor determinista sobre textos sintéticos y reales."""

from datetime import date
from pathlib import Path

import pytest

from kipu_knowledge.adapters.extraction.deterministic import DeterministicExtractor
from kipu_knowledge.adapters.extraction.patterns import split_org_path
from kipu_knowledge.adapters.parsing.html_parser import ElPeruanoHtmlParser
from kipu_knowledge.domain.contracts import SourceReference
from kipu_knowledge.domain.enums import (
    ArticleClass,
    AssignmentEffect,
    AssignmentKind,
    DateStatus,
    DocumentTypeCode,
    EventType,
    ParticipantRole,
    SectionType,
)
from kipu_knowledge.domain.parsed import ParsedDocument, ParsedSection

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "elperuano"


def _doc_with_articles(*articles: str) -> ParsedDocument:
    sections = []
    for i, text in enumerate(articles):
        label = text.split("- ")[0] + "- " if ".-" in text[:30] else None
        sections.append(
            ParsedSection(
                section_type=SectionType.ARTICLE,
                label_raw=label.strip() if label else None,
                order_index=i,
                text_raw=text,
                text_normalized=text,
            )
        )
    return ParsedDocument(
        publication_code="0000000-0",
        source_series="NL",
        title_raw="t",
        document_type_raw="RESOLUCIÓN MINISTERIAL",
        document_type_code=DocumentTypeCode.RESOLUCION_MINISTERIAL,
        number_raw="N° 1-2026-X",
        number_normalized="1-2026-X",
        sections=sections,
    )


@pytest.fixture(scope="module")
def extractor() -> DeterministicExtractor:
    return DeterministicExtractor()


def _extract_fixture(code: str):
    parser = ElPeruanoHtmlParser()
    ref = SourceReference("EL_PERUANO_NL", "NL", code, None)
    doc = parser.parse((FIXTURES / f"{code}.html").read_bytes(), ref)
    return DeterministicExtractor().extract(doc)


class TestVerbPatterns:
    def test_designar_simple(self, extractor):
        doc = _doc_with_articles(
            "Artículo 1.- Designar al señor JUAN PEREZ QUISPE en el cargo de "
            "Director de la Oficina General de Administración del Ministerio de Salud."
        )
        result = extractor.extract(doc)
        assert len(result.events) == 1
        event = result.events[0]
        assert event.event_type == EventType.DESIGNATION
        assert event.assignment_effect == AssignmentEffect.START
        assert event.effective_from.status == DateStatus.NOT_STATED
        assert event.participants[0].person.text_raw == "JUAN PEREZ QUISPE"

    def test_designar_con_fecha(self, extractor):
        doc = _doc_with_articles(
            "Artículo 1.- DESIGNAR a partir del 06 de agosto de 2026, a la señora "
            "MARIA LOPEZ DIAZ en el cargo de confianza de Gerenta General de la "
            "Biblioteca Nacional del Perú."
        )
        event = extractor.extract(doc).events[0]
        assert event.effective_from.value == date(2026, 8, 6)
        assert event.effective_from.status == DateStatus.EXPLICIT
        assert event.effective_from.source_phrase  # la fecha lleva su frase fuente

    def test_nombrar_es_appointment(self, extractor):
        doc = _doc_with_articles(
            "Artículo 2.- Nombrar a la señora Clara Rossana Urteaga Goldstein como "
            "Presidenta del Tribunal Fiscal del Ministerio de Economía y Finanzas."
        )
        event = extractor.extract(doc).events[0]
        assert event.event_type == EventType.APPOINTMENT

    def test_aceptar_renuncia_con_fecha(self, extractor):
        doc = _doc_with_articles(
            "Artículo 1.- Aceptar la renuncia, a partir del 4 de agosto de 2026, del señor "
            "CARLOS MANUEL YAÑEZ LAZO al cargo de Jefe del Centro Nacional de Estimación, "
            "Prevención y Reducción del Riesgo de Desastres -CENEPRED, dándosele las gracias "
            "por los servicios prestados."
        )
        event = extractor.extract(doc).events[0]
        assert event.event_type == EventType.ACCEPT_RESIGNATION
        assert event.assignment_effect == AssignmentEffect.END
        assert event.effective_from.value == date(2026, 8, 4)
        assignment = event.assignments[0]
        assert assignment.valid_to.value == date(2026, 8, 4)
        assert "dándosele" not in assignment.position_label_raw

    def test_dar_por_concluida_designacion(self, extractor):
        doc = _doc_with_articles(
            "Artículo 1.- Dar por concluida la designación del señor PEDRO GOMEZ RIOS "
            "en el cargo de Asesor del Despacho Ministerial del Ministerio del Interior."
        )
        event = extractor.extract(doc).events[0]
        assert event.event_type == EventType.END_DESIGNATION
        assert event.assignment_effect == AssignmentEffect.END

    def test_dar_por_concluido_encargo_sin_persona(self, extractor):
        doc = _doc_with_articles(
            "Artículo 1.- Dar por concluido el encargo de puesto de Presidenta del "
            "Tribunal Fiscal del Ministerio de Economía y Finanzas, dispuesto mediante "
            "la Resolución Suprema Nº 044-2025-EF."
        )
        event = extractor.extract(doc).events[0]
        assert event.event_type == EventType.END_ACTING_ASSIGNMENT
        assert event.prior_document_number_raw == "Resolución Suprema N° 044-2025-EF"

    def test_encargar_persona_con_condicion(self, extractor):
        doc = _doc_with_articles(
            "Artículo 1º.- ENCARGAR, al servidor civil Arnold Anthony Crisóstomo Quispe, "
            "con eficacia anticipada a partir del 30 de julio de 2026, la obligación de "
            "brindar información ante la Intendencia Nacional de Bomberos del Perú, "
            "hasta el retorno del descanso vacacional del servidor Jhon Eduardo Cárcamo Litano."
        )
        event = extractor.extract(doc).events[0]
        assert event.event_type == EventType.ADDITIONAL_RESPONSIBILITY
        assert event.effective_from.value == date(2026, 7, 30)
        assert event.end_condition_text is not None
        assert event.end_condition_text.startswith("hasta el retorno")
        roles = {p.role for p in event.participants}
        assert ParticipantRole.RETURNING_HOLDER in roles
        assert event.assignments[0].assignment_kind == AssignmentKind.ADDITIONAL_RESPONSIBILITY

    def test_encargar_a_oficina_no_es_evento(self, extractor):
        # Regla 23: encargos de publicación a unidades no son eventos de personal.
        doc = _doc_with_articles(
            "Artículo 2.- ENCARGAR a la Oficina de Tecnologías de la Información la "
            "publicación de la presente Resolución en el portal web institucional."
        )
        result = extractor.extract(doc)
        assert result.events == []
        assert result.article_classifications[0].article_class == ArticleClass.PUBLICATION_NOTICE

    def test_declaracion_jurada_no_es_designacion(self, extractor):
        doc = _doc_with_articles(
            "Artículo 2.- La mencionada servidora debe presentar su Declaración Jurada de "
            "Ingresos, Bienes y Rentas conforme a la normatividad vigente."
        )
        result = extractor.extract(doc)
        assert result.events == []
        assert result.article_classifications[0].article_class == ArticleClass.DERIVED_OBLIGATION

    def test_multiple_actions_in_one_document(self, extractor):
        result = _extract_fixture("2540905-3")
        assert len(result.events) == 2
        types = [e.event_type for e in result.events]
        assert EventType.ACCEPT_RESIGNATION in types
        assert EventType.DESIGNATION in types

    def test_collective_event_three_people(self, extractor):
        result = _extract_fixture("2540905-2")
        event = result.events[0]
        assert event.is_collective
        assert len(event.assignments) == 3
        names = [a.person.text_raw for a in event.assignments]
        assert names == [
            "Inés Marylin Choy Chong",
            "Luis Miguel Palomino Bonilla",
            "Gustavo Adolfo Yamada Fukusaki",
        ]


class TestOrgPathSplit:
    def test_full_chain(self):
        split = split_org_path(
            "Jefa de Atención al Ciudadano y Gestión Documental de la Oficina de Atención "
            "al Ciudadano y Gestión Documental de la Secretaría General del Ministerio de "
            "Desarrollo Agrario y Riego"
        )
        assert split.organization == "Ministerio de Desarrollo Agrario y Riego"
        assert split.unit_chain == [
            "Oficina de Atención al Ciudadano y Gestión Documental",
            "Secretaría General",
        ]
        assert split.role_label == "Jefa de Atención al Ciudadano y Gestión Documental"

    def test_no_org_head_is_conservative(self):
        split = split_org_path("Superintendente Nacional de Aduanas y de Administración Tributaria")
        assert split.organization is None
        assert split.unit_chain == []

    def test_comma_inside_unit_name(self):
        split = split_org_path(
            "Jefa de Presupuesto de la Oficina de Presupuesto de la Oficina General de "
            "Planeamiento, Presupuesto y Modernización del Ministerio de la Producción"
        )
        assert split.organization == "Ministerio de la Producción"
        assert "Oficina General de Planeamiento, Presupuesto y Modernización" in split.unit_chain


class TestSignatories:
    def test_signature_capacity_pairing(self):
        result = _extract_fixture("2540903-1")
        names = [(s.person.text_raw, s.capacity_raw) for s in result.signatories]
        assert names == [
            ("KEIKO SOFÍA FUJIMORI HIGUCHI", "Presidenta de la República"),
            ("RAFAEL JORGE BELAUNDE LLOSA", "Ministro de Defensa"),
        ]

    def test_multiline_capacity(self):
        result = _extract_fixture("2540702-1")
        assert result.signatories[0].capacity_raw == (
            "Intendente Nacional, Intendencia Nacional de Bomberos del Perú"
        )
