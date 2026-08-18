"""Recolección diaria completa, sin red: índice sintético + fixtures reales.

El parser del índice se prueba contra las capturas reales en
tests/unit/test_listing_parser.py. Aquí se prueba la orquestación: qué se
ingiere, qué se descarta, qué queda pendiente y qué se escribe de todo ello.
Las piezas del índice sintético viven en tests/conftest.py (`daily_kit`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DataError

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.adapters.sources.http_capture import CaptureHttpError
from kipu_knowledge.application.capture import ensure_issue
from kipu_knowledge.application.daily_ingest import (
    DailyIngestService,
    backfill_event_counts,
    backfill_issue_links,
)
from kipu_knowledge.application.ingest import IngestService
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain.relevance import RULE_VERSION

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


class _ExtractorWithAGap(IngestService):
    """Ingesta normal, salvo que para ciertos códigos no extrae ningún evento."""

    def __init__(self, session, store, adapter, *, blind_to: set[str]):
        super().__init__(session, store, adapter=adapter)
        self._blind_to = blind_to

    def ingest_url(self, url: str):
        outcome = super().ingest_url(url)
        if url.rsplit("/", 1)[-1] in self._blind_to:
            outcome.event_ids = []
        return outcome


@pytest.fixture
def daily(session, store, daily_kit):
    """Servicio listo con el catálogo completo en una sola página."""
    adapter = daily_kit.adapter([daily_kit.listing(daily_kit.catalogue, total=4)])
    return DailyIngestService(session, store, adapter=adapter), adapter


def test_run_ingests_personnel_acts_and_records_everything_it_saw(session, daily, daily_kit):
    service, _adapter = daily
    result = service.run(daily_kit.run_date, capture_pdf=False)

    assert result.total_declared == 4
    assert result.counts() == {"INGESTED": 3, "SKIPPED_NOT_RELEVANT": 1}
    assert result.status == "COMPLETED"

    rows = session.execute(select(m.CrawlItem)).scalars().all()
    # Nada se descarta en silencio: los 4 descubiertos quedan anotados.
    assert len(rows) == 4
    skipped = next(r for r in rows if r.publication_code == "2540896-1")
    assert skipped.status is e.CrawlItemStatus.SKIPPED_NOT_RELEVANT
    assert skipped.relevance is e.Relevance.NOT_RELEVANT
    assert skipped.relevance_rule == RULE_VERSION
    assert "no personal" in (skipped.relevance_rationale or "")
    assert skipped.summary_raw == "Aprueban el Reglamento Interno de Trabajo"
    assert skipped.publication_item_id is None

    assert len(session.execute(select(m.LegalDocument)).scalars().all()) == 3


def test_relevant_documents_without_events_are_counted(session, store, daily_kit):
    """Un acto de personal del que el extractor no saca nada es un hueco visible.

    Pasó en 6 de 19 dispositivos de la edición del 2026-08-07 (artículos con la
    parte dispositiva en un párrafo aparte, artículos colectivos con otra
    redacción). No es un fallo de captura —el texto está íntegro— pero tampoco
    puede pasar por una ingesta correcta sin más.
    """
    catalogue = [
        ("2540861-1", "Designan Jefa de la Oficina de Gestión Documental", "MIDAGRI"),
        ("2540903-1", "Designan Jefe de la Oficina General de Administración", "CENEPRED"),
    ]
    adapter = daily_kit.adapter([daily_kit.listing(catalogue, total=2)])
    service = DailyIngestService(
        session,
        store,
        adapter=adapter,
        # Un extractor que no reconoce el segundo acto: el documento entra, con
        # su texto y su evidencia, pero sin ningún evento afirmado.
        ingest_service=_ExtractorWithAGap(session, store, adapter, blind_to={"2540903-1"}),
    )
    result = service.run(daily_kit.run_date, capture_pdf=False)

    assert result.counts() == {"INGESTED": 2}
    assert [item.publication_code for item in result.relevant_but_empty] == ["2540903-1"]

    rows = {r.publication_code: r for r in session.execute(select(m.CrawlItem)).scalars()}
    assert rows["2540861-1"].events_extracted == 1
    assert rows["2540903-1"].events_extracted == 0
    assert rows["2540903-1"].status is e.CrawlItemStatus.INGESTED  # no es un fallo
    assert rows["2540903-1"].outcome_detail.startswith("eventos=0")
    assert rows["2540903-1"].last_error is None


def test_listing_bytes_are_archived_as_evidence_of_the_day(session, daily, daily_kit):
    service, _adapter = daily
    service.run(daily_kit.run_date, capture_pdf=False)

    issue = session.execute(select(m.PublicationIssue)).scalar_one()
    assert issue.issue_code == "NL20260806"
    assert issue.publication_date == daily_kit.run_date

    listing = session.execute(
        select(m.Artifact)
        .join(m.PublicationItem, m.PublicationItem.id == m.Artifact.publication_item_id)
        .where(
            m.PublicationItem.publication_code == "NL20260806",
            m.Artifact.representation_type == e.RepresentationType.LISTING,
        )
    ).scalar_one()
    versions = (
        session.execute(
            select(m.ArtifactVersion).where(m.ArtifactVersion.artifact_id == listing.id)
        )
        .scalars()
        .all()
    )
    assert len(versions) == 1
    # Cada dispositivo apunta a los bytes que lo declararon.
    rows = session.execute(select(m.CrawlItem)).scalars().all()
    assert {row.listing_artifact_version_id for row in rows} == {versions[0].id}


def _listing_versions(session):
    return (
        session.execute(
            select(m.ArtifactVersion)
            .join(m.Artifact, m.Artifact.id == m.ArtifactVersion.artifact_id)
            .where(m.Artifact.representation_type == e.RepresentationType.LISTING)
        )
        .scalars()
        .all()
    )


def test_listing_is_not_rearchived_when_it_declares_the_same(session, store, daily_kit):
    """El índice trae un token anti-bot distinto en cada respuesta.

    Comprobado el 2026-08-07: dos capturas del mismo listado difieren solo en
    ese token y en una cookie con marca de tiempo. Con la deduplicación por
    bytes del CAS, cada corrida abriría versiones nuevas para siempre; lo que
    decide es lo que el índice declara.
    """
    base = daily_kit.listing(daily_kit.catalogue, total=4)
    volatile = base.replace(b"<main>", b"<main><script src='/bnith__TOKEN-DISTINTO'></script>")
    assert volatile != base

    DailyIngestService(session, store, adapter=daily_kit.adapter([base])).run(
        daily_kit.run_date, capture_pdf=False
    )
    DailyIngestService(session, store, adapter=daily_kit.adapter([volatile])).run(
        daily_kit.run_date, capture_pdf=False
    )

    assert len(_listing_versions(session)) == 1


def test_listing_is_rearchived_when_the_edition_changes(session, store, daily_kit):
    """Una norma añadida más tarde sí es un hecho: abre versión nueva."""
    DailyIngestService(
        session, store, adapter=daily_kit.adapter([daily_kit.listing(daily_kit.catalogue, total=4)])
    ).run(daily_kit.run_date, capture_pdf=False)

    grown = daily_kit.catalogue + [
        ("2540905-2", "Designan miembros del Directorio", "BCRP"),
    ]
    DailyIngestService(
        session, store, adapter=daily_kit.adapter([daily_kit.listing(grown, total=5)])
    ).run(daily_kit.run_date, capture_pdf=False)

    assert len(_listing_versions(session)) == 2


def test_ingested_publications_are_linked_to_the_issue(session, daily, daily_kit):
    service, _adapter = daily
    service.run(daily_kit.run_date, capture_pdf=False)

    issue = session.execute(select(m.PublicationIssue)).scalar_one()
    items = (
        session.execute(
            select(m.PublicationItem).where(m.PublicationItem.publication_code != "NL20260806")
        )
        .scalars()
        .all()
    )
    assert items and all(item.issue_id == issue.id for item in items)


def test_second_run_is_idempotent(session, store, daily_kit):
    first_adapter = daily_kit.adapter([daily_kit.listing(daily_kit.catalogue, total=4)])
    DailyIngestService(session, store, adapter=first_adapter).run(
        daily_kit.run_date, capture_pdf=False
    )

    again = daily_kit.adapter([daily_kit.listing(daily_kit.catalogue, total=4)])
    second = DailyIngestService(session, store, adapter=again).run(
        daily_kit.run_date, capture_pdf=False
    )

    assert second.counts() == {"ALREADY_PRESENT": 3, "SKIPPED_NOT_RELEVANT": 1}
    assert again.fetched == []  # no se vuelve a pedir nada a la fuente
    assert len(session.execute(select(m.LegalDocument)).scalars().all()) == 3
    # La bitácora del día responde igual aunque el documento entrara en otra corrida.
    assert all(
        item.events_extracted is not None
        for item in second.items
        if item.status is e.CrawlItemStatus.ALREADY_PRESENT
    )


def test_pagination_is_followed_until_the_declared_total(session, store, daily_kit):
    pages = [
        daily_kit.listing(daily_kit.catalogue[:2], total=4, next_start=20),
        daily_kit.listing(daily_kit.catalogue[2:], total=4),
    ]
    service = DailyIngestService(session, store, adapter=daily_kit.adapter(pages))
    result = service.run(daily_kit.run_date, capture_pdf=False)
    assert result.listing_pages == 2
    assert len(result.items) == 4


def test_incomplete_pagination_fails_the_run_instead_of_undercounting(session, store, daily_kit):
    """Si la fuente declara más de lo que se recogió, la corrida falla: un día
    incompleto se leería como "ese día se publicó menos"."""
    adapter = daily_kit.adapter([daily_kit.listing(daily_kit.catalogue, total=9)])
    result = DailyIngestService(session, store, adapter=adapter).run(
        daily_kit.run_date, capture_pdf=False
    )
    assert result.status == "FAILED"
    assert "declara 9" in (result.error_summary or "")
    assert result.items == []
    run = session.execute(select(m.CrawlRun)).scalar_one()
    assert run.status == "FAILED"
    assert run.error_summary


def test_transient_failure_is_kept_for_an_explicit_retry(session, store, daily_kit):
    """El 404 observado en esta fuente es a veces pasajero: se registra para
    reintentarlo aparte, nunca se reintenta dentro de la misma corrida."""
    error = CaptureHttpError("404", status_code=404, url="x")
    adapter = daily_kit.adapter(
        [daily_kit.listing(daily_kit.catalogue, total=4)], fail_codes={"2540861-1": error}
    )
    service = DailyIngestService(session, store, adapter=adapter)
    result = service.run(daily_kit.run_date, capture_pdf=False)

    assert result.counts()["RETRY_PENDING"] == 1
    assert result.status == "PARTIAL"
    assert adapter.fetched.count("2540861-1") == 1  # una petición, sin reintento en caliente

    row = session.execute(
        select(m.CrawlItem).where(m.CrawlItem.publication_code == "2540861-1")
    ).scalar_one()
    assert row.status is e.CrawlItemStatus.RETRY_PENDING
    assert "404" in (row.last_error or "")
    assert row.attempts == 1

    retried = service.retry_pending(capture_pdf=False)
    assert [item.status for item in retried] == [e.CrawlItemStatus.INGESTED]
    session.refresh(row)
    assert row.status is e.CrawlItemStatus.INGESTED
    assert row.attempts == 2
    assert row.last_error is None


def test_parse_failure_is_final_not_retryable(session, store, daily_kit):
    adapter = daily_kit.adapter(
        [daily_kit.listing(daily_kit.catalogue, total=4)],
        fail_codes={"2540861-1": ValueError("HTML contaminado")},
    )
    result = DailyIngestService(session, store, adapter=adapter).run(
        daily_kit.run_date, capture_pdf=False
    )
    assert result.counts()["FAILED"] == 1
    # Un dispositivo roto no tumba la corrida: los demás siguen entrando.
    assert result.counts()["INGESTED"] == 2
    row = session.execute(
        select(m.CrawlItem).where(m.CrawlItem.publication_code == "2540861-1")
    ).scalar_one()
    assert row.status is e.CrawlItemStatus.FAILED


def test_failed_items_can_be_retried_once_the_parser_is_fixed(session, store, daily_kit):
    """Un fallo de parser es final para la corrida, pero no para siempre.

    Corregido el parser, `retry-pending --include-failed` es la vía de
    recuperación: reintenta lo FAILED sin tocar el comportamiento por defecto.
    """
    broken = daily_kit.adapter(
        [daily_kit.listing(daily_kit.catalogue, total=4)],
        fail_codes={"2540861-1": ValueError("Campos obligatorios ausentes: número")},
    )
    DailyIngestService(session, store, adapter=broken).run(daily_kit.run_date, capture_pdf=False)
    row = session.execute(
        select(m.CrawlItem).where(m.CrawlItem.publication_code == "2540861-1")
    ).scalar_one()
    assert row.status is e.CrawlItemStatus.FAILED

    # Sin el flag, lo FAILED no se toca: sigue siendo un fallo final.
    fixed = daily_kit.adapter([daily_kit.listing(daily_kit.catalogue, total=4)])
    service = DailyIngestService(session, store, adapter=fixed)
    assert service.retry_pending(capture_pdf=False) == []

    retried = service.retry_pending(capture_pdf=False, include_failed=True)
    assert [item.status for item in retried] == [e.CrawlItemStatus.INGESTED]
    session.refresh(row)
    assert row.status is e.CrawlItemStatus.INGESTED
    assert row.attempts == 2
    assert row.last_error is None


def test_failed_item_now_irrelevant_is_skipped_without_fetching(session, store, daily_kit):
    """Entre el fallo y el reintento las reglas de relevancia pueden cambiar.

    Pasó con las apelaciones electorales del JNE (regla 1.1): decenas de
    dispositivos FAILED que hoy ni se ingerirían. El reintento los reevalúa con
    las reglas vigentes y los despacha sin gastar una petición en ellos.
    """
    broken = daily_kit.adapter(
        [daily_kit.listing(daily_kit.catalogue, total=4)],
        fail_codes={"2540861-1": ValueError("Campos obligatorios ausentes: número")},
    )
    DailyIngestService(session, store, adapter=broken).run(daily_kit.run_date, capture_pdf=False)
    row = session.execute(
        select(m.CrawlItem).where(m.CrawlItem.publication_code == "2540861-1")
    ).scalar_one()
    assert row.status is e.CrawlItemStatus.FAILED
    # La sumilla con que se descubrió era de otra época de las reglas; hoy es
    # una apelación electoral que el filtro descarta.
    row.summary_raw = "Confirman la Resolución N.º 00028-2026-JEE-QSPI/JNE emitida por el JEE"
    session.flush()

    would_fail = daily_kit.adapter(
        [daily_kit.listing(daily_kit.catalogue, total=4)],
        fail_codes={"2540861-1": ValueError("no debería pedirse")},
    )
    service = DailyIngestService(session, store, adapter=would_fail)
    retried = service.retry_pending(capture_pdf=False, include_failed=True)

    assert [item.status for item in retried] == [e.CrawlItemStatus.SKIPPED_NOT_RELEVANT]
    assert would_fail.fetched == []  # se despachó sin tocar la fuente
    session.refresh(row)
    assert row.status is e.CrawlItemStatus.SKIPPED_NOT_RELEVANT
    assert row.relevance is e.Relevance.NOT_RELEVANT
    assert "reevaluado al reintentar" in (row.outcome_detail or "")


def test_database_error_on_one_device_does_not_abort_the_run(session, store, daily_kit):
    """Un dato que no cabe en el esquema aborta la transacción en PostgreSQL.

    Ocurrió de verdad el 2026-08-07 (una sumilla larga en `label_raw`): la
    corrida entera se perdió, con sus treinta peticiones ya hechas a la fuente.
    Cada dispositivo va ahora en su propio punto de guardado y el fallo se
    clasifica como definitivo: no mejora por reintentar.
    """
    error = DataError("INSERT …", {}, Exception("value too long for type character varying(200)"))
    adapter = daily_kit.adapter(
        [daily_kit.listing(daily_kit.catalogue, total=4)], fail_codes={"2540861-1": error}
    )
    result = DailyIngestService(session, store, adapter=adapter).run(
        daily_kit.run_date, capture_pdf=False
    )
    assert result.counts() == {"FAILED": 1, "INGESTED": 2, "SKIPPED_NOT_RELEVANT": 1}
    row = session.execute(
        select(m.CrawlItem).where(m.CrawlItem.publication_code == "2540861-1")
    ).scalar_one()
    assert row.status is e.CrawlItemStatus.FAILED
    assert "value too long" in (row.last_error or "")


def test_dry_run_writes_nothing(session, store, daily_kit):
    adapter = daily_kit.adapter([daily_kit.listing(daily_kit.catalogue, total=4)])
    result = DailyIngestService(session, store, adapter=adapter).run(
        daily_kit.run_date, dry_run=True
    )

    assert result.counts() == {"DISCOVERED": 3, "SKIPPED_NOT_RELEVANT": 1}
    assert adapter.fetched == []
    assert session.execute(select(m.CrawlRun)).scalars().all() == []
    assert session.execute(select(m.CrawlItem)).scalars().all() == []
    assert session.execute(select(m.LegalDocument)).scalars().all() == []


def test_limit_leaves_the_rest_discovered_not_dropped(session, store, daily_kit):
    adapter = daily_kit.adapter([daily_kit.listing(daily_kit.catalogue, total=4)])
    result = DailyIngestService(session, store, adapter=adapter).run(
        daily_kit.run_date, limit=1, capture_pdf=False
    )
    counts = result.counts()
    assert counts["INGESTED"] == 1
    assert counts["DISCOVERED"] == 2  # los no procesados quedan visibles, no perdidos
    pending = [item for item in result.items if item.status is e.CrawlItemStatus.DISCOVERED]
    assert all("límite" in item.detail for item in pending)


def test_include_not_relevant_ingests_the_whole_edition(session, store, daily_kit):
    adapter = daily_kit.adapter([daily_kit.listing(daily_kit.catalogue, total=4)])
    result = DailyIngestService(session, store, adapter=adapter).run(
        daily_kit.run_date, include_not_relevant=True, capture_pdf=False
    )
    assert result.counts() == {"INGESTED": 4}


def test_backfill_counts_events_of_older_log_rows(session, store, daily_kit):
    """Reparación determinista: la cuenta sale de los eventos ya persistidos."""
    adapter = daily_kit.adapter([daily_kit.listing(daily_kit.catalogue, total=4)])
    DailyIngestService(session, store, adapter=adapter).run(daily_kit.run_date, capture_pdf=False)
    for row in session.execute(select(m.CrawlItem)).scalars():
        row.events_extracted = None  # simula las filas anteriores a la columna
    session.flush()

    assert backfill_event_counts(session, dry_run=True)
    # El dry-run no escribe.
    assert all(r.events_extracted is None for r in session.execute(select(m.CrawlItem)).scalars())

    backfill_event_counts(session)
    rows = {r.publication_code: r for r in session.execute(select(m.CrawlItem)).scalars()}
    assert rows["2540861-1"].events_extracted == 1
    # Lo descartado no tiene publicación, así que no se le inventa una cuenta.
    assert rows["2540896-1"].events_extracted is None


def test_backfill_links_old_publications_to_their_issue(session, store, daily_kit, ingest_service):
    """La edición sale de la fecha que declara la captura, no de cuándo se ingirió."""
    ingest_service.ingest_fixture("2540861-1", FIXTURES_DIR)
    session.flush()
    # Sin edición registrada no se enlaza nada: no se inventan cuadernillos.
    before = backfill_issue_links(session)
    assert [r.issue_code for r in before] == [None]
    assert "no hay edición registrada" in before[0].detail

    ensure_issue(session, "NL20260806")
    after = backfill_issue_links(session)
    linked = [r for r in after if r.issue_code == "NL20260806"]
    assert [r.publication_code for r in linked] == ["2540861-1"]
    item = session.execute(
        select(m.PublicationItem).where(m.PublicationItem.publication_code == "2540861-1")
    ).scalar_one()
    assert item.issue_id is not None


def test_device_listed_under_another_date_is_not_ingested(session, store, daily_kit):
    """La tarjeta repite su fecha; si no es la de la edición consultada, no se
    ingiere bajo una edición que no es la suya."""
    html = (
        b"<html><body><p>Dispositivos del 06/08/2026 al 06/08/2026</p>"
        b"<p>1 dispositivos encontrados</p>"
        b"<div class='rounded-xl border bg-card'>"
        b"<p>ENTIDAD</p>"
        b"<a href='/dispositivo/NL/2540861-1'>Designan Jefa</a>"
        b"<span>2540861-1</span><span>lunes 03.08.2026</span>"
        b"</div></body></html>"
    )
    adapter = daily_kit.adapter([html])
    result = DailyIngestService(session, store, adapter=adapter).run(
        daily_kit.run_date, capture_pdf=False
    )
    assert result.counts() == {"FAILED": 1}
    assert "2026-08-03" in result.items[0].detail
    assert adapter.fetched == []
