"""Todo lo que el sistema tiene sobre una persona, y todo lo que no.

Esta es la consulta más fácil de convertir en una mentira. Un buscador por
nombre que devuelve "la ficha" de quien se escribe da a entender dos cosas que
el sistema tiene prohibido afirmar: que esa grafía identifica a una persona
—regla 13: el nombre por sí solo nunca vincula dos menciones— y que lo que
muestra es todo lo que hay. Por eso aquí:

- Buscar devuelve **fichas candidatas**, en plural, y avisa cuando una grafía
  responde a más de una persona. Encontrar no es identificar.
- El expediente separa lo que un acto atribuye de lo que la fuente declara al
  pie de una firma. "Firma como Ministra" no es "fue designada Ministra": lo
  segundo exige un acto, y confundirlos inventaría un nombramiento.
- Las menciones que coinciden con la grafía pero no están vinculadas a esta
  ficha se muestran aparte, dichas como lo que son. Omitirlas haría pasar por
  completo un expediente que no lo está.
- Lo derivado va etiquetado y con la regla que lo produjo. Nada se infiere que
  no sea recalculable desde la evidencia (`domain/legal_effect.py`,
  `application/queries.py`); no hay resumen redactado sobre una persona real.
- La cobertura se declara: de cuántos documentos se construyó y si alguno de
  ellos no produjo ningún hecho. Una ficha escueta puede significar "no consta"
  o "no lo leímos", y el lector tiene derecho a saber cuál de las dos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.adapters.resolution.resolver import SimpleEntityResolver
from kipu_knowledge.application.queries import effective_end, effective_start
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain.normalization import normalize_person_name, person_name_tokens

# Regla con la que se responde "¿qué ocupa hoy?": no es una inferencia nueva,
# es la lectura de las asignaciones vigentes con las fechas que ya están
# determinadas (expresadas por la fuente o fijadas por norma).
CURRENT_ROLE_RULE = "current-assignment/1.0"


@dataclass
class PersonHit:
    """Una ficha que responde a la búsqueda. No es "la persona buscada"."""

    person_id: str
    preferred_name: str
    status: str
    aliases: list[str]
    matched_aliases: list[str]
    documents: int
    appointments: int
    # Cuántas fichas vivas responden a la grafía por la que entró este resultado.
    # Más de una significa que el nombre no distingue, y hay que decirlo.
    persons_sharing_spelling: int


@dataclass
class PersonDossier:
    person: m.Person
    aliases: list[dict[str, Any]]
    spelling_is_ambiguous: bool
    others_with_same_spelling: list[dict[str, Any]]
    appointments: list[dict[str, Any]] = field(default_factory=list)
    signing_capacities: list[dict[str, Any]] = field(default_factory=list)
    other_participations: list[dict[str, Any]] = field(default_factory=list)
    unlinked_mentions: list[dict[str, Any]] = field(default_factory=list)
    open_reviews: list[dict[str, Any]] = field(default_factory=list)
    possible_duplicates: list[dict[str, Any]] = field(default_factory=list)
    current_roles: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Búsqueda
# ---------------------------------------------------------------------------


def _token_filter(stmt: Select[Any], tokens: tuple[str, ...]) -> Select[Any]:
    """Exige que la grafía contenga todas las palabras escritas, como prefijos.

    Escribir "cuba bustinza" tiene que encontrar "ELMER RAFAEL CUBA BUSTINZA":
    la igualdad exacta del nombre completo, que es lo que hacía la API, no
    sirve para una caja de búsqueda —quien consulta rara vez conoce la grafía
    registral entera— y devolver cero sin explicación se lee como "no consta".

    Es un ayudante de recuperación, nada más: que dos fichas salgan en la misma
    lista no dice que sean la misma persona ni que sean distintas.
    """
    for token in tokens:
        pattern = f"% {token}%"
        stmt = stmt.where((" " + m.PersonMention.text_normalized).like(pattern))
    return stmt


def search_persons(session: Session, query: str, limit: int = 25) -> list[PersonHit]:
    normalized = normalize_person_name(query)
    tokens = person_name_tokens(normalized)
    if not tokens:
        return []

    stmt = _token_filter(
        select(m.PersonMention.canonical_person_id, m.PersonMention.text_normalized).where(
            m.PersonMention.canonical_person_id.is_not(None)
        ),
        tokens,
    )
    matched_by_person: dict[str, set[str]] = {}
    for person_id, spelling in session.execute(stmt).all():
        matched_by_person.setdefault(str(person_id), set()).add(spelling)

    resolver = SimpleEntityResolver(session)
    hits: dict[str, PersonHit] = {}
    for person_id, spellings in matched_by_person.items():
        survivor = _surviving_person(session, person_id)
        if survivor is None or survivor.id in hits:
            continue
        sharing = max(
            (resolver.distinct_persons_for_name(spelling) for spelling in spellings), default=1
        )
        hits[survivor.id] = PersonHit(
            person_id=survivor.id,
            preferred_name=survivor.preferred_name,
            status=str(survivor.status),
            aliases=resolver.person_aliases(survivor.id),
            matched_aliases=sorted(spellings),
            documents=_documents_mentioning(session, survivor.id),
            appointments=len(_live_assignments(session, survivor.id)),
            persons_sharing_spelling=sharing,
        )
    return sorted(hits.values(), key=lambda hit: hit.preferred_name)[:limit]


def _surviving_person(session: Session, person_id: str) -> m.Person | None:
    """Sigue la cadena de fusiones hasta la ficha vigente."""
    person = session.get(m.Person, person_id)
    seen: set[str] = set()
    while person is not None and person.merged_into_person_id:
        if person.id in seen:
            break
        seen.add(person.id)
        person = session.get(m.Person, person.merged_into_person_id)
    return person


# ---------------------------------------------------------------------------
# Expediente
# ---------------------------------------------------------------------------


def build_dossier(session: Session, person_id: str, on: date | None = None) -> PersonDossier | None:
    person = session.get(m.Person, person_id)
    if person is None:
        return None
    resolver = SimpleEntityResolver(session)
    mentions = _mentions_of(session, person_id)
    aliases = _aliases(session, person, mentions)

    ambiguous = [
        alias for alias in aliases if resolver.distinct_persons_for_name(alias["spelling"]) > 1
    ]
    dossier = PersonDossier(
        person=person,
        aliases=aliases,
        spelling_is_ambiguous=bool(ambiguous),
        others_with_same_spelling=_others_with_same_spelling(session, person_id, ambiguous),
    )

    assignments = _live_assignments(session, person_id)
    assigned_mention_ids = {a.person_mention_id for a in assignments}
    dossier.appointments = [_appointment(session, a) for a in assignments]
    dossier.current_roles = [
        row for row in dossier.appointments if _covers(row, on or date.today())
    ]
    dossier.signing_capacities = _signing_capacities(session, mentions, assigned_mention_ids)
    dossier.other_participations = _participations_without_assignment(
        session, mentions, assignments
    )
    dossier.unlinked_mentions = _unlinked_mentions(session, [a["spelling"] for a in aliases])
    dossier.open_reviews = _open_reviews(session, mentions, assignments)
    dossier.possible_duplicates = _possible_duplicates(resolver, person_id, aliases)
    dossier.coverage = _coverage(session, mentions)
    return dossier


def _mentions_of(session: Session, person_id: str) -> list[m.PersonMention]:
    return list(
        session.execute(
            select(m.PersonMention).where(m.PersonMention.canonical_person_id == person_id)
        )
        .scalars()
        .all()
    )


def _aliases(
    session: Session, person: m.Person, mentions: list[m.PersonMention]
) -> list[dict[str, Any]]:
    """Grafías con que las fuentes la nombran, y cuáles respalda un humano.

    Un alias "de hecho" es que un documento la llamó así; uno confirmado es que
    un revisor declaró que esa grafía ES esta persona. Solo el segundo autoriza
    a vincular sin preguntar, y por eso se distinguen a la vista.
    """
    confirmed = set(
        session.execute(
            select(m.IdentityPrecedent.name_normalized).where(
                m.IdentityPrecedent.subject_type == "person",
                m.IdentityPrecedent.person_id == person.id,
                m.IdentityPrecedent.revoked_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    counts: dict[str, dict[str, Any]] = {}
    for mention in mentions:
        row = counts.setdefault(
            mention.text_normalized,
            {
                "spelling": mention.text_normalized,
                "written_as": set(),
                "mentions": 0,
                "confirmed_by_human": mention.text_normalized in confirmed,
            },
        )
        row["mentions"] += 1
        row["written_as"].add(mention.text_raw)
    for row in counts.values():
        row["written_as"] = sorted(row["written_as"])
    return sorted(counts.values(), key=lambda row: str(row["spelling"]))


def _others_with_same_spelling(
    session: Session, person_id: str, ambiguous: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Otras fichas que responden a la misma grafía.

    No se fusionan ni se proponen: se nombran. Que dos personas se escriban
    igual es un hecho del corpus, y esconderlo dejaría al lector creyendo que
    esta ficha es la única respuesta a ese nombre.
    """
    others: dict[str, dict[str, Any]] = {}
    for alias in ambiguous:
        rows = session.execute(
            select(m.Person)
            .join(m.PersonMention, m.PersonMention.canonical_person_id == m.Person.id)
            .where(
                m.PersonMention.text_normalized == alias["spelling"],
                m.Person.merged_into_person_id.is_(None),
                m.Person.id != person_id,
            )
            .distinct()
        ).scalars()
        for other in rows:
            others[other.id] = {
                "person_id": other.id,
                "preferred_name": other.preferred_name,
                "spelling": alias["spelling"],
            }
    return list(others.values())


def _live_assignments(session: Session, person_id: str) -> list[m.RoleAssignment]:
    return list(
        session.execute(
            select(m.RoleAssignment)
            .where(
                m.RoleAssignment.person_id == person_id,
                m.RoleAssignment.superseded_at.is_(None),
            )
            .order_by(m.RoleAssignment.recorded_at)
        )
        .scalars()
        .all()
    )


def _document_of(session: Session, document_id: str | None) -> dict[str, Any] | None:
    if document_id is None:
        return None
    doc = session.get(m.LegalDocument, document_id)
    if doc is None:
        return None
    item = session.get(m.PublicationItem, doc.publication_item_id)
    return {
        "document_id": doc.id,
        "publication_code": item.publication_code if item else None,
        "canonical_url": item.canonical_url if item else None,
        "title_raw": doc.title_raw,
        "number_raw": doc.number_raw,
        "published_on": doc.published_on,
    }


def _evidence_of(session: Session, span_id: str | None) -> dict[str, Any] | None:
    if span_id is None:
        return None
    span = session.get(m.EvidenceSpan, span_id)
    if span is None:
        return None
    return {
        "quoted_text": span.quoted_text,
        "article_label": span.article_label,
        "artifact_version_id": span.artifact_version_id,
    }


def _appointment(session: Session, assignment: m.RoleAssignment) -> dict[str, Any]:
    """Un puesto que un acto atribuye, con de dónde sale cada fecha."""
    event = None
    if assignment.start_event_id:
        event = session.get(m.PersonnelEvent, assignment.start_event_id)
    elif assignment.end_event_id:
        event = session.get(m.PersonnelEvent, assignment.end_event_id)
    start, start_basis = effective_start(assignment)
    end, end_basis = effective_end(assignment)
    position = session.get(m.Position, assignment.position_id) if assignment.position_id else None
    organization = (
        session.get(m.Organization, position.organization_id)
        if position is not None and position.organization_id
        else None
    )
    mention = session.get(m.PersonMention, assignment.person_mention_id)
    return {
        "assignment_id": assignment.id,
        "position_label": assignment.position_label_raw,
        "organization": organization.preferred_name if organization else None,
        "organization_path_raw": assignment.organization_path_raw,
        "assignment_kind": str(assignment.assignment_kind),
        "event_type": str(event.event_type) if event else None,
        "start": start,
        "start_basis": start_basis,
        "end": end,
        "end_basis": end_basis,
        "start_stated": str(assignment.valid_from_status),
        "end_stated": str(assignment.valid_to_status),
        "end_condition_text": assignment.end_condition_text,
        "document": _document_of(session, event.legal_document_id if event else None),
        "evidence": _evidence_of(session, mention.evidence_span_id if mention else None),
        "written_as": mention.text_raw if mention else None,
    }


def _covers(appointment: dict[str, Any], on: date) -> bool:
    """¿La asignación cubre esa fecha con fechas determinadas?

    Un inicio que no consta no se supone anterior: sin fecha determinada no se
    puede afirmar que estuviera vigente, y esto devuelve False. La ficha lo
    dice aparte en vez de callarlo.
    """
    start, end = appointment["start"], appointment["end"]
    if start is None or start > on:
        return False
    return end is None or end >= on


def _signing_capacities(
    session: Session, mentions: list[m.PersonMention], assigned: set[str]
) -> list[dict[str, Any]]:
    """El cargo con que la persona firma, cuando ningún acto se lo atribuye.

    La fuente lo declara al pie del documento, así que es un hecho citable. Lo
    que NO es, es un nombramiento: nadie ha publicado aquí el acto que se lo
    dio. Presentarlo junto a los puestos registrados haría creer que consta un
    acto que no consta — y presentarlo como nada dejaría la ficha de quien
    firma como Presidenta o Ministro completamente vacía.
    """
    # Se agrupa por cargo, no por grafía: firmar el mismo cargo con y sin el
    # segundo nombre es un solo hecho escrito de dos maneras, y partirlo en dos
    # filas sugeriría dos capacidades donde hay una.
    rows: dict[str, dict[str, Any]] = {}
    for mention in mentions:
        if mention.id in assigned or not mention.role_context_raw:
            continue
        signatory = (
            session.execute(select(m.Signatory).where(m.Signatory.person_mention_id == mention.id))
            .scalars()
            .first()
        )
        if signatory is None:
            continue
        key = mention.role_context_normalized or mention.role_context_raw
        row = rows.setdefault(
            key,
            {
                "capacity_raw": mention.role_context_raw,
                "written_as": set(),
                "documents": [],
                "evidence": _evidence_of(session, mention.evidence_span_id),
            },
        )
        row["written_as"].add(mention.text_raw)
        document = _document_of(session, mention.legal_document_id)
        if document is not None:
            row["documents"].append(document)
    for row in rows.values():
        row["written_as"] = sorted(row["written_as"])
    return list(rows.values())


def _participations_without_assignment(
    session: Session, mentions: list[m.PersonMention], assignments: list[m.RoleAssignment]
) -> list[dict[str, Any]]:
    """Eventos que nombran a la persona sin que resulte una asignación suya.

    Por ejemplo el titular cuyo retorno pone fin a una encargatura: el acto lo
    nombra, pero no le atribuye a él ese puesto. Omitirlos perdería que el
    documento habla de esta persona.
    """
    covered = {a.start_event_id for a in assignments} | {a.end_event_id for a in assignments}
    mention_ids = [mention.id for mention in mentions]
    if not mention_ids:
        return []
    rows: list[dict[str, Any]] = []
    for participant, event in session.execute(
        select(m.EventParticipant, m.PersonnelEvent)
        .join(m.PersonnelEvent, m.PersonnelEvent.id == m.EventParticipant.event_id)
        .where(m.EventParticipant.person_mention_id.in_(mention_ids))
    ).all():
        if event.id in covered:
            continue
        mention = session.get(m.PersonMention, participant.person_mention_id)
        # El artículo del que sale el acto vive en el span del evento, no en el
        # evento: es una coordenada de la cita, no un atributo del hecho.
        event_span = _evidence_of(session, event.evidence_span_id)
        rows.append(
            {
                "role_in_event": str(participant.role_in_event),
                "event_type": str(event.event_type),
                "article_label": (event_span or {}).get("article_label"),
                "document": _document_of(session, event.legal_document_id),
                "evidence": _evidence_of(session, mention.evidence_span_id if mention else None),
            }
        )
    return rows


def _unlinked_mentions(session: Session, spellings: list[str]) -> list[dict[str, Any]]:
    """Menciones que se escriben igual y NO están atribuidas a esta ficha.

    Existen siempre que una resolución de identidad esté pendiente. Meterlas en
    el expediente sería vincular por nombre; ocultarlas sería presentar como
    completo lo que no lo está. Van aparte y dichas: "coincide la grafía; el
    sistema no ha decidido si es la misma persona".
    """
    if not spellings:
        return []
    rows: list[dict[str, Any]] = []
    for mention in session.execute(
        select(m.PersonMention).where(
            m.PersonMention.text_normalized.in_(spellings),
            m.PersonMention.canonical_person_id.is_(None),
        )
    ).scalars():
        rows.append(
            {
                "written_as": mention.text_raw,
                "spelling": mention.text_normalized,
                "role_context_raw": mention.role_context_raw,
                "resolution_status": str(mention.resolution_status),
                "document": _document_of(session, mention.legal_document_id),
                "evidence": _evidence_of(session, mention.evidence_span_id),
            }
        )
    return rows


def _open_reviews(
    session: Session, mentions: list[m.PersonMention], assignments: list[m.RoleAssignment]
) -> list[dict[str, Any]]:
    """Lo que está pendiente de decidir y afecta a esta ficha."""
    targets: list[str] = [mention.id for mention in mentions]
    targets += [a.id for a in assignments]
    targets += [a.start_event_id for a in assignments if a.start_event_id]
    targets += [a.end_event_id for a in assignments if a.end_event_id]
    if not targets:
        return []
    rows = session.execute(
        select(m.ReviewTask).where(
            m.ReviewTask.target_id.in_(targets),
            m.ReviewTask.status == e.ReviewTaskStatus.PENDING,
        )
    ).scalars()
    return [
        {
            "task_id": task.id,
            "task_type": str(task.task_type),
            "reason": task.reason,
            "priority": task.priority,
        }
        for task in rows
    ]


def _possible_duplicates(
    resolver: SimpleEntityResolver, person_id: str, aliases: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Fichas que podrían ser esta misma persona escrita de otra forma.

    Se señalan, nunca se fusionan: fusionar por parecido de nombre es
    exactamente lo que la regla 13 prohíbe. Decidirlo es de un humano.
    """
    found: dict[str, dict[str, Any]] = {}
    for alias in aliases:
        for proposal in resolver.variant_person_candidates(str(alias["spelling"])):
            if proposal.entity_id == person_id or proposal.entity_id in found:
                continue
            found[proposal.entity_id] = {
                "person_id": proposal.entity_id,
                "preferred_name": proposal.entity_label,
                "rationale": proposal.rationale,
            }
    return list(found.values())


def _documents_mentioning(session: Session, person_id: str) -> int:
    return len(
        set(
            session.execute(
                select(m.PersonMention.legal_document_id).where(
                    m.PersonMention.canonical_person_id == person_id
                )
            )
            .scalars()
            .all()
        )
    )


def _coverage(session: Session, mentions: list[m.PersonMention]) -> dict[str, Any]:
    """De cuánto se construyó el expediente, y qué se leyó sin sacar nada.

    Un expediente escueto tiene dos lecturas incompatibles: "de esta persona no
    consta más" y "de estos documentos no supimos leer más". Declarar los
    documentos que no produjeron ningún hecho separa una de la otra. Es la
    misma cuenta que vigila la bitácora del recolector, traída a donde alguien
    la va a leer.
    """
    document_ids = {mention.legal_document_id for mention in mentions}
    silent: list[dict[str, Any]] = []
    for document_id in document_ids:
        has_event = session.execute(
            select(m.PersonnelEvent.id)
            .where(m.PersonnelEvent.legal_document_id == document_id)
            .limit(1)
        ).first()
        if has_event is None:
            document = _document_of(session, document_id)
            if document is not None:
                silent.append(document)
    return {
        "documents": len(document_ids),
        "documents_without_any_event": silent,
        "corpus_note": (
            "El expediente cubre únicamente los documentos capturados e ingeridos por "
            "este sistema. Que algo no aparezca aquí no significa que no exista."
        ),
    }


def person_alias_spellings(session: Session, person_id: str) -> list[str]:
    """Grafías de la ficha, para buscar coincidencias sin vincular por nombre."""
    return list(
        session.execute(
            select(m.PersonMention.text_normalized)
            .where(m.PersonMention.canonical_person_id == person_id)
            .distinct()
        )
        .scalars()
        .all()
    )


__all__ = [
    "CURRENT_ROLE_RULE",
    "PersonDossier",
    "PersonHit",
    "build_dossier",
    "person_alias_spellings",
    "search_persons",
]
