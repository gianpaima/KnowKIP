"""UI mínima de revisión (server-rendered, Jinja2 + formularios HTML).

Permite: listar tareas, ver documento/evidencia/texto original vs normalizado,
aceptar/rechazar, vincular mención a entidad, crear entidad, separar fusión,
marcar fecha no expresada, resolver puesto y ver historial de decisiones.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.adapters.resolution.resolver import SimpleEntityResolver
from kipu_knowledge.application.legal_effect import determined_payload, verdict_for_event
from kipu_knowledge.application.person_dossier import build_dossier, search_persons
from kipu_knowledge.application.review import ReviewError, ReviewService
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain.cargos import structural_cargo
from kipu_knowledge.domain.contracts import ArtifactStore
from kipu_knowledge.domain.legal_effect import LegalEffectVerdict
from kipu_knowledge.domain.normalization import normalize_org_name, strip_accents
from kipu_knowledge.domain.state_entities import catalog_entity, parent_entity
from kipu_knowledge.interfaces.api.deps import get_db, get_store

router = APIRouter(prefix="/review", tags=["review-ui"])

# Acciones ofrecidas por tipo de objetivo, con la etiqueta redactada en el idioma
# de la decisión y no en el del enum.
#
# La clave es `target_type` y no el tipo de tarea porque es lo que los manejadores
# de ReviewService validan: una acción ausente de esta tabla o bien falla con 422
# sobre ese objetivo (MARK_DATE_NOT_STATED sobre una mención) o bien no cambia
# nada (ACCEPT fuera de una afirmación). Ofrecerla solo puede confundir.
#
# ACCEPT no aparece para `person_mention` a propósito: cerraría la tarea sin dejar
# la mención en HUMAN_CONFIRMED, de modo que "confirmo que es otra persona" se
# expresa mejor con CREATE_ENTITY, que sí registra el resultado.
_ACTIONS_BY_TARGET: dict[str, list[tuple[str, str]]] = {
    "person_mention": [
        ("LINK_ENTITY", "Es la misma persona — vincular a una ficha existente"),
        ("CREATE_ENTITY", "Es otra persona — registrar como ficha nueva"),
        ("REJECT", "La mención está mal extraída — rechazarla"),
        ("DISMISS", "Descartar esta tarea sin decidir"),
    ],
    "organization_mention": [
        ("LINK_ENTITY", "Es la misma organización — vincular a una ficha existente"),
        ("DISMISS", "Descartar esta tarea sin decidir"),
    ],
    "personnel_event": [
        (
            "APPLY_LEGAL_EFFECT_DATE",
            "La fecha la fija la norma — aplicar la que la regla determina",
        ),
        (
            "SET_LEGAL_EFFECT_DATE",
            "La norma no la determina — fijar la fecha a mano, con motivo",
        ),
        ("MARK_DATE_NOT_STATED", "El documento no expresa la fecha efectiva"),
        ("ACCEPT", "Dar por buena la extracción y cerrar la tarea"),
        ("DISMISS", "Descartar esta tarea sin decidir"),
    ],
    "position": [
        ("RESOLVE_POSITION", "Asignar el puesto a una organización"),
        ("DISMISS", "Descartar esta tarea sin decidir"),
    ],
    "assertion": [
        ("ACCEPT", "Aceptar la afirmación"),
        ("REJECT", "Rechazar la afirmación"),
        ("DISMISS", "Descartar esta tarea sin decidir"),
    ],
    # ORG_VARIANT_CHECK y las tareas de coletilla/catálogo apuntan a una
    # `organization` ya creada. LINK_ENTITY la fusiona con la elegida
    # (`merged_into_organization_id`): menciones, unidades, puestos y
    # asignaciones pasan a la superviviente y la fila queda apuntándola.
    "organization": [
        ("LINK_ENTITY", "Es la misma entidad — fusionarla con la organización elegida"),
        ("DISMISS", "Descartar esta tarea sin decidir"),
    ],
}

# Acción marcada por defecto: la más probable para ese objetivo, nunca una
# destructiva ni una que cierre la tarea sin registrar el resultado.
_DEFAULT_ACTION = {
    "person_mention": "LINK_ENTITY",
    "organization_mention": "LINK_ENTITY",
    "personnel_event": "MARK_DATE_NOT_STATED",
    "position": "RESOLVE_POSITION",
    "assertion": "ACCEPT",
    "organization": "LINK_ENTITY",
}

# Acciones que registran un precedente de identidad y por tanto muestran el
# control de alcance.
_PRECEDENT_ACTIONS = ("LINK_ENTITY", "CREATE_ENTITY")


def _applicable_actions(
    target_type: str, verdict: LegalEffectVerdict | None
) -> list[tuple[str, str]]:
    """Acciones que este objetivo admite **ahora mismo**, no en abstracto.

    El catálogo por `target_type` no basta: sobre un mismo evento de personal,
    que "aplicar la regla" funcione o falle depende de lo que la regla diga de
    ese acto en este momento. Ofrecer las dos siempre garantizaba que una de
    ellas terminara en 422 con el formulario perdido (ADR-0009).
    """
    actions = list(_ACTIONS_BY_TARGET.get(target_type, [("DISMISS", "Descartar esta tarea")]))
    if target_type != "personnel_event" or verdict is None:
        return actions
    determined = verdict.determined
    excluded = "SET_LEGAL_EFFECT_DATE" if determined else "APPLY_LEGAL_EFFECT_DATE"
    return [(value, label) for value, label in actions if value != excluded]


_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
)


def _render(template: str, **context: Any) -> HTMLResponse:
    return HTMLResponse(_env.get_template(template).render(**context))


@router.get("", response_class=HTMLResponse)
def task_list(db: Session = Depends(get_db), status: str = "PENDING") -> HTMLResponse:
    tasks = (
        db.execute(
            select(m.ReviewTask)
            .where(m.ReviewTask.status == status)
            .order_by(m.ReviewTask.priority, m.ReviewTask.created_at)
        )
        .scalars()
        .all()
    )
    return _render("tasks.html", tasks=tasks, status=status)


def _dispatch_rows(db: Session) -> tuple[list[dict[str, Any]], int]:
    """Tareas de resolución de identidad despachables en lote.

    Solo entran las menciones con exactamente UNA ficha candidata por nombre:
    el lote acelera la confirmación humana, no la reemplaza (regla 13) — cada
    fila marcada es una decisión LINK_ENTITY del revisor, con su firma y su
    precedente. Las de candidatos múltiples siguen en la revisión una a una,
    donde está el contexto completo. Devuelve (filas, cuántas quedaron fuera
    por tener más de un candidato).
    """
    tasks = (
        db.execute(
            select(m.ReviewTask)
            .where(
                m.ReviewTask.task_type == e.ReviewTaskType.ENTITY_RESOLUTION,
                m.ReviewTask.status == e.ReviewTaskStatus.PENDING,
            )
            .order_by(m.ReviewTask.created_at)
        )
        .scalars()
        .all()
    )
    resolver = SimpleEntityResolver(db)
    rows: list[dict[str, Any]] = []
    multi = 0
    for task in tasks:
        mention = db.get(m.PersonMention, task.target_id)
        if mention is None:
            continue
        proposals = resolver.propose_matches(mention.text_normalized, {"kind": "person"})
        if len(proposals) != 1:
            multi += 1
            continue
        person = db.get(m.Person, proposals[0].entity_id)
        if person is None or person.merged_into_person_id is not None:
            multi += 1
            continue
        doc = db.get(m.LegalDocument, mention.legal_document_id)
        item = db.get(m.PublicationItem, doc.publication_item_id) if doc is not None else None
        # El cargo vigente conocido de la ficha: es el contexto mínimo con que
        # el revisor juzga si la mención habla de la misma persona.
        last = (
            db.execute(
                select(m.RoleAssignment)
                .where(
                    m.RoleAssignment.person_id == person.id,
                    m.RoleAssignment.superseded_at.is_(None),
                )
                .order_by(m.RoleAssignment.recorded_at.desc())
            )
            .scalars()
            .first()
        )
        known_role = last.position_label_raw if last is not None else None
        if known_role is None and last is not None and last.position_id is not None:
            position = db.get(m.Position, last.position_id)
            known_role = position.preferred_label if position is not None else None
        rows.append(
            {
                "task": task,
                "mention": mention,
                "person": person,
                "known_role": known_role,
                "document_number": doc.number_raw if doc is not None else None,
                "publication_code": item.publication_code if item is not None else None,
            }
        )
    return rows, multi


@router.get("/dispatch", response_class=HTMLResponse)
def dispatch_view(db: Session = Depends(get_db)) -> HTMLResponse:
    rows, multi = _dispatch_rows(db)
    return _render(
        "dispatch.html", rows=rows, multi_candidate_count=multi, message=None, error=None
    )


@router.post("/dispatch")
def dispatch_submit(
    db: Session = Depends(get_db),
    reviewer: str = Form(""),
    task_id: list[str] = Form([]),
) -> Response:
    def render(message: str | None, error: str | None, status_code: int = 200) -> HTMLResponse:
        rows, multi = _dispatch_rows(db)
        return HTMLResponse(
            _env.get_template("dispatch.html").render(
                rows=rows, multi_candidate_count=multi, message=message, error=error
            ),
            status_code=status_code,
        )

    if not reviewer.strip():
        return render(None, "Indica quién revisa: cada vinculación queda firmada.", 422)
    if not task_id:
        return render(None, "No se marcó ninguna fila.", 422)

    resolver = SimpleEntityResolver(db)
    service = ReviewService(db)
    linked = 0
    already = 0
    failed: list[str] = []
    for tid in task_id:
        task = db.get(m.ReviewTask, tid)
        if (
            task is None
            or task.task_type != e.ReviewTaskType.ENTITY_RESOLUTION
            or task.status != e.ReviewTaskStatus.PENDING
        ):
            # El precedente sembrado por una fila anterior del propio lote pudo
            # resolverla ya: no es un fallo, es el lote trabajando en cadena.
            already += 1
            continue
        mention = db.get(m.PersonMention, task.target_id)
        proposals = (
            resolver.propose_matches(mention.text_normalized, {"kind": "person"})
            if mention is not None
            else []
        )
        if len(proposals) != 1:
            failed.append(tid)
            continue
        savepoint = db.begin_nested()
        try:
            service.decide(
                task.id,
                e.DecisionAction.LINK_ENTITY,
                reviewer=reviewer.strip(),
                payload={"entity_id": proposals[0].entity_id},
                notes="despacho en lote: coincidencia única de nombre confirmada por el revisor",
            )
        except (ReviewError, ValueError) as exc:
            savepoint.rollback()
            failed.append(f"{tid}: {exc}")
            continue
        linked += 1
    parts = [f"{linked} mención(es) vinculada(s)"]
    if already:
        parts.append(f"{already} ya estaban resueltas (precedentes del propio lote)")
    if failed:
        parts.append(f"{len(failed)} no despachadas: {'; '.join(failed[:5])}")
    return render("; ".join(parts), None)


@router.get("/crawls", response_class=HTMLResponse)
def crawl_runs(db: Session = Depends(get_db)) -> HTMLResponse:
    """Bitácora operativa de las recolecciones diarias.

    La página se apoya exclusivamente en ``crawl_run``/``crawl_item``: muestra
    lo que la aplicación alcanzó a registrar y no pretende inferir el historial
    del Programador de tareas de Windows.
    """
    runs = (
        db.execute(select(m.CrawlRun).order_by(m.CrawlRun.started_at.desc()).limit(50))
        .scalars()
        .all()
    )
    rows: list[dict[str, Any]] = []
    for run in runs:
        items = (
            db.execute(
                select(m.CrawlItem)
                .where(m.CrawlItem.crawl_run_id == run.id)
                .order_by(m.CrawlItem.publication_code)
            )
            .scalars()
            .all()
        )
        counts: dict[str, int] = {}
        for item in items:
            counts[item.status.value] = counts.get(item.status.value, 0) + 1
        empty_relevant = sum(
            item.relevance == e.Relevance.RELEVANT and item.events_extracted == 0 for item in items
        )
        rows.append(
            {
                "run": run,
                "items": items,
                "counts": counts,
                "total": len(items),
                "empty_relevant": empty_relevant,
            }
        )
    return _render("crawls.html", rows=rows)


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(task_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    return _render("task_detail.html", **_task_context(task_id, db))


def _task_context(task_id: str, db: Session, error: str | None = None) -> dict[str, Any]:
    """Todo lo que la página de una tarea necesita para pintarse.

    Está separado de la vista porque un envío rechazado tiene que volver al
    mismo formulario con el motivo: antes se respondía un 422 desnudo y el
    revisor perdía lo que había escrito.
    """
    task = db.get(m.ReviewTask, task_id)
    if task is None:
        raise HTTPException(404, "Tarea no encontrada")

    context: dict[str, Any] = {
        "task": task,
        "actions": _ACTIONS_BY_TARGET.get(task.target_type, [("DISMISS", "Descartar esta tarea")]),
        "default_action": _DEFAULT_ACTION.get(task.target_type, "DISMISS"),
        "precedent_actions": _PRECEDENT_ACTIONS,
        "entity_choices": [],
        "entity_kind": "person",
        "organizations": [],
        "organizations_rest": [],
        "position_org": None,
        "position_units": [],
        "structural_cargo": None,
        "error": error,
    }
    evidence = None
    document = None
    candidates: Sequence[m.Person] = []
    origins: list[dict[str, Any]] = []

    if task.target_type == "person_mention":
        mention = db.get(m.PersonMention, task.target_id)
        if mention is not None:
            context["mention"] = mention
            evidence = db.get(m.EvidenceSpan, mention.evidence_span_id)
            document = db.get(m.LegalDocument, mention.legal_document_id)
            candidates = (
                db.execute(
                    select(m.Person)
                    .join(m.PersonMention, m.PersonMention.canonical_person_id == m.Person.id)
                    .where(m.PersonMention.text_normalized == mention.text_normalized)
                    .distinct()
                )
                .scalars()
                .all()
            )
            # En PERSON_VARIANT_CHECK el nombre no coincide de forma exacta, así que
            # los candidatos útiles son los de grafía compatible, no los homónimos.
            variants = [
                (db.get(m.Person, prop.entity_id), prop)
                for prop in SimpleEntityResolver(db).variant_person_candidates(
                    mention.text_normalized
                )
            ]
            context["variants"] = [
                (person, prop) for person, prop in variants if person is not None
            ]
            context["precedent"] = (
                db.get(m.IdentityPrecedent, mention.identity_precedent_id)
                if mention.identity_precedent_id
                else None
            )
            # Un alias sin cargo solo es admisible sobre una grafía discriminante;
            # anticiparlo evita que el revisor lo intente y reciba un 422.
            context["homonym_count"] = SimpleEntityResolver(db).distinct_persons_for_name(
                mention.text_normalized
            )
            context["entity_choices"] = _person_choices(db, context["variants"], candidates)
            context["default_preferred_name"] = mention.text_raw
    elif task.target_type == "personnel_event":
        event = db.get(m.PersonnelEvent, task.target_id)
        if event is not None:
            context["event"] = event
            evidence = db.get(m.EvidenceSpan, event.evidence_span_id)
            document = db.get(m.LegalDocument, event.legal_document_id)
            context["participants"] = _participants_with_evidence(db, event.id)
            # Qué dice la norma sobre este acto, ahora mismo: si ya determinó la
            # fecha o, cuando la tarea sigue abierta, por qué no la determinó.
            # Sin esto el revisor no puede saber si "aplicar la regla" va a
            # funcionar, y la ofrecería a ciegas.
            verdict = verdict_for_event(db, event)
            context["legal_effect"] = determined_payload(event)
            context["legal_effect_verdict"] = verdict
            # El menú se recorta con el veredicto vivo, no con el tipo de
            # objetivo: es la diferencia entre ofrecer una salida y ofrecer un
            # 422 (ADR-0009).
            context["actions"] = _applicable_actions(task.target_type, verdict)
            if not verdict.determined:
                # Un acto que difiere su vigencia sí dice cuándo empieza; lo que
                # falta es la fecha. Proponer "no expresa la fecha" ahí sería
                # sugerirle al revisor que afirme algo que el artículo contradice.
                context["default_action"] = (
                    "SET_LEGAL_EFFECT_DATE"
                    if verdict.deferral is not None
                    else "MARK_DATE_NOT_STATED"
                )
            else:
                context["default_action"] = "APPLY_LEGAL_EFFECT_DATE"
    elif task.target_type == "position":
        position = db.get(m.Position, task.target_id)
        context["position"] = position
        origins = _position_origins(db, task.target_id)
        context["origins_title"] = "De dónde procede este puesto"
        # Un puesto creado desde un CAP (PositionSlot) no tiene mención de
        # persona detrás; el documento que lo declaró es lo único que queda.
        slot_documents = (
            db.execute(
                select(m.LegalDocument)
                .join(m.PositionSlot, m.PositionSlot.source_document_id == m.LegalDocument.id)
                .where(m.PositionSlot.position_id == task.target_id)
            )
            .scalars()
            .all()
        )
        if not origins and slot_documents:
            document = slot_documents[0]
        if position is not None:
            candidates_orgs, rest = _position_org_choices(db, position, origins, slot_documents)
            context["organizations"] = candidates_orgs
            context["organizations_rest"] = rest
            context["position_org"] = (
                db.get(m.Organization, position.organization_id)
                if position.organization_id
                else None
            )
            context["position_units"] = _unit_chain_labels(db, position.organizational_unit_id)
            context["structural_cargo"] = structural_cargo(position.label_normalized)
    elif task.target_type == "organization":
        organization = db.get(m.Organization, task.target_id)
        context["organization"] = organization
        origins = _organization_origins(db, task.target_id)
        context["origins_title"] = "De dónde procede esta organización"
        if organization is not None and organization.merged_into_organization_id is None:
            # Con quién fusionar: primero las de nombre contenido/continente
            # (las que motivan ORG_VARIANT_CHECK); si no hay ninguna, el resto
            # del registro, porque LINK_ENTITY sin destino no lleva a nada.
            context["entity_kind"] = "organization"
            context["entity_choices"] = _organization_choices(db, organization)
    elif task.target_type == "organization_mention":
        org_mention = db.get(m.OrganizationMention, task.target_id)
        if org_mention is not None:
            context["org_mention"] = org_mention
            if org_mention.evidence_span_id:
                evidence = db.get(m.EvidenceSpan, org_mention.evidence_span_id)
            if org_mention.legal_document_id:
                document = db.get(m.LegalDocument, org_mention.legal_document_id)
    elif task.target_type == "assertion":
        assertion = db.get(m.Assertion, task.target_id)
        if assertion is not None:
            context["assertion"] = assertion
            evidence = db.get(m.EvidenceSpan, assertion.evidence_span_id)

    section = None
    if evidence is not None and evidence.document_section_id:
        section = db.get(m.DocumentSection, evidence.document_section_id)

    # Un puesto o una organización son entidades canónicas: no tienen EvidenceSpan
    # propio, existen porque alguna mención los creó. Sin tomar prestada la
    # evidencia de esa mención, sus tareas se mostraban sin cita ni captura y el
    # revisor no tenía con qué decidir.
    if document is None:
        document = next((o["document"] for o in origins if o["document"] is not None), None)
    preview_evidence = evidence or next(
        (o["evidence"] for o in origins if o["evidence"] is not None), None
    )

    version = None
    artifact = None
    publication_item = None
    if preview_evidence is not None:
        version = db.get(m.ArtifactVersion, preview_evidence.artifact_version_id)
    if version is None and document is not None:
        # Última salida cuando no hay ningún span al que agarrarse: la captura de
        # la que se parseó el documento. Es la misma evidencia, sin fragmento
        # resaltado, y siempre es preferible a no mostrar nada.
        version = db.get(m.ArtifactVersion, document.parsed_from_artifact_version_id)
    if version is not None:
        artifact = db.get(m.Artifact, version.artifact_id)
    if artifact is not None:
        publication_item = db.get(m.PublicationItem, artifact.publication_item_id)
    if document is None and publication_item is not None:
        # Las tareas sobre assertions no llevan documento directo: se deriva
        # del artefacto que respalda la evidencia.
        document = (
            db.execute(
                select(m.LegalDocument).where(
                    m.LegalDocument.publication_item_id == publication_item.id
                )
            )
            .scalars()
            .first()
        )

    highlight, span_intact = _evidence_highlight(evidence, section)
    preview = _preview_for(version, artifact, publication_item, preview_evidence)
    sources = _sources_for_document(db, document)

    decisions = (
        db.execute(
            select(m.ReviewDecision)
            .where(m.ReviewDecision.review_task_id == task.id)
            .order_by(m.ReviewDecision.decided_at)
        )
        .scalars()
        .all()
    )

    context.update(
        evidence=evidence,
        evidence_label=_evidence_label(evidence, section),
        section=section,
        document=document,
        candidates=candidates,
        decisions=decisions,
        highlight=highlight,
        span_intact=span_intact,
        preview=preview,
        origins=origins,
        sources=sources,
    )
    return context


# Cuántos documentos de origen se listan por ficha candidata. La primera mención
# es la que responde "¿de dónde salió esta persona?"; el resto es corroboración
# y no necesita ser exhaustivo en la página de decisión (para eso está el
# expediente).
_CHOICE_APPEARANCES_LIMIT = 4


def _person_choices(
    db: Session,
    variants: Sequence[tuple[m.Person, Any]],
    candidates: Sequence[m.Person],
) -> list[dict[str, Any]]:
    """Fichas a las que se puede vincular la mención, para elegir por nombre.

    El identificador viaja en el `value` del control; el revisor nunca tiene que
    leerlo ni copiarlo. Las variantes de grafía van primero porque son las que
    motivan la tarea; los homónimos exactos después. Cada opción lleva el motivo
    por el que se propone y en qué documentos aparece esa ficha —empezando por el
    que la mencionó por primera vez—, que es lo que permite decidir si es la
    misma persona sin salir de la página.
    """
    choices: dict[str, dict[str, Any]] = {}
    ordered: list[tuple[m.Person, str]] = [
        *((person, prop.rationale) for person, prop in variants),
        *((person, "mismo nombre normalizado en una mención previa") for person in candidates),
    ]
    for person, rationale in ordered:
        if person.id in choices:
            continue
        mentions = db.execute(
            select(func.count())
            .select_from(m.PersonMention)
            .where(m.PersonMention.canonical_person_id == person.id)
        ).scalar_one()
        choices[person.id] = {
            "id": person.id,
            "label": person.preferred_name,
            "rationale": rationale,
            "mentions": mentions,
            "appearances": _person_appearances(db, person.id),
        }
    return list(choices.values())


def _person_appearances(db: Session, person_id: str) -> list[dict[str, Any]]:
    """Documentos donde una ficha candidata ya fue mencionada, el inicial primero.

    Es lo que la tarea de resolución le pide comparar al revisor: la mención
    nueva contra dónde y como qué apareció antes la persona existente. Sin esta
    lista solo veía el nombre repetido y tenía que salir a la base a buscar el
    documento de origen.
    """
    rows = db.execute(
        select(m.PersonMention, m.LegalDocument)
        .join(m.LegalDocument, m.LegalDocument.id == m.PersonMention.legal_document_id)
        .where(m.PersonMention.canonical_person_id == person_id)
        # El documento sin fecha de publicación al final: no puede ser "el inicial".
        .order_by(
            m.LegalDocument.published_on.is_(None),
            m.LegalDocument.published_on,
            m.PersonMention.id,
        )
        .limit(_CHOICE_APPEARANCES_LIMIT)
    ).all()
    return [
        {
            "document_type": document.document_type_raw,
            "number": document.number_raw,
            "published_on": document.published_on,
            "role": mention.role_context_raw,
            "resolution_status": mention.resolution_status.value,
        }
        for mention, document in rows
    ]


def _cited(db: Session, evidence_span_id: str | None) -> dict[str, Any]:
    """Evidencia lista para pintar: la cita, su sección y el resaltado calculado."""
    evidence = db.get(m.EvidenceSpan, evidence_span_id) if evidence_span_id else None
    section = (
        db.get(m.DocumentSection, evidence.document_section_id)
        if evidence is not None and evidence.document_section_id
        else None
    )
    highlight, intact = _evidence_highlight(evidence, section)
    return {"evidence": evidence, "highlight": highlight, "intact": intact}


def _participants_with_evidence(db: Session, event_id: str) -> list[dict[str, Any]]:
    """Personas del evento con su propia evidencia resaltada.

    En LINK_AFFECTED_ASSIGNMENT la persona candidata proviene de los
    considerandos, no del artículo resolutivo: sin su cita el revisor no puede
    confirmar el vínculo desde la página de la tarea.
    """
    rows = db.execute(
        select(m.EventParticipant, m.PersonMention)
        .join(m.PersonMention, m.PersonMention.id == m.EventParticipant.person_mention_id)
        .where(m.EventParticipant.event_id == event_id)
        .order_by(m.EventParticipant.id)
    ).all()
    return [
        {
            "role": participant.role_in_event,
            "mention": mention,
            **_cited(db, mention.evidence_span_id),
        }
        for participant, mention in rows
    ]


def _position_origins(db: Session, position_id: str) -> list[dict[str, Any]]:
    """Menciones que dieron origen a un puesto, con su cita y su documento.

    Un Position no guarda evidencia: se crea al registrar la asignación de rol de
    una mención. Para decidir a qué organización pertenece —lo único que pide
    POSITION_ORG_UNRESOLVED— hay que leer la ruta cruda tal como la escribió el
    documento, que es justo lo que conserva `organization_path_raw`.
    """
    rows = db.execute(
        select(m.RoleAssignment, m.PersonMention)
        .join(m.PersonMention, m.PersonMention.id == m.RoleAssignment.person_mention_id)
        .where(m.RoleAssignment.position_id == position_id)
        .order_by(m.RoleAssignment.recorded_at)
    ).all()
    origins: list[dict[str, Any]] = []
    seen: set[str] = set()
    for assignment, mention in rows:
        if mention.id in seen:
            continue
        seen.add(mention.id)
        origins.append(
            {
                "title": mention.text_raw,
                "detail": assignment.organization_path_raw or assignment.position_label_raw,
                "document": db.get(m.LegalDocument, mention.legal_document_id),
                **_cited(db, mention.evidence_span_id),
            }
        )
    return origins


def _canonical_org(db: Session, organization_id: str | None) -> m.Organization | None:
    """Sigue la cadena de fusiones hasta la organización superviviente."""
    org = db.get(m.Organization, organization_id) if organization_id else None
    hops = 0
    while org is not None and org.merged_into_organization_id and hops < 10:
        org = db.get(m.Organization, org.merged_into_organization_id)
        hops += 1
    return org


def _unit_chain_labels(db: Session, unit_id: str | None) -> list[str]:
    """Nombres de la cadena de unidades, de la más específica a la más general."""
    labels: list[str] = []
    seen: set[str] = set()
    while unit_id and unit_id not in seen:
        seen.add(unit_id)
        unit = db.get(m.OrganizationalUnit, unit_id)
        if unit is None:
            break
        labels.append(unit.preferred_name)
        unit_id = unit.parent_unit_id
    return labels


def _acronym_in(acronym: str | None, haystack_upper: str) -> bool:
    if not acronym or not haystack_upper:
        return False
    # La sigla curada puede llevar tilde (PROMPERÚ) y los números de documento
    # no la llevan (000139-2026-PROMPERU/PE): se compara sin acentos.
    pattern = rf"(?<![A-Z0-9]){re.escape(strip_accents(acronym).upper())}(?![A-Z0-9])"
    return re.search(pattern, haystack_upper) is not None


def _position_org_choices(
    db: Session,
    position: m.Position,
    origins: Sequence[dict[str, Any]],
    slot_documents: Sequence[m.LegalDocument],
) -> tuple[list[dict[str, Any]], list[m.Organization]]:
    """Organizaciones candidatas para RESOLVE_POSITION, con su motivo visible.

    La tarea pregunta a qué órgano pertenece un puesto concreto: las candidatas
    salen de la propia resolución —la ruta cruda del puesto, la entidad emisora,
    las organizaciones que el documento menciona y las siglas de su número—,
    no del registro entero. Ofrecer las 40 fichas del registro obligaba a leer
    una lista inmensa para encontrar las dos que el documento efectivamente
    nombra. El registro completo sigue disponible, plegado, como salida de
    emergencia: acotar es priorizar, no afirmar que la respuesta esté dentro.
    """
    documents: list[m.LegalDocument] = [
        o["document"] for o in origins if o.get("document") is not None
    ]
    for doc in slot_documents:
        if all(doc.id != d.id for d in documents):
            documents.append(doc)

    # La ruta tal como la escribió cada documento + la etiqueta del puesto.
    path_texts: list[str] = [position.preferred_label]
    for ra in db.execute(
        select(m.RoleAssignment).where(m.RoleAssignment.position_id == position.id)
    ).scalars():
        for text in (ra.organization_path_raw, ra.position_label_raw):
            if text and text not in path_texts:
                path_texts.append(text)
    path_normalized = " | ".join(normalize_org_name(t) for t in path_texts)
    path_upper = strip_accents(" | ".join(path_texts)).upper()
    numbers_upper = " | ".join((d.number_raw or "").upper() for d in documents)

    registry = (
        db.execute(
            select(m.Organization)
            .where(m.Organization.merged_into_organization_id.is_(None))
            .order_by(m.Organization.preferred_name)
        )
        .scalars()
        .all()
    )

    chosen: dict[str, dict[str, Any]] = {}

    def add(org: m.Organization | None, reason: str) -> None:
        if org is None or org.merged_into_organization_id is not None:
            return
        entry = chosen.setdefault(
            org.id,
            {"id": org.id, "label": org.preferred_name, "acronym": org.acronym, "reasons": []},
        )
        if reason not in entry["reasons"]:
            entry["reasons"].append(reason)

    # 1) La ruta del puesto nombra a la organización (o a su sigla).
    for org in registry:
        if org.name_normalized and org.name_normalized in path_normalized:
            add(org, "su nombre aparece en la ruta del puesto")
        elif _acronym_in(org.acronym, path_upper):
            add(org, "su sigla aparece en la ruta del puesto")

    # 2) La entidad emisora y las organizaciones mencionadas en los documentos
    #    de origen: son las únicas que la resolución efectivamente involucra.
    for doc in documents:
        if doc.issuer_mention_id:
            mention = db.get(m.OrganizationMention, doc.issuer_mention_id)
            if mention is not None:
                add(_canonical_org(db, mention.canonical_organization_id), "emisora del documento")
        for mention in db.execute(
            select(m.OrganizationMention).where(m.OrganizationMention.legal_document_id == doc.id)
        ).scalars():
            add(
                _canonical_org(db, mention.canonical_organization_id),
                f"mencionada en el documento como «{mention.text_raw}»",
            )

    # 3) Siglas dentro del número del documento (000128-2026-MINEDU-VMGI-PRONIED-DE
    #    nombra al ministerio y al programa aunque el cuerpo no los repita).
    for org in registry:
        if org.id not in chosen and _acronym_in(org.acronym, numbers_upper):
            add(org, "su sigla figura en el número del documento")

    # 4) La entidad de la que depende una candidata (adscripción curada): si el
    #    puesto es del programa, el ministerio del que depende también es una
    #    respuesta plausible, y viceversa.
    for entry in list(chosen.values()):
        child = db.get(m.Organization, entry["id"])
        if child is None:
            continue
        parent = _canonical_org(db, child.parent_organization_id)
        if parent is None:
            catalogued = catalog_entity(child.name_normalized)
            declared = parent_entity(catalogued) if catalogued else None
            if declared is not None:
                names = [normalize_org_name(declared.canonical_name)]
                if declared.acronym:
                    names.append(
                        normalize_org_name(f"{declared.canonical_name} - {declared.acronym}")
                    )
                parent = next((o for o in registry if o.name_normalized in names), None)
        if parent is not None and parent.id != child.id:
            add(parent, f"entidad de la que depende {child.preferred_name}")

    rest = [org for org in registry if org.id not in chosen]
    return list(chosen.values()), rest


def _organization_choices(db: Session, organization: m.Organization) -> list[dict[str, Any]]:
    """Organizaciones con las que ORG_VARIANT_CHECK puede fusionar la variante.

    Mismo contrato que `_person_choices`: id en el `value`, motivo visible y en
    qué documentos aparece cada candidata, que es lo que permite decidir si es
    la misma entidad sin salir de la página.
    """
    similar = SimpleEntityResolver.similar_orgs(db, organization.name_normalized)
    candidates: list[tuple[m.Organization, str]] = [
        (org, "nombre contenido en la variante o viceversa") for org in similar
    ]
    if not candidates:
        rest = (
            db.execute(
                select(m.Organization)
                .where(
                    m.Organization.id != organization.id,
                    m.Organization.merged_into_organization_id.is_(None),
                )
                .order_by(m.Organization.preferred_name)
            )
            .scalars()
            .all()
        )
        candidates = [(org, "registro existente") for org in rest]
    choices: list[dict[str, Any]] = []
    for org, rationale in candidates:
        mentions = db.execute(
            select(func.count())
            .select_from(m.OrganizationMention)
            .where(m.OrganizationMention.canonical_organization_id == org.id)
        ).scalar_one()
        choices.append(
            {
                "id": org.id,
                "label": org.preferred_name,
                "rationale": rationale,
                "mentions": mentions,
                "appearances": _organization_appearances(db, org.id),
            }
        )
    return choices


def _organization_appearances(db: Session, organization_id: str) -> list[dict[str, Any]]:
    """Documentos donde una organización candidata ya fue mencionada."""
    rows = db.execute(
        select(m.OrganizationMention, m.LegalDocument)
        .join(m.LegalDocument, m.LegalDocument.id == m.OrganizationMention.legal_document_id)
        .where(m.OrganizationMention.canonical_organization_id == organization_id)
        .order_by(
            m.LegalDocument.published_on.is_(None),
            m.LegalDocument.published_on,
            m.OrganizationMention.id,
        )
        .limit(_CHOICE_APPEARANCES_LIMIT)
    ).all()
    return [
        {
            "document_type": document.document_type_raw,
            "number": document.number_raw,
            "published_on": document.published_on,
            "role": None,
            "resolution_status": mention.resolution_status.value,
        }
        for mention, document in rows
    ]


def _organization_origins(db: Session, organization_id: str) -> list[dict[str, Any]]:
    """Menciones vinculadas a una organización canónica, con su cita.

    ORG_VARIANT_CHECK pregunta si dos organizaciones son la misma: sin ver cómo
    las nombró cada documento la pregunta no se puede responder.
    """
    mentions = (
        db.execute(
            select(m.OrganizationMention)
            .where(m.OrganizationMention.canonical_organization_id == organization_id)
            .order_by(m.OrganizationMention.id)
        )
        .scalars()
        .all()
    )
    return [
        {
            "title": mention.text_raw,
            "detail": f"normalizado «{mention.text_normalized}»",
            "document": (
                db.get(m.LegalDocument, mention.legal_document_id)
                if mention.legal_document_id
                else None
            ),
            **_cited(db, mention.evidence_span_id),
        }
        for mention in mentions
    ]


# Nombre legible de cada tipo de sección, para cuando la evidencia no lleva
# etiqueta de artículo. Decir "bloque de firma" explica por sí solo por qué la
# cita es únicamente un nombre; "sin etiqueta de artículo" no explicaba nada.
_SECTION_TYPE_LABEL = {
    e.SectionType.SUMMARY: "sumilla",
    e.SectionType.DOC_TYPE: "tipo de documento",
    e.SectionType.DOC_NUMBER: "número del documento",
    e.SectionType.ISSUE_LINE: "línea de fecha y lugar",
    e.SectionType.VISTOS: "sección de vistos",
    e.SectionType.CONSIDERANDO: "considerando",
    e.SectionType.RESOLVE_HEADER: "encabezado resolutivo",
    e.SectionType.ARTICLE: "artículo",
    e.SectionType.ARTICLE_BODY: "cuerpo de artículo",
    e.SectionType.ARTICLE_LIST_ITEM: "ítem de artículo colectivo",
    e.SectionType.ARTICLE_TABLE_HEADER: "cabecera de tabla",
    e.SectionType.ARTICLE_TABLE_ROW: "fila de tabla de artículo",
    e.SectionType.CLOSING: "fórmula de cierre",
    e.SectionType.SIGNATURE: "bloque de firma",
    e.SectionType.PUBLICATION_CODE: "código de publicación",
    e.SectionType.ANNEX: "anexo",
}


def _evidence_label(
    evidence: m.EvidenceSpan | None, section: m.DocumentSection | None
) -> str | None:
    """Etiqueta con la que presentar una cita: artículo si lo hay, sección si no."""
    if evidence is not None and evidence.article_label:
        return evidence.article_label
    if section is not None:
        return _SECTION_TYPE_LABEL.get(section.section_type)
    return None


def _evidence_highlight(
    evidence: m.EvidenceSpan | None, section: m.DocumentSection | None
) -> tuple[tuple[str, str, str] | None, bool]:
    """Divide el texto de la sección en (antes, cita, después) para resaltarla.

    Devuelve además si el span sigue anclado a su sección: si el texto en el
    rango declarado ya no coincide con la cita, no se resalta nada y la UI debe
    advertirlo en lugar de señalar un fragmento equivocado.
    """
    if evidence is None or section is None:
        return None, True
    if evidence.char_start is None or evidence.char_end is None:
        return None, True
    if section.text_raw[evidence.char_start : evidence.char_end] != evidence.quoted_text:
        return None, False
    if evidence.char_start == 0 and evidence.char_end == len(section.text_raw):
        # La cita es la sección completa: resaltar todo no aporta señal.
        return None, True
    return (
        section.text_raw[: evidence.char_start],
        evidence.quoted_text,
        section.text_raw[evidence.char_end :],
    ), True


_AUTHORITY_LABEL = {
    e.SourceAuthority.OFFICIAL_GAZETTE: "diario oficial",
    e.SourceAuthority.ISSUING_ENTITY: "entidad emisora",
    e.SourceAuthority.MIRROR: "copia de terceros",
    e.SourceAuthority.PRESS: "prensa (contexto)",
    e.SourceAuthority.SOCIAL_MEDIA: "red social (contexto)",
    e.SourceAuthority.OTHER_WEB: "web (contexto)",
}


def _sources_for_document(db: Session, document: m.LegalDocument | None) -> list[dict[str, Any]]:
    """Todas las publicaciones conocidas del mismo acto, con su respaldo.

    El acto se publica en más de un sitio —el diario oficial y el portal de la
    entidad que lo emitió— y el revisor necesita saber de cuál salió el texto que
    está leyendo, cuáles hay además, y cuáles están respaldadas en el CAS frente
    a las que son solo un enlace que puede morir.
    """
    if document is None:
        return []
    rows = db.execute(
        select(m.DocumentSource, m.PublicationItem)
        .join(m.PublicationItem, m.PublicationItem.id == m.DocumentSource.publication_item_id)
        .where(m.DocumentSource.legal_document_id == document.id)
        # La autoritativa primero: es la que produce efectos y de la que se extrae.
        .order_by(m.DocumentSource.role, m.DocumentSource.recorded_at)
    ).all()
    sources: list[dict[str, Any]] = []
    for link, item in rows:
        system = db.get(m.SourceSystem, item.source_system_id) if item.source_system_id else None
        sources.append(
            {
                "name": system.name if system else "sistema fuente no registrado",
                "authority": _AUTHORITY_LABEL.get(system.authority) if system else None,
                "role": link.role,
                "is_authoritative": link.role == e.DocumentSourceRole.AUTHORITATIVE,
                "code": item.publication_code,
                "canonical_url": item.canonical_url,
                "pdf_url": item.pdf_url,
                "matched_by": link.matched_by,
                "captures": _captures_of(db, item.id),
            }
        )
    return sources


def _captures_of(db: Session, publication_item_id: str) -> list[dict[str, Any]]:
    """Respaldos en el CAS de una publicación, el más reciente de cada representación."""
    artifacts = (
        db.execute(select(m.Artifact).where(m.Artifact.publication_item_id == publication_item_id))
        .scalars()
        .all()
    )
    captures: list[dict[str, Any]] = []
    for artifact in artifacts:
        version = (
            db.execute(
                select(m.ArtifactVersion)
                .where(m.ArtifactVersion.artifact_id == artifact.id)
                .order_by(m.ArtifactVersion.captured_at.desc())
            )
            .scalars()
            .first()
        )
        if version is None:
            continue
        total = db.execute(
            select(func.count())
            .select_from(m.ArtifactVersion)
            .where(m.ArtifactVersion.artifact_id == artifact.id)
        ).scalar_one()
        captures.append(
            {
                "representation": artifact.representation_type.value,
                "version_id": version.id,
                "sha256": version.sha256,
                "captured_at": version.captured_at,
                "versions": total,
            }
        )
    return captures


def _preview_for(
    version: m.ArtifactVersion | None,
    artifact: m.Artifact | None,
    publication_item: m.PublicationItem | None,
    evidence: m.EvidenceSpan | None,
) -> dict[str, Any] | None:
    """Describe cómo previsualizar la captura que respalda la evidencia.

    Incluye de dónde salió: la página del dispositivo en la fuente y el PDF que
    esa captura declaró. Los datos estaban en la BD desde la ingesta pero el
    panel no los mostraba, así que el revisor no podía contrastar contra el
    original ni citar la procedencia sin salir a consultar la base a mano.
    """
    if version is None or artifact is None:
        return None
    base_media = (artifact.media_type or "").split(";")[0].strip().lower()
    if (
        artifact.representation_type
        in (
            e.RepresentationType.PDF,
            e.RepresentationType.ISSUE_PDF,
        )
        or base_media == "application/pdf"
    ):
        kind = "pdf"
    elif artifact.representation_type == e.RepresentationType.IMAGE or base_media.startswith(
        "image/"
    ):
        kind = "image"
    else:
        kind = "html"
    src = f"/review/artifacts/{version.id}/raw"
    if kind == "pdf" and evidence is not None and evidence.page_number:
        src += f"#page={evidence.page_number}"
    elif kind == "html" and publication_item is not None:
        # La captura puede contener otros dispositivos (title contaminado):
        # anclar al contenedor div#x<código> del dispositivo correcto.
        src += f"#x{publication_item.publication_code}"
    source_url = (publication_item.canonical_url if publication_item else None) or None
    # La URL final solo se muestra cuando difiere de la solicitada: una redirección
    # es información de integridad de la captura, no adorno.
    redirected_to = (
        version.final_url
        if version.final_url and version.final_url != version.requested_url
        else None
    )
    return {
        "kind": kind,
        "src": src,
        "sha256": version.sha256,
        "captured_at": version.captured_at,
        "source_url": source_url,
        "requested_url": version.requested_url,
        "redirected_to": redirected_to,
        "pdf_url": publication_item.pdf_url if publication_item else None,
        # El visor y el archivo son cosas distintas: `…/<código>/pdf` es una
        # página HTML que muestra el PDF, y ofrecerla como si fuera el documento
        # descargable ya llevó a archivar la página creyendo que era el PDF.
        "pdf_viewer_url": f"{source_url}/pdf"
        if source_url and "/dispositivo/" in source_url
        else None,
    }


@router.post("/tasks/{task_id}/decide")
def submit_decision(
    task_id: str,
    db: Session = Depends(get_db),
    action: str = Form(...),
    reviewer: str = Form(""),
    entity_id: str = Form(""),
    entity_id_other: str = Form(""),
    organization_id: str = Form(""),
    preferred_name: str = Form(""),
    precedent_scope: str = Form("office"),
    legal_effect_from: str = Form(""),
    notes: str = Form(""),
) -> Response:
    task = db.get(m.ReviewTask, task_id)
    if task is None:
        raise HTTPException(404, "Tarea no encontrada")

    def rejected(message: str) -> HTMLResponse:
        # 422 sigue siendo la respuesta correcta —el envío no se aceptó—, pero
        # devolviendo la página con el motivo a la vista en lugar de una pantalla
        # de error que obliga a volver atrás y reescribirlo todo.
        return HTMLResponse(
            _env.get_template("task_detail.html").render(
                **_task_context(task_id, db, error=message)
            ),
            status_code=422,
        )

    # El formulario ya solo ofrece las acciones aplicables; validarlas aquí evita
    # que un envío construido a mano llegue a un manejador que no le corresponde.
    allowed = {value for value, _ in _ACTIONS_BY_TARGET.get(task.target_type, [])}
    if action not in allowed:
        return rejected(
            f"La acción {action} no aplica a una tarea sobre {task.target_type} "
            f"(admitidas: {', '.join(sorted(allowed)) or 'ninguna'})"
        )

    payload: dict[str, Any] = {}
    if legal_effect_from.strip():
        payload["legal_effect_from"] = legal_effect_from.strip()
    # El identificador escrito a mano gana sobre la ficha marcada en la lista: si
    # el revisor se tomó el trabajo de escribirlo es porque la correcta no estaba.
    chosen_entity = entity_id_other.strip() or entity_id
    if chosen_entity:
        payload["entity_id"] = chosen_entity
    if organization_id:
        payload["organization_id"] = organization_id
    if preferred_name:
        payload["preferred_name"] = preferred_name
    # Un solo control para las tres posturas posibles sobre el precedente: atarlo
    # al cargo declarado, declararlo alias de la grafía, o no sentar ninguno.
    if precedent_scope == "none":
        payload["create_precedent"] = False
    elif precedent_scope:
        payload["scope"] = precedent_scope
    # La decisión se intenta dentro de un savepoint: `decide` inserta el
    # ReviewDecision antes de ejecutar el manejador, así que un rechazo a mitad
    # dejaría registrada una decisión que nunca se aplicó. Se deshace solo el
    # intento, no lo que la sesión tuviera pendiente por otras razones.
    savepoint = db.begin_nested()
    try:
        ReviewService(db).decide(
            task_id,
            e.DecisionAction(action),
            reviewer=reviewer or None,
            payload=payload or None,
            notes=notes or None,
        )
    except (ReviewError, ValueError) as exc:
        savepoint.rollback()
        return rejected(str(exc))
    return RedirectResponse(url="/review", status_code=303)


@router.get("/artifacts/{version_id}/raw")
def artifact_raw(
    version_id: str,
    request: Request,
    db: Session = Depends(get_db),
    store: ArtifactStore = Depends(get_store),
) -> Response:
    """Sirve los bytes capturados de una versión de artefacto, sin transformar.

    Solo existe bajo /review (herramienta interna): las capturas crudas pueden
    contener datos personales (p. ej. DNI) que el API público y la proyección
    RDF excluyen deliberadamente. La respuesta HTML viaja con CSP `sandbox`
    porque es contenido de terceros: nunca debe ejecutar scripts ni cargar
    recursos remotos dentro del origin de la aplicación.
    """
    version = db.get(m.ArtifactVersion, version_id)
    if version is None:
        raise HTTPException(404, "Versión de artefacto no encontrada")

    etag = f'"{version.sha256}"'
    headers = {
        # El CAS es inmutable: el mismo id sirve los mismos bytes para siempre.
        "ETag": etag,
        "Cache-Control": "private, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    if not store.exists_by_hash(version.sha256):
        raise HTTPException(404, f"Bytes no disponibles en el CAS ({version.object_key})")
    content = store.get(version.object_key)
    if hashlib.sha256(content).hexdigest() != version.sha256:
        raise HTTPException(500, "Los bytes del CAS no coinciden con el sha256 registrado")

    artifact = db.get(m.Artifact, version.artifact_id)
    media_type = "application/octet-stream"
    if artifact is not None and artifact.media_type:
        media_type = artifact.media_type
    if media_type.split(";")[0].strip().lower() == "text/html":
        headers["Content-Security-Policy"] = (
            "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
        )
    return Response(content, media_type=media_type, headers=headers)


@router.get("/history", response_class=HTMLResponse)
def decision_history(db: Session = Depends(get_db)) -> HTMLResponse:
    rows = db.execute(
        select(m.ReviewDecision, m.ReviewTask)
        .join(m.ReviewTask, m.ReviewTask.id == m.ReviewDecision.review_task_id)
        .order_by(m.ReviewDecision.decided_at.desc())
    ).all()
    return _render("history.html", rows=rows)


# ---------------------------------------------------------------------------
# Expediente de una persona
# ---------------------------------------------------------------------------
#
# Vive bajo /review y no en el API público por la regla 6: aquí se cita
# evidencia literal y se enlaza la captura original, que puede contener
# documentos de identidad. Lo que esta superficie muestra no sale de /review.


@router.get("/persons", response_class=HTMLResponse)
def person_search(db: Session = Depends(get_db), q: str = "") -> HTMLResponse:
    """Busca por nombre y devuelve fichas candidatas, en plural.

    Que un resultado aparezca no afirma que sea la persona buscada; que
    aparezcan dos no afirma que sean distintas. Buscar es recuperar.
    """
    hits = search_persons(db, q) if q.strip() else []
    return _render("persons.html", query=q, hits=hits)


@router.get("/persons/{person_id}", response_class=HTMLResponse)
def person_dossier(person_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    dossier = build_dossier(db, person_id)
    if dossier is None:
        raise HTTPException(404, "Persona no encontrada")
    return _render("person_detail.html", d=dossier, today=date.today())
