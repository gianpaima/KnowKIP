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
from kipu_knowledge.application.legal_effect import determined_payload
from kipu_knowledge.application.queries import effective_end, effective_start
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain.normalization import (
    normalize_person_name,
    normalize_position_label,
    person_name_tokens,
)
from kipu_knowledge.domain.web_context import is_short_name_form

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
    signed_acts: list[dict[str, Any]] = field(default_factory=list)
    other_participations: list[dict[str, Any]] = field(default_factory=list)
    unlinked_mentions: list[dict[str, Any]] = field(default_factory=list)
    open_reviews: list[dict[str, Any]] = field(default_factory=list)
    possible_duplicates: list[dict[str, Any]] = field(default_factory=list)
    current_roles: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    # Capa de contexto atribuido (docs/web-context-design.md): lo que fuentes
    # web admitidas publicaron sobre esta persona. Nunca se mezcla con el
    # registro funcional de arriba: cada entrada dice quién lo publicó.
    web_context: dict[str, Any] = field(default_factory=dict)


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
            # Períodos, no filas: una designación y su cese posterior son un
            # solo paso por el cargo, y contarlos como dos inflaría la ficha.
            appointments=len(_paired_assignments(_live_assignments(session, survivor.id))),
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
    dossier.appointments = [
        _appointment(session, lead, end) for lead, end in _paired_assignments(assignments)
    ]
    # Cronología legible: lo más reciente primero; lo sin fecha determinada, al
    # final, que es donde no afirma nada sobre el orden.
    dossier.appointments.sort(key=_chronology_key, reverse=True)
    dossier.current_roles = [
        row for row in dossier.appointments if _covers(row, on or date.today())
    ]
    dossier.signing_capacities = _signing_capacities(session, mentions, assigned_mention_ids)
    dossier.signed_acts = _signed_acts(session, mentions)
    dossier.other_participations = _participations_without_assignment(
        session, mentions, assignments
    )
    dossier.unlinked_mentions = _unlinked_mentions(session, [a["spelling"] for a in aliases])
    dossier.open_reviews = _open_reviews(session, mentions, assignments)
    dossier.possible_duplicates = _possible_duplicates(resolver, person_id, aliases)
    dossier.coverage = _coverage(session, mentions)
    dossier.web_context = _web_context(session, person_id, [a["spelling"] for a in aliases])
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
        "document_type_raw": doc.document_type_raw,
        "number_raw": doc.number_raw,
        "published_on": doc.published_on,
        "issuer": _issuer_of(session, doc),
    }


def _issuer_of(session: Session, doc: m.LegalDocument) -> dict[str, Any] | None:
    """Organismo emisor del documento, si el índice del diario lo declaró.

    Es un dato del documento, no del bloque de firma: viene del encabezado bajo
    el que el índice oficial listó el dispositivo (`application/issuer.py`), y
    se dice con esa procedencia para que nadie lo lea como declarado en el acto.
    """
    if not doc.issuer_mention_id:
        return None
    mention = session.get(m.OrganizationMention, doc.issuer_mention_id)
    if mention is None:
        return None
    organization = (
        session.get(m.Organization, mention.canonical_organization_id)
        if mention.canonical_organization_id
        else None
    )
    return {
        "text_raw": mention.text_raw,
        "organization": organization.preferred_name if organization else None,
        "basis": "declarado por el índice del diario oficial",
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


def _paired_assignments(
    assignments: list[m.RoleAssignment],
) -> list[tuple[m.RoleAssignment, m.RoleAssignment | None]]:
    """Une la designación y su cese del mismo cargo en un solo período.

    El persister guarda el alta y la baja como filas separadas —cada acto es su
    propia fila—, pero un lector ve UN paso por el cargo, no dos puestos. Se
    emparejan solo cuando la evidencia lo respalda: misma persona (la consulta
    ya filtró), mismo cargo normalizado, sin contradicción de entidad ni de
    fechas, y candidato único. Dos candidatos posibles no eligen: se muestran
    las filas tal cual, que es lo que consta.
    """

    def position_key(ra: m.RoleAssignment) -> str:
        return normalize_position_label(ra.position_label_raw or "")

    def compatible(start_ra: m.RoleAssignment, end_ra: m.RoleAssignment) -> bool:
        if (
            start_ra.position_id
            and end_ra.position_id
            and start_ra.position_id != end_ra.position_id
        ):
            return False
        if (
            start_ra.organization_id
            and end_ra.organization_id
            and start_ra.organization_id != end_ra.organization_id
        ):
            return False
        started, _ = effective_start(start_ra)
        ended, _ = effective_end(end_ra)
        return started is None or ended is None or started <= ended

    open_starts: list[m.RoleAssignment] = []
    bare_ends: list[m.RoleAssignment] = []
    standalone: list[m.RoleAssignment] = []
    for ra in assignments:
        if (
            ra.start_event_id
            and not ra.end_event_id
            and ra.valid_to is None
            and ra.legal_effect_to is None
        ):
            open_starts.append(ra)
        elif ra.end_event_id and not ra.start_event_id:
            bare_ends.append(ra)
        else:
            standalone.append(ra)

    pairs: list[tuple[m.RoleAssignment, m.RoleAssignment | None]] = []
    used: set[str] = set()
    for end_ra in bare_ends:
        candidates = [
            s
            for s in open_starts
            if s.id not in used
            and position_key(s) == position_key(end_ra)
            and compatible(s, end_ra)
        ]
        if len(candidates) == 1:
            used.add(candidates[0].id)
            pairs.append((candidates[0], end_ra))
        else:
            pairs.append((end_ra, None))
    pairs.extend((s, None) for s in open_starts if s.id not in used)
    pairs.extend((ra, None) for ra in standalone)
    return pairs


def _event_of(session: Session, event_id: str | None) -> m.PersonnelEvent | None:
    return session.get(m.PersonnelEvent, event_id) if event_id else None


def _legal_basis_if_ruled(
    basis: str | None, event: m.PersonnelEvent | None
) -> dict[str, Any] | None:
    """La norma que determinó la fecha, solo cuando la fecha salió de una norma."""
    if basis != "legal_rule" or event is None:
        return None
    return determined_payload(event)


def _appointment(
    session: Session, assignment: m.RoleAssignment, end_assignment: m.RoleAssignment | None = None
) -> dict[str, Any]:
    """Un período en un puesto, con el acto (o los dos actos) que lo sostienen.

    `end_assignment` llega cuando el cese vive en su propia fila y el pareo lo
    unió a esta designación; entonces el final del período —fecha, documento y
    cita— sale del acto de cese, dicho como tal.
    """
    start_event = _event_of(session, assignment.start_event_id)
    lead_event = start_event or _event_of(session, assignment.end_event_id)
    end_event = (
        _event_of(session, end_assignment.end_event_id)
        if end_assignment is not None
        else (start_event and _event_of(session, assignment.end_event_id))
    )
    start, start_basis = effective_start(assignment)
    end, end_basis = effective_end(end_assignment if end_assignment is not None else assignment)
    position = session.get(m.Position, assignment.position_id) if assignment.position_id else None
    organization = (
        session.get(m.Organization, position.organization_id)
        if position is not None and position.organization_id
        else None
    )
    mention = session.get(m.PersonMention, assignment.person_mention_id)
    end_mention = (
        session.get(m.PersonMention, end_assignment.person_mention_id)
        if end_assignment is not None
        else None
    )
    slots = (
        session.execute(select(m.PositionSlot).where(m.PositionSlot.position_id == position.id))
        .scalars()
        .all()
        if position is not None
        else []
    )
    mandate = None
    mandate_id = assignment.mandate_id or (end_assignment.mandate_id if end_assignment else None)
    if mandate_id:
        row = session.get(m.Mandate, mandate_id)
        if row is not None:
            mandate = {
                "mandate_type": str(row.mandate_type),
                "label": row.label,
                "end_condition_text": row.end_condition_text,
            }
    end_row = end_assignment if end_assignment is not None else assignment
    return {
        "assignment_id": assignment.id,
        "position_label": assignment.position_label_raw,
        "organization": organization.preferred_name if organization else None,
        "organization_path_raw": assignment.organization_path_raw,
        "assignment_kind": str(assignment.assignment_kind),
        "event_type": str(lead_event.event_type) if lead_event else None,
        "assignment_effect": (
            str(lead_event.assignment_effect)
            if lead_event and lead_event.assignment_effect
            else None
        ),
        "legal_verb_raw": lead_event.legal_verb_raw if lead_event else None,
        "start": start,
        "start_basis": start_basis,
        "end": end,
        "end_basis": end_basis,
        "start_stated": str(assignment.valid_from_status),
        "end_stated": str(end_row.valid_to_status),
        "end_condition_text": assignment.end_condition_text or end_row.end_condition_text,
        "start_legal_basis": _legal_basis_if_ruled(start_basis, start_event or lead_event),
        "end_legal_basis": _legal_basis_if_ruled(end_basis, end_event or lead_event),
        "position_slots": [
            {"scheme": slot.external_scheme, "code": slot.external_code} for slot in slots
        ],
        "mandate": mandate,
        "document": _document_of(session, lead_event.legal_document_id if lead_event else None),
        "evidence": _evidence_of(session, mention.evidence_span_id if mention else None),
        "written_as": mention.text_raw if mention else None,
        # El acto de cese, cuando es otro documento: se muestra junto al
        # período pero dicho como el acto separado que es.
        "merged_from_two_acts": end_assignment is not None,
        "end_event_type": (
            str(end_event.event_type) if end_assignment is not None and end_event else None
        ),
        "end_legal_verb_raw": (
            end_event.legal_verb_raw if end_assignment is not None and end_event else None
        ),
        "end_document": (
            _document_of(session, end_event.legal_document_id)
            if end_assignment is not None and end_event
            else None
        ),
        "end_evidence": (
            _evidence_of(session, end_mention.evidence_span_id if end_mention else None)
            if end_assignment is not None
            else None
        ),
    }


def _chronology_key(row: dict[str, Any]) -> date:
    value = row["start"] or row["end"]
    if value is None and row["document"]:
        value = row["document"]["published_on"]
    return value or date.min


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
                # El bloque de firma puede decir solo "Ministra", sin cartera.
                # La entidad sale del emisor que el índice declaró para cada
                # documento firmado: es un dato derivado y se rotula como tal.
                "issuers": set(),
                "evidence": _evidence_of(session, mention.evidence_span_id),
            },
        )
        row["written_as"].add(mention.text_raw)
        document = _document_of(session, mention.legal_document_id)
        if document is not None:
            row["documents"].append(document)
            if document["issuer"]:
                row["issuers"].add(
                    document["issuer"]["organization"] or document["issuer"]["text_raw"]
                )
    for row in rows.values():
        row["written_as"] = sorted(row["written_as"])
        row["issuers"] = sorted(row["issuers"])
    return list(rows.values())


def _signed_acts(session: Session, mentions: list[m.PersonMention]) -> list[dict[str, Any]]:
    """Lo que resolvieron los documentos que esta persona firmó.

    Firmar no atribuye ningún puesto al firmante, pero sí lo sitúa: quien firma
    una designación es quien la dictó. Sin esta vista, la ficha de una ministra
    que solo firma queda reducida a "aparece en 4 documentos", cuando el sistema
    sabe exactamente a quién designó cada uno, a qué cargo y desde cuándo. Cada
    hecho mostrado es el evento ya extraído, con su evidencia; aquí no se
    infiere nada nuevo. Solo aparecen documentos con algún hecho extraído: los
    leídos sin sacar nada ya los declara la cobertura.
    """
    by_id = {mention.id: mention for mention in mentions}
    if not by_id:
        return []
    rows: list[dict[str, Any]] = []
    for signatory in (
        session.execute(select(m.Signatory).where(m.Signatory.person_mention_id.in_(list(by_id))))
        .scalars()
        .all()
    ):
        events = (
            session.execute(
                select(m.PersonnelEvent)
                .where(m.PersonnelEvent.legal_document_id == signatory.legal_document_id)
                .order_by(m.PersonnelEvent.id)
            )
            .scalars()
            .all()
        )
        if not events:
            continue
        acts: list[dict[str, Any]] = []
        for event in events:
            event_span = _evidence_of(session, event.evidence_span_id)
            subjects: list[dict[str, Any]] = []
            for participant in session.execute(
                select(m.EventParticipant).where(
                    m.EventParticipant.event_id == event.id,
                    m.EventParticipant.person_mention_id.is_not(None),
                )
            ).scalars():
                subject = session.get(m.PersonMention, participant.person_mention_id)
                if subject is None:
                    continue
                subjects.append(
                    {
                        "name": subject.text_raw,
                        "role_in_event": str(participant.role_in_event),
                        "person_id": subject.canonical_person_id,
                    }
                )
            assignments: list[dict[str, Any]] = []
            for ra in session.execute(
                select(m.RoleAssignment).where(
                    (m.RoleAssignment.start_event_id == event.id)
                    | (m.RoleAssignment.end_event_id == event.id),
                    m.RoleAssignment.superseded_at.is_(None),
                )
            ).scalars():
                start, start_basis = effective_start(ra)
                end, end_basis = effective_end(ra)
                assignments.append(
                    {
                        "position_label": ra.position_label_raw,
                        "start": start,
                        "start_basis": start_basis,
                        "end": end,
                        "end_basis": end_basis,
                    }
                )
            acts.append(
                {
                    "event_type": str(event.event_type),
                    "assignment_effect": (
                        str(event.assignment_effect) if event.assignment_effect else None
                    ),
                    "legal_verb_raw": event.legal_verb_raw,
                    "article_label": (event_span or {}).get("article_label"),
                    "subjects": subjects,
                    "assignments": assignments,
                }
            )
        mention = by_id[str(signatory.person_mention_id)]
        rows.append(
            {
                "capacity_raw": mention.role_context_raw or signatory.capacity_raw,
                "document": _document_of(session, signatory.legal_document_id),
                "acts": acts,
            }
        )
    rows.sort(key=lambda row: (row["document"] or {}).get("published_on") or date.min, reverse=True)
    return rows


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


def _web_context(session: Session, person_id: str, spellings: list[str]) -> dict[str, Any]:
    """La capa de contexto atribuido de la ficha: documentos web y sus afirmaciones.

    Cada entrada declara su publicador, su alcance de captura (`body_scope`) y
    la cita en que se apoya. Los documentos cuya mención sigue sin resolver van
    aparte, como las menciones sin atribuir del corpus: contarlos dentro sería
    vincular por nombre.
    """
    # Una entrada por documento, no por mención: un artículo que nombra a la
    # persona con tres grafías es UNA pieza de cobertura escrita de tres formas.
    documents: list[dict[str, Any]] = []
    by_doc: dict[str, dict[str, Any]] = {}
    for mention in session.execute(
        select(m.WebPersonMention).where(m.WebPersonMention.canonical_person_id == person_id)
    ).scalars():
        entry = by_doc.get(mention.web_document_id)
        if entry is None:
            entry = _web_document_entry(session, mention.web_document_id)
            if entry is None:
                continue
            entry["claims"] = _web_claims(session, person_id, entry["artifact_version_id"])
            by_doc[mention.web_document_id] = entry
            documents.append(entry)
        entry["mentions"].append(_web_mention_of(session, mention))
    documents.sort(key=lambda row: row["published_at_raw"] or "", reverse=True)

    # Las menciones web guardan la forma corta que escribió la fuente ("CESAR
    # LUNA VICTORIA"); la ficha conoce grafías registrales completas. La
    # comparación es la misma regla de formas cortas del extractor, aplicada
    # aquí en memoria: la tabla de menciones sin resolver es pequeña.
    unlinked: list[dict[str, Any]] = []
    if spellings:
        full_forms = [person_name_tokens(s) for s in spellings]
        for mention in session.execute(
            select(m.WebPersonMention).where(m.WebPersonMention.canonical_person_id.is_(None))
        ).scalars():
            run = person_name_tokens(mention.text_normalized)
            if not any(is_short_name_form(run, full) for full in full_forms):
                continue
            entry = _web_document_entry(session, mention.web_document_id)
            if entry is not None:
                entry["mentions"].append(_web_mention_of(session, mention))
                unlinked.append(entry)

    return {
        "documents": documents,
        "unlinked_mentions": unlinked,
        "note": (
            "Contexto publicado por fuentes sin peso jurídico (prensa, web). Cada "
            "afirmación es de su publicador, no del registro funcional: se muestra "
            "con su cita y nunca crea ni modifica cargos ni fechas."
        ),
    }


def _web_mention_of(session: Session, mention: m.WebPersonMention) -> dict[str, Any]:
    return {
        "written_as": mention.text_raw,
        "resolution_status": str(mention.resolution_status),
        "matched_by": mention.matched_by,
        "evidence": _evidence_of(session, mention.evidence_span_id),
    }


def _web_document_entry(session: Session, web_document_id: str) -> dict[str, Any] | None:
    doc = session.get(m.WebDocument, web_document_id)
    if doc is None:
        return None
    item = session.get(m.PublicationItem, doc.publication_item_id)
    system = session.get(m.SourceSystem, item.source_system_id) if item else None
    references: list[dict[str, Any]] = []
    for reference in session.execute(
        select(m.WebReference).where(m.WebReference.web_document_id == doc.id)
    ).scalars():
        references.append(
            {
                "target_number_raw": reference.target_number_raw,
                "resolved_document": _document_of(session, reference.target_document_id),
                "evidence": _evidence_of(session, reference.evidence_span_id),
            }
        )
    return {
        "web_document_id": doc.id,
        "artifact_version_id": doc.parsed_from_artifact_version_id,
        "source_name": system.name if system else None,
        "source_authority": str(system.authority) if system else None,
        "kind": str(doc.kind),
        "headline_raw": doc.headline_raw,
        "published_at_raw": doc.published_at_raw,
        "author_raw": doc.author_raw,
        "section_raw": doc.section_raw,
        "body_scope": str(doc.body_scope),
        "canonical_url": item.canonical_url if item else None,
        "mentions": [],
        "references": references,
    }


def _web_claims(session: Session, person_id: str, artifact_version_id: str) -> list[dict[str, Any]]:
    """Afirmaciones `web:*` vigentes extraídas de esa captura sobre esta persona."""
    rows: list[dict[str, Any]] = []
    for assertion, run in session.execute(
        select(m.Assertion, m.ExtractionRun)
        .join(m.ExtractionRun, m.ExtractionRun.id == m.Assertion.extraction_run_id)
        .where(
            m.ExtractionRun.artifact_version_id == artifact_version_id,
            m.Assertion.subject_type == "person",
            m.Assertion.subject_id == person_id,
            m.Assertion.superseded_at.is_(None),
            m.Assertion.predicate.like("web:%"),
        )
        .order_by(m.Assertion.predicate)
    ).all():
        rows.append(
            {
                "predicate": assertion.predicate,
                "object_value": assertion.object_value_json,
                "review_status": str(assertion.review_status),
                "confidence": assertion.confidence,
                "extracted_by": run.extractor_version
                + (f" ({run.model_provider}/{run.model_name})" if run.model_provider else ""),
                "evidence": _evidence_of(session, assertion.evidence_span_id),
            }
        )
    return rows


def persons_without_projected_facts(session: Session) -> list[dict[str, Any]]:
    """Fichas que algún documento nombra sin que se proyecte ningún hecho.

    Es el mismo vacío que "dispositivo relevante sin eventos", visto desde la
    persona: hay menciones vinculadas pero ni un puesto, ni una firma, ni una
    participación en acto alguno. Una ficha así se lee como "no consta nada",
    cuando lo cierto puede ser "no supimos leerlo" — y por eso se cuenta y se
    avisa en la bitácora, en vez de esperarse a que alguien la busque.
    """
    rows: list[dict[str, Any]] = []
    for person in session.execute(
        select(m.Person).where(m.Person.merged_into_person_id.is_(None))
    ).scalars():
        mention_ids = list(
            session.execute(
                select(m.PersonMention.id).where(m.PersonMention.canonical_person_id == person.id)
            ).scalars()
        )
        if not mention_ids:
            continue
        has_assignment = session.execute(
            select(m.RoleAssignment.id)
            .where(
                m.RoleAssignment.person_id == person.id,
                m.RoleAssignment.superseded_at.is_(None),
            )
            .limit(1)
        ).first()
        if has_assignment is not None:
            continue
        has_signature = session.execute(
            select(m.Signatory.id).where(m.Signatory.person_mention_id.in_(mention_ids)).limit(1)
        ).first()
        if has_signature is not None:
            continue
        has_participation = session.execute(
            select(m.EventParticipant.id)
            .where(m.EventParticipant.person_mention_id.in_(mention_ids))
            .limit(1)
        ).first()
        if has_participation is not None:
            continue
        rows.append(
            {
                "person_id": person.id,
                "preferred_name": person.preferred_name,
                "mentions": len(mention_ids),
            }
        )
    return rows


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
    "persons_without_projected_facts",
    "search_persons",
]
