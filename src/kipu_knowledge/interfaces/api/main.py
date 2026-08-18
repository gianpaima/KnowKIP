"""API HTTP versionada de Kipu Knowledge (FastAPI)."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from kipu_knowledge import __version__
from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.adapters.resolution.resolver import SimpleEntityResolver
from kipu_knowledge.adapters.search.backend import search_backend_for
from kipu_knowledge.application.export import export_document_jsonld, export_document_rdf
from kipu_knowledge.application.queries import (
    assignments_for_person,
    document_uncertainty_flags,
    position_assignments,
    position_holder_at,
)
from kipu_knowledge.application.review import ReviewError, ReviewService
from kipu_knowledge.application.source_links import official_publication_item
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain.normalization import normalize_person_name
from kipu_knowledge.interfaces.api.deps import get_db
from kipu_knowledge.interfaces.api.serializers import (
    assertion_payload,
    assignment_payload,
    document_payload,
    event_payload,
)
from kipu_knowledge.interfaces.review_ui.routes import router as review_ui_router

app = FastAPI(
    title="Kipu Knowledge API",
    version=__version__,
    description=(
        "Conocimiento verificable sobre resoluciones publicadas en El Peruano. "
        "Toda respuesta de hechos incluye evidencia y, cuando la fuente no permite "
        "responder, incertidumbre explicable en lugar de datos inventados."
    ),
)
app.include_router(review_ui_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/ready")
def ready(db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - fallo de infraestructura
        raise HTTPException(status_code=503, detail=f"BD no disponible: {exc}") from exc
    return {"status": "ready"}


# ---------------------------------------------------------------------------
# Documentos
# ---------------------------------------------------------------------------


@app.get("/v1/documents")
def list_documents(
    db: Session = Depends(get_db),
    limit: int = Query(50, le=200),
    offset: int = 0,
) -> dict[str, Any]:
    docs = (
        db.execute(
            select(m.LegalDocument)
            .order_by(m.LegalDocument.published_on)
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return {"items": [document_payload(db, d) for d in docs], "count": len(docs)}


@app.get("/v1/documents/by-source/{series}/{publication_code}")
def get_document_by_source(
    series: str, publication_code: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    item = official_publication_item(db, publication_code, series)
    if item is None:
        raise HTTPException(404, "Publicación no encontrada")
    doc = db.execute(
        select(m.LegalDocument).where(m.LegalDocument.publication_item_id == item.id)
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(404, "Publicación capturada pero sin documento parseado")
    payload = document_payload(db, doc, detail=True)
    payload["uncertainty"] = document_uncertainty_flags(db, doc.id)
    return payload


@app.get("/v1/documents/{document_id}")
def get_document(document_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    doc = db.get(m.LegalDocument, document_id)
    if doc is None:
        raise HTTPException(404, "Documento no encontrado")
    payload = document_payload(db, doc, detail=True)
    payload["uncertainty"] = document_uncertainty_flags(db, doc.id)
    return payload


# ---------------------------------------------------------------------------
# Eventos
# ---------------------------------------------------------------------------


@app.get("/v1/events")
def list_events(
    db: Session = Depends(get_db),
    event_type: str | None = None,
    limit: int = Query(50, le=200),
) -> dict[str, Any]:
    query = select(m.PersonnelEvent).limit(limit)
    if event_type:
        query = query.where(m.PersonnelEvent.event_type == event_type)
    events = db.execute(query).scalars().all()
    return {"items": [event_payload(db, ev) for ev in events], "count": len(events)}


@app.get("/v1/events/{event_id}")
def get_event(event_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    event = db.get(m.PersonnelEvent, event_id)
    if event is None:
        raise HTTPException(404, "Evento no encontrado")
    return event_payload(db, event)


# ---------------------------------------------------------------------------
# Personas, puestos, organizaciones
# ---------------------------------------------------------------------------


@app.get("/v1/persons")
def find_persons(
    name: str,
    db: Session = Depends(get_db),
    limit: int = Query(20, le=100),
) -> dict[str, Any]:
    """Busca personas por cualquiera de sus grafías, no solo por el nombre preferido.

    Una misma persona aparece en el diario con y sin segundo nombre. Buscar por
    una grafía y no encontrarla porque la ficha se registró con otra es un
    problema de lectura, no de identidad: aquí se consulta el conjunto de
    menciones vinculadas, que es donde consta cómo la nombró cada documento.

    Una persona absorbida por una fusión resuelve a la superviviente, para que
    los identificadores ya publicados sigan llevando a la ficha vigente.
    """
    normalized = normalize_person_name(name)
    if not normalized:
        raise HTTPException(422, "El parámetro 'name' no puede estar vacío")
    rows = (
        db.execute(
            select(m.Person)
            .join(m.PersonMention, m.PersonMention.canonical_person_id == m.Person.id)
            .where(m.PersonMention.text_normalized == normalized)
            .distinct()
            .limit(limit)
        )
        .scalars()
        .all()
    )
    resolver = SimpleEntityResolver(db)
    items = []
    for person in rows:
        survivor = person
        while survivor.merged_into_person_id:
            nxt = db.get(m.Person, survivor.merged_into_person_id)
            if nxt is None or nxt.id == survivor.id:
                break
            survivor = nxt
        items.append(
            {
                "id": survivor.id,
                "preferred_name": survivor.preferred_name,
                "status": survivor.status,
                "matched_alias": normalized,
                "aliases": resolver.person_aliases(survivor.id),
                "merged_from": person.id if survivor.id != person.id else None,
            }
        )
    # Dos grafías distintas de la misma persona no deben devolverla dos veces.
    unique = {item["id"]: item for item in items}
    return {"query": name, "name_normalized": normalized, "items": list(unique.values())}


@app.get("/v1/persons/{person_id}")
def get_person(person_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    person = db.get(m.Person, person_id)
    if person is None:
        raise HTTPException(404, "Persona no encontrada")
    mentions = (
        db.execute(select(m.PersonMention).where(m.PersonMention.canonical_person_id == person.id))
        .scalars()
        .all()
    )
    # Grafías respaldadas por una decisión humana vigente: el resto son alias de
    # hecho (así la nombró un documento), no alias autorizados a vincular.
    confirmed = set(
        db.execute(
            select(m.IdentityPrecedent.name_normalized).where(
                m.IdentityPrecedent.subject_type == "person",
                m.IdentityPrecedent.person_id == person.id,
                m.IdentityPrecedent.revoked_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    alias_counts: dict[str, int] = {}
    for mention in mentions:
        alias_counts[mention.text_normalized] = alias_counts.get(mention.text_normalized, 0) + 1
    return {
        "id": person.id,
        "preferred_name": person.preferred_name,
        "status": person.status,
        "merged_into_person_id": person.merged_into_person_id,
        "aliases": [
            {
                "name_normalized": alias,
                "mentions": count,
                "confirmed_by_precedent": alias in confirmed,
            }
            for alias, count in sorted(alias_counts.items())
        ],
        "mentions": [
            {
                "id": mention.id,
                "text_raw": mention.text_raw,
                "document_id": mention.legal_document_id,
                "resolution_status": str(mention.resolution_status),
            }
            for mention in mentions
        ],
    }


@app.get("/v1/persons/{person_id}/assignments")
def get_person_assignments(person_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    if db.get(m.Person, person_id) is None:
        raise HTTPException(404, "Persona no encontrada")
    return {"items": assignments_for_person(db, person_id)}


@app.get("/v1/positions/{position_id}")
def get_position(position_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    position = db.get(m.Position, position_id)
    if position is None:
        raise HTTPException(404, "Puesto no encontrado")
    org = db.get(m.Organization, position.organization_id) if position.organization_id else None
    unit = (
        db.get(m.OrganizationalUnit, position.organizational_unit_id)
        if position.organizational_unit_id
        else None
    )
    slots = (
        db.execute(select(m.PositionSlot).where(m.PositionSlot.position_id == position.id))
        .scalars()
        .all()
    )
    return {
        "id": position.id,
        "preferred_label": position.preferred_label,
        "organization": org.preferred_name if org else None,
        "organization_id": position.organization_id,
        "organizational_unit": unit.preferred_name if unit else None,
        "slots": [
            {"external_scheme": s.external_scheme, "external_code": s.external_code} for s in slots
        ],
    }


@app.get("/v1/positions/{position_id}/timeline")
def get_position_timeline(
    position_id: str,
    db: Session = Depends(get_db),
    on: date | None = Query(None, description="Fecha para consulta puntual (YYYY-MM-DD)"),
) -> dict[str, Any]:
    position = db.get(m.Position, position_id)
    if position is None:
        raise HTTPException(404, "Puesto no encontrado")
    views = position_assignments(db, position_id)
    payload: dict[str, Any] = {
        "position_id": position_id,
        "preferred_label": position.preferred_label,
        "assignments": [assignment_payload(db, v.assignment) for v in views],
    }
    if on is not None:
        answer = position_holder_at(db, position_id, on)
        payload["holder_at"] = {
            "date": on.isoformat(),
            "status": answer.status,
            "reason": answer.reason,
            "basis": answer.basis,
            "holder": answer.holder,
            "supporting_evidence": answer.supporting,
        }
    return payload


@app.get("/v1/organizations/{organization_id}")
def get_organization(organization_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    org = db.get(m.Organization, organization_id)
    if org is None:
        raise HTTPException(404, "Organización no encontrada")
    units = (
        db.execute(
            select(m.OrganizationalUnit).where(m.OrganizationalUnit.organization_id == org.id)
        )
        .scalars()
        .all()
    )
    positions = (
        db.execute(select(m.Position).where(m.Position.organization_id == org.id)).scalars().all()
    )
    from kipu_knowledge.application.queries import (
        assignments_across_succession,
        succession_chain_ids,
    )

    chain = succession_chain_ids(db, org.id)
    succession = [
        {
            "id": era.id,
            "preferred_name": era.preferred_name,
            "is_current_era": era.id == chain[0],
        }
        for era_id in chain
        if (era := db.get(m.Organization, era_id)) is not None
    ]
    return {
        "id": org.id,
        "preferred_name": org.preferred_name,
        "acronym": org.acronym,
        "organization_type": org.organization_type,
        "units": [
            {"id": u.id, "preferred_name": u.preferred_name, "parent_unit_id": u.parent_unit_id}
            for u in units
        ],
        "positions": [{"id": p.id, "preferred_label": p.preferred_label} for p in positions],
        # La cadena de sucesión declarada por el catálogo (de la época vigente a
        # la más antigua) y las asignaciones a través de todas las épocas: la
        # materia prima de "¿cuántos ministros tuvo X?" sin colapsar la historia.
        "succession": succession,
        "assignments_across_succession": assignments_across_succession(db, org.id),
    }


# ---------------------------------------------------------------------------
# Afirmaciones, revisión, búsqueda, exportaciones
# ---------------------------------------------------------------------------


@app.get("/v1/assertions/{assertion_id}")
def get_assertion(assertion_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    assertion = db.get(m.Assertion, assertion_id)
    if assertion is None:
        raise HTTPException(404, "Afirmación no encontrada")
    return assertion_payload(db, assertion)


@app.get("/v1/review-tasks")
def list_review_tasks(
    db: Session = Depends(get_db),
    status: str = "PENDING",
    limit: int = Query(100, le=500),
) -> dict[str, Any]:
    tasks = (
        db.execute(
            select(m.ReviewTask)
            .where(m.ReviewTask.status == status)
            .order_by(m.ReviewTask.priority, m.ReviewTask.created_at)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": t.id,
                "task_type": str(t.task_type),
                "target_type": t.target_type,
                "target_id": t.target_id,
                "reason": t.reason,
                "priority": t.priority,
                "status": str(t.status),
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in tasks
        ]
    }


@app.get("/v1/identity-precedents")
def list_identity_precedents(
    db: Session = Depends(get_db),
    include_revoked: bool = False,
    limit: int = Query(100, le=500),
) -> dict[str, Any]:
    """Decisiones de identidad reutilizadas en ingestas posteriores.

    Auditoría de qué vinculaciones automáticas están vigentes, quién las autorizó
    y con qué alcance: `office` cuando la decisión se ató al cargo declarado,
    `global` cuando el revisor declaró la grafía como alias de la persona.
    """
    stmt = select(m.IdentityPrecedent).order_by(m.IdentityPrecedent.created_at.desc()).limit(limit)
    if not include_revoked:
        stmt = stmt.where(m.IdentityPrecedent.revoked_at.is_(None))
    rows = db.execute(stmt).scalars().all()
    return {
        "items": [
            {
                "id": p.id,
                "name_normalized": p.name_normalized,
                "scope": "office" if p.role_context else "global",
                "role_context": p.role_context,
                "person_id": p.person_id,
                "person": person.preferred_name
                if (person := db.get(m.Person, p.person_id))
                else None,
                "review_decision_id": p.review_decision_id,
                "reviewer": p.reviewer,
                "applied_to_mentions": db.execute(
                    select(func.count())
                    .select_from(m.PersonMention)
                    .where(m.PersonMention.identity_precedent_id == p.id)
                ).scalar_one(),
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "revoked_at": p.revoked_at.isoformat() if p.revoked_at else None,
                "revoked_reason": p.revoked_reason,
            }
            for p in rows
        ]
    }


@app.post("/v1/review-tasks/{task_id}/decisions")
def decide_review_task(
    task_id: str,
    body: dict[str, Any],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        action = e.DecisionAction(body.get("action", ""))
    except ValueError as exc:
        raise HTTPException(422, f"Acción inválida: {body.get('action')}") from exc
    service = ReviewService(db)
    try:
        decision = service.decide(
            task_id,
            action,
            reviewer=body.get("reviewer"),
            payload=body.get("payload"),
            notes=body.get("notes"),
        )
    except ReviewError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"decision_id": decision.id, "task_id": task_id, "action": str(action)}


@app.get("/v1/search")
def search(
    q: str,
    db: Session = Depends(get_db),
    limit: int = Query(20, le=100),
) -> dict[str, Any]:
    backend = search_backend_for(db)
    return {"query": q, "items": backend.search(q, limit)}


def _resolve_code(document_id: str, db: Session) -> str:
    doc = db.get(m.LegalDocument, document_id)
    if doc is None:
        raise HTTPException(404, "Documento no encontrado")
    item = db.get(m.PublicationItem, doc.publication_item_id)
    assert item is not None
    return item.publication_code


@app.get("/v1/exports/documents/{document_id}.jsonld")
def export_jsonld(document_id: str, db: Session = Depends(get_db)) -> Response:
    code = _resolve_code(document_id, db)
    return Response(export_document_jsonld(db, code), media_type="application/ld+json")


@app.get("/v1/exports/documents/{document_id}.ttl")
def export_ttl(document_id: str, db: Session = Depends(get_db)) -> Response:
    code = _resolve_code(document_id, db)
    return Response(export_document_rdf(db, code), media_type="text/turtle")
