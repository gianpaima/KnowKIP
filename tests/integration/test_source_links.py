"""Backfill de los punteros a la fuente sobre datos ya ingeridos.

Garantías congeladas aquí:
- El PDF declarado se re-deriva de los bytes del CAS, sin tocar la red.
- `--dry-run` no escribe.
- Si los bytes no están o no cuadran con el sha256 registrado, no se inventa
  procedencia: se informa y se deja el valor anterior intacto.
"""

from __future__ import annotations

from sqlalchemy import select

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.application.source_links import LinkOutcome, backfill_pdf_urls
from kipu_knowledge.domain import enums as e


def _items(session) -> list[m.PublicationItem]:
    return list(session.execute(select(m.PublicationItem)).scalars().all())


def test_backfill_sets_the_declared_file_pdf_url(ingested_session, store):
    # Simula el estado real anterior a la migración: columna vacía.
    for item in _items(ingested_session):
        item.pdf_url = None
    ingested_session.flush()

    results = backfill_pdf_urls(ingested_session, store)

    assert results and all(r.outcome == LinkOutcome.UPDATED for r in results), [
        (r.publication_code, str(r.outcome)) for r in results
    ]
    for item in _items(ingested_session):
        assert item.pdf_url.endswith(f"/{item.publication_code}.PDF")


def test_backfill_registers_publisher_and_authoritative_source(ingested_session, store):
    """Estado de las filas anteriores a la migración: sin publicador ni vínculo."""
    for item in _items(ingested_session):
        item.source_system_id = None
    for link in ingested_session.execute(select(m.DocumentSource)).scalars().all():
        ingested_session.delete(link)
    ingested_session.flush()

    backfill_pdf_urls(ingested_session, store)

    for item in _items(ingested_session):
        assert item.source_system_id, f"{item.publication_code} quedó sin sistema fuente"
    links = ingested_session.execute(select(m.DocumentSource)).scalars().all()
    assert links and all(link.role == e.DocumentSourceRole.AUTHORITATIVE for link in links)


def test_dry_run_reports_without_writing(ingested_session, store):
    for item in _items(ingested_session):
        item.pdf_url = None
    ingested_session.flush()

    results = backfill_pdf_urls(ingested_session, store, dry_run=True)

    assert all(r.outcome == LinkOutcome.UPDATED for r in results)
    assert all(item.pdf_url is None for item in _items(ingested_session))


def test_second_run_is_a_no_op(ingested_session, store):
    backfill_pdf_urls(ingested_session, store)
    results = backfill_pdf_urls(ingested_session, store)
    assert all(r.outcome == LinkOutcome.UNCHANGED for r in results)


def test_tampered_capture_yields_no_link(ingested_session, store, monkeypatch):
    """Si los bytes no son los capturados, derivar de ellos sería inventar.

    Solo aplica cuando hay que leer la captura, es decir cuando la URL no puede
    derivarse del código —el caso de un publicador sin esa convención, como el
    portal de una entidad—. Para El Peruano la forma derivable evita el CAS.
    """
    item = _items(ingested_session)[0]
    item.pdf_url = None
    item.canonical_url = "https://www.gob.pe/institucion/midagri/normas-legales/8450966"
    ingested_session.flush()

    monkeypatch.setattr(type(store), "get", lambda self, key: b"<html>otra cosa</html>")

    results = backfill_pdf_urls(ingested_session, store)
    afectado = next(r for r in results if r.publication_code == item.publication_code)
    assert afectado.outcome == LinkOutcome.NO_CAPTURE
    assert "sha256" in afectado.detail
    assert item.pdf_url is None
