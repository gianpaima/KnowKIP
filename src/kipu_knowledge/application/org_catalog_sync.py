"""Sincronización de las fichas de organización con el catálogo curado.

Tres pasadas, todas declarativas y auditables (cada cambio se reporta):

1. **Enriquecimiento**: sigla y tipo de las fichas cuya grafía el catálogo
   conoce; solo completa lo que falta o difiere.
2. **Fusión declarada**: dos fichas vivas cuyas grafías el catálogo declara de
   la misma entidad son la misma organización por dato declarado — es el espejo
   persistente de la convergencia que la ingesta aplica al crear. Sobrevive la
   de nombre canónico; si ninguna lo lleva, la más antigua. Las tareas
   pendientes de la absorbida quedan resueltas con decisión firmada.
3. **Sucesión**: cada ficha de un nombre vigente apunta a la ficha de su nombre
   anterior (`predecessor_organization_id`), y las de nombres históricos se
   encadenan entre sí, tal como el catálogo declara la cadena
   (MIDAGRI → MINAGRI → Ministerio de Agricultura). Solo se escribe cuando las
   dos fichas existen: el catálogo no crea filas.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.application.review import ReviewService
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain.normalization import normalize_org_name
from kipu_knowledge.domain.state_entities import StateEntity, catalog_entity, catalog_knows


@dataclass(frozen=True)
class SyncReport:
    enriched: list[str]
    merged: list[str]
    succession: list[str]


def _live_orgs(session: Session) -> list[m.Organization]:
    return list(
        session.execute(
            select(m.Organization)
            .where(m.Organization.merged_into_organization_id.is_(None))
            .order_by(m.Organization.id)
        ).scalars()
    )


def sync_org_catalog(session: Session, *, dry_run: bool = False) -> SyncReport:
    enriched: list[str] = []
    merged: list[str] = []
    succession: list[str] = []

    by_entity: dict[int, tuple[StateEntity, list[m.Organization]]] = {}
    for org in _live_orgs(session):
        entry = catalog_entity(org.name_normalized)
        if entry is None:
            continue
        by_entity.setdefault(id(entry), (entry, []))[1].append(org)
        updates: list[str] = []
        if org.acronym != entry.acronym:
            updates.append(f"acronym: {org.acronym!r} -> {entry.acronym!r}")
            if not dry_run:
                org.acronym = entry.acronym
        if org.organization_type != entry.entity_type:
            updates.append(f"type: {org.organization_type!r} -> {entry.entity_type!r}")
            if not dry_run:
                org.organization_type = entry.entity_type
        if updates:
            enriched.append(f"{org.preferred_name}: " + "; ".join(updates))

    service = ReviewService(session)
    for entry, group in by_entity.values():
        if len(group) < 2:
            continue
        canonical_normalized = normalize_org_name(entry.canonical_name)
        survivor = next((o for o in group if o.name_normalized == canonical_normalized), group[0])
        for org in group:
            if org.id == survivor.id:
                continue
            merged.append(
                f"'{org.preferred_name}' -> '{survivor.preferred_name}' "
                f"({entry.acronym or entry.canonical_name})"
            )
            if dry_run:
                continue
            pending = (
                session.execute(
                    select(m.ReviewTask).where(
                        m.ReviewTask.target_type == "organization",
                        m.ReviewTask.target_id == org.id,
                        m.ReviewTask.status == e.ReviewTaskStatus.PENDING,
                    )
                )
                .scalars()
                .all()
            )
            if pending:
                # La fusión resuelve la pregunta que la tarea hacía; la decisión
                # queda firmada por la sincronización, no anónima.
                service.decide(
                    pending[0].id,
                    e.DecisionAction.LINK_ENTITY,
                    reviewer="sync-org-catalog",
                    payload={"entity_id": survivor.id},
                    notes="grafías declaradas de la misma entidad por el catálogo curado",
                )
                for task in pending[1:]:
                    service.decide(
                        task.id,
                        e.DecisionAction.DISMISS,
                        reviewer="sync-org-catalog",
                        notes="resuelta por la fusión declarada del catálogo",
                    )
            else:
                service.merge_declared_duplicate(org.id, survivor.id)

    # Sucesión: se resuelve sobre las fichas vivas tras las fusiones. Para cada
    # eslabón declarado (vigente→anterior, anterior→más antiguo) se enlaza si
    # ambas fichas existen. Una época sin ficha no rompe la cadena: el eslabón
    # se tiende hacia el nombre anterior más cercano que sí la tenga.
    alive = _live_orgs(session)

    def ficha_of(name: str) -> m.Organization | None:
        target = normalize_org_name(name)
        candidates = [o for o in alive if catalog_knows(o.name_normalized)]
        exact = [o for o in candidates if o.name_normalized == target]
        if exact:
            return exact[0]
        # Grafías con sigla ("… - MIDAGRI") de la misma época vigente.
        entry = catalog_entity(target)
        if entry is not None:
            same = [o for o in candidates if catalog_entity(o.name_normalized) is entry]
            if same:
                return same[0]
        return None

    seen_entries: set[int] = set()
    for org in alive:
        entry = catalog_entity(org.name_normalized)
        if entry is None or not entry.former_names or id(entry) in seen_entries:
            continue
        seen_entries.add(id(entry))
        chain_names = [entry.canonical_name, *(f.name for f in entry.former_names)]
        fichas = [(name, ficha_of(name)) for name in chain_names]
        previous: m.Organization | None = None
        previous_name: str | None = None
        for name, ficha in fichas:
            if ficha is None:
                continue
            if previous is not None and previous.predecessor_organization_id != ficha.id:
                succession.append(f"'{previous_name}' <- '{name}' ({entry.acronym or ''})")
                if not dry_run:
                    previous.predecessor_organization_id = ficha.id
            previous, previous_name = ficha, name
    if not dry_run:
        session.flush()
    return SyncReport(enriched=enriched, merged=merged, succession=succession)
