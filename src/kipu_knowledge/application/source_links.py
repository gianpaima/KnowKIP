"""Re-derivación de los punteros a la fuente desde las capturas ya guardadas.

El PDF que declara cada captura empezó a registrarse después de que el corpus
estuviera ingerido, así que las filas existentes tienen `pdf_url` vacío. Esto lo
rellena **releyendo los bytes del CAS**, nunca la red: la captura es inmutable,
de modo que el resultado es reproducible y no depende de que El Peruano siga en
pie ni de que `LIVE_SOURCE_ENABLED` esté activo.

Solo toca `publication_item.pdf_url`. No crea documentos, ni menciones, ni
tareas, ni afirmaciones: por eso no entra en el flujo de supersede pendiente
para re-extraer la BD viva.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.adapters.parsing.html_parser import declared_pdf_url
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain.contracts import ArtifactStore


class LinkOutcome(StrEnum):
    UPDATED = "ACTUALIZADO"
    UNCHANGED = "SIN_CAMBIOS"
    NOT_DECLARED = "NO_DECLARADO"
    NO_CAPTURE = "SIN_CAPTURA"


@dataclass(frozen=True)
class LinkResult:
    publication_code: str
    outcome: LinkOutcome
    pdf_url: str | None
    detail: str


def backfill_pdf_urls(
    session: Session, store: ArtifactStore, *, dry_run: bool = False
) -> list[LinkResult]:
    """Pone al día los punteros a la fuente de cada publicación ya ingerida.

    Tres cosas que las filas anteriores no tienen: el sistema fuente que las
    publicó, su fila en `document_source` y la URL del archivo PDF que su propia
    captura declara. Ninguna requiere red.
    """
    gazette = _official_gazette(session)
    items = (
        session.execute(select(m.PublicationItem).order_by(m.PublicationItem.publication_code))
        .scalars()
        .all()
    )
    results: list[LinkResult] = []
    for item in items:
        notes: list[str] = []
        if item.source_system_id is None and gazette is not None:
            # Todo lo ingerido hasta ahora vino del diario oficial: no hay otra
            # fuente registrada de la que pudiera proceder.
            if not dry_run:
                item.source_system_id = gazette.id
            notes.append(f"sistema fuente <- {gazette.name}")
        notes.extend(_ensure_authoritative_source(session, item, dry_run=dry_run))

        resolved, outcome, detail = _resolve_pdf_url(session, store, item)
        if outcome is LinkOutcome.NO_CAPTURE or resolved is None:
            results.append(LinkResult(item.publication_code, outcome, item.pdf_url, detail))
            continue
        if resolved == item.pdf_url:
            outcome = LinkOutcome.UNCHANGED if not notes else LinkOutcome.UPDATED
            notes.append("PDF ya estaba al día")
        else:
            outcome = LinkOutcome.UPDATED
            notes.append(f"PDF <- {detail} (anterior: {item.pdf_url or 'vacío'})")
            if not dry_run:
                item.pdf_url = resolved
        results.append(
            LinkResult(item.publication_code, outcome, resolved, "; ".join(notes) or "sin cambios")
        )
    if not dry_run:
        session.flush()
    return results


def _resolve_pdf_url(
    session: Session, store: ArtifactStore, item: m.PublicationItem
) -> tuple[str | None, LinkOutcome, str]:
    """URL del archivo PDF de esta publicación.

    Es la que declara el payload de la captura. Se intentó preferir la forma
    derivable del código (`…/<código>/pdf`) por ser estable y sin token opaco,
    pero esa ruta devuelve el visor HTML, no el documento: guardarla como PDF
    archivaba una página web creyendo que era el respaldo. La derivable se sigue
    ofreciendo en la UI como enlace para mirar, no para descargar.
    """
    version = latest_html_version(session, item.id)
    if version is None:
        return None, LinkOutcome.NO_CAPTURE, "no hay ninguna versión HTML capturada"
    content = verified_bytes(store, version)
    if content is None:
        return (
            None,
            LinkOutcome.NO_CAPTURE,
            f"los bytes de {version.object_key} no están en el CAS o no coinciden "
            f"con el sha256 registrado",
        )
    declared = declared_pdf_url(
        content.decode("utf-8", errors="replace"), item.publication_code, item.canonical_url
    )
    if declared is None:
        return None, LinkOutcome.NOT_DECLARED, "la captura no declara un PDF para este código"
    return declared, LinkOutcome.UPDATED, "declarada por la captura"


def official_publication_item(
    session: Session, publication_code: str, source_series: str | None = None
) -> m.PublicationItem | None:
    """La publicación del diario oficial para un código.

    Desde que un acto puede estar publicado en varios sitios, el código dejó de
    identificar una fila: otro publicador puede reutilizar el mismo número. Toda
    búsqueda "por código" tiene que decir de qué fuente habla, y la respuesta por
    defecto es la que produce efectos jurídicos.
    """
    query = (
        select(m.PublicationItem)
        .join(m.SourceSystem, m.SourceSystem.id == m.PublicationItem.source_system_id)
        .where(
            m.PublicationItem.publication_code == publication_code,
            m.SourceSystem.authority == e.SourceAuthority.OFFICIAL_GAZETTE,
        )
    )
    if source_series is not None:
        query = query.where(m.PublicationItem.source_series == source_series)
    item = session.execute(query).scalars().first()
    if item is not None:
        return item
    # Filas anteriores a la migración multi-fuente: sin sistema fuente asignado,
    # y por definición del diario oficial, que era la única fuente que había.
    return (
        session.execute(
            select(m.PublicationItem).where(
                m.PublicationItem.publication_code == publication_code,
                m.PublicationItem.source_system_id.is_(None),
            )
        )
        .scalars()
        .first()
    )


def _official_gazette(session: Session) -> m.SourceSystem | None:
    return (
        session.execute(
            select(m.SourceSystem).where(
                m.SourceSystem.authority == e.SourceAuthority.OFFICIAL_GAZETTE
            )
        )
        .scalars()
        .first()
    )


def _ensure_authoritative_source(
    session: Session, item: m.PublicationItem, *, dry_run: bool
) -> list[str]:
    """Registra en `document_source` la publicación de la que se extrajo el documento."""
    doc = (
        session.execute(
            select(m.LegalDocument).where(m.LegalDocument.publication_item_id == item.id)
        )
        .scalars()
        .first()
    )
    if doc is None:
        return []
    existing = (
        session.execute(
            select(m.DocumentSource).where(
                m.DocumentSource.legal_document_id == doc.id,
                m.DocumentSource.publication_item_id == item.id,
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return []
    if not dry_run:
        session.add(
            m.DocumentSource(
                legal_document_id=doc.id,
                publication_item_id=item.id,
                role=e.DocumentSourceRole.AUTHORITATIVE,
                matched_by="publicación de la que se parseó el documento",
            )
        )
    return ["fuente autoritativa registrada"]


def latest_html_version(session: Session, item_id: str) -> m.ArtifactVersion | None:
    """Última captura HTML de una publicación (la que se parsea y se re-procesa).

    Distinta de "la última versión de este artefacto": aquí se busca *qué
    artefacto* representa el texto, no el histórico de una representación.
    """
    return (
        session.execute(
            select(m.ArtifactVersion)
            .join(m.Artifact, m.Artifact.id == m.ArtifactVersion.artifact_id)
            .where(
                m.Artifact.publication_item_id == item_id,
                m.Artifact.representation_type == e.RepresentationType.HTML,
            )
            .order_by(m.ArtifactVersion.captured_at.desc())
        )
        .scalars()
        .first()
    )


def verified_bytes(store: ArtifactStore, version: m.ArtifactVersion) -> bytes | None:
    """Bytes de la captura, solo si su sha256 sigue siendo el registrado.

    Derivar un enlace de bytes que no son los que se capturaron sería inventar
    procedencia; ante cualquier desajuste se prefiere no registrar nada.
    """
    if not store.exists_by_hash(version.sha256):
        return None
    content = store.get(version.object_key)
    if hashlib.sha256(content).hexdigest() != version.sha256:
        return None
    return content
