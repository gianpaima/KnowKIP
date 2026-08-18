"""Filtro de relevancia sobre las 32 sumillas reales del índice del 2026-08-07."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from kipu_knowledge.adapters.parsing.listing_parser import parse_listing
from kipu_knowledge.domain.enums import Relevance
from kipu_knowledge.domain.relevance import RULE_VERSION, classify_summary

LISTING_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "elperuano" / "listing"

# Partición congelada del 2026-08-07, revisada a mano contra cada sumilla.
EXPECTED_RELEVANT = {
    "2540927-1",  # Encargan funciones de Presidente Ejecutivo del OFIS
    "2540926-2",  # Designan Viceministro
    "2540926-1",  # Aceptan renuncia de Viceministro
    "2540924-1",
    "2540923-1",
    "2540922-1",
    "2540921-1",
    "2540919-1",
    "2540918-1",
    "2540917-1",
    "2540911-1",
    "2540909-1",
    "2540908-1",
    "2540907-1",
    "2540891-1",
    "2540840-1",
    "2540832-1",
    "2540828-1",
    "2540315-1",  # Dejan sin efecto designaciones y designan fedatarios
}
EXPECTED_NOT_RELEVANT = {
    "2540916-1",  # Aprueban Convenio de Apoyo Presupuestario
    "2540844-1",  # Disponen la notificación mediante publicación
    "2540785-1",  # Aprueban tarifario
    "2540744-3",  # Autorizan viaje
    "2540744-2",  # Autorizan viaje
    "2540744-1",  # Autorizan viaje
    "2540506-1",  # Autorizan viaje
    "2540457-1",  # Aprueban modificación de concesión
    "2540452-1",  # Rectifican errores materiales
    "2540396-1",  # Aprueban publicación para comentarios
    "2539657-1",  # Autorizan viaje
    "2539107-1",  # Aprueban modificación de contrato
    "2535949-1",  # Aprueban actualización del ROF
}


def _entries():
    entries = []
    for name in ("NL-20260807-start0", "NL-20260807-start20"):
        page = parse_listing(
            (LISTING_DIR / f"{name}.html").read_bytes(),
            requested_date=date(2026, 8, 7),
            series="NL",
            base_url="https://busquedas.elperuano.pe",
            source_family="EL_PERUANO_NL",
        )
        entries.extend(page.entries)
    return entries


def test_partition_of_the_real_edition() -> None:
    verdicts = {
        entry.reference.publication_code: classify_summary(entry.summary_raw)
        for entry in _entries()
    }
    assert len(verdicts) == 32
    relevant = {c for c, v in verdicts.items() if v.relevance is Relevance.RELEVANT}
    not_relevant = {c for c, v in verdicts.items() if v.relevance is Relevance.NOT_RELEVANT}
    assert relevant == EXPECTED_RELEVANT
    assert not_relevant == EXPECTED_NOT_RELEVANT


def test_compound_summary_counts_as_personnel() -> None:
    """La sumilla encabeza con un verbo del catálogo negativo y aun así designa:
    el acto de personal manda sobre el prefijo."""
    verdict = classify_summary(
        "Dejan sin efecto designaciones y designan fedatarios institucionales de la "
        "Intendencia de Aduana de Chancay"
    )
    assert verdict.relevance is Relevance.RELEVANT
    assert verdict.rule == RULE_VERSION


@pytest.mark.parametrize(
    "summary",
    [
        "Incorporan disposiciones al Reglamento Interno",
        "Delegan facultades en el Secretario General",
        "",
        None,
    ],
)
def test_unknown_summaries_are_ingested(summary: str | None) -> None:
    """Lo que no está catalogado no se descarta: se ingiere y decide el extractor."""
    verdict = classify_summary(summary)
    assert verdict.relevance is Relevance.UNDECIDED
    assert verdict.should_ingest


def test_noun_designacion_alone_is_not_a_personnel_verb() -> None:
    """'designación' dentro de un reglamento no es un acto de personal; solo el
    verbo conjugado lo es."""
    verdict = classify_summary(
        "Aprueban el Reglamento que regula el procedimiento de designación de "
        "representantes ante el Consejo Directivo"
    )
    assert verdict.relevance is Relevance.NOT_RELEVANT
    assert not verdict.should_ingest


@pytest.mark.parametrize(
    "summary",
    [
        "Confirman la Resolución N.º 00028-2026-JEE-QSPI/JNE emitida por el Jurado "
        "Electoral Especial de Quispicanchi",
        "Revocan extremo de la Res. N.° 03459-2026-JEE-LICN/JNE emitida por el JEE",
        "Declaran nula la Resolución N.º 00053-2026-JEE-CHAN/JNE",
        "Declaran infundado el recurso de apelación interpuesto contra la resolución",
        "Resuelven recurso extraordinario por afectación al debido proceso",
        "Integran credenciales a la Resolución Nº 1021-2026-JNE",
    ],
)
def test_jurisdictional_appeals_are_not_personnel_acts(summary: str) -> None:
    """El JNE en temporada electoral resuelve recursos por decenas al día
    (70 de 102 dispositivos el 2026-08-11): confirman o anulan resoluciones,
    no designan a nadie."""
    verdict = classify_summary(summary)
    assert verdict.relevance is Relevance.NOT_RELEVANT
    assert not verdict.should_ingest


def test_declaring_a_vacancy_is_still_a_personnel_act() -> None:
    """El catálogo positivo se evalúa antes: la vacancia sí es un acto sobre el
    cargo, aunque empiece con 'Declaran'."""
    verdict = classify_summary("Declaran vacancia del cargo de alcalde de la Municipalidad")
    assert verdict.relevance is Relevance.RELEVANT


def test_summoning_a_citizen_to_assume_office_is_still_ingested() -> None:
    """'Convocan a ciudadano para que asuma el cargo' es un acto sobre el cargo:
    queda UNDECIDED y se ingiere, no se cataloga como jurisdiccional."""
    verdict = classify_summary(
        "Convocan a ciudadana para que asuma, provisionalmente, el cargo de "
        "alcaldesa de la Municipalidad Distrital"
    )
    assert verdict.relevance is Relevance.UNDECIDED
    assert verdict.should_ingest


def test_a_mixed_summary_with_a_personnel_act_still_ingests() -> None:
    verdict = classify_summary(
        "Revocan credenciales y designan fedatarios institucionales de la entidad"
    )
    assert verdict.relevance is Relevance.RELEVANT
