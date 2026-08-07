"""Corroboración por recital: veredictos deterministas y extracción de candidatos.

La señal solo autoriza el vínculo cuando el patrón completo se cumple; cada
prueba negativa congela un modo de fallo que debe seguir yendo a humano.
"""

from __future__ import annotations

from kipu_knowledge.adapters.extraction.deterministic import DeterministicExtractor
from kipu_knowledge.application.corroboration import (
    RecitalCandidate,
    RecitalOutcome,
    bare_document_number,
    corroborate_recital,
)
from kipu_knowledge.domain.enums import DocumentTypeCode, ParticipantRole, SectionType
from kipu_knowledge.domain.parsed import ParsedDocument, ParsedSection

POSITION = "Presidenta del Tribunal Fiscal del Ministerio de Economía y Finanzas"


def _candidate(
    name: str = "Luisa Ysila Castillo Soto",
    position: str | None = POSITION,
    cited: str | None = "044-2025-EF",
) -> RecitalCandidate:
    return RecitalCandidate(name=name, encargo_position_raw=position, cited_document_number=cited)


def test_corroborates_unique_candidate_with_matching_position_and_instrument():
    verdict = corroborate_recital([_candidate()], POSITION, {"044-2025-EF"})
    assert verdict.outcome == RecitalOutcome.CORROBORATED
    assert "mismo instrumento" in verdict.rationale


def test_corroborates_without_instruments_if_position_matches():
    verdict = corroborate_recital([_candidate(cited=None)], POSITION, set())
    assert verdict.outcome == RecitalOutcome.CORROBORATED


def test_two_candidates_open_conflict_never_choose():
    verdict = corroborate_recital(
        [_candidate(), _candidate(name="Otra Persona Distinta")], POSITION, set()
    )
    assert verdict.outcome == RecitalOutcome.CONFLICT
    assert "más de un encargo" in verdict.rationale


def test_position_mismatch_stays_unconfirmed():
    verdict = corroborate_recital(
        [_candidate(position="Presidenta de Otro Tribunal")], POSITION, set()
    )
    assert verdict.outcome == RecitalOutcome.UNCONFIRMED


def test_missing_position_stays_unconfirmed():
    verdict = corroborate_recital([_candidate(position=None)], POSITION, set())
    assert verdict.outcome == RecitalOutcome.UNCONFIRMED


def test_instrument_mismatch_is_a_veto():
    verdict = corroborate_recital([_candidate(cited="099-2020-EF")], POSITION, {"044-2025-EF"})
    assert verdict.outcome == RecitalOutcome.CONFLICT
    assert "instrumentos distintos" in verdict.rationale


def test_position_match_tolerates_normalization_differences():
    verdict = corroborate_recital(
        [
            _candidate(
                position="presidenta del tribunal fiscal del ministerio de economía y finanzas"
            )
        ],
        POSITION,
        set(),
    )
    assert verdict.outcome == RecitalOutcome.CORROBORATED


def test_bare_document_number_variants():
    assert bare_document_number("Resolución Suprema N° 044-2025-EF") == "044-2025-EF"
    assert bare_document_number("044-2025-EF") == "044-2025-EF"
    assert bare_document_number(None) is None
    assert bare_document_number("texto sin número de instrumento") is None


# ---------------------------------------------------------------------------
# Extractor: todos los candidatos, nunca solo el primero
# ---------------------------------------------------------------------------


def _doc_with_recitals_and_article(recitals: list[str], article: str) -> ParsedDocument:
    sections: list[ParsedSection] = []
    for i, text in enumerate(recitals):
        sections.append(
            ParsedSection(
                section_type=SectionType.CONSIDERANDO,
                label_raw=None,
                order_index=i,
                text_raw=text,
                text_normalized=text,
            )
        )
    sections.append(
        ParsedSection(
            section_type=SectionType.ARTICLE,
            label_raw="Artículo 1.-",
            order_index=len(recitals),
            text_raw=article,
            text_normalized=article,
        )
    )
    return ParsedDocument(
        publication_code="0000000-0",
        source_series="NL",
        title_raw="t",
        document_type_raw="RESOLUCIÓN SUPREMA",
        document_type_code=DocumentTypeCode.RESOLUCION_SUPREMA,
        number_raw="N° 1-2026-X",
        number_normalized="1-2026-X",
        sections=sections,
    )


END_ARTICLE = (
    "Artículo 1.- Dar por concluido el encargo de puesto de Presidenta del "
    "Tribunal Fiscal del Ministerio de Economía y Finanzas, dispuesto mediante "
    "la Resolución Suprema N° 044-2025-EF."
)


def test_extractor_collects_every_recital_candidate():
    doc = _doc_with_recitals_and_article(
        [
            "Que, conforme a lo dispuesto en la Resolución Suprema N° 044-2025-EF, "
            "se encarga a la señora Luisa Ysila Castillo Soto, Asesora del Despacho, "
            "el puesto de Presidenta del Tribunal Fiscal del Ministerio de Economía "
            "y Finanzas, hasta que se designe a su titular;",
            "Que, mediante la Resolución Suprema N° 050-2025-EF, se encarga al señor "
            "Pedro Pablo Ramírez Vega, el puesto de Presidenta del Tribunal Fiscal "
            "del Ministerio de Economía y Finanzas, hasta nueva disposición;",
        ],
        END_ARTICLE,
    )
    result = DeterministicExtractor().extract(doc)
    (event,) = result.events
    candidates = [
        pt
        for pt in event.participants
        if pt.role == ParticipantRole.AFFECTED_PERSON_RECITAL_CANDIDATE
    ]
    assert len(candidates) == 2, "dos encargos declarados deben producir dos candidatos"
    assert {pt.person.text_raw for pt in candidates} == {
        "Luisa Ysila Castillo Soto",
        "Pedro Pablo Ramírez Vega",
    }
    assert {pt.cited_document_number_raw for pt in candidates} == {"044-2025-EF", "050-2025-EF"}


def test_extractor_captures_position_substantive_and_instrument():
    doc = _doc_with_recitals_and_article(
        [
            "Que, conforme a lo dispuesto en la Resolución Suprema N° 044-2025-EF, "
            "se encarga a la señora Luisa Ysila Castillo Soto, Asesora del Despacho "
            "Viceministerial de Economía II, el puesto de Presidenta del Tribunal "
            "Fiscal del Ministerio de Economía y Finanzas, hasta que se designe a su titular;",
        ],
        END_ARTICLE,
    )
    result = DeterministicExtractor().extract(doc)
    (event,) = result.events
    (candidate,) = [
        pt
        for pt in event.participants
        if pt.role == ParticipantRole.AFFECTED_PERSON_RECITAL_CANDIDATE
    ]
    assert candidate.encargo_position_raw == POSITION
    assert candidate.substantive_role_raw == "Asesora del Despacho Viceministerial de Economía II"
    assert candidate.cited_document_number_raw == "044-2025-EF"
