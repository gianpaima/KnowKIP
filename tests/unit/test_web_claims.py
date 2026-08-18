"""Clasificación de afirmaciones de contexto: verificación mecánica de citas.

El clasificador es un doble de prueba: lo que se prueba aquí es la jaula — la
cita byte a byte, el vocabulario cerrado, el supersede al re-clasificar — no
ningún modelo.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from tests.unit.test_web_enrich import (
    ARTICLE_PARAGRAPHS,
    ARTICLE_URL,
    FakeFetcher,
    article_html,
    sunat_person,  # noqa: F401 - fixture
)

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.adapters.extraction.web_llm import WebContextClaim, parse_claims
from kipu_knowledge.application.web_claims import classify_web_document
from kipu_knowledge.application.web_enrich import enrich_person
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain import web_context as wc


class FakeClassifier:
    provider = "fake"
    model = "fake-model"
    prompt_version = "test-prompt/1"

    def __init__(self, claims: list[WebContextClaim]) -> None:
        self.claims = claims

    def classify(self, person_name: str, full_text: str) -> list[WebContextClaim]:
        return self.claims


@pytest.fixture
def linked_web_document(ingested_session, store, sunat_person):  # noqa: F811
    fetcher = FakeFetcher({ARTICLE_URL: article_html(ARTICLE_PARAGRAPHS)})
    report = enrich_person(ingested_session, store, sunat_person.id, [ARTICLE_URL], fetcher)
    return report.results[0].web_document_id


REAL_QUOTE = (
    "El flamante titular de la entidad es abogado tributarista de la Pontificia "
    "Universidad Católica del Perú y fue ministro de Pesquería en los años noventa."
)


class TestQuoteVerification:
    def test_cita_literal_se_acepta_y_persiste_con_evidencia(
        self,
        ingested_session,
        store,
        linked_web_document,
        sunat_person,  # noqa: F811
    ):
        classifier = FakeClassifier(
            [
                WebContextClaim(
                    predicate=wc.EDUCATION,
                    quote=REAL_QUOTE,
                    data={"institution": "Pontificia Universidad Católica del Perú"},
                )
            ]
        )
        report = classify_web_document(ingested_session, store, classifier, linked_web_document)
        assert report.accepted == 1 and report.rejected == 0

        assertion = ingested_session.execute(
            select(m.Assertion).where(m.Assertion.predicate == wc.EDUCATION)
        ).scalar_one()
        assert assertion.subject_id == sunat_person.id
        assert assertion.review_status == e.ReviewStatus.CANDIDATE
        span = ingested_session.get(m.EvidenceSpan, assertion.evidence_span_id)
        assert span.quoted_text == REAL_QUOTE
        run = ingested_session.get(m.ExtractionRun, assertion.extraction_run_id)
        assert run.model_provider == "fake"
        assert run.prompt_version == "test-prompt/1"

    def test_cita_inventada_se_descarta(self, ingested_session, store, linked_web_document):
        classifier = FakeClassifier(
            [
                WebContextClaim(
                    predicate=wc.PROFESSION,
                    quote="una frase que el artículo jamás escribió sobre esta persona",
                )
            ]
        )
        report = classify_web_document(ingested_session, store, classifier, linked_web_document)
        assert report.accepted == 0 and report.rejected == 1
        assert "byte a byte" in report.outcomes[0].reason
        assert (
            ingested_session.execute(
                select(m.Assertion).where(m.Assertion.predicate == wc.PROFESSION)
            ).scalar_one_or_none()
            is None
        )

    def test_cita_recortada_o_corregida_tambien_se_descarta(
        self, ingested_session, store, linked_web_document
    ):
        # Misma frase con una palabra cambiada: para la verificación es inventada.
        altered = REAL_QUOTE.replace("tributarista", "tributario")
        classifier = FakeClassifier([WebContextClaim(predicate=wc.EDUCATION, quote=altered)])
        report = classify_web_document(ingested_session, store, classifier, linked_web_document)
        assert report.accepted == 0

    def test_predicado_reservado_al_extractor_determinista_se_rechaza(
        self, ingested_session, store, linked_web_document
    ):
        classifier = FakeClassifier(
            [WebContextClaim(predicate=wc.CITES_OFFICIAL_ACT, quote=REAL_QUOTE)]
        )
        report = classify_web_document(ingested_session, store, classifier, linked_web_document)
        assert report.accepted == 0
        assert "determinista" in report.outcomes[0].reason


class TestSupersede:
    def test_reclasificar_reemplaza_sin_borrar(self, ingested_session, store, linked_web_document):
        first = FakeClassifier([WebContextClaim(predicate=wc.EDUCATION, quote=REAL_QUOTE)])
        classify_web_document(ingested_session, store, first, linked_web_document)
        second = FakeClassifier([WebContextClaim(predicate=wc.PROFESSION, quote=REAL_QUOTE)])
        report = classify_web_document(ingested_session, store, second, linked_web_document)
        assert report.superseded == 1

        education = ingested_session.execute(
            select(m.Assertion).where(m.Assertion.predicate == wc.EDUCATION)
        ).scalar_one()
        # Nunca se borra: queda SUPERSEDED con su marca de tiempo.
        assert education.review_status == e.ReviewStatus.SUPERSEDED
        assert education.superseded_at is not None
        profession = ingested_session.execute(
            select(m.Assertion).where(m.Assertion.predicate == wc.PROFESSION)
        ).scalar_one()
        assert profession.superseded_at is None


class TestParseClaims:
    def test_filtra_predicados_fuera_del_vocabulario_y_estructura_rota(self) -> None:
        raw = """[
          {"predicate": "web:education", "quote": "frase válida del artículo"},
          {"predicate": "holds_position", "quote": "predicado del registro funcional"},
          {"predicate": "web:profession"},
          "no soy un objeto",
          {"predicate": "web:profession", "quote": "  "}
        ]"""
        claims = parse_claims(raw)
        assert [c.predicate for c in claims] == ["web:education"]

    def test_tolera_bloque_de_codigo_markdown(self) -> None:
        raw = '```json\n[{"predicate": "web:profession", "quote": "es abogado"}]\n```'
        assert len(parse_claims(raw)) == 1

    def test_salida_no_json_devuelve_vacio(self) -> None:
        assert parse_claims("El artículo dice que...") == []
