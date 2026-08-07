"""Corroboración por recital: cuarta señal que autoriza vincular sin humano.

Cuando un artículo resolutivo concluye un encargo sin nombrar a la persona,
pero un considerando del MISMO documento declara quién ejercía exactamente ese
puesto, la atribución es verificable mecánicamente — no es una inferencia
libre de contexto. La señal exige todas estas condiciones:

- el puesto del encargo declarado en el considerando coincide (normalizado)
  con el puesto que el artículo concluye;
- hay exactamente un candidato en los considerandos (dos encargos declarados
  son una contradicción: abren conflicto, nunca eligen);
- si el considerando y el artículo citan instrumento previo, debe ser el
  mismo número; números distintos vetan la señal y abren conflicto.

Todo lo que no encaje en el patrón sigue el camino actual: tarea de revisión
humana. La señal nunca adivina; corrobora o pregunta.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.adapters.extraction import patterns as p
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain.normalization import normalize_position_label

RULE_VERSION = "recital-corroboration/1.0"

_NUM_IN_RAW_RE = re.compile(r"N[º°]\s*(?P<num>[A-Za-z0-9./-]+)")


class RecitalOutcome(StrEnum):
    CORROBORATED = "CORROBORATED"
    CONFLICT = "CONFLICT"
    UNCONFIRMED = "UNCONFIRMED"


@dataclass(frozen=True)
class RecitalCandidate:
    name: str
    encargo_position_raw: str | None
    cited_document_number: str | None
    substantive_role_raw: str | None = None


@dataclass(frozen=True)
class RecitalVerdict:
    outcome: RecitalOutcome
    rationale: str
    candidate: RecitalCandidate | None = None


def bare_document_number(raw: str | None) -> str | None:
    """Extrae el número desnudo de un instrumento ("RS N° 044-2025-EF" -> "044-2025-EF")."""
    if not raw:
        return None
    match = _NUM_IN_RAW_RE.search(raw)
    if match:
        return match.group("num").upper()
    if re.fullmatch(r"[A-Za-z0-9./-]+", raw.strip()):
        return raw.strip().upper()
    return None


def corroborate_recital(
    candidates: list[RecitalCandidate],
    ended_position_label_raw: str | None,
    article_cited_numbers: set[str],
) -> RecitalVerdict:
    """Decide si el candidato de considerando queda corroborado, en conflicto
    o sin confirmar. Determinista: mismas entradas, mismo veredicto."""
    if not candidates:
        return RecitalVerdict(RecitalOutcome.UNCONFIRMED, "sin candidatos en considerandos")
    if len(candidates) > 1:
        names = ", ".join(f"'{c.name}'" for c in candidates)
        return RecitalVerdict(
            RecitalOutcome.CONFLICT,
            f"los considerandos declaran más de un encargo ({names}); "
            "señales contradictorias abren conflicto, no eligen",
        )
    candidate = candidates[0]
    if not candidate.encargo_position_raw or not ended_position_label_raw:
        return RecitalVerdict(
            RecitalOutcome.UNCONFIRMED,
            "el considerando no declara el puesto del encargo o el artículo no "
            "identifica el puesto concluido; sin puesto no hay corroboración",
            candidate,
        )
    if normalize_position_label(candidate.encargo_position_raw) != normalize_position_label(
        ended_position_label_raw
    ):
        return RecitalVerdict(
            RecitalOutcome.UNCONFIRMED,
            f"el puesto del considerando ('{candidate.encargo_position_raw}') no es "
            f"el puesto que el artículo concluye ('{ended_position_label_raw}')",
            candidate,
        )
    recital_number = bare_document_number(candidate.cited_document_number)
    if recital_number and article_cited_numbers and recital_number not in article_cited_numbers:
        return RecitalVerdict(
            RecitalOutcome.CONFLICT,
            f"el considerando cita el instrumento {recital_number} pero el artículo "
            f"cita {', '.join(sorted(article_cited_numbers))}: instrumentos distintos "
            "describen encargos distintos",
            candidate,
        )
    instrument_note = (
        f"; mismo instrumento previo citado en ambos ({recital_number})"
        if recital_number and recital_number in article_cited_numbers
        else ""
    )
    return RecitalVerdict(
        RecitalOutcome.CORROBORATED,
        f"candidato único '{candidate.name}' con encargo declarado sobre el mismo "
        f"puesto que el artículo concluye ('{ended_position_label_raw}')" + instrument_note,
        candidate,
    )


# ---------------------------------------------------------------------------
# Reparación de tareas pendientes (datos ingeridos antes de la señal)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairResult:
    task_id: str
    document_number: str | None
    outcome: RecitalOutcome
    detail: str


def recital_candidates_from_sections(session: Session, document_id: str) -> list[RecitalCandidate]:
    """Re-deriva los candidatos desde las secciones almacenadas (congeladas).

    Determinista: aplica los mismos patrones del extractor sobre el mismo texto
    que quedó persistido en la ingesta original.
    """
    sections = (
        session.execute(
            select(m.DocumentSection)
            .where(
                m.DocumentSection.legal_document_id == document_id,
                m.DocumentSection.section_type == e.SectionType.CONSIDERANDO,
            )
            .order_by(m.DocumentSection.order_index)
        )
        .scalars()
        .all()
    )
    candidates: list[RecitalCandidate] = []
    for section in sections:
        match = p.RECITAL_ENCARGO_RE.search(section.text_normalized)
        if not match or not p.looks_like_person_name(match.group("name").strip()):
            continue
        cited = None
        for cm in p.RECITAL_CITED_DOC_RE.finditer(section.text_normalized[: match.start()]):
            cited = cm
        substantive = match.group("substantive")
        candidates.append(
            RecitalCandidate(
                name=match.group("name").strip(),
                encargo_position_raw=match.group("position").strip().rstrip(".;,"),
                cited_document_number=cited.group("num") if cited else None,
                substantive_role_raw=substantive.strip() if substantive else None,
            )
        )
    return candidates


def resolve_pending_affected(session: Session, *, dry_run: bool = False) -> list[RepairResult]:
    """Re-evalúa las tareas LINK_AFFECTED_ASSIGNMENT pendientes con la señal.

    Solo actúa cuando el veredicto es CORROBORATED; todo lo demás queda intacto
    y pendiente para el humano. Cada resolución deja rastro completo: la
    assertion CANDIDATE se supersede (nunca se borra) por una AUTO_ACCEPTED con
    el rationale, y la tarea se cierra con un ReviewDecision atribuido a esta
    regla, citando la evidencia del considerando.
    """
    from kipu_knowledge.application.review import ReviewService
    from kipu_knowledge.domain.normalization import (
        normalize_person_name,
    )
    from kipu_knowledge.domain.normalization import (
        normalize_position_label as _norm_pos,
    )

    results: list[RepairResult] = []
    tasks = (
        session.execute(
            select(m.ReviewTask).where(
                m.ReviewTask.task_type == e.ReviewTaskType.LINK_AFFECTED_ASSIGNMENT,
                m.ReviewTask.status == e.ReviewTaskStatus.PENDING,
                m.ReviewTask.target_type == "personnel_event",
            )
        )
        .scalars()
        .all()
    )
    for task in tasks:
        event = session.get(m.PersonnelEvent, task.target_id)
        if event is None:
            results.append(
                RepairResult(task.id, None, RecitalOutcome.UNCONFIRMED, "evento inexistente")
            )
            continue
        document = session.get(m.LegalDocument, event.legal_document_id)
        ended = (
            session.execute(
                select(m.RoleAssignment).where(m.RoleAssignment.end_event_id == event.id)
            )
            .scalars()
            .all()
        )
        participant_rows = session.execute(
            select(m.EventParticipant, m.PersonMention)
            .join(m.PersonMention, m.PersonMention.id == m.EventParticipant.person_mention_id)
            .where(
                m.EventParticipant.event_id == event.id,
                m.EventParticipant.role_in_event
                == e.ParticipantRole.AFFECTED_PERSON_RECITAL_CANDIDATE,
            )
        ).all()
        doc_number = document.number_raw if document else None
        if len(ended) != 1 or not participant_rows:
            results.append(
                RepairResult(
                    task.id,
                    doc_number,
                    RecitalOutcome.UNCONFIRMED,
                    "sin asignación concluida única o sin candidato registrado",
                )
            )
            continue

        candidates = recital_candidates_from_sections(session, event.legal_document_id)
        article_numbers = {
            number
            for row in session.execute(
                select(m.DocumentReference).where(
                    m.DocumentReference.source_document_id == event.legal_document_id,
                    m.DocumentReference.reference_type == e.ReferenceType.PRIOR_APPOINTMENT,
                )
            ).scalars()
            if (number := bare_document_number(row.target_number_raw))
        }
        verdict = corroborate_recital(candidates, ended[0].position_label_raw, article_numbers)

        participant, mention = participant_rows[0]
        candidate = verdict.candidate
        matches_stored = candidate is not None and normalize_person_name(
            candidate.name
        ) == normalize_person_name(mention.text_raw)
        if (
            verdict.outcome != RecitalOutcome.CORROBORATED
            or candidate is None
            or not matches_stored
        ):
            detail = verdict.rationale
            if verdict.outcome == RecitalOutcome.CORROBORATED and candidate is not None:
                detail = (
                    f"el candidato re-derivado '{candidate.name}' no coincide "
                    f"con la mención registrada '{mention.text_raw}'"
                )
            results.append(RepairResult(task.id, doc_number, RecitalOutcome.UNCONFIRMED, detail))
            continue

        if dry_run:
            results.append(
                RepairResult(task.id, doc_number, verdict.outcome, f"[dry-run] {verdict.rationale}")
            )
            continue

        # 1) Rol del participante: candidato -> corroborado.
        participant.role_in_event = e.ParticipantRole.AFFECTED_PERSON_RECITAL_CORROBORATED
        participant.confidence = 0.9
        # 2) Cargo sustantivo declarado por el mismo considerando, si faltaba.
        if candidate.substantive_role_raw and not mention.role_context_raw:
            mention.role_context_raw = candidate.substantive_role_raw
            mention.role_context_normalized = _norm_pos(candidate.substantive_role_raw)
        # 3) Cadena de afirmaciones: supersede de la CANDIDATE por una corroborada.
        review = ReviewService(session)
        old_assertion = (
            session.execute(
                select(m.Assertion).where(
                    m.Assertion.subject_type == "personnel_event",
                    m.Assertion.subject_id == event.id,
                    m.Assertion.predicate == "event_affects_person",
                    m.Assertion.object_id == mention.id,
                    m.Assertion.review_status == e.ReviewStatus.CANDIDATE,
                )
            )
            .scalars()
            .first()
        )
        rationale: dict[str, Any] = {
            "basis": "recital_corroborated",
            "rule": RULE_VERSION,
            "rationale": verdict.rationale,
            "repaired_at": datetime.now(UTC).isoformat(),
        }
        if old_assertion is not None:
            replacement = m.Assertion(
                extraction_run_id=old_assertion.extraction_run_id,
                subject_type="personnel_event",
                subject_id=event.id,
                predicate="event_affects_person",
                object_type="person_mention",
                object_id=mention.id,
                object_value_json=rationale,
                confidence=0.9,
                evidence_span_id=mention.evidence_span_id,
                review_status=e.ReviewStatus.AUTO_ACCEPTED,
            )
            session.add(replacement)
            session.flush()
            review.supersede(old_assertion.id, replacement.id)
        # 4) Cierre auditable de la tarea.
        review.decide(
            task.id,
            e.DecisionAction.ACCEPT,
            reviewer=f"sistema · {RULE_VERSION}",
            payload={"person_mention_id": mention.id, "basis": "recital_corroborated"},
            notes=verdict.rationale,
        )
        results.append(RepairResult(task.id, doc_number, verdict.outcome, verdict.rationale))
    return results
