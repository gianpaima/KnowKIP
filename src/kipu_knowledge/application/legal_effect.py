"""Aplicación de la fecha de inicio de efectos determinada por norma.

La regla vive en ``domain/legal_effect.py`` y es pura. Aquí se le dan las
entradas desde lo que está capturado —tipo de evento, autoridad de la
publicación de la que se extrajo, fecha de publicación y parte resolutiva— y se
escribe el resultado con evidencia citable:

- ``personnel_event.legal_effect_from`` y su fundamento en JSON;
- la proyección en la asignación afectada (``legal_effect_from`` / ``_to``);
- una ``Assertion`` AUTO_ACCEPTED cuyo ``EvidenceSpan`` es la frase de la propia
  captura que declara la fecha de publicación. Sin esa cita la afirmación no
  cumpliría la regla 2, porque la fecha no está en el texto del dispositivo sino
  en la página que lo publica.

``effective_from`` no se toca nunca: sigue diciendo lo que el documento dice.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.adapters.parsing.html_parser import publication_date_phrase
from kipu_knowledge.application.source_links import latest_html_version, verified_bytes
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain.contracts import ArtifactStore
from kipu_knowledge.domain.legal_effect import (
    RULE_VERSION,
    LegalEffectOutcome,
    LegalEffectVerdict,
    determine_legal_effect,
    find_postponement_clause,
)

PREDICATE = "legal_effect_from"
# Marca del locator con la que se reconoce el span de la fecha de publicación:
# no cuelga de ninguna sección, sus offsets son sobre el texto del artefacto.
SPAN_KIND = "publication_date"

# Secciones que pueden postergar la vigencia del acto: solo la parte resolutiva.
_DISPOSITIVE = (e.SectionType.ARTICLE, e.SectionType.ARTICLE_LIST_ITEM)


class BackfillOutcome(StrEnum):
    DETERMINED = "DETERMINADO"
    VETOED = "VETADO"
    NOT_APPLICABLE = "NO_APLICA"
    NO_EVIDENCE = "SIN_EVIDENCIA"


@dataclass(frozen=True)
class BackfillResult:
    event_id: str
    document_number: str | None
    outcome: BackfillOutcome
    value: str | None
    detail: str


# ---------------------------------------------------------------------------
# Entradas de la regla, leídas de lo capturado
# ---------------------------------------------------------------------------


def authoritative_authority(session: Session, doc: m.LegalDocument) -> e.SourceAuthority | None:
    """Peso jurídico de la publicación de la que se extrajo este documento.

    Se prefiere la marcada AUTHORITATIVE en `document_source`; si no la hay
    todavía, la publicación de la que se parseó. Un documento sin sistema fuente
    registrado devuelve None y la regla lo veta: adivinar que "seguro era El
    Peruano" es exactamente la clase de supuesto que esta capa no debe hacer.
    """
    item_ids = [
        row.publication_item_id
        for row in session.execute(
            select(m.DocumentSource).where(
                m.DocumentSource.legal_document_id == doc.id,
                m.DocumentSource.role == e.DocumentSourceRole.AUTHORITATIVE,
            )
        ).scalars()
    ]
    if doc.publication_item_id and doc.publication_item_id not in item_ids:
        item_ids.append(doc.publication_item_id)
    for item_id in item_ids:
        item = session.get(m.PublicationItem, item_id)
        if item is None or item.source_system_id is None:
            continue
        system = session.get(m.SourceSystem, item.source_system_id)
        if system is not None:
            return system.authority
    return None


def dispositive_texts(session: Session, document_id: str) -> list[str]:
    return [
        section.text_raw
        for section in session.execute(
            select(m.DocumentSection)
            .where(
                m.DocumentSection.legal_document_id == document_id,
                m.DocumentSection.section_type.in_(_DISPOSITIVE),
            )
            .order_by(m.DocumentSection.order_index)
        ).scalars()
    ]


def verdict_for_event(session: Session, event: m.PersonnelEvent) -> LegalEffectVerdict:
    """Re-deriva el veredicto desde los datos congelados del documento.

    Es la misma función que usa la ingesta, de modo que un invariante puede
    volver a ejecutarla más tarde y comprobar que la fecha guardada sigue siendo
    la que la regla produce.
    """
    doc = session.get(m.LegalDocument, event.legal_document_id)
    if doc is None:
        return LegalEffectVerdict(
            LegalEffectOutcome.VETOED, "el evento no tiene documento asociado"
        )
    return determine_legal_effect(
        event_type=event.event_type,
        stated_status=event.effective_from_status,
        published_on=doc.published_on,
        source_authority=authoritative_authority(session, doc),
        postponement_clause=find_postponement_clause(dispositive_texts(session, doc.id)),
    )


# ---------------------------------------------------------------------------
# Escritura del resultado
# ---------------------------------------------------------------------------


def apply_verdict(session: Session, event: m.PersonnelEvent, verdict: LegalEffectVerdict) -> None:
    """Escribe la fecha determinada en el evento y en la asignación afectada.

    La proyección a la asignación respeta lo declarado: si la fuente expresó la
    fecha correspondiente, esa manda y aquí no se escribe nada.
    """
    if not verdict.determined or verdict.value is None:
        return
    event.legal_effect_from = verdict.value
    event.legal_effect_basis_json = verdict.as_dict()
    is_end = event.assignment_effect == e.AssignmentEffect.END
    for ra in session.execute(
        select(m.RoleAssignment).where(
            (m.RoleAssignment.end_event_id == event.id)
            if is_end
            else (m.RoleAssignment.start_event_id == event.id)
        )
    ).scalars():
        if is_end:
            if ra.valid_to_status == e.DateStatus.NOT_STATED:
                ra.legal_effect_to = verdict.value
        elif ra.valid_from_status == e.DateStatus.NOT_STATED:
            ra.legal_effect_from = verdict.value


def publication_date_span(session: Session, doc: m.LegalDocument) -> m.EvidenceSpan | None:
    """Span ya registrado con la frase que declara la fecha de publicación."""
    if not doc.parsed_from_artifact_version_id:
        return None
    for span in session.execute(
        select(m.EvidenceSpan).where(
            m.EvidenceSpan.artifact_version_id == doc.parsed_from_artifact_version_id,
            m.EvidenceSpan.document_section_id.is_(None),
        )
    ).scalars():
        if (span.locator_json or {}).get("kind") == SPAN_KIND:
            return span
    return None


def build_publication_date_span(
    session: Session,
    artifact_version_id: str,
    phrase: str,
    char_start: int,
    char_end: int,
) -> m.EvidenceSpan:
    span = m.EvidenceSpan(
        document_section_id=None,
        artifact_version_id=artifact_version_id,
        char_start=char_start,
        char_end=char_end,
        quoted_text=phrase,
        quoted_text_sha256=hashlib.sha256(phrase.encode("utf-8")).hexdigest(),
        # Los offsets son sobre el texto del artefacto decodificado en UTF-8, no
        # sobre una sección: la fecha la declara la página que publica el
        # dispositivo, fuera de div#x<código>.
        locator_json={"kind": SPAN_KIND, "char_basis": "artifact_text_utf8"},
    )
    session.add(span)
    session.flush()
    return span


def ensure_publication_date_span(
    session: Session, store: ArtifactStore, doc: m.LegalDocument
) -> m.EvidenceSpan | None:
    """Localiza —o reconstruye desde el CAS— la cita de la fecha de publicación.

    Los documentos ingeridos antes de esta regla no tienen el span, así que se
    vuelve a derivar releyendo los bytes capturados (verificando su sha256 antes
    de confiar en ellos). Sin red: la captura es inmutable.
    """
    existing = publication_date_span(session, doc)
    if existing is not None:
        return existing
    if not doc.publication_item_id:
        return None
    version = latest_html_version(session, doc.publication_item_id)
    if version is None:
        return None
    content = verified_bytes(store, version)
    if content is None:
        return None
    found = publication_date_phrase(content.decode("utf-8", errors="replace"))
    if found is None:
        return None
    phrase, char_start, char_end = found
    return build_publication_date_span(session, version.id, phrase, char_start, char_end)


def record_assertion(
    session: Session,
    event: m.PersonnelEvent,
    verdict: LegalEffectVerdict,
    evidence: m.EvidenceSpan,
    extraction_run_id: str,
) -> m.Assertion:
    row = m.Assertion(
        extraction_run_id=extraction_run_id,
        subject_type="personnel_event",
        subject_id=event.id,
        predicate=PREDICATE,
        object_type="date",
        object_value_json=verdict.as_dict(),
        confidence=1.0,
        evidence_span_id=evidence.id,
        review_status=e.ReviewStatus.AUTO_ACCEPTED,
    )
    session.add(row)
    session.flush()
    return row


def latest_extraction_run(session: Session, doc: m.LegalDocument) -> m.ExtractionRun | None:
    if not doc.parsed_from_artifact_version_id:
        return None
    return (
        session.execute(
            select(m.ExtractionRun)
            .where(m.ExtractionRun.artifact_version_id == doc.parsed_from_artifact_version_id)
            .order_by(m.ExtractionRun.started_at.desc())
        )
        .scalars()
        .first()
    )


# ---------------------------------------------------------------------------
# Reparación de lo ingerido antes de la regla
# ---------------------------------------------------------------------------


def backfill_legal_effect_dates(
    session: Session, store: ArtifactStore, *, dry_run: bool = False
) -> list[BackfillResult]:
    """Determina la fecha de efectos de los eventos ya ingeridos.

    Toca solo los eventos cuya fecha no está expresada y que la regla determina;
    los vetados y los no cubiertos quedan intactos. Las tareas
    EFFECTIVE_DATE_UNSTATED pendientes de esos eventos se cierran con un
    ReviewDecision atribuido a la regla, citando la evidencia.
    """
    from kipu_knowledge.application.review import ReviewService

    results: list[BackfillResult] = []
    events = (
        session.execute(
            select(m.PersonnelEvent)
            .where(
                m.PersonnelEvent.effective_from_status == e.DateStatus.NOT_STATED,
                m.PersonnelEvent.legal_effect_from.is_(None),
            )
            .order_by(m.PersonnelEvent.id)
        )
        .scalars()
        .all()
    )
    for event in events:
        doc = session.get(m.LegalDocument, event.legal_document_id)
        number = doc.number_raw if doc is not None else None
        verdict = verdict_for_event(session, event)
        if verdict.outcome == LegalEffectOutcome.NOT_APPLICABLE:
            results.append(
                BackfillResult(
                    event.id, number, BackfillOutcome.NOT_APPLICABLE, None, verdict.rationale
                )
            )
            continue
        if verdict.outcome == LegalEffectOutcome.VETOED:
            results.append(
                BackfillResult(event.id, number, BackfillOutcome.VETOED, None, verdict.rationale)
            )
            continue
        assert doc is not None and verdict.value is not None
        value = verdict.value.isoformat()
        if dry_run:
            results.append(
                BackfillResult(
                    event.id,
                    number,
                    BackfillOutcome.DETERMINED,
                    value,
                    f"[dry-run] {verdict.rationale}",
                )
            )
            continue

        evidence = ensure_publication_date_span(session, store, doc)
        run = latest_extraction_run(session, doc)
        if evidence is None or run is None:
            results.append(
                BackfillResult(
                    event.id,
                    number,
                    BackfillOutcome.NO_EVIDENCE,
                    None,
                    "no se pudo reconstruir la cita de la fecha de publicación desde el CAS "
                    "(bytes ausentes o alterados) o falta la corrida de extracción; sin "
                    "evidencia no se registra la afirmación",
                )
            )
            continue

        apply_verdict(session, event, verdict)
        record_assertion(session, event, verdict, evidence, run.id)
        detail = verdict.rationale
        review = ReviewService(session)
        for task in session.execute(
            select(m.ReviewTask).where(
                m.ReviewTask.task_type == e.ReviewTaskType.EFFECTIVE_DATE_UNSTATED,
                m.ReviewTask.target_type == "personnel_event",
                m.ReviewTask.target_id == event.id,
                m.ReviewTask.status == e.ReviewTaskStatus.PENDING,
            )
        ).scalars():
            review.decide(
                task.id,
                e.DecisionAction.APPLY_LEGAL_EFFECT_DATE,
                reviewer=f"sistema · {RULE_VERSION}",
                payload={
                    "legal_effect_from": value,
                    "basis": verdict.basis.as_dict() if verdict.basis else None,
                    "repaired_at": datetime.now(UTC).isoformat(),
                },
                notes=verdict.rationale,
            )
            detail = f"{verdict.rationale}; tarea {task.id} cerrada"
        results.append(BackfillResult(event.id, number, BackfillOutcome.DETERMINED, value, detail))
    session.flush()
    return results


def determined_payload(event: m.PersonnelEvent) -> dict[str, Any] | None:
    """Fundamento guardado, listo para serializar en API/UI."""
    if event.legal_effect_from is None:
        return None
    basis = (event.legal_effect_basis_json or {}).get("basis") or {}
    return {
        "legal_effect_from": event.legal_effect_from.isoformat(),
        "status": str(e.DateStatus.DERIVED),
        "rule": (event.legal_effect_basis_json or {}).get("rule", RULE_VERSION),
        "method": (event.legal_effect_basis_json or {}).get("method"),
        "rationale": (event.legal_effect_basis_json or {}).get("rationale"),
        "norm": basis.get("norm"),
        "article": basis.get("article"),
        "rule_text": basis.get("rule_text"),
        "quote_kind": basis.get("quote_kind"),
        "source_url": basis.get("source_url"),
    }
