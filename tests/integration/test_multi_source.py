"""Varias publicaciones del mismo acto, respaldo en el CAS y seguimiento de versiones.

Garantías congeladas aquí:
- Vincular otra fuente no duplica el documento ni le quita la autoridad a la
  publicación oficial.
- Una re-captura con los mismos bytes NO abre versión; con bytes distintos sí, y
  encadenada a la anterior del MISMO artefacto (nunca cruzando representaciones).
- Sin `LIVE_SOURCE_ENABLED` nada toca la red: falla explícitamente.

Ninguna prueba sale a internet: el fetcher se sustituye por un doble.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.adapters.sources.http_capture import LiveSourceDisabled, PoliteFetcher
from kipu_knowledge.application.capture import (
    CaptureError,
    capture_representation,
    link_external_source,
    recapture_html,
)
from kipu_knowledge.application.source_links import official_publication_item
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain.contracts import CaptureRecord

CODE = "2540861-1"  # Caso A: RM D000284-2026-MIDAGRI-DM, la del ejemplo del usuario
LANDING = "https://www.gob.pe/institucion/midagri/normas-legales/8450966-d000284-2026-midagri-dm"
CDN_PDF = "https://cdn.www.gob.pe/uploads/document/file/10412064/8450966.pdf"


class FakeFetcher(PoliteFetcher):
    """Fetcher que devuelve bytes fijos por URL, sin abrir un socket."""

    def __init__(self, responses: dict[str, bytes], content_type: str = "text/html") -> None:
        self._responses = responses
        self._content_type = content_type
        self.calls: list[str] = []

    def get(self, url: str) -> tuple[bytes, CaptureRecord]:
        self.calls.append(url)
        content = self._responses[url]
        return content, CaptureRecord(
            requested_url=url,
            final_url=url,
            http_status=200,
            content_type=self._content_type,
            byte_length=len(content),
            captured_at=datetime.now(UTC),
            crawler_version="test",
        )


def _gazette_item(session) -> m.PublicationItem:
    item = official_publication_item(session, CODE)
    assert item is not None
    return item


def test_linking_a_second_source_does_not_duplicate_the_document(ingested_session, store):
    before = len(ingested_session.execute(select(m.LegalDocument)).scalars().all())

    item, outcomes = link_external_source(
        ingested_session,
        store,
        publication_code=CODE,
        system_name="gob.pe - MIDAGRI",
        system_base_url="https://www.gob.pe",
        authority=e.SourceAuthority.ISSUING_ENTITY,
        external_code="8450966",
        landing_url=LANDING,
        pdf_url=CDN_PDF,
        matched_by="mismo número de resolución y misma entidad emisora",
        capture=False,
    )

    after = ingested_session.execute(select(m.LegalDocument)).scalars().all()
    assert len(after) == before, "vincular otra fuente no puede crear un documento nuevo"

    links = (
        ingested_session.execute(
            select(m.DocumentSource).where(m.DocumentSource.publication_item_id == item.id)
        )
        .scalars()
        .all()
    )
    assert len(links) == 1
    assert links[0].role == e.DocumentSourceRole.CORROBORATING, (
        "solo la publicación de la que se extrajo puede ser autoritativa"
    )
    assert links[0].matched_by, "la razón del emparejamiento debe quedar registrada"
    assert not outcomes, "con capture=False no se descarga nada"


def test_the_same_code_may_exist_in_two_sources(ingested_session, store):
    """El código dejó de ser único global: gob.pe puede usar el mismo número."""
    item, _ = link_external_source(
        ingested_session,
        store,
        publication_code=CODE,
        system_name="gob.pe - MIDAGRI",
        system_base_url="https://www.gob.pe",
        authority=e.SourceAuthority.ISSUING_ENTITY,
        external_code=CODE,  # deliberadamente el mismo
        landing_url=LANDING,
        pdf_url=None,
        matched_by="prueba de colisión de códigos entre fuentes",
        capture=False,
    )
    same_code = (
        ingested_session.execute(
            select(m.PublicationItem).where(m.PublicationItem.publication_code == CODE)
        )
        .scalars()
        .all()
    )
    assert len(same_code) == 2
    assert len({p.source_system_id for p in same_code}) == 2
    assert item.source_system_id != _gazette_item(ingested_session).source_system_id


def test_identical_recapture_does_not_open_a_version(ingested_session, store, monkeypatch):
    item = _gazette_item(ingested_session)
    original = store.get(
        ingested_session.execute(
            select(m.ArtifactVersion)
            .join(m.Artifact, m.Artifact.id == m.ArtifactVersion.artifact_id)
            .where(m.Artifact.publication_item_id == item.id)
        )
        .scalars()
        .first()
        .object_key
    )
    fake = FakeFetcher({item.canonical_url: original})
    monkeypatch.setattr("kipu_knowledge.application.capture.PoliteFetcher", lambda *a, **k: fake)

    outcome = recapture_html(ingested_session, store, CODE)

    assert not outcome.created
    assert "idénticos" in outcome.detail
    versions = (
        ingested_session.execute(
            select(m.ArtifactVersion)
            .join(m.Artifact, m.Artifact.id == m.ArtifactVersion.artifact_id)
            .where(
                m.Artifact.publication_item_id == item.id,
                m.Artifact.representation_type == e.RepresentationType.HTML,
            )
        )
        .scalars()
        .all()
    )
    assert len(versions) == 1


def test_changed_bytes_open_a_chained_version(ingested_session, store, monkeypatch):
    """Si la fuente reemplaza el texto en silencio, queda constancia de ambas."""
    item = _gazette_item(ingested_session)
    fake = FakeFetcher({item.canonical_url: b"<html>texto corregido por la fuente</html>"})
    monkeypatch.setattr("kipu_knowledge.application.capture.PoliteFetcher", lambda *a, **k: fake)

    outcome = recapture_html(ingested_session, store, CODE)

    assert outcome.created
    nueva = ingested_session.get(m.ArtifactVersion, outcome.version_id)
    anterior = ingested_session.get(m.ArtifactVersion, nueva.previous_version_id)
    assert anterior is not None, "la versión nueva debe encadenar con la anterior"
    assert anterior.artifact_id == nueva.artifact_id, "la cadena no puede cruzar representaciones"
    assert anterior.sha256 != nueva.sha256


def test_pdf_capture_never_chains_to_the_html_series(ingested_session, store, monkeypatch):
    """Regresión: el encadenamiento tomaba la última versión HTML del ítem.

    La primera captura de un PDF quedaba colgando de una captura HTML, mezclando
    dos historiales distintos en una sola cadena.
    """
    item = _gazette_item(ingested_session)
    fake = FakeFetcher({item.pdf_url: b"%PDF-1.7 contenido"}, content_type="application/pdf")

    outcome = capture_representation(
        ingested_session,
        store,
        item=item,
        url=item.pdf_url,
        representation=e.RepresentationType.PDF,
        fetcher=fake,
    )

    version = ingested_session.get(m.ArtifactVersion, outcome.version_id)
    assert version.previous_version_id is None, (
        "la primera captura del PDF no tiene predecesora: la del HTML es otra serie"
    )
    artifact = ingested_session.get(m.Artifact, version.artifact_id)
    assert artifact.representation_type == e.RepresentationType.PDF


def test_an_html_viewer_page_is_never_archived_as_a_pdf(ingested_session, store):
    """Regresión de un error real cometido contra la fuente en vivo.

    `…/dispositivo/NL/<código>/pdf` parece la URL del archivo y se deriva
    limpiamente del código, pero devuelve el visor HTML. Se archivó esa página
    como respaldo PDF y nada avisó: el content-type del servidor decía
    `text/html` y aun así se guardó. Ahora la comprobación es sobre los bytes.
    """
    item = _gazette_item(ingested_session)
    viewer = f"{item.canonical_url}/pdf"
    fake = FakeFetcher({viewer: b"<!DOCTYPE html><html><head><title>Visor</title>"})

    with pytest.raises(CaptureError, match="no devolvi"):
        capture_representation(
            ingested_session,
            store,
            item=item,
            url=viewer,
            representation=e.RepresentationType.PDF,
            fetcher=fake,
        )

    # Y los bytes de verdad sí pasan.
    real = FakeFetcher({item.pdf_url: b"%PDF-1.7\nreal"}, content_type="application/pdf")
    outcome = capture_representation(
        ingested_session,
        store,
        item=item,
        url=item.pdf_url,
        representation=e.RepresentationType.PDF,
        fetcher=real,
    )
    assert outcome.created


def test_capture_refuses_without_the_live_flag(ingested_session, store):
    """La política manda: sin la bandera, ni una petición."""
    item = _gazette_item(ingested_session)
    with pytest.raises(LiveSourceDisabled):
        capture_representation(
            ingested_session,
            store,
            item=item,
            url=item.pdf_url,
            representation=e.RepresentationType.PDF,
        )


def test_linking_an_uningested_code_fails_explicitly(ingested_session, store):
    with pytest.raises(CaptureError, match="no ingerida"):
        link_external_source(
            ingested_session,
            store,
            publication_code="0000000-0",
            system_name="gob.pe - MIDAGRI",
            system_base_url="https://www.gob.pe",
            authority=e.SourceAuthority.ISSUING_ENTITY,
            external_code="8450966",
            landing_url=LANDING,
            pdf_url=None,
            matched_by="prueba",
            capture=False,
        )
