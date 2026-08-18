"""Resolución de puestos sin órgano por la emisora declarada del documento.

Un puesto nace sin organización cuando el texto del cargo no la nombra
("Coordinador de Recursos Humanos", sin más). La regla 21 prohíbe inferirla,
pero hay un caso en que no hace falta inferir: cuando la resolución la dicta la
propia entidad sobre su propio personal, la emisora —que el índice del diario
declaró y cuya cita está archivada— es el órgano del puesto por la naturaleza
del acto, no por adivinación.

La condición se exige estrecha a propósito:

- La emisora debe estar registrada y resuelta a una organización del catálogo
  curado que NO sea ministerio ni la PCM. Una Resolución Suprema o Ministerial
  designa con frecuencia en entidades adscritas (el Superintendente de la SBN
  se designa por RS del sector Vivienda): ahí la emisora NO es el órgano y
  atribuírselo fabricaría el dato. Los organismos (SBS, SUNAT, ANIN, …)
  resuelven por sí mismos sobre su propia planilla.
- El tipo documental tampoco puede ser Resolución Suprema ni Ministerial, por
  la misma razón, aunque la emisora sea un organismo.
- Todos los documentos que sostienen el puesto deben declarar la misma emisora;
  dos emisoras distintas son una contradicción que decide un humano.

Cada resolución queda escrita como ReviewDecision firmada por este paso, con la
emisora y el motivo en el payload: es una decisión del sistema con base
declarada, nunca silenciosa.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.application.review import ReviewService
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain.state_entities import catalog_entity

# Tipos documentales con los que un pliego resuelve sobre personal ajeno: la
# emisora no es el órgano del puesto y el fallback no aplica.
_VETOED_DOC_TYPES = frozenset(
    {e.DocumentTypeCode.RESOLUCION_SUPREMA, e.DocumentTypeCode.RESOLUCION_MINISTERIAL}
)


class IssuerResolutionOutcome(StrEnum):
    RESOLVED = "RESOLVED"
    VETOED_DOC_TYPE = "VETOED_DOC_TYPE"
    VETOED_ISSUER_KIND = "VETOED_ISSUER_KIND"
    NO_ISSUER = "NO_ISSUER"
    CONFLICTING_ISSUERS = "CONFLICTING_ISSUERS"
    NO_DOCUMENT = "NO_DOCUMENT"
    ALREADY_RESOLVED = "ALREADY_RESOLVED"


@dataclass(frozen=True)
class IssuerResolutionResult:
    task_id: str
    position_label: str | None
    outcome: IssuerResolutionOutcome
    detail: str


def _live_org(session: Session, org_id: str) -> m.Organization | None:
    org = session.get(m.Organization, org_id)
    hops = 0
    while org is not None and org.merged_into_organization_id is not None and hops < 10:
        org = session.get(m.Organization, org.merged_into_organization_id)
        hops += 1
    return org


def _documents_of_position(session: Session, position_id: str) -> list[m.LegalDocument]:
    rows = (
        session.execute(
            select(m.LegalDocument)
            .join(
                m.PersonnelEvent,
                m.PersonnelEvent.legal_document_id == m.LegalDocument.id,
            )
            .join(
                m.RoleAssignment,
                (m.RoleAssignment.start_event_id == m.PersonnelEvent.id)
                | (m.RoleAssignment.end_event_id == m.PersonnelEvent.id),
            )
            .where(m.RoleAssignment.position_id == position_id)
        )
        .scalars()
        .all()
    )
    return list({doc.id: doc for doc in rows}.values())


def resolve_pending_position_orgs(
    session: Session, *, dry_run: bool = False
) -> list[IssuerResolutionResult]:
    """Re-evalúa las tareas POSITION_ORG_UNRESOLVED pendientes con la emisora."""
    tasks = (
        session.execute(
            select(m.ReviewTask)
            .where(
                m.ReviewTask.task_type == e.ReviewTaskType.POSITION_ORG_UNRESOLVED,
                m.ReviewTask.status == e.ReviewTaskStatus.PENDING,
            )
            .order_by(m.ReviewTask.created_at)
        )
        .scalars()
        .all()
    )
    service = ReviewService(session)
    results: list[IssuerResolutionResult] = []
    for task in tasks:
        position = session.get(m.Position, task.target_id)
        label = position.preferred_label if position else None
        if position is None or position.organization_id is not None:
            results.append(
                IssuerResolutionResult(
                    task.id,
                    label,
                    IssuerResolutionOutcome.ALREADY_RESOLVED,
                    "el puesto ya tiene organización u objetivo ausente; nada que decidir aquí",
                )
            )
            continue
        documents = _documents_of_position(session, position.id)
        if not documents:
            results.append(
                IssuerResolutionResult(
                    task.id,
                    label,
                    IssuerResolutionOutcome.NO_DOCUMENT,
                    "ninguna asignación sostiene el puesto; lo barrerá el reproceso",
                )
            )
            continue

        issuer_orgs: dict[str, m.Organization] = {}
        missing_issuer = False
        for doc in documents:
            if doc.issuer_mention_id is None:
                missing_issuer = True
                continue
            mention = session.get(m.OrganizationMention, doc.issuer_mention_id)
            if mention is None or mention.canonical_organization_id is None:
                missing_issuer = True
                continue
            org = _live_org(session, mention.canonical_organization_id)
            if org is None:
                missing_issuer = True
                continue
            issuer_orgs[org.id] = org
        if missing_issuer or not issuer_orgs:
            results.append(
                IssuerResolutionResult(
                    task.id,
                    label,
                    IssuerResolutionOutcome.NO_ISSUER,
                    "algún documento del puesto no declara emisora resuelta",
                )
            )
            continue
        if len(issuer_orgs) > 1:
            names = ", ".join(sorted(o.preferred_name for o in issuer_orgs.values()))
            results.append(
                IssuerResolutionResult(
                    task.id,
                    label,
                    IssuerResolutionOutcome.CONFLICTING_ISSUERS,
                    f"emisoras distintas entre documentos ({names}); decide un humano",
                )
            )
            continue
        issuer = next(iter(issuer_orgs.values()))

        vetoed_types = sorted(
            {
                str(doc.document_type_code)
                for doc in documents
                if doc.document_type_code in _VETOED_DOC_TYPES
            }
        )
        if vetoed_types:
            results.append(
                IssuerResolutionResult(
                    task.id,
                    label,
                    IssuerResolutionOutcome.VETOED_DOC_TYPE,
                    f"{', '.join(vetoed_types)}: un pliego designa con frecuencia en "
                    f"entidades adscritas; la emisora no prueba el órgano",
                )
            )
            continue
        entry = catalog_entity(issuer.name_normalized)
        if entry is None or entry.entity_type in ("MINISTRY", "EXECUTIVE_OFFICE"):
            results.append(
                IssuerResolutionResult(
                    task.id,
                    label,
                    IssuerResolutionOutcome.VETOED_ISSUER_KIND,
                    f"la emisora '{issuer.preferred_name}' no es un organismo del catálogo "
                    f"que resuelva sobre su propia planilla",
                )
            )
            continue

        detail = (
            f"'{label}' -> {issuer.preferred_name} (emisora declarada por el índice; "
            f"resolución del propio organismo)"
        )
        results.append(
            IssuerResolutionResult(task.id, label, IssuerResolutionOutcome.RESOLVED, detail)
        )
        if dry_run:
            continue
        service.decide(
            task.id,
            e.DecisionAction.RESOLVE_POSITION,
            reviewer="resolve-positions-by-issuer",
            payload={"organization_id": issuer.id},
            notes=(
                "la resolución la dicta el propio organismo sobre su personal: la emisora "
                "declarada por el índice del diario es el órgano del puesto; el texto del "
                "cargo no nombra ningún otro"
            ),
        )
    return results
