"""Enriquecimiento con contexto web: lista blanca, captura, guard y anclas.

Sin red: un fetcher falso entrega HTML sintético con la estructura real de un
artículo de prensa (JSON-LD NewsArticle + párrafos). El corpus de contraste es
el fixture C (2540905-3, RS 027-2026-EF, designación SUNAT), ya ingerido por
`ingested_session`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.application.web_claims import ClassificationError, classify_web_document
from kipu_knowledge.application.web_enrich import (
    UrlOutcome,
    enrich_person,
    suggested_queries,
)
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain import web_context as wc
from kipu_knowledge.domain.contracts import CaptureRecord

PERSON_NAME = "César Alfonso Luna Victoria León"
ARTICLE_URL = "https://rpp.pe/economia/economia/sunat-luna-victoria-noticia-1701340"

ARTICLE_PARAGRAPHS = [
    (
        "César Luna Victoria juró este miércoles como nuevo superintendente de la "
        "Superintendencia Nacional de Aduanas y de Administración Tributaria (Sunat), "
        "tras ser designado por el Gobierno mediante Resolución Suprema N° 027-2026-EF."
    ),
    (
        "El flamante titular de la entidad es abogado tributarista de la Pontificia "
        "Universidad Católica del Perú y fue ministro de Pesquería en los años noventa."
    ),
]


def article_html(paragraphs: list[str], *, name_in_headline: str = "César Luna Victoria") -> bytes:
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return f"""<!DOCTYPE html>
<html lang="es"><head>
<title>Sunat: perfil del nuevo jefe</title>
<link rel="canonical" href="{ARTICLE_URL}"/>
<script type="application/ld+json">{{
  "@type": "NewsArticle",
  "headline": "Sunat: {name_in_headline} juró como nuevo superintendente",
  "datePublished": "2026-08-13T11:34:00-05:00",
  "author": {{"@type": "Person", "name": "Luz Alarcón"}},
  "articleSection": "Economía"
}}</script>
</head><body><article>{body}</article></body></html>""".encode()


class FakeFetcher:
    """Entrega bytes preparados por URL, con el acta de captura completa."""

    def __init__(self, pages: dict[str, bytes]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url: str) -> tuple[bytes, CaptureRecord]:
        self.requested.append(url)
        content = self.pages[url]
        return content, CaptureRecord(
            requested_url=url,
            final_url=url,
            http_status=200,
            content_type="text/html; charset=utf-8",
            byte_length=len(content),
            captured_at=datetime.now(UTC),
            crawler_version="test/1.0",
        )


@pytest.fixture
def sunat_person(ingested_session):
    person = ingested_session.execute(
        select(m.Person)
        .join(m.PersonMention, m.PersonMention.canonical_person_id == m.Person.id)
        .where(m.PersonMention.text_normalized == "CESAR ALFONSO LUNA VICTORIA LEON")
        .limit(1)
    ).scalar_one()
    return person


class TestWhitelist:
    def test_dominio_fuera_de_lista_se_registra_y_no_se_captura(
        self, ingested_session, store, sunat_person
    ):
        fetcher = FakeFetcher({})
        report = enrich_person(
            ingested_session, store, sunat_person.id, ["https://ejemplo.pe/nota"], fetcher
        )
        assert report.results[0].outcome == UrlOutcome.OUT_OF_WHITELIST
        assert fetcher.requested == []  # ni un byte pedido fuera de la lista
        item = ingested_session.execute(
            select(m.CrawlItem).where(m.CrawlItem.source_series == wc.WEB_SOURCE_SERIES)
        ).scalar_one()
        assert item.status == e.CrawlItemStatus.SKIPPED_NOT_RELEVANT

    def test_ruta_prohibida_por_robots_se_rechaza(self, ingested_session, store, sunat_person):
        fetcher = FakeFetcher({})
        report = enrich_person(
            ingested_session, store, sunat_person.id, ["https://rpp.pe/buscar?q=sunat"], fetcher
        )
        assert report.results[0].outcome == UrlOutcome.DISALLOWED_PATH
        assert "robots" in report.results[0].detail
        assert fetcher.requested == []


class TestEnrichment:
    def test_articulo_que_cita_la_norma_vincula_y_ancla(
        self, ingested_session, store, sunat_person
    ):
        fetcher = FakeFetcher({ARTICLE_URL: article_html(ARTICLE_PARAGRAPHS)})
        report = enrich_person(ingested_session, store, sunat_person.id, [ARTICLE_URL], fetcher)
        result = report.results[0]
        assert result.outcome == UrlOutcome.INGESTED

        doc = ingested_session.get(m.WebDocument, result.web_document_id)
        assert doc.kind == e.WebDocumentKind.NEWS_ARTICLE
        assert doc.headline_raw.startswith("Sunat:")
        assert doc.author_raw == "Luz Alarcón"
        assert doc.body_scope == e.WebBodyScope.FULL

        # Señal A del guard: la mención queda vinculada por la norma citada.
        mention = ingested_session.execute(
            select(m.WebPersonMention).where(m.WebPersonMention.web_document_id == doc.id)
        ).scalar_one()
        assert mention.canonical_person_id == sunat_person.id
        assert mention.resolution_status == e.ResolutionStatus.AUTO_LINKED
        assert "027-2026-EF" in mention.matched_by
        # La grafía guardada es la que escribió la fuente, tildes incluidas.
        assert mention.text_raw == "César Luna Victoria"

        reference = ingested_session.execute(
            select(m.WebReference).where(m.WebReference.web_document_id == doc.id)
        ).scalar_one()
        target = ingested_session.get(m.LegalDocument, reference.target_document_id)
        assert target.number_normalized == "027-2026-EF"

        assertion = ingested_session.execute(
            select(m.Assertion).where(m.Assertion.predicate == wc.CITES_OFFICIAL_ACT)
        ).scalar_one()
        assert assertion.subject_id == sunat_person.id
        assert assertion.object_id == target.id
        assert assertion.review_status == e.ReviewStatus.CANDIDATE
        span = ingested_session.get(m.EvidenceSpan, assertion.evidence_span_id)
        assert "Resolución Suprema N° 027-2026-EF" in span.quoted_text

    def test_sin_senal_corroborante_queda_unresolved_con_tarea(
        self, ingested_session, store, sunat_person
    ):
        paragraphs = [
            (
                "César Luna Victoria participó en un conversatorio sobre política "
                "fiscal organizado por una universidad limeña la semana pasada."
            )
        ]
        fetcher = FakeFetcher({ARTICLE_URL: article_html(paragraphs)})
        report = enrich_person(ingested_session, store, sunat_person.id, [ARTICLE_URL], fetcher)
        result = report.results[0]
        assert result.linked_mentions == 0
        assert result.review_tasks == 1

        mention = ingested_session.execute(
            select(m.WebPersonMention).where(
                m.WebPersonMention.web_document_id == result.web_document_id
            )
        ).scalar_one()
        assert mention.canonical_person_id is None
        assert mention.resolution_status == e.ResolutionStatus.UNRESOLVED
        task = ingested_session.execute(
            select(m.ReviewTask).where(
                m.ReviewTask.task_type == e.ReviewTaskType.WEB_MENTION_RESOLUTION
            )
        ).scalar_one()
        assert task.target_id == mention.id
        # Sin sujeto resuelto no hay afirmaciones.
        assert result.assertions == 0

    def test_cargo_y_entidad_en_el_parrafo_vinculan_sin_cita_de_norma(
        self, ingested_session, store, sunat_person
    ):
        paragraphs = [
            (
                "César Luna Victoria, Superintendente Nacional de Aduanas y de "
                "Administración Tributaria, expuso los planes de la entidad."
            )
        ]
        fetcher = FakeFetcher({ARTICLE_URL: article_html(paragraphs)})
        report = enrich_person(ingested_session, store, sunat_person.id, [ARTICLE_URL], fetcher)
        mention = ingested_session.execute(
            select(m.WebPersonMention).where(
                m.WebPersonMention.web_document_id == report.results[0].web_document_id
            )
        ).scalar_one()
        assert mention.resolution_status == e.ResolutionStatus.AUTO_LINKED
        assert "asignación" in mention.matched_by

    def test_apellido_compuesto_con_guion_editorial_tambien_se_encuentra(
        self, ingested_session, store, sunat_person
    ):
        # Observado en RPP (2026-08-18): "César Luna-Victoria" con guion de
        # estilo. La comparación lo trata como espacio; text_raw lo conserva.
        paragraphs = [
            (
                "César Luna-Victoria fue designado mediante Resolución Suprema "
                "N° 027-2026-EF como nuevo jefe de la entidad recaudadora del país."
            )
        ]
        fetcher = FakeFetcher({ARTICLE_URL: article_html(paragraphs)})
        report = enrich_person(ingested_session, store, sunat_person.id, [ARTICLE_URL], fetcher)
        mention = ingested_session.execute(
            select(m.WebPersonMention).where(
                m.WebPersonMention.web_document_id == report.results[0].web_document_id
            )
        ).scalar_one()
        assert mention.text_raw == "César Luna-Victoria"
        assert mention.resolution_status == e.ResolutionStatus.AUTO_LINKED

    def test_repetir_la_url_no_duplica(self, ingested_session, store, sunat_person):
        fetcher = FakeFetcher({ARTICLE_URL: article_html(ARTICLE_PARAGRAPHS)})
        enrich_person(ingested_session, store, sunat_person.id, [ARTICLE_URL], fetcher)
        report = enrich_person(ingested_session, store, sunat_person.id, [ARTICLE_URL], fetcher)
        assert report.results[0].outcome == UrlOutcome.ALREADY_PRESENT
        docs = ingested_session.execute(select(m.WebDocument)).scalars().all()
        assert len(docs) == 1

    def test_la_bitacora_registra_cada_url_con_su_desenlace(
        self, ingested_session, store, sunat_person
    ):
        fetcher = FakeFetcher({ARTICLE_URL: article_html(ARTICLE_PARAGRAPHS)})
        enrich_person(
            ingested_session,
            store,
            sunat_person.id,
            [ARTICLE_URL, "https://ejemplo.pe/otra"],
            fetcher,
        )
        statuses = {
            item.status
            for item in ingested_session.execute(
                select(m.CrawlItem).where(m.CrawlItem.source_series == wc.WEB_SOURCE_SERIES)
            ).scalars()
        }
        assert statuses == {
            e.CrawlItemStatus.INGESTED,
            e.CrawlItemStatus.SKIPPED_NOT_RELEVANT,
        }


class TestSuggestedQueries:
    def test_salen_de_lo_que_la_base_sabe(self, ingested_session, sunat_person):
        queries = suggested_queries(ingested_session, sunat_person.id)
        assert any("LUNA VICTORIA" in q.upper() for q in queries)
        assert any("027-2026-EF" in q.upper() for q in queries)


class TestDossierWebContext:
    def test_el_expediente_muestra_la_capa_de_contexto(self, ingested_session, store, sunat_person):
        from kipu_knowledge.application.person_dossier import build_dossier

        fetcher = FakeFetcher({ARTICLE_URL: article_html(ARTICLE_PARAGRAPHS)})
        enrich_person(ingested_session, store, sunat_person.id, [ARTICLE_URL], fetcher)
        dossier = build_dossier(ingested_session, sunat_person.id)
        docs = dossier.web_context["documents"]
        assert len(docs) == 1
        assert docs[0]["source_name"] == "RPP Noticias"
        assert docs[0]["references"][0]["resolved_document"]["publication_code"] == "2540905-3"
        assert all(wm["resolution_status"] == "AUTO_LINKED" for wm in docs[0]["mentions"])

    def test_lo_no_vinculado_se_muestra_aparte(self, ingested_session, store, sunat_person):
        from kipu_knowledge.application.person_dossier import build_dossier

        paragraphs = ["César Luna Victoria asistió a una conferencia académica en Lima."]
        fetcher = FakeFetcher({ARTICLE_URL: article_html(paragraphs)})
        enrich_person(ingested_session, store, sunat_person.id, [ARTICLE_URL], fetcher)
        dossier = build_dossier(ingested_session, sunat_person.id)
        assert dossier.web_context["documents"] == []
        assert len(dossier.web_context["unlinked_mentions"]) == 1


class TestClassifierIsNotBlocked:
    def test_sin_mencion_vinculada_no_se_clasifica(self, ingested_session, store, sunat_person):
        paragraphs = ["César Luna Victoria asistió a una conferencia académica en Lima."]
        fetcher = FakeFetcher({ARTICLE_URL: article_html(paragraphs)})
        report = enrich_person(ingested_session, store, sunat_person.id, [ARTICLE_URL], fetcher)

        class NullClassifier:
            provider = "test"
            model = "test"
            prompt_version = "test/1"

            def classify(self, person_name, full_text):  # noqa: ANN001
                raise AssertionError("no debe llegar a llamarse")

        with pytest.raises(ClassificationError, match="mención vinculada"):
            classify_web_document(
                ingested_session, store, NullClassifier(), report.results[0].web_document_id
            )
