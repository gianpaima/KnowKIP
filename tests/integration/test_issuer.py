"""El emisor que el índice declara: registro con cita, reparación y sus límites.

El dispositivo no nombra a su emisor; el índice del diario oficial sí, y sus
bytes quedan archivados. Estas pruebas congelan tres promesas: lo declarado se
registra con su cita literal del listado; lo no declarado o no verificable
queda sin poner y dicho por qué; y nada de esto fabrica organizaciones
canónicas a partir de rótulos de catálogo.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.application.daily_ingest import DailyIngestService
from kipu_knowledge.application.issuer import (
    IssuerOutcome,
    backfill_issuers,
    ensure_document_issuer,
)
from kipu_knowledge.domain import enums as e


def _document_of_code(session: Session, code: str) -> m.LegalDocument:
    return session.execute(
        select(m.LegalDocument)
        .join(m.PublicationItem, m.PublicationItem.id == m.LegalDocument.publication_item_id)
        .where(m.PublicationItem.publication_code == code)
    ).scalar_one()


def _strip_issuer(session: Session, doc: m.LegalDocument) -> None:
    """Deja el documento como quedó todo lo ingerido antes de la regla."""
    mention_id = doc.issuer_mention_id
    doc.issuer_mention_id = None
    session.flush()
    if mention_id:
        mention = session.get(m.OrganizationMention, mention_id)
        if mention is not None:
            session.delete(mention)
    session.flush()


def test_daily_ingest_records_the_declared_issuer_with_its_quote(session, store, daily_kit):
    adapter = daily_kit.adapter([daily_kit.listing(daily_kit.catalogue, total=4)])
    service = DailyIngestService(session, store, adapter=adapter)
    service.run(daily_kit.run_date, capture_pdf=False)

    doc = _document_of_code(session, "2540861-1")
    assert doc.issuer_mention_id, "el emisor declarado por el índice debe quedar registrado"
    mention = session.get(m.OrganizationMention, doc.issuer_mention_id)
    assert mention is not None
    assert mention.text_raw == "DESARROLLO AGRARIO"

    # La cita apunta a los bytes archivados del listado, no al dispositivo.
    span = session.get(m.EvidenceSpan, mention.evidence_span_id)
    assert span is not None
    assert span.quoted_text == "DESARROLLO AGRARIO"
    assert (span.locator_json or {}).get("kind") == "listing_issuer_heading"
    crawl_row = session.execute(
        select(m.CrawlItem).where(m.CrawlItem.publication_code == "2540861-1")
    ).scalar_one()
    assert span.artifact_version_id == crawl_row.listing_artifact_version_id

    # El rótulo del índice no es el nombre registral: sin coincidencia exacta la
    # mención queda sin vincular, y jamás se fabrica una organización nueva.
    assert mention.resolution_status is e.ResolutionStatus.UNRESOLVED
    assert mention.canonical_organization_id is None
    assert (
        session.execute(
            select(m.Organization).where(m.Organization.preferred_name == "DESARROLLO AGRARIO")
        ).scalar_one_or_none()
        is None
    )


def test_backfill_repairs_documents_ingested_before_the_rule(session, store, daily_kit):
    adapter = daily_kit.adapter([daily_kit.listing(daily_kit.catalogue, total=4)])
    DailyIngestService(session, store, adapter=adapter).run(daily_kit.run_date, capture_pdf=False)
    codes = ["2540861-1", "2540903-1", "2540779-1"]
    for code in codes:
        _strip_issuer(session, _document_of_code(session, code))

    previewed = backfill_issuers(session, store, dry_run=True)
    assert [r.outcome for r in previewed] == [IssuerOutcome.LINKED] * 3
    assert all(_document_of_code(session, code).issuer_mention_id is None for code in codes), (
        "dry-run no modifica nada"
    )

    repaired = backfill_issuers(session, store)
    assert [r.outcome for r in repaired] == [IssuerOutcome.LINKED] * 3
    for code in codes:
        assert _document_of_code(session, code).issuer_mention_id is not None

    # Idempotente: la segunda pasada no encuentra nada que reparar.
    assert backfill_issuers(session, store) == []


def test_fixture_ingest_without_index_declares_nothing(ingested_session, store):
    """Lo ingerido sin pasar por el recolector no tiene emisor que citar.

    Registrarle uno sería inventar procedencia; el resultado lo dice y el
    documento queda como está.
    """
    results = backfill_issuers(ingested_session, store)
    assert results, "el corpus de fixtures se ingiere sin índice"
    assert {r.outcome for r in results} == {IssuerOutcome.NOT_DECLARED}
    assert all(
        doc.issuer_mention_id is None
        for doc in ingested_session.execute(select(m.LegalDocument)).scalars()
    )


def test_unverifiable_listing_bytes_do_not_produce_an_issuer(session, store, daily_kit):
    adapter = daily_kit.adapter([daily_kit.listing(daily_kit.catalogue, total=4)])
    DailyIngestService(session, store, adapter=adapter).run(daily_kit.run_date, capture_pdf=False)
    doc = _document_of_code(session, "2540861-1")
    _strip_issuer(session, doc)

    crawl_row = session.execute(
        select(m.CrawlItem).where(m.CrawlItem.publication_code == "2540861-1")
    ).scalar_one()
    version = session.get(m.ArtifactVersion, crawl_row.listing_artifact_version_id)
    version.sha256 = "0" * 64  # los bytes ya no cuadran con lo registrado
    session.flush()

    result = ensure_document_issuer(session, store, doc)
    assert result.outcome is IssuerOutcome.NO_CAPTURE
    assert doc.issuer_mention_id is None, "sin evidencia verificable no se registra nada"
