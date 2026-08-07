"""Señal de corroboración por recital: ingesta, reparación y auditoría.

Caso D (Tribunal Fiscal, 2540905-4): el artículo 1 concluye un encargo sin
nombrar a la persona; el considerando declara que Luisa Ysila Castillo Soto lo
ejercía por la RS N° 044-2025-EF sobre exactamente ese puesto. Con la señal,
el vínculo se corrobora en ingesta sin tarea humana; los datos ingeridos antes
de la señal se reparan con rastro completo (assertion supersedida + decisión
del sistema), nunca borrando.
"""

from __future__ import annotations

from sqlalchemy import select

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.application.corroboration import (
    RecitalOutcome,
    resolve_pending_affected,
)
from kipu_knowledge.domain import enums as e


def _end_event(session) -> m.PersonnelEvent:
    return session.execute(
        select(m.PersonnelEvent).where(
            m.PersonnelEvent.event_type == e.EventType.END_ACTING_ASSIGNMENT
        )
    ).scalar_one()


def _corroborated_participant(session, event) -> tuple[m.EventParticipant, m.PersonMention]:
    return session.execute(
        select(m.EventParticipant, m.PersonMention)
        .join(m.PersonMention, m.PersonMention.id == m.EventParticipant.person_mention_id)
        .where(
            m.EventParticipant.event_id == event.id,
            m.EventParticipant.role_in_event
            == e.ParticipantRole.AFFECTED_PERSON_RECITAL_CORROBORATED,
        )
    ).one()


def test_ingest_corroborates_case_d_without_human_task(ingested_session):
    event = _end_event(ingested_session)
    participant, mention = _corroborated_participant(ingested_session, event)
    assert mention.text_raw == "Luisa Ysila Castillo Soto"
    assert participant.confidence == 0.9
    # El cargo sustantivo declarado por el mismo considerando queda registrado.
    assert mention.role_context_raw == "Asesora del Despacho Viceministerial de Economía II"
    # La asignación concluida apunta a la mención corroborada.
    ra = ingested_session.execute(
        select(m.RoleAssignment).where(m.RoleAssignment.end_event_id == event.id)
    ).scalar_one()
    assert ra.person_mention_id == mention.id
    # Sin tarea humana: la señal corroboró.
    tasks = (
        ingested_session.execute(
            select(m.ReviewTask).where(
                m.ReviewTask.task_type == e.ReviewTaskType.LINK_AFFECTED_ASSIGNMENT,
                m.ReviewTask.target_id == event.id,
            )
        )
        .scalars()
        .all()
    )
    assert tasks == []


def test_corroborated_assertion_is_auto_accepted_with_rationale(ingested_session):
    event = _end_event(ingested_session)
    assertion = ingested_session.execute(
        select(m.Assertion).where(
            m.Assertion.subject_id == event.id,
            m.Assertion.predicate == "event_affects_person",
        )
    ).scalar_one()
    assert assertion.review_status == e.ReviewStatus.AUTO_ACCEPTED
    assert assertion.object_value_json["basis"] == "recital_corroborated"
    assert "mismo instrumento previo citado" in assertion.object_value_json["rationale"]
    # La evidencia citada es el considerando, no el artículo.
    span = ingested_session.get(m.EvidenceSpan, assertion.evidence_span_id)
    section = ingested_session.get(m.DocumentSection, span.document_section_id)
    assert section.section_type == e.SectionType.CONSIDERANDO


def _downgrade_to_legacy_state(session) -> tuple[m.ReviewTask, m.Assertion]:
    """Reproduce el estado que dejó el extractor 0.1/0.2: candidato sin
    corroborar, assertion CANDIDATE y tarea pendiente."""
    event = _end_event(session)
    participant, mention = _corroborated_participant(session, event)
    participant.role_in_event = e.ParticipantRole.AFFECTED_PERSON_RECITAL_CANDIDATE
    participant.confidence = 0.6
    mention.role_context_raw = None
    mention.role_context_normalized = None
    assertion = session.execute(
        select(m.Assertion).where(
            m.Assertion.subject_id == event.id,
            m.Assertion.predicate == "event_affects_person",
        )
    ).scalar_one()
    assertion.review_status = e.ReviewStatus.CANDIDATE
    assertion.object_value_json = {"basis": "recital"}
    task = m.ReviewTask(
        task_type=e.ReviewTaskType.LINK_AFFECTED_ASSIGNMENT,
        target_type="personnel_event",
        target_id=event.id,
        reason="estado legado simulado: candidato de considerando sin confirmar",
        priority=2,
    )
    session.add(task)
    session.flush()
    return task, assertion


def test_repair_resolves_legacy_pending_task_with_full_audit_trail(ingested_session):
    task, old_assertion = _downgrade_to_legacy_state(ingested_session)

    results = resolve_pending_affected(ingested_session)
    assert [r.outcome for r in results] == [RecitalOutcome.CORROBORATED]

    ingested_session.flush()
    assert task.status == e.ReviewTaskStatus.RESOLVED
    event = _end_event(ingested_session)
    participant, mention = _corroborated_participant(ingested_session, event)
    assert participant.confidence == 0.9
    assert mention.role_context_raw == "Asesora del Despacho Viceministerial de Economía II"
    # Cadena de afirmaciones: la CANDIDATE quedó supersedida, nunca borrada.
    assert old_assertion.review_status == e.ReviewStatus.SUPERSEDED
    replacement = ingested_session.get(m.Assertion, old_assertion.superseded_by_id)
    assert replacement.review_status == e.ReviewStatus.AUTO_ACCEPTED
    assert replacement.object_value_json["basis"] == "recital_corroborated"
    # Decisión auditable atribuida a la regla, con el rationale como notas.
    decision = ingested_session.execute(
        select(m.ReviewDecision).where(m.ReviewDecision.review_task_id == task.id)
    ).scalar_one()
    assert decision.action == e.DecisionAction.ACCEPT
    assert "recital-corroboration" in (decision.reviewer or "")
    assert "candidato único" in (decision.notes or "")


def test_repair_dry_run_changes_nothing(ingested_session):
    task, old_assertion = _downgrade_to_legacy_state(ingested_session)

    results = resolve_pending_affected(ingested_session, dry_run=True)
    assert [r.outcome for r in results] == [RecitalOutcome.CORROBORATED]
    assert all(r.detail.startswith("[dry-run]") for r in results)

    assert task.status == e.ReviewTaskStatus.PENDING
    assert old_assertion.review_status == e.ReviewStatus.CANDIDATE
    event = _end_event(ingested_session)
    participant = ingested_session.execute(
        select(m.EventParticipant).where(m.EventParticipant.event_id == event.id)
    ).scalar_one()
    assert participant.role_in_event == e.ParticipantRole.AFFECTED_PERSON_RECITAL_CANDIDATE


def test_repair_leaves_non_matching_tasks_pending(ingested_session):
    """Una tarea cuyo documento no permite corroborar queda intacta y pendiente."""
    task, _ = _downgrade_to_legacy_state(ingested_session)
    event = _end_event(ingested_session)
    # Rompe la corroboración: la asignación concluida pasa a otro puesto.
    ra = ingested_session.execute(
        select(m.RoleAssignment).where(m.RoleAssignment.end_event_id == event.id)
    ).scalar_one()
    ra.position_label_raw = "Presidenta de Otro Organismo Completamente Distinto"
    ingested_session.flush()

    results = resolve_pending_affected(ingested_session)
    assert [r.outcome for r in results] == [RecitalOutcome.UNCONFIRMED]
    assert task.status == e.ReviewTaskStatus.PENDING
