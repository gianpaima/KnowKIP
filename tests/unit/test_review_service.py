"""Pruebas del servicio de revisión: decisiones auditables sin borrar extracción."""

from __future__ import annotations

from sqlalchemy import select

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.application.review import ReviewService
from kipu_knowledge.domain import enums as e


def _pending_entity_task(session) -> m.ReviewTask:
    """Cualquier tarea pendiente de identidad sobre una mención de persona.

    Tras la corroboración por oficio unipersonal, los firmantes recurrentes ya no
    generan ENTITY_RESOLUTION; la resolución de identidad que queda en el corpus
    es PERSON_VARIANT_CHECK. Ambas usan los mismos manejadores de decisión.
    """
    task = (
        session.execute(
            select(m.ReviewTask).where(
                m.ReviewTask.task_type.in_(
                    (e.ReviewTaskType.ENTITY_RESOLUTION, e.ReviewTaskType.PERSON_VARIANT_CHECK)
                ),
                m.ReviewTask.target_type == "person_mention",
                m.ReviewTask.status == e.ReviewTaskStatus.PENDING,
            )
        )
        .scalars()
        .first()
    )
    assert task is not None, "el corpus debe dejar alguna tarea de identidad pendiente"
    return task


def test_link_entity_confirms_mention(ingested_session):
    task = _pending_entity_task(ingested_session)
    mention = ingested_session.get(m.PersonMention, task.target_id)
    person = (
        ingested_session.execute(
            select(m.Person)
            .join(m.PersonMention, m.PersonMention.canonical_person_id == m.Person.id)
            .where(m.PersonMention.text_normalized == mention.text_normalized)
        )
        .scalars()
        .first()
    )
    ReviewService(ingested_session).decide(
        task.id, e.DecisionAction.LINK_ENTITY, reviewer="r", payload={"entity_id": person.id}
    )
    assert mention.canonical_person_id == person.id
    assert mention.resolution_status == e.ResolutionStatus.HUMAN_CONFIRMED
    assert task.status == e.ReviewTaskStatus.RESOLVED


def test_create_entity_makes_new_person(ingested_session):
    task = _pending_entity_task(ingested_session)
    mention = ingested_session.get(m.PersonMention, task.target_id)
    before = ingested_session.execute(select(m.Person)).scalars().all()
    ReviewService(ingested_session).decide(
        task.id, e.DecisionAction.CREATE_ENTITY, payload={"preferred_name": mention.text_raw}
    )
    after = ingested_session.execute(select(m.Person)).scalars().all()
    assert len(after) == len(before) + 1
    assert mention.resolution_status == e.ResolutionStatus.HUMAN_CONFIRMED


def test_split_entity_undoes_link(ingested_session):
    # Vincula primero, luego separa: la mención recibe persona propia.
    task = _pending_entity_task(ingested_session)
    mention = ingested_session.get(m.PersonMention, task.target_id)
    person = (
        ingested_session.execute(
            select(m.Person)
            .join(m.PersonMention, m.PersonMention.canonical_person_id == m.Person.id)
            .where(m.PersonMention.text_normalized == mention.text_normalized)
        )
        .scalars()
        .first()
    )
    service = ReviewService(ingested_session)
    service.decide(task.id, e.DecisionAction.LINK_ENTITY, payload={"entity_id": person.id})

    split_task = m.ReviewTask(
        task_type=e.ReviewTaskType.ENTITY_RESOLUTION,
        target_type="person_mention",
        target_id=mention.id,
        reason="separación solicitada",
    )
    ingested_session.add(split_task)
    ingested_session.flush()
    service.decide(split_task.id, e.DecisionAction.SPLIT_ENTITY)
    assert mention.canonical_person_id != person.id
    assert mention.resolution_status == e.ResolutionStatus.SPLIT
    # La persona original sigue existiendo (no se borra historia).
    assert ingested_session.get(m.Person, person.id) is not None


def test_supersede_keeps_chain(ingested_session):
    assertion = ingested_session.execute(select(m.Assertion)).scalars().first()
    replacement = m.Assertion(
        extraction_run_id=assertion.extraction_run_id,
        subject_type=assertion.subject_type,
        subject_id=assertion.subject_id,
        predicate=assertion.predicate,
        confidence=1.0,
        evidence_span_id=assertion.evidence_span_id,
        review_status=e.ReviewStatus.HUMAN_ACCEPTED,
    )
    ingested_session.add(replacement)
    ingested_session.flush()
    ReviewService(ingested_session).supersede(assertion.id, replacement.id)
    assert assertion.review_status == e.ReviewStatus.SUPERSEDED
    assert assertion.superseded_at is not None
    assert assertion.superseded_by_id == replacement.id
    # La afirmación original sigue consultable (no se borra).
    assert ingested_session.get(m.Assertion, assertion.id) is not None


def test_decisions_are_recorded(ingested_session):
    task = _pending_entity_task(ingested_session)
    ReviewService(ingested_session).decide(
        task.id, e.DecisionAction.DISMISS, reviewer="auditor", notes="duplicado"
    )
    decision = ingested_session.execute(
        select(m.ReviewDecision).where(m.ReviewDecision.review_task_id == task.id)
    ).scalar_one()
    assert decision.action == e.DecisionAction.DISMISS
    assert decision.reviewer == "auditor"
    assert task.status == e.ReviewTaskStatus.DISMISSED
