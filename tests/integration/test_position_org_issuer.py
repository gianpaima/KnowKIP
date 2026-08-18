"""Resolución de puestos sin órgano por la emisora declarada del documento.

La condición es estrecha por diseño: solo resoluciones del propio organismo
(nunca un pliego por RS/RM, nunca un ministerio ni la PCM como emisora) y con
emisora única y resuelta. Todo lo demás queda para el revisor.
"""

from pathlib import Path

from sqlalchemy import select

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.application.position_org import (
    IssuerResolutionOutcome,
    resolve_pending_position_orgs,
)
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain.normalization import normalize_org_name

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"
CODE = "2540702-1"

SUNAT_NAME = "Superintendencia Nacional de Aduanas y de Administración Tributaria"


def _prepare(session, ingest_service, doc_type: e.DocumentTypeCode) -> tuple[str, str]:
    """Documento real con emisora del catálogo y un puesto sin órgano.

    Devuelve (task_id, org_id de la emisora).
    """
    ingest_service.ingest_fixture(CODE, FIXTURES_DIR)
    session.commit()
    doc = session.execute(select(m.LegalDocument)).scalars().one()
    doc.document_type_code = doc_type

    issuer = m.Organization(
        preferred_name=SUNAT_NAME,
        name_normalized=normalize_org_name(SUNAT_NAME),
        acronym="SUNAT",
        organization_type="SPECIALIZED_TECHNICAL_BODY",
    )
    session.add(issuer)
    session.flush()
    mention = m.OrganizationMention(
        legal_document_id=doc.id,
        text_raw=SUNAT_NAME.upper(),
        text_normalized=normalize_org_name(SUNAT_NAME),
        canonical_organization_id=issuer.id,
        resolution_status=e.ResolutionStatus.AUTO_LINKED,
    )
    session.add(mention)
    session.flush()
    doc.issuer_mention_id = mention.id

    position = m.Position(
        organization_id=None,
        preferred_label="Fedatario Administrativo",
        label_normalized="FEDATARIO ADMINISTRATIVO",
    )
    session.add(position)
    session.flush()
    task = m.ReviewTask(
        task_type=e.ReviewTaskType.POSITION_ORG_UNRESOLVED,
        target_type="position",
        target_id=position.id,
        reason="prueba: puesto sin organización determinable",
    )
    session.add(task)
    event = session.execute(select(m.PersonnelEvent)).scalars().first()
    person_mention = session.execute(select(m.PersonMention)).scalars().first()
    session.add(
        m.RoleAssignment(
            person_mention_id=person_mention.id,
            position_id=position.id,
            start_event_id=event.id,
        )
    )
    session.commit()
    return task.id, issuer.id


def test_own_body_resolution_attaches_the_declared_issuer(session, ingest_service):
    task_id, issuer_id = _prepare(session, ingest_service, e.DocumentTypeCode.RESOLUCION_JEFATURAL)

    results = resolve_pending_position_orgs(session)
    session.commit()

    assert [r.outcome for r in results] == [IssuerResolutionOutcome.RESOLVED]
    task = session.get(m.ReviewTask, task_id)
    assert task.status is e.ReviewTaskStatus.RESOLVED
    position = session.get(m.Position, task.target_id)
    assert position.organization_id == issuer_id
    decision = session.execute(
        select(m.ReviewDecision).where(m.ReviewDecision.review_task_id == task_id)
    ).scalar_one()
    assert decision.reviewer == "resolve-positions-by-issuer"
    assert decision.payload == {"organization_id": issuer_id}


def test_supreme_resolution_is_vetoed_because_the_issuer_does_not_prove_the_organ(
    session, ingest_service
):
    """Una RS designa con frecuencia en entidades adscritas (el Superintendente
    de la SBN se designa por RS del sector Vivienda): atribuir la emisora
    fabricaría el dato, así que el caso queda para el revisor."""
    task_id, _issuer_id = _prepare(session, ingest_service, e.DocumentTypeCode.RESOLUCION_SUPREMA)

    results = resolve_pending_position_orgs(session)
    session.commit()

    assert [r.outcome for r in results] == [IssuerResolutionOutcome.VETOED_DOC_TYPE]
    task = session.get(m.ReviewTask, task_id)
    assert task.status is e.ReviewTaskStatus.PENDING
    position = session.get(m.Position, task.target_id)
    assert position.organization_id is None


def test_dry_run_reports_without_deciding(session, ingest_service):
    task_id, _issuer_id = _prepare(session, ingest_service, e.DocumentTypeCode.RESOLUCION_JEFATURAL)

    results = resolve_pending_position_orgs(session, dry_run=True)
    session.commit()

    assert [r.outcome for r in results] == [IssuerResolutionOutcome.RESOLVED]
    assert session.get(m.ReviewTask, task_id).status is e.ReviewTaskStatus.PENDING
