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


class TestOrganizationMerge:
    """LINK_ENTITY sobre una tarea de organización absorbe el duplicado."""

    @staticmethod
    def _build_duplicate(session) -> tuple[m.Organization, m.Organization, m.ReviewTask]:
        survivor = session.execute(select(m.Organization)).scalars().first()
        assert survivor is not None
        duplicate = m.Organization(
            preferred_name=f"{survivor.preferred_name}, bajo el régimen de la Ley N° 30057",
            name_normalized=f"{survivor.name_normalized}, BAJO EL REGIMEN DE LA LEY N° 30057",
        )
        session.add(duplicate)
        session.flush()
        session.add(
            m.OrganizationMention(
                text_raw=duplicate.preferred_name,
                text_normalized=duplicate.name_normalized,
                canonical_organization_id=duplicate.id,
                resolution_status=e.ResolutionStatus.AUTO_LINKED,
            )
        )
        task = m.ReviewTask(
            task_type=e.ReviewTaskType.ORG_VARIANT_CHECK,
            target_type="organization",
            target_id=duplicate.id,
            reason="prueba",
        )
        session.add(task)
        session.flush()
        return survivor, duplicate, task

    def test_merge_repoints_everything_and_keeps_the_row(self, ingested_session):
        session = ingested_session
        survivor, duplicate, task = self._build_duplicate(session)
        unit = m.OrganizationalUnit(
            organization_id=duplicate.id,
            preferred_name="Secretaría General",
            name_normalized="SECRETARIA GENERAL",
        )
        session.add(unit)
        session.flush()
        position = m.Position(
            organization_id=duplicate.id,
            organizational_unit_id=unit.id,
            preferred_label="Asesor de Secretaría General",
            label_normalized="ASESOR DE SECRETARIA GENERAL",
        )
        session.add(position)
        session.flush()
        assignment = session.execute(select(m.RoleAssignment)).scalars().first()
        assignment.organization_id = duplicate.id
        assignment.position_id = position.id
        session.flush()

        ReviewService(session).decide(
            task.id, e.DecisionAction.LINK_ENTITY, reviewer="r", payload={"entity_id": survivor.id}
        )

        assert duplicate.merged_into_organization_id == survivor.id
        assert task.status == e.ReviewTaskStatus.RESOLVED
        mention = session.execute(
            select(m.OrganizationMention).where(
                m.OrganizationMention.text_normalized == duplicate.name_normalized
            )
        ).scalar_one()
        assert mention.canonical_organization_id == survivor.id
        assert mention.resolution_status == e.ResolutionStatus.HUMAN_CONFIRMED
        # La unidad viaja a la superviviente o se pliega a su equivalente; en
        # ambos casos el duplicado queda sin unidades y el puesto apunta a una
        # unidad de la superviviente.
        assert (
            session.execute(
                select(m.OrganizationalUnit).where(
                    m.OrganizationalUnit.organization_id == duplicate.id
                )
            )
            .scalars()
            .first()
            is None
        )
        surviving_unit = session.get(m.OrganizationalUnit, position.organizational_unit_id)
        assert surviving_unit is not None
        assert surviving_unit.organization_id == survivor.id
        assert surviving_unit.name_normalized == "SECRETARIA GENERAL"
        assert position.organization_id == survivor.id
        assert assignment.organization_id == survivor.id
        # La fila no se borra (regla 3) pero ya no enlaza menciones nuevas.
        from kipu_knowledge.adapters.resolution.resolver import SimpleEntityResolver

        proposals = SimpleEntityResolver(session).propose_matches(
            duplicate.name_normalized, {"kind": "organization"}
        )
        assert proposals == []

    def test_merge_folds_equivalent_positions(self, ingested_session):
        session = ingested_session
        survivor, duplicate, task = self._build_duplicate(session)
        existing = m.Position(
            organization_id=survivor.id,
            preferred_label="Asesor II",
            label_normalized="ASESOR II",
        )
        twin = m.Position(
            organization_id=duplicate.id,
            preferred_label="Asesor II",
            label_normalized="ASESOR II",
        )
        session.add_all([existing, twin])
        session.flush()
        assignment = session.execute(select(m.RoleAssignment)).scalars().first()
        assignment.position_id = twin.id
        session.flush()

        ReviewService(session).decide(
            task.id, e.DecisionAction.LINK_ENTITY, payload={"entity_id": survivor.id}
        )

        assert assignment.position_id == existing.id
        assert session.get(m.Position, twin.id) is None

    def test_merge_into_itself_is_rejected(self, ingested_session):
        session = ingested_session
        _, duplicate, task = self._build_duplicate(session)
        import pytest

        from kipu_knowledge.application.review import ReviewError

        with pytest.raises(ReviewError):
            ReviewService(session).decide(
                task.id, e.DecisionAction.LINK_ENTITY, payload={"entity_id": duplicate.id}
            )
