"""Consultas de dominio: líneas de tiempo de puestos y respuestas con incertidumbre.

Regla 24: cuando la fuente no permite responder, la API devuelve incertidumbre
explicable con evidencia, nunca una respuesta inventada.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.domain import enums as e


@dataclass
class AssignmentView:
    assignment: m.RoleAssignment
    mention: m.PersonMention | None
    person: m.Person | None


@dataclass
class HolderAnswer:
    """Respuesta a "¿quién ocupaba el puesto P en la fecha D?"."""

    status: str  # "confirmed" | "vacant" | "unresolved" | "no_data"
    reason: str
    holder: dict[str, Any] | None = None
    supporting: list[dict[str, Any]] = field(default_factory=list)
    # De dónde salen las fechas que sostienen la respuesta: "source" si todas
    # están expresadas en el documento, "legal_rule" si alguna la determina una
    # norma (ver domain/legal_effect.py), None si no hay respuesta que sostener.
    basis: str | None = None


# Fechas con las que se responde: la que el documento expresa manda siempre; la
# determinada por norma solo entra donde la fuente calló.
def effective_start(ra: m.RoleAssignment) -> tuple[date | None, str | None]:
    if ra.valid_from is not None:
        return ra.valid_from, "source"
    if ra.legal_effect_from is not None:
        return ra.legal_effect_from, "legal_rule"
    return None, None


def effective_end(ra: m.RoleAssignment) -> tuple[date | None, str | None]:
    if ra.valid_to is not None:
        return ra.valid_to, "source"
    if ra.legal_effect_to is not None:
        return ra.legal_effect_to, "legal_rule"
    return None, None


def position_assignments(session: Session, position_id: str) -> list[AssignmentView]:
    rows = (
        session.execute(
            select(m.RoleAssignment)
            .where(m.RoleAssignment.position_id == position_id)
            .where(m.RoleAssignment.superseded_at.is_(None))
            .order_by(m.RoleAssignment.recorded_at)
        )
        .scalars()
        .all()
    )
    views: list[AssignmentView] = []
    for ra in rows:
        mention = session.get(m.PersonMention, ra.person_mention_id)
        person = session.get(m.Person, ra.person_id) if ra.person_id else None
        views.append(AssignmentView(ra, mention, person))
    return views


def _assignment_summary(session: Session, view: AssignmentView) -> dict[str, Any]:
    ra = view.assignment
    doc_id = None
    event = None
    if ra.start_event_id:
        event = session.get(m.PersonnelEvent, ra.start_event_id)
    elif ra.end_event_id:
        event = session.get(m.PersonnelEvent, ra.end_event_id)
    if event is not None:
        doc_id = event.legal_document_id
    start, start_basis = effective_start(ra)
    end, end_basis = effective_end(ra)
    return {
        "assignment_id": ra.id,
        "person": view.person.preferred_name if view.person else None,
        "person_mention": view.mention.text_raw if view.mention else None,
        "assignment_kind": str(ra.assignment_kind),
        "valid_from": ra.valid_from.isoformat() if ra.valid_from else None,
        "valid_from_status": str(ra.valid_from_status),
        "valid_to": ra.valid_to.isoformat() if ra.valid_to else None,
        "valid_to_status": str(ra.valid_to_status),
        # Fecha determinada por norma cuando la fuente calló, y con qué base se
        # responde: quien lea la respuesta tiene que poder distinguirlas.
        "legal_effect_from": ra.legal_effect_from.isoformat() if ra.legal_effect_from else None,
        "legal_effect_to": ra.legal_effect_to.isoformat() if ra.legal_effect_to else None,
        "effective_start": start.isoformat() if start else None,
        "effective_start_basis": start_basis,
        "effective_end": end.isoformat() if end else None,
        "effective_end_basis": end_basis,
        "end_condition_text": ra.end_condition_text,
        "document_id": doc_id,
    }


def position_holder_at(session: Session, position_id: str, on: date) -> HolderAnswer:
    """Determina el titular en una fecha, o incertidumbre explicable.

    Una fecha cuenta como determinada si la fuente la expresa o si una norma la
    fija (Ley N.º 27594 art. 6 y concordantes, ver `domain/legal_effect.py`).
    Ejemplo CENEPRED (caso B): la asignación anterior terminó el 2026-08-04 con
    fecha explícita y la designación sucesora, publicada el 2026-08-06, surte
    efectos ese día por mandato legal; consultar el 2026-08-05 ya no devuelve
    incertidumbre sino `vacant`, con ambas piezas de evidencia. Lo que ninguna
    de las dos vías determina sigue devolviendo `unresolved`.
    """
    views = position_assignments(session, position_id)
    if not views:
        return HolderAnswer(
            status="no_data",
            reason="No hay asignaciones registradas para este puesto",
        )

    confirmed: list[AssignmentView] = []
    uncertain: list[AssignmentView] = []
    bases: set[str] = set()
    for view in views:
        ra = view.assignment
        start, start_basis = effective_start(ra)
        end, end_basis = effective_end(ra)
        if start is not None and start > on:
            continue  # el acto todavía no producía efectos en la fecha consultada
        if end is not None and end < on:
            continue  # la asignación ya había terminado
        if start is not None:
            confirmed.append(view)
            bases.update(b for b in (start_basis, end_basis) if b)
        else:
            uncertain.append(view)  # podría estar vigente, pero su inicio no consta

    if confirmed and not uncertain:
        view = confirmed[-1]
        by_law = "legal_rule" in bases
        return HolderAnswer(
            status="confirmed",
            reason=(
                "Asignación vigente; alguna de sus fechas no está expresada en el "
                "documento y la determina la norma citada en la afirmación"
                if by_law
                else "Asignación vigente con fechas declaradas en la fuente"
            ),
            holder=_assignment_summary(session, view),
            supporting=[_assignment_summary(session, v) for v in views],
            basis="legal_rule" if by_law else "source",
        )

    if not confirmed and not uncertain:
        # Toda asignación quedó fuera por una fecha determinada: el puesto estaba
        # vacante ese día. Es una respuesta, no una laguna.
        summaries = [_assignment_summary(session, v) for v in views]
        by_law = any(
            s["effective_start_basis"] == "legal_rule" or s["effective_end_basis"] == "legal_rule"
            for s in summaries
        )
        return HolderAnswer(
            status="vacant",
            reason=(
                "Ninguna asignación cubre la fecha consultada y todas las fechas que lo "
                "deciden están determinadas (expresadas en la fuente o fijadas por norma): "
                "el puesto estaba vacante"
            ),
            supporting=summaries,
            basis="legal_rule" if by_law else "source",
        )

    # Sin titular confirmable: construir explicación con lo que sí se sabe.
    return HolderAnswer(
        status="unresolved",
        reason=(
            "Evidencia insuficiente: existe al menos una asignación cuyo inicio no está "
            "expresado en la fuente (effective_from NOT_STATED) ni determinado por norma; "
            "el sistema no infiere fechas (regla 12)"
        ),
        supporting=[_assignment_summary(session, v) for v in views],
    )


def find_position_by_code_or_label(session: Session, text: str) -> m.Position | None:
    from kipu_knowledge.domain.normalization import normalize_position_label

    normalized = normalize_position_label(text)
    position = (
        session.execute(select(m.Position).where(m.Position.label_normalized == normalized))
        .scalars()
        .first()
    )
    if position is not None:
        return position
    # La etiqueta del puesto ya no arrastra la ruta organizacional (esa vive en
    # organización + unidades), pero la frase completa tal como la escribió el
    # documento se conserva en la asignación: buscar por ella debe seguir
    # encontrando el puesto.
    for ra in (
        session.execute(select(m.RoleAssignment).where(m.RoleAssignment.position_id.is_not(None)))
        .scalars()
        .all()
    ):
        if ra.position_label_raw and normalize_position_label(ra.position_label_raw) == normalized:
            return session.get(m.Position, ra.position_id)
    return None


def assignments_for_person(session: Session, person_id: str) -> list[dict[str, Any]]:
    rows = (
        session.execute(
            select(m.RoleAssignment)
            .where(m.RoleAssignment.person_id == person_id)
            .where(m.RoleAssignment.superseded_at.is_(None))
            .order_by(m.RoleAssignment.recorded_at)
        )
        .scalars()
        .all()
    )
    result = []
    for ra in rows:
        mention = session.get(m.PersonMention, ra.person_mention_id)
        person = session.get(m.Person, person_id)
        summary = _assignment_summary(session, AssignmentView(ra, mention, person))
        summary["position_id"] = ra.position_id
        summary["position_label_raw"] = ra.position_label_raw
        summary["organization_path_raw"] = ra.organization_path_raw
        result.append(summary)
    return result


def document_uncertainty_flags(session: Session, document_id: str) -> list[dict[str, Any]]:
    """Enumera incertidumbres explícitas registradas para un documento."""
    flags: list[dict[str, Any]] = []
    events = (
        session.execute(
            select(m.PersonnelEvent).where(m.PersonnelEvent.legal_document_id == document_id)
        )
        .scalars()
        .all()
    )
    for event in events:
        if (
            event.assignment_effect == e.AssignmentEffect.START
            and event.effective_from_status == e.DateStatus.NOT_STATED
        ):
            # La bandera se mantiene aunque la norma determine la fecha: el
            # documento sigue sin expresarla, y esconderlo borraría la diferencia
            # entre lo que la fuente dice y lo que la ley añade. Lo que cambia es
            # que deja de ser una laguna, y eso se dice en el mismo sitio.
            flag: dict[str, Any] = {
                "event_id": event.id,
                "kind": "effective_from_not_stated",
                "detail": "La fecha efectiva de inicio no está expresada en el documento",
            }
            if event.legal_effect_from is not None:
                basis = (event.legal_effect_basis_json or {}).get("basis") or {}
                citation = f"{basis.get('norm')}, artículo {basis.get('article')}"
                flag["determined_by_law"] = {
                    "legal_effect_from": event.legal_effect_from.isoformat(),
                    "citation": citation,
                    "rule": (event.legal_effect_basis_json or {}).get("rule"),
                }
                flag["detail"] += (
                    f"; queda determinada por {citation} en {event.legal_effect_from.isoformat()}"
                )
            flags.append(flag)
        if event.end_condition_text:
            flags.append(
                {
                    "event_id": event.id,
                    "kind": "conditional_end",
                    "detail": f"Final condicional: {event.end_condition_text}",
                }
            )
    return flags
