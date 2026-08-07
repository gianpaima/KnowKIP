"""Serialización de entidades canónicas a respuestas de API con evidencia.

Cada respuesta de hechos incluye procedencia: URL fuente, código de publicación,
documento, artículo, cita textual, confianza, estado de revisión, estatus de la
fecha efectiva y versiones de extracción/ontología.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.application.legal_effect import determined_payload


def evidence_payload(session: Session, evidence_span_id: str | None) -> dict[str, Any] | None:
    if evidence_span_id is None:
        return None
    span = session.get(m.EvidenceSpan, evidence_span_id)
    if span is None:
        return None
    return {
        "evidence_span_id": span.id,
        "article_label": span.article_label,
        "quoted_text": span.quoted_text,
        "quoted_text_sha256": span.quoted_text_sha256,
        "char_start": span.char_start,
        "char_end": span.char_end,
        "artifact_version_id": span.artifact_version_id,
    }


def document_payload(
    session: Session, doc: m.LegalDocument, detail: bool = False
) -> dict[str, Any]:
    item = session.get(m.PublicationItem, doc.publication_item_id)
    version = session.get(m.ArtifactVersion, doc.parsed_from_artifact_version_id)
    payload: dict[str, Any] = {
        "id": doc.id,
        "publication_code": item.publication_code if item else None,
        "source_series": item.source_series if item else None,
        "source_url": item.canonical_url if item else None,
        "document_type_raw": doc.document_type_raw,
        "document_type_code": str(doc.document_type_code),
        "number_raw": doc.number_raw,
        "number_normalized": doc.number_normalized,
        "title": doc.title_raw,
        "issue_place": doc.issue_place_raw,
        "issued_on": doc.issued_on.isoformat() if doc.issued_on else None,
        "published_on": doc.published_on.isoformat() if doc.published_on else None,
        "artifact_sha256": version.sha256 if version else None,
    }
    if detail:
        payload["sections"] = [
            {
                "id": s.id,
                "section_type": str(s.section_type),
                "label": s.label_raw,
                "order_index": s.order_index,
                "text": s.text_raw,
            }
            for s in session.execute(
                select(m.DocumentSection)
                .where(m.DocumentSection.legal_document_id == doc.id)
                .order_by(m.DocumentSection.order_index)
            ).scalars()
        ]
        payload["references"] = [
            {
                "reference_type": str(r.reference_type),
                "target_number_raw": r.target_number_raw,
                "target_doc_kind_raw": r.target_doc_kind_raw,
                "evidence": evidence_payload(session, r.evidence_span_id),
            }
            for r in session.execute(
                select(m.DocumentReference).where(m.DocumentReference.source_document_id == doc.id)
            ).scalars()
        ]
        payload["signatories"] = [
            {
                "name": mention.text_raw if mention else None,
                "capacity_raw": s.capacity_raw,
                "signature_order": s.signature_order,
            }
            for s in session.execute(
                select(m.Signatory)
                .where(m.Signatory.legal_document_id == doc.id)
                .order_by(m.Signatory.signature_order)
            ).scalars()
            if (mention := session.get(m.PersonMention, s.person_mention_id))
        ]
        payload["events"] = [
            event_payload(session, ev)
            for ev in session.execute(
                select(m.PersonnelEvent).where(m.PersonnelEvent.legal_document_id == doc.id)
            ).scalars()
        ]
    return payload


def _extraction_context(session: Session, doc: m.LegalDocument) -> dict[str, Any]:
    run = (
        session.execute(
            select(m.ExtractionRun)
            .where(m.ExtractionRun.artifact_version_id == doc.parsed_from_artifact_version_id)
            .order_by(m.ExtractionRun.started_at.desc())
        )
        .scalars()
        .first()
    )
    if run is None:
        return {}
    return {
        "extraction_run_id": run.id,
        "parser_version": run.parser_version,
        "extractor_version": run.extractor_version,
        "ontology_version": run.ontology_version,
    }


def event_payload(session: Session, event: m.PersonnelEvent) -> dict[str, Any]:
    doc = session.get(m.LegalDocument, event.legal_document_id)
    participants = []
    for participant in session.execute(
        select(m.EventParticipant).where(m.EventParticipant.event_id == event.id)
    ).scalars():
        mention = (
            session.get(m.PersonMention, participant.person_mention_id)
            if participant.person_mention_id
            else None
        )
        participants.append(
            {
                "role": str(participant.role_in_event),
                "person_mention": mention.text_raw if mention else None,
                "person_mention_id": mention.id if mention else None,
                "person_id": mention.canonical_person_id if mention else None,
                "confidence": participant.confidence,
            }
        )
    assignments = [
        assignment_payload(session, ra)
        for ra in session.execute(
            select(m.RoleAssignment).where(
                (m.RoleAssignment.start_event_id == event.id)
                | (m.RoleAssignment.end_event_id == event.id)
            )
        ).scalars()
    ]
    payload = {
        "id": event.id,
        "document_id": event.legal_document_id,
        "event_type": str(event.event_type),
        "assignment_effect": str(event.assignment_effect),
        "legal_verb_raw": event.legal_verb_raw,
        "effective_from": event.effective_from.isoformat() if event.effective_from else None,
        "effective_from_status": str(event.effective_from_status),
        "effective_to": event.effective_to.isoformat() if event.effective_to else None,
        "effective_to_status": str(event.effective_to_status),
        # Lo que la fuente no expresa pero la norma determina, siempre con su
        # fundamento: la fecha de arriba sigue siendo la que el documento dice.
        "legal_effect": determined_payload(event),
        "end_condition_text": event.end_condition_text,
        "participants": participants,
        "assignments": assignments,
        "evidence": evidence_payload(session, event.evidence_span_id),
    }
    if doc is not None:
        item = session.get(m.PublicationItem, doc.publication_item_id)
        payload["publication_code"] = item.publication_code if item else None
        payload["source_url"] = item.canonical_url if item else None
        payload.update(_extraction_context(session, doc))
    return payload


def assignment_payload(session: Session, ra: m.RoleAssignment) -> dict[str, Any]:
    mention = session.get(m.PersonMention, ra.person_mention_id)
    position = session.get(m.Position, ra.position_id) if ra.position_id else None
    org = session.get(m.Organization, ra.organization_id) if ra.organization_id else None
    mandate = session.get(m.Mandate, ra.mandate_id) if ra.mandate_id else None
    slots = []
    if position is not None:
        slots = [
            {"external_scheme": s.external_scheme, "external_code": s.external_code}
            for s in session.execute(
                select(m.PositionSlot).where(m.PositionSlot.position_id == position.id)
            ).scalars()
        ]
    return {
        "id": ra.id,
        "person_id": ra.person_id,
        "person_mention": mention.text_raw if mention else None,
        "position_id": ra.position_id,
        "position_label": position.preferred_label if position else None,
        "position_label_raw": ra.position_label_raw,
        "position_slots": slots,
        "organization": org.preferred_name if org else None,
        "organization_path_raw": ra.organization_path_raw,
        "assignment_kind": str(ra.assignment_kind),
        "valid_from": ra.valid_from.isoformat() if ra.valid_from else None,
        "valid_from_status": str(ra.valid_from_status),
        "valid_to": ra.valid_to.isoformat() if ra.valid_to else None,
        "valid_to_status": str(ra.valid_to_status),
        "legal_effect_from": ra.legal_effect_from.isoformat() if ra.legal_effect_from else None,
        "legal_effect_to": ra.legal_effect_to.isoformat() if ra.legal_effect_to else None,
        "end_condition_text": ra.end_condition_text,
        "mandate": (
            {
                "id": mandate.id,
                "mandate_type": str(mandate.mandate_type),
                "label": mandate.label,
                "end_condition_text": mandate.end_condition_text,
            }
            if mandate
            else None
        ),
        "start_event_id": ra.start_event_id,
        "end_event_id": ra.end_event_id,
        "recorded_at": ra.recorded_at.isoformat() if ra.recorded_at else None,
        "superseded_at": ra.superseded_at.isoformat() if ra.superseded_at else None,
    }


def assertion_payload(session: Session, assertion: m.Assertion) -> dict[str, Any]:
    run = session.get(m.ExtractionRun, assertion.extraction_run_id)
    return {
        "id": assertion.id,
        "subject_type": assertion.subject_type,
        "subject_id": assertion.subject_id,
        "predicate": assertion.predicate,
        "object_type": assertion.object_type,
        "object_id": assertion.object_id,
        "object_value": assertion.object_value_json,
        "confidence": assertion.confidence,
        "review_status": str(assertion.review_status),
        "recorded_at": assertion.recorded_at.isoformat() if assertion.recorded_at else None,
        "superseded_at": assertion.superseded_at.isoformat() if assertion.superseded_at else None,
        "evidence": evidence_payload(session, assertion.evidence_span_id),
        "extraction": (
            {
                "run_id": run.id,
                "parser_version": run.parser_version,
                "extractor_version": run.extractor_version,
                "ontology_version": run.ontology_version,
            }
            if run
            else None
        ),
    }
