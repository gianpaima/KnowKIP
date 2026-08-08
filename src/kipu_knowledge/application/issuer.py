"""El organismo emisor que el índice del diario oficial declara para cada dispositivo.

El dispositivo mismo no nombra a su emisor: el visor muestra sumilla, tipo,
número y texto, y el bloque de firma puede decir solo "Ministra", sin cartera
(verificado contra las capturas: la RM N.º 000399-2026-MIMP firma exactamente
así). Quien sí lo declara es el índice diario, que agrupa cada dispositivo bajo
el organismo que lo emite, y cuyos bytes quedan archivados como representación
LISTING de la edición. Es la misma página oficial cuya frase de fecha de
publicación ya se cita como evidencia (`application/legal_effect.py`), así que
el encabezado puede registrarse como mención de organización con cita literal.

Lo que ese encabezado NO es, es el nombre registral de la entidad: el índice
escribe "MUJER Y POBLACIONES VULNERABLES" donde la entidad se llama "Ministerio
de la Mujer y Poblaciones Vulnerables". Por eso la mención solo se vincula a una
organización canónica si la grafía normalizada coincide exactamente con una ya
existente, y **nunca crea una organización nueva**: fabricar entidades canónicas
desde rótulos de catálogo sembraría duplicados que después alguien tendría que
fusionar a mano.

Sin evidencia no se registra nada (regla 2): si los bytes del listado faltan,
no cuadran con su sha256 o no contienen el encabezado, el emisor queda sin
poner y el resultado lo dice.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.application.source_links import verified_bytes
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain.contracts import ArtifactStore
from kipu_knowledge.domain.normalization import normalize_org_name

RULE_VERSION = "issuer-from-index/1.0"
SPAN_KIND = "listing_issuer_heading"


class IssuerOutcome(StrEnum):
    LINKED = "LINKED"
    ALREADY_SET = "ALREADY_SET"
    NOT_DECLARED = "NOT_DECLARED"
    NO_CAPTURE = "NO_CAPTURE"
    NO_EVIDENCE = "NO_EVIDENCE"


@dataclass(frozen=True)
class IssuerResult:
    document_number: str | None
    publication_code: str | None
    outcome: IssuerOutcome
    issuer_raw: str | None
    detail: str


def _declared_issuer(session: Session, doc: m.LegalDocument) -> tuple[str, str] | None:
    """(encabezado del índice, versión del artefacto LISTING) que lo declaró.

    Sale de la bitácora del recolector por el código de la publicación
    autoritativa del documento. La bitácora no es evidencia —la evidencia son
    los bytes del listado que ella señala—; aquí solo dice dónde mirar.
    """
    item = session.get(m.PublicationItem, doc.publication_item_id)
    if item is None:
        return None
    row = session.execute(
        select(m.CrawlItem.issuer_raw, m.CrawlItem.listing_artifact_version_id)
        .join(m.CrawlRun, m.CrawlRun.id == m.CrawlItem.crawl_run_id)
        .where(
            m.CrawlItem.publication_code == item.publication_code,
            m.CrawlItem.issuer_raw.is_not(None),
            m.CrawlItem.listing_artifact_version_id.is_not(None),
        )
        .order_by(m.CrawlRun.started_at.desc())
    ).first()
    if row is None:
        return None
    return str(row[0]), str(row[1])


def _locate_heading(text: str, issuer_raw: str) -> tuple[int, int, str] | None:
    """Rango y texto literal del encabezado dentro del listado decodificado.

    El parser del índice colapsa espacios al leer el encabezado, así que se
    busca con espaciado flexible y se cita lo que el artefacto realmente dice,
    no la forma normalizada.
    """
    start = text.find(issuer_raw)
    if start >= 0:
        return start, start + len(issuer_raw), issuer_raw
    tokens = issuer_raw.split()
    if not tokens:
        return None
    pattern = r"\s+".join(re.escape(token) for token in tokens)
    match = re.search(pattern, text)
    if match is None:
        return None
    return match.start(), match.end(), match.group(0)


def ensure_document_issuer(
    session: Session,
    store: ArtifactStore,
    doc: m.LegalDocument,
    *,
    dry_run: bool = False,
) -> IssuerResult:
    """Registra el emisor que el índice declara, con su cita, si aún no consta.

    Idempotente: un documento con emisor puesto no se toca. Determinista y sin
    red: todo sale de la bitácora del recolector y de los bytes archivados del
    listado, verificados contra su sha256 antes de confiar en ellos.
    """
    item = session.get(m.PublicationItem, doc.publication_item_id)
    code = item.publication_code if item is not None else None

    def result(outcome: IssuerOutcome, issuer_raw: str | None, detail: str) -> IssuerResult:
        return IssuerResult(doc.number_raw, code, outcome, issuer_raw, detail)

    if doc.issuer_mention_id is not None:
        return result(IssuerOutcome.ALREADY_SET, None, "el documento ya tiene emisor registrado")

    declared = _declared_issuer(session, doc)
    if declared is None:
        return result(
            IssuerOutcome.NOT_DECLARED,
            None,
            "ninguna captura de índice declara el emisor de este documento "
            "(ingerido sin pasar por el recolector diario)",
        )
    issuer_raw, listing_version_id = declared

    version = session.get(m.ArtifactVersion, listing_version_id)
    content = verified_bytes(store, version) if version is not None else None
    if content is None:
        return result(
            IssuerOutcome.NO_CAPTURE,
            issuer_raw,
            "los bytes del listado no están en el CAS o no cuadran con su sha256; "
            "sin evidencia verificable no se registra el emisor",
        )

    located = _locate_heading(content.decode("utf-8", errors="replace"), issuer_raw)
    if located is None:
        return result(
            IssuerOutcome.NO_EVIDENCE,
            issuer_raw,
            "el encabezado declarado no aparece literal en los bytes del listado; "
            "sin cita no se registra el emisor",
        )
    char_start, char_end, quoted = located

    if dry_run:
        return result(IssuerOutcome.LINKED, issuer_raw, f"[dry-run] citaría «{quoted}»")

    span = m.EvidenceSpan(
        document_section_id=None,
        artifact_version_id=listing_version_id,
        char_start=char_start,
        char_end=char_end,
        quoted_text=quoted,
        quoted_text_sha256=hashlib.sha256(quoted.encode("utf-8")).hexdigest(),
        locator_json={
            "kind": SPAN_KIND,
            "rule": RULE_VERSION,
            "char_basis": "artifact_text_utf8",
            "publication_code": code,
        },
    )
    session.add(span)
    session.flush()

    normalized = normalize_org_name(issuer_raw)
    canonical = (
        session.execute(select(m.Organization).where(m.Organization.name_normalized == normalized))
        .scalars()
        .all()
    )
    # Solo la coincidencia exacta y única autoriza el vínculo automático; el
    # rótulo del índice no es el nombre registral y el parecido no decide nada.
    linked = canonical[0] if len(canonical) == 1 else None
    mention = m.OrganizationMention(
        legal_document_id=doc.id,
        text_raw=issuer_raw,
        text_normalized=normalized,
        canonical_organization_id=linked.id if linked is not None else None,
        resolution_status=(
            e.ResolutionStatus.AUTO_LINKED if linked is not None else e.ResolutionStatus.UNRESOLVED
        ),
        evidence_span_id=span.id,
    )
    session.add(mention)
    session.flush()
    doc.issuer_mention_id = mention.id
    session.flush()
    return result(
        IssuerOutcome.LINKED,
        issuer_raw,
        f"emisor «{issuer_raw}» citado del índice"
        + (f", vinculado a {linked.preferred_name}" if linked is not None else ", sin vincular"),
    )


def backfill_issuers(
    session: Session, store: ArtifactStore, *, dry_run: bool = False
) -> list[IssuerResult]:
    """Repara el emisor de los documentos ingeridos antes de esta regla.

    Determinista, auditable y sin red: relee los bytes archivados del índice y
    solo registra lo que puede citar. Los documentos que ningún índice declaró
    quedan como están, y el resultado lo dice de cada uno.
    """
    results: list[IssuerResult] = []
    docs = (
        session.execute(
            select(m.LegalDocument)
            .where(m.LegalDocument.issuer_mention_id.is_(None))
            .order_by(m.LegalDocument.number_normalized)
        )
        .scalars()
        .all()
    )
    for doc in docs:
        results.append(ensure_document_issuer(session, store, doc, dry_run=dry_run))
    session.flush()
    return results


__all__ = [
    "RULE_VERSION",
    "SPAN_KIND",
    "IssuerOutcome",
    "IssuerResult",
    "backfill_issuers",
    "ensure_document_issuer",
]
