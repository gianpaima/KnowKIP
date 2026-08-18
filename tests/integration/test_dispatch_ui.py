"""Despacho en lote de tareas de resolución de identidad.

El lote no infiere nada: cada fila marcada es una decisión LINK_ENTITY del
revisor, firmada y con precedente. Solo se ofrecen menciones con exactamente
una ficha candidata por nombre; el resto sigue en la revisión una a una.
"""

from sqlalchemy import select

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.domain import enums as e


def _pending_single_candidate_task(session) -> tuple[m.ReviewTask, m.PersonMention, m.Person]:
    """Construye una mención pendiente cuya grafía coincide con una sola ficha.

    Parte de una mención real ya vinculada (de los casos A–H): la nueva mención
    comparte su grafía, así que el resolver propone exactamente esa ficha.
    """
    linked = (
        session.execute(
            select(m.PersonMention).where(m.PersonMention.canonical_person_id.is_not(None))
        )
        .scalars()
        .first()
    )
    person = session.get(m.Person, linked.canonical_person_id)
    doc = session.get(m.LegalDocument, linked.legal_document_id)
    mention = m.PersonMention(
        legal_document_id=doc.id,
        text_raw=linked.text_raw,
        text_normalized=linked.text_normalized,
        role_context_raw="Cargo de prueba",
        role_context_normalized="CARGO DE PRUEBA",
        evidence_span_id=linked.evidence_span_id,
        resolution_status=e.ResolutionStatus.CANDIDATE_MATCH,
    )
    session.add(mention)
    session.flush()
    task = m.ReviewTask(
        task_type=e.ReviewTaskType.ENTITY_RESOLUTION,
        target_type="person_mention",
        target_id=mention.id,
        reason="coincide por nombre con 1 persona(s) existentes",
    )
    session.add(task)
    session.commit()
    return task, mention, person


def test_dispatch_page_lists_single_candidate_mentions(api_client, ingested_session):
    task, mention, person = _pending_single_candidate_task(ingested_session)

    response = api_client.get("/review/dispatch")

    assert response.status_code == 200
    assert mention.text_raw in response.text
    assert person.preferred_name in response.text
    assert task.id in response.text


def test_dispatch_links_each_marked_row_as_a_signed_decision(api_client, ingested_session):
    task, mention, person = _pending_single_candidate_task(ingested_session)

    response = api_client.post(
        "/review/dispatch",
        data={"reviewer": "revisora de prueba", "task_id": [task.id]},
    )

    assert response.status_code == 200
    assert "1 mención(es) vinculada(s)" in response.text
    ingested_session.expire_all()
    assert ingested_session.get(m.ReviewTask, task.id).status is e.ReviewTaskStatus.RESOLVED
    refreshed = ingested_session.get(m.PersonMention, mention.id)
    assert refreshed.canonical_person_id == person.id
    assert refreshed.resolution_status is e.ResolutionStatus.HUMAN_CONFIRMED
    decision = ingested_session.execute(
        select(m.ReviewDecision).where(m.ReviewDecision.review_task_id == task.id)
    ).scalar_one()
    assert decision.reviewer == "revisora de prueba"
    assert decision.action is e.DecisionAction.LINK_ENTITY


def test_dispatch_requires_a_reviewer_signature(api_client, ingested_session):
    task, _mention, _person = _pending_single_candidate_task(ingested_session)

    response = api_client.post("/review/dispatch", data={"reviewer": "", "task_id": [task.id]})

    assert response.status_code == 422
    ingested_session.expire_all()
    assert ingested_session.get(m.ReviewTask, task.id).status is e.ReviewTaskStatus.PENDING
