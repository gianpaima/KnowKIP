"""Captura de respaldo: PDFs, cuadernillo y fuentes adicionales del mismo acto.

Todo aquí toca la red y por tanto exige `LIVE_SOURCE_ENABLED=true`. Es la
diferencia entre guardar un enlace —que se cae o cambia sin aviso— y guardar los
bytes en el CAS, que es lo único que sobrevive a que la fuente reorganice su sitio.

Reglas que se mantienen:
- Los bytes van al CAS antes de cualquier otra cosa; el sha256 es la identidad.
- Una captura idéntica NO crea versión nueva: `artifact_version` es única por
  (artefacto, sha256). Solo un contenido distinto abre versión, encadenada con
  la anterior del mismo artefacto.
- Ninguna captura de una fuente no autoritativa produce afirmaciones. Se guarda
  como respaldo y contraste; extraer de ella exigiría decidir antes qué pasa
  cuando contradice al diario oficial.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.adapters.sources.elperuano import ElPeruanoSourceAdapter
from kipu_knowledge.adapters.sources.http_capture import PoliteFetcher
from kipu_knowledge.application.source_links import (
    get_or_create_official_gazette,
    official_publication_item,
)
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain.contracts import ArtifactStore, CaptureRecord


@dataclass(frozen=True)
class CaptureOutcome:
    url: str
    representation: e.RepresentationType
    sha256: str
    version_id: str
    created: bool
    byte_length: int
    detail: str


class CaptureError(RuntimeError):
    pass


def capture_representation(
    session: Session,
    store: ArtifactStore,
    *,
    item: m.PublicationItem,
    url: str,
    representation: e.RepresentationType,
    fetcher: PoliteFetcher | None = None,
) -> CaptureOutcome:
    """Descarga `url` y la guarda como una representación de `item`."""
    content, capture = (fetcher or PoliteFetcher()).get(url)
    return store_capture(
        session, store, item=item, representation=representation, content=content, capture=capture
    )


def store_capture(
    session: Session,
    store: ArtifactStore,
    *,
    item: m.PublicationItem,
    representation: e.RepresentationType,
    content: bytes,
    capture: CaptureRecord,
) -> CaptureOutcome:
    """Persiste bytes ya obtenidos como versión de la representación indicada."""
    _reject_mislabelled(representation, content, capture)
    stored = store.put_immutable(content)
    artifact = session.execute(
        select(m.Artifact).where(
            m.Artifact.publication_item_id == item.id,
            m.Artifact.representation_type == representation,
        )
    ).scalar_one_or_none()
    if artifact is None:
        artifact = m.Artifact(
            publication_item_id=item.id,
            representation_type=representation,
            media_type=capture.content_type,
        )
        session.add(artifact)
        session.flush()

    existing = session.execute(
        select(m.ArtifactVersion).where(
            m.ArtifactVersion.artifact_id == artifact.id,
            m.ArtifactVersion.sha256 == stored.sha256,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return CaptureOutcome(
            url=capture.requested_url or "",
            representation=representation,
            sha256=stored.sha256,
            version_id=existing.id,
            created=False,
            byte_length=stored.byte_length,
            detail="bytes idénticos a una captura anterior: no se crea versión",
        )

    previous = (
        session.execute(
            select(m.ArtifactVersion)
            .where(m.ArtifactVersion.artifact_id == artifact.id)
            .order_by(m.ArtifactVersion.captured_at.desc())
        )
        .scalars()
        .first()
    )
    version = m.ArtifactVersion(
        artifact_id=artifact.id,
        sha256=stored.sha256,
        byte_length=stored.byte_length,
        captured_at=capture.captured_at,
        http_status=capture.http_status,
        etag=capture.etag,
        last_modified=capture.last_modified,
        response_headers=capture.response_headers or None,
        requested_url=capture.requested_url,
        final_url=capture.final_url,
        object_key=stored.object_key,
        crawler_version=capture.crawler_version,
        previous_version_id=previous.id if previous else None,
    )
    session.add(version)
    session.flush()
    return CaptureOutcome(
        url=capture.requested_url or "",
        representation=representation,
        sha256=stored.sha256,
        version_id=version.id,
        created=True,
        byte_length=stored.byte_length,
        detail=(
            f"versión nueva encadenada con {previous.sha256[:12]}"
            if previous
            else "primera captura de esta representación"
        ),
    )


def capture_device_pdf(
    session: Session, store: ArtifactStore, publication_code: str
) -> CaptureOutcome:
    """Respalda el PDF del dispositivo publicado en el diario oficial."""
    item = _gazette_item(session, publication_code)
    if not item.pdf_url:
        raise CaptureError(
            f"{publication_code} no tiene URL de PDF registrada; "
            f"ejecuta antes `kipu backfill-source-links`"
        )
    return capture_representation(
        session,
        store,
        item=item,
        url=item.pdf_url,
        representation=e.RepresentationType.PDF,
    )


def recapture_html(session: Session, store: ArtifactStore, publication_code: str) -> CaptureOutcome:
    """Vuelve a capturar el HTML y abre versión solo si los bytes cambiaron.

    Es el mecanismo de seguimiento: si la fuente corrige el texto (fe de erratas,
    reemplazo silencioso), queda una versión nueva encadenada y la anterior
    intacta. Si no cambió nada, no se ensucia el histórico.
    """
    item = _gazette_item(session, publication_code)
    if not item.canonical_url:
        raise CaptureError(f"{publication_code} no tiene URL canónica registrada")
    return capture_representation(
        session,
        store,
        item=item,
        url=item.canonical_url,
        representation=e.RepresentationType.HTML,
    )


def link_external_source(
    session: Session,
    store: ArtifactStore,
    *,
    publication_code: str,
    system_name: str,
    system_base_url: str,
    authority: e.SourceAuthority,
    external_code: str,
    landing_url: str,
    pdf_url: str | None,
    matched_by: str,
    capture: bool = True,
) -> tuple[m.PublicationItem, list[CaptureOutcome]]:
    """Registra otra publicación del mismo acto y, si se pide, la respalda.

    `matched_by` es obligatorio y se guarda tal cual: afirmar que dos
    publicaciones son el mismo acto es una decisión, y quien la toma debe dejar
    escrito en qué se basó. El sistema no empareja fuentes por su cuenta.
    """
    gazette_item = _gazette_item(session, publication_code)
    document = session.execute(
        select(m.LegalDocument).where(m.LegalDocument.publication_item_id == gazette_item.id)
    ).scalar_one_or_none()
    if document is None:
        raise CaptureError(f"{publication_code} no tiene documento parseado al que vincular")

    system = session.execute(
        select(m.SourceSystem).where(m.SourceSystem.name == system_name)
    ).scalar_one_or_none()
    if system is None:
        system = m.SourceSystem(
            name=system_name,
            base_url=system_base_url,
            source_family=_family_of(system_base_url),
            authority=authority,
            policy_status="DOCUMENTED",
        )
        session.add(system)
        session.flush()

    item = session.execute(
        select(m.PublicationItem).where(
            m.PublicationItem.source_system_id == system.id,
            m.PublicationItem.publication_code == external_code,
        )
    ).scalar_one_or_none()
    if item is None:
        item = m.PublicationItem(
            source_system_id=system.id,
            source_series=system.source_family,
            publication_code=external_code,
            canonical_url=landing_url,
            pdf_url=pdf_url,
        )
        session.add(item)
        session.flush()
    else:
        item.canonical_url = landing_url
        item.pdf_url = pdf_url or item.pdf_url

    existing_link = session.execute(
        select(m.DocumentSource).where(
            m.DocumentSource.legal_document_id == document.id,
            m.DocumentSource.publication_item_id == item.id,
        )
    ).scalar_one_or_none()
    if existing_link is None:
        session.add(
            m.DocumentSource(
                legal_document_id=document.id,
                publication_item_id=item.id,
                # Nunca AUTHORITATIVE: esa es la publicación de la que se extrajo,
                # y solo puede haber una.
                role=e.DocumentSourceRole.CORROBORATING,
                matched_by=matched_by,
            )
        )
        session.flush()

    outcomes: list[CaptureOutcome] = []
    if capture:
        # Un solo fetcher para las dos descargas: comparte el reloj del rate
        # limit, de modo que gob.pe recibe el mismo trato que el diario oficial.
        fetcher = PoliteFetcher()
        for url, representation in (
            (landing_url, e.RepresentationType.HTML),
            (pdf_url, e.RepresentationType.PDF),
        ):
            if not url:
                continue
            outcomes.append(
                capture_representation(
                    session,
                    store,
                    item=item,
                    url=url,
                    representation=representation,
                    fetcher=fetcher,
                )
            )
    return item, outcomes


def ensure_issue(
    session: Session, issue_code: str, publication_date: str | None = None
) -> tuple[m.PublicationIssue, m.PublicationItem]:
    """Edición del diario oficial y su publicación propia, sin tocar la red.

    La edición es una publicación en sí misma —tiene cuadernillo e índice—, así
    que se le da un `PublicationItem` para no colgar sus artefactos de un ítem
    que no le toca. La fecha no se deduce de la de sus normas: sale del propio
    código de edición, que es lo que la fuente declara.
    """
    system = get_or_create_official_gazette(session)

    issue = session.execute(
        select(m.PublicationIssue).where(
            m.PublicationIssue.source_system_id == system.id,
            m.PublicationIssue.issue_code == issue_code,
        )
    ).scalar_one_or_none()
    if issue is None:
        issue = m.PublicationIssue(
            source_system_id=system.id,
            family=issue_code[:2],
            issue_code=issue_code,
            publication_date=_date_from_issue_code(issue_code, publication_date),
        )
        session.add(issue)
        session.flush()

    item = session.execute(
        select(m.PublicationItem).where(
            m.PublicationItem.source_system_id == system.id,
            m.PublicationItem.publication_code == issue_code,
        )
    ).scalar_one_or_none()
    if item is None:
        item = m.PublicationItem(
            source_system_id=system.id,
            issue_id=issue.id,
            source_series=issue.family,
            publication_code=issue_code,
            canonical_url=ElPeruanoSourceAdapter().issue_url_for(issue_code),
        )
        session.add(item)
        session.flush()
    return issue, item


def capture_issue(
    session: Session, store: ArtifactStore, issue_code: str, publication_date: str | None = None
) -> tuple[m.PublicationIssue, CaptureOutcome]:
    """Captura el cuadernillo de una edición y lo registra como PublicationIssue."""
    adapter = ElPeruanoSourceAdapter()
    url = adapter.issue_url_for(issue_code)
    issue, item = ensure_issue(session, issue_code, publication_date)

    content, capture = adapter.fetch_url(url)
    outcome = store_capture(
        session,
        store,
        item=item,
        representation=e.RepresentationType.HTML,
        content=content,
        capture=capture,
    )
    return issue, outcome


def _reject_mislabelled(
    representation: e.RepresentationType, content: bytes, capture: CaptureRecord
) -> None:
    """Impide guardar como PDF algo que no lo es.

    Nació de un error real: `…/dispositivo/NL/<código>/pdf` parece la URL del
    archivo y se deriva limpiamente del código, pero devuelve el **visor** HTML.
    Se archivó una página web como si fuera el PDF de respaldo, y nada avisó. El
    tipo declarado por el servidor puede mentir o faltar, así que la comprobación
    es sobre los bytes.
    """
    if representation not in (e.RepresentationType.PDF, e.RepresentationType.ISSUE_PDF):
        return
    if content.startswith(b"%PDF-"):
        return
    raise CaptureError(
        f"{capture.requested_url} no devolvió un PDF (content-type "
        f"{capture.content_type!r}, empieza por {content[:16]!r}). "
        f"En El Peruano la URL del archivo es la que declara el visor "
        f"(/api/archivo/file/<token>/*/<código>.PDF); la ruta /pdf del dispositivo "
        f"es la página del visor, no el documento."
    )


def _gazette_item(session: Session, publication_code: str) -> m.PublicationItem:
    item = official_publication_item(session, publication_code)
    if item is None:
        raise CaptureError(f"Publicación no ingerida en el diario oficial: {publication_code}")
    return item


def _family_of(base_url: str) -> str:
    host = base_url.split("//", 1)[-1].split("/", 1)[0]
    return host.replace("www.", "").split(".")[0].upper()[:20]


def _date_from_issue_code(issue_code: str, override: str | None):  # noqa: ANN202
    if override:
        return datetime.strptime(override, "%Y-%m-%d").replace(tzinfo=UTC).date()
    digits = "".join(c for c in issue_code if c.isdigit())
    if len(digits) != 8:
        return None
    return datetime.strptime(digits, "%Y%m%d").replace(tzinfo=UTC).date()
