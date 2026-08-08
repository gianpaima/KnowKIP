"""Expediente de una persona: qué promete mostrar y qué promete no afirmar.

Estas pruebas no comprueban una pantalla, comprueban una lectura. La consulta
"todo lo que se sabe de esta persona" es la que con más facilidad convierte un
sistema honesto en uno que miente sin darse cuenta: basta agrupar por nombre
para inventar identidades, o callar lo que falta para que un expediente
incompleto se lea como exhaustivo.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.application.person_dossier import build_dossier, search_persons
from kipu_knowledge.domain import enums as e

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def _person_named(session: Session, fragment: str) -> m.Person:
    person = (
        session.execute(select(m.Person).where(m.Person.preferred_name.ilike(f"%{fragment}%")))
        .scalars()
        .first()
    )
    assert person is not None, f"no hay ficha que contenga '{fragment}'"
    return person


# ---------------------------------------------------------------------------
# Búsqueda
# ---------------------------------------------------------------------------


def test_search_finds_a_person_by_part_of_the_name(ingested_session: Session):
    """Quien consulta rara vez conoce la grafía registral completa.

    La API exigía igualdad exacta del nombre entero, así que escribir un
    apellido devolvía cero — y cero sin explicación se lee como "no consta".
    """
    hits = search_persons(ingested_session, "cuba bustinza")
    assert hits, "un apellido compuesto debe encontrar a la persona"
    assert any("CUBA BUSTINZA" in hit.preferred_name.upper() for hit in hits)

    # Y por una sola palabra, en cualquier orden.
    assert search_persons(ingested_session, "bustinza")
    assert search_persons(ingested_session, "bustinza cuba")


def test_search_returns_nothing_for_an_empty_query(ingested_session: Session):
    assert search_persons(ingested_session, "   ") == []


def test_search_never_merges_two_persons_that_share_a_spelling(session: Session, ingest_service):
    """Dos personas distintas escritas igual son dos resultados, y se avisa.

    Agrupar por nombre convertiría la búsqueda en una afirmación de identidad,
    que es exactamente lo que la regla 13 prohíbe. La ficha homónima no se
    fusiona ni se esconde: se nombra, y el resultado dice cuántas comparten la
    grafía para que nadie lea "la persona" donde hay dos candidatas.
    """
    ingest_service.ingest_fixture("2540861-1", FIXTURES_DIR)
    session.commit()
    mention = session.execute(select(m.PersonMention)).scalars().first()
    assert mention is not None and mention.canonical_person_id

    # Un homónimo real: otra ficha a la que otra mención con la MISMA grafía
    # apunta. No se declara en ningún sitio que sean la misma persona.
    twin = m.Person(preferred_name=mention.text_raw)
    session.add(twin)
    session.flush()
    session.add(
        m.PersonMention(
            legal_document_id=mention.legal_document_id,
            text_raw=mention.text_raw,
            text_normalized=mention.text_normalized,
            evidence_span_id=mention.evidence_span_id,
            canonical_person_id=twin.id,
            resolution_status=e.ResolutionStatus.CANDIDATE_MATCH,
        )
    )
    session.commit()

    hits = search_persons(session, mention.text_raw)
    ids = {hit.person_id for hit in hits}
    assert len(ids) >= 2, "las dos fichas homónimas deben salir por separado"
    assert all(hit.persons_sharing_spelling >= 2 for hit in hits if hit.person_id in ids)

    dossier = build_dossier(session, twin.id)
    assert dossier is not None
    assert dossier.spelling_is_ambiguous, "la ficha debe advertir que el nombre no distingue"
    assert any(o["person_id"] != twin.id for o in dossier.others_with_same_spelling)


# ---------------------------------------------------------------------------
# Expediente
# ---------------------------------------------------------------------------


def test_dossier_never_exposes_a_declared_identifier(ingested_session: Session):
    """Regla 6: los documentos de identidad no se publican.

    El expediente cita evidencia literal por todas partes, así que es el sitio
    donde un DNI se colaría sin que nadie lo note. Se comprueba sobre todas las
    fichas y sobre todo el texto que el expediente ofrece.
    """
    identifiers = (
        ingested_session.execute(select(m.PersonIdentifier.value_normalized)).scalars().all()
    )
    persons = ingested_session.execute(select(m.Person)).scalars().all()
    for person in persons:
        dossier = build_dossier(ingested_session, person.id)
        assert dossier is not None
        rendered = repr(
            [
                dossier.aliases,
                dossier.appointments,
                dossier.signing_capacities,
                dossier.other_participations,
                dossier.unlinked_mentions,
                dossier.coverage,
            ]
        )
        for value in identifiers:
            assert value not in rendered, f"el expediente de {person.id} expone {value}"


def test_every_stated_fact_carries_its_evidence(ingested_session: Session):
    """Regla 2: toda afirmación lleva la cita que la sostiene.

    Un expediente sin evidencia es una lista de rumores bien maquetada.
    """
    for person in ingested_session.execute(select(m.Person)).scalars():
        dossier = build_dossier(ingested_session, person.id)
        assert dossier is not None
        for row in dossier.appointments:
            assert row["evidence"], f"puesto sin evidencia en {person.preferred_name}"
            assert row["evidence"]["quoted_text"]
            assert row["evidence"]["artifact_version_id"]
        for row in dossier.signing_capacities:
            assert row["evidence"] and row["evidence"]["quoted_text"]


def test_a_signing_capacity_is_not_an_appointment(ingested_session: Session):
    """Firmar como Ministra no es haber sido designada Ministra.

    Lo primero lo declara la fuente al pie del documento; lo segundo exige un
    acto. Si el expediente los mezclara, inventaría nombramientos que nadie
    publicó — y si omitiera el primero, la ficha de quien solo firma quedaría
    vacía, que se lee como "de esta persona no consta nada".
    """
    signatory_only = None
    for person in ingested_session.execute(select(m.Person)).scalars():
        dossier = build_dossier(ingested_session, person.id)
        assert dossier is not None
        if dossier.signing_capacities and not dossier.appointments:
            signatory_only = dossier
            break
    assert signatory_only is not None, "el corpus tiene firmantes sin acto que los designe"
    assert signatory_only.appointments == [], "no se le atribuye ningún puesto"
    assert signatory_only.signing_capacities[0]["capacity_raw"]
    assert signatory_only.signing_capacities[0]["documents"], "y se dice dónde firma"


def test_unlinked_mentions_are_listed_apart_and_never_counted_as_the_person(
    session: Session, ingest_service
):
    """Una mención con la misma grafía, sin vincular, no es de esta ficha.

    Meterla en el expediente sería vincular por nombre. Omitirla haría pasar
    por completo un expediente que no lo está. Va aparte, dicha como lo que es.
    """
    ingest_service.ingest_fixture("2540861-1", FIXTURES_DIR)
    session.commit()
    linked = (
        session.execute(
            select(m.PersonMention).where(m.PersonMention.canonical_person_id.is_not(None))
        )
        .scalars()
        .first()
    )
    assert linked is not None and linked.canonical_person_id
    session.add(
        m.PersonMention(
            legal_document_id=linked.legal_document_id,
            text_raw=linked.text_raw,
            text_normalized=linked.text_normalized,
            evidence_span_id=linked.evidence_span_id,
            canonical_person_id=None,
            resolution_status=e.ResolutionStatus.CANDIDATE_MATCH,
        )
    )
    session.commit()

    dossier = build_dossier(session, linked.canonical_person_id)
    assert dossier is not None
    assert len(dossier.unlinked_mentions) == 1, "la mención sin vincular debe aparecer, aparte"
    assert dossier.unlinked_mentions[0]["spelling"] == linked.text_normalized
    # Y no se ha colado en lo que el expediente atribuye a la persona.
    alias_counts = {a["spelling"]: a["mentions"] for a in dossier.aliases}
    assert alias_counts[linked.text_normalized] == 1


def test_coverage_declares_documents_that_produced_no_fact(session: Session, ingest_service):
    """Una ficha escueta no distingue sola entre "no consta" y "no lo leímos".

    Es la confusión que dejó invisible durante una edición entera a quien
    renunció a un viceministerio: su documento estaba capturado y el extractor
    no supo leerlo, así que su ficha salía vacía como si nada hubiera pasado.
    """
    ingest_service.ingest_fixture("2540861-1", FIXTURES_DIR)
    session.commit()
    person = _person_named(session, "")
    dossier = build_dossier(session, person.id)
    assert dossier is not None
    assert dossier.coverage["documents"] >= 1
    assert "documents_without_any_event" in dossier.coverage
    assert dossier.coverage["corpus_note"], "la ficha declara el límite del corpus"

    # Un documento que no produjo ningún hecho se declara, no se omite.
    document = session.execute(select(m.LegalDocument)).scalars().first()
    assert document is not None
    for event in session.execute(
        select(m.PersonnelEvent).where(m.PersonnelEvent.legal_document_id == document.id)
    ).scalars():
        for row in session.execute(
            select(m.EventParticipant).where(m.EventParticipant.event_id == event.id)
        ).scalars():
            session.delete(row)
        for row in session.execute(
            select(m.RoleAssignment).where(
                (m.RoleAssignment.start_event_id == event.id)
                | (m.RoleAssignment.end_event_id == event.id)
            )
        ).scalars():
            session.delete(row)
        session.flush()
        session.delete(event)
    session.commit()

    mention = (
        session.execute(
            select(m.PersonMention).where(m.PersonMention.legal_document_id == document.id)
        )
        .scalars()
        .first()
    )
    assert mention is not None and mention.canonical_person_id
    dossier = build_dossier(session, mention.canonical_person_id)
    assert dossier is not None
    silent = [d["document_id"] for d in dossier.coverage["documents_without_any_event"]]
    assert document.id in silent, "el documento leído sin sacar nada tiene que decirse"


def test_a_merged_person_points_at_the_surviving_record(ingested_session: Session):
    """Una ficha absorbida sigue existiendo y lleva a la vigente.

    Los identificadores ya publicados tienen que seguir llevando a algún sitio;
    borrar la ficha vieja rompería enlaces y escondería que hubo una fusión.
    """
    merged = (
        ingested_session.execute(
            select(m.Person).where(m.Person.merged_into_person_id.is_not(None))
        )
        .scalars()
        .first()
    )
    if merged is None:
        return  # el corpus base puede no tener fusiones; no es un fallo
    dossier = build_dossier(ingested_session, merged.id)
    assert dossier is not None
    assert dossier.person.merged_into_person_id == merged.merged_into_person_id
    hits = search_persons(ingested_session, merged.preferred_name)
    assert all(hit.person_id != merged.id for hit in hits), "la búsqueda lleva a la vigente"


# ---------------------------------------------------------------------------
# Actos firmados, períodos y cobertura de personas
# ---------------------------------------------------------------------------


def test_the_signer_of_an_act_sees_what_the_act_resolved(ingested_session: Session):
    """La ficha de quien solo firma no puede reducirse a "aparece en N documentos".

    El sistema sabe exactamente qué resolvió cada documento firmado —a quién se
    designó, a qué cargo, desde cuándo— y todo eso ya está extraído con su
    evidencia. Mostrárselo al lector junto a la firma no infiere nada nuevo.
    """
    signatory_only = None
    for person in ingested_session.execute(select(m.Person)).scalars():
        dossier = build_dossier(ingested_session, person.id)
        assert dossier is not None
        if dossier.signing_capacities and not dossier.appointments:
            signatory_only = dossier
            break
    assert signatory_only is not None
    assert signatory_only.signed_acts, "los documentos firmados resolvieron actos: se muestran"
    for signed in signatory_only.signed_acts:
        assert signed["document"], "cada acto firmado dice en qué documento"
        assert signed["acts"], "solo aparecen documentos con algún hecho extraído"
        for act in signed["acts"]:
            assert act["event_type"]
            assert act["subjects"] or act["assignments"], (
                "un acto sin sujeto ni puesto no dice nada"
            )


def test_a_designation_and_its_later_end_read_as_one_period(session: Session, ingest_service):
    """El alta y la baja del mismo cargo son un período, no dos puestos.

    El persister guarda cada acto en su propia fila; sin el pareo, la ficha de
    alguien designado y luego cesado mostraba dos "puestos" para un solo paso
    por el cargo, con los dos documentos apuntándose mutuamente en silencio.
    """
    from datetime import date as d

    ingest_service.ingest_fixture("2540861-1", FIXTURES_DIR)
    session.commit()
    doc = session.execute(select(m.LegalDocument)).scalars().first()
    span = session.execute(select(m.EvidenceSpan)).scalars().first()
    assert doc is not None and span is not None

    person = m.Person(preferred_name="PERSONA DE PRUEBA PERIODO")
    session.add(person)
    session.flush()

    def _mention() -> m.PersonMention:
        row = m.PersonMention(
            legal_document_id=doc.id,
            text_raw="PERSONA DE PRUEBA PERIODO",
            text_normalized="PERSONA DE PRUEBA PERIODO",
            evidence_span_id=span.id,
            canonical_person_id=person.id,
            resolution_status=e.ResolutionStatus.HUMAN_CONFIRMED,
        )
        session.add(row)
        session.flush()
        return row

    start_event = m.PersonnelEvent(
        legal_document_id=doc.id,
        event_type=e.EventType.DESIGNATION,
        assignment_effect=e.AssignmentEffect.START,
        legal_verb_raw="Designar",
        effective_from=d(2026, 1, 10),
        effective_from_status=e.DateStatus.EXPLICIT,
        evidence_span_id=span.id,
    )
    end_event = m.PersonnelEvent(
        legal_document_id=doc.id,
        event_type=e.EventType.ACCEPT_RESIGNATION,
        assignment_effect=e.AssignmentEffect.END,
        legal_verb_raw="Aceptar la renuncia",
        evidence_span_id=span.id,
    )
    session.add_all([start_event, end_event])
    session.flush()
    session.add_all(
        [
            m.RoleAssignment(
                person_id=person.id,
                person_mention_id=_mention().id,
                position_label_raw="Jefe de la Oficina de Prueba",
                assignment_kind=e.AssignmentKind.TITULAR,
                valid_from=d(2026, 1, 10),
                valid_from_status=e.DateStatus.EXPLICIT,
                start_event_id=start_event.id,
            ),
            m.RoleAssignment(
                person_id=person.id,
                person_mention_id=_mention().id,
                position_label_raw="Jefe de la Oficina de Prueba",
                assignment_kind=e.AssignmentKind.TITULAR,
                valid_to=d(2026, 6, 30),
                valid_to_status=e.DateStatus.EXPLICIT,
                end_event_id=end_event.id,
            ),
        ]
    )
    session.commit()

    dossier = build_dossier(session, person.id)
    assert dossier is not None
    assert len(dossier.appointments) == 1, "un solo período, no dos puestos"
    period = dossier.appointments[0]
    assert period["merged_from_two_acts"]
    assert period["start"] == d(2026, 1, 10)
    assert period["end"] == d(2026, 6, 30)
    assert period["end_event_type"] == str(e.EventType.ACCEPT_RESIGNATION)
    assert period["evidence"] and period["end_evidence"], "cada acto conserva su cita"

    # Y la búsqueda cuenta lo mismo que muestra la ficha.
    hits = search_persons(session, "PERSONA DE PRUEBA PERIODO")
    assert [h.appointments for h in hits if h.person_id == person.id] == [1]


def test_two_open_designations_with_the_same_label_never_merge(session: Session, ingest_service):
    """Dos candidatos posibles no eligen: se muestran las filas tal cual.

    Emparejar por parecido cuando hay ambigüedad sería inventar qué acto cerró
    qué período — la versión organizacional del vínculo por nombre.
    """
    from datetime import date as d

    ingest_service.ingest_fixture("2540861-1", FIXTURES_DIR)
    session.commit()
    doc = session.execute(select(m.LegalDocument)).scalars().first()
    span = session.execute(select(m.EvidenceSpan)).scalars().first()
    person = m.Person(preferred_name="PERSONA AMBIGUA")
    session.add(person)
    session.flush()

    def _mention() -> m.PersonMention:
        row = m.PersonMention(
            legal_document_id=doc.id,
            text_raw="PERSONA AMBIGUA",
            text_normalized="PERSONA AMBIGUA",
            evidence_span_id=span.id,
            canonical_person_id=person.id,
            resolution_status=e.ResolutionStatus.HUMAN_CONFIRMED,
        )
        session.add(row)
        session.flush()
        return row

    def _event(effect: e.AssignmentEffect, event_type: e.EventType) -> m.PersonnelEvent:
        row = m.PersonnelEvent(
            legal_document_id=doc.id,
            event_type=event_type,
            assignment_effect=effect,
            legal_verb_raw="Designar",
            evidence_span_id=span.id,
        )
        session.add(row)
        session.flush()
        return row

    for _ in range(2):
        session.add(
            m.RoleAssignment(
                person_id=person.id,
                person_mention_id=_mention().id,
                position_label_raw="Cargo Repetido de Prueba",
                assignment_kind=e.AssignmentKind.TITULAR,
                valid_from=d(2026, 2, 1),
                valid_from_status=e.DateStatus.EXPLICIT,
                start_event_id=_event(e.AssignmentEffect.START, e.EventType.DESIGNATION).id,
            )
        )
    session.add(
        m.RoleAssignment(
            person_id=person.id,
            person_mention_id=_mention().id,
            position_label_raw="Cargo Repetido de Prueba",
            assignment_kind=e.AssignmentKind.TITULAR,
            valid_to=d(2026, 7, 1),
            valid_to_status=e.DateStatus.EXPLICIT,
            end_event_id=_event(e.AssignmentEffect.END, e.EventType.END_DESIGNATION).id,
        )
    )
    session.commit()

    dossier = build_dossier(session, person.id)
    assert dossier is not None
    assert len(dossier.appointments) == 3, "ambigüedad: nada se empareja"
    assert not any(a["merged_from_two_acts"] for a in dossier.appointments)


def test_appointments_read_in_chronological_order(ingested_session: Session):
    """Lo más reciente primero; lo sin fecha determinada, al final."""
    for person in ingested_session.execute(select(m.Person)).scalars():
        dossier = build_dossier(ingested_session, person.id)
        assert dossier is not None
        dated = [
            a["start"] or a["end"] or (a["document"] or {}).get("published_on")
            for a in dossier.appointments
        ]
        with_dates = [value for value in dated if value is not None]
        assert with_dates == sorted(with_dates, reverse=True)
        if None in dated:
            assert dated.index(None) >= len(with_dates), "lo sin fecha va al final"


def test_persons_without_projected_facts_are_counted(session: Session, ingest_service):
    """La ficha que quedaría vacía se cuenta en la bitácora, no se descubre por azar."""
    from kipu_knowledge.application.person_dossier import persons_without_projected_facts

    ingest_service.ingest_fixture("2540861-1", FIXTURES_DIR)
    session.commit()
    baseline = {row["person_id"] for row in persons_without_projected_facts(session)}

    span = session.execute(select(m.EvidenceSpan)).scalars().first()
    doc = session.execute(select(m.LegalDocument)).scalars().first()
    ghost = m.Person(preferred_name="PERSONA SIN HECHOS")
    session.add(ghost)
    session.flush()
    session.add(
        m.PersonMention(
            legal_document_id=doc.id,
            text_raw="PERSONA SIN HECHOS",
            text_normalized="PERSONA SIN HECHOS",
            evidence_span_id=span.id,
            canonical_person_id=ghost.id,
            resolution_status=e.ResolutionStatus.HUMAN_CONFIRMED,
        )
    )
    session.commit()

    rows = persons_without_projected_facts(session)
    ids = {row["person_id"] for row in rows}
    assert ghost.id in ids, "una mención sin puesto, firma ni participación se cuenta"
    assert ids - baseline == {ghost.id}, "quien tiene algún hecho proyectado no aparece"


def test_derived_issuer_labels_the_signing_capacity(session: Session, ingest_service):
    """ "Firma como Ministra" gana la entidad del documento, dicha como derivada.

    El bloque de firma puede no traer cartera (verificado contra la captura de
    la RM 000399-2026-MIMP: dice solo "Ministra"). La entidad sale del emisor
    que el índice declaró para el documento firmado, nunca del bloque de firma,
    y la ficha lo rotula con esa procedencia.
    """
    ingest_service.ingest_fixture("2540861-1", FIXTURES_DIR)
    session.commit()
    signatory = session.execute(select(m.Signatory)).scalars().first()
    assert signatory is not None
    doc = session.get(m.LegalDocument, signatory.legal_document_id)

    span = session.execute(select(m.EvidenceSpan)).scalars().first()
    issuer_mention = m.OrganizationMention(
        legal_document_id=doc.id,
        text_raw="DESARROLLO AGRARIO Y RIEGO",
        text_normalized="DESARROLLO AGRARIO Y RIEGO",
        resolution_status=e.ResolutionStatus.UNRESOLVED,
        evidence_span_id=span.id,
    )
    session.add(issuer_mention)
    session.flush()
    doc.issuer_mention_id = issuer_mention.id
    session.commit()

    mention = session.get(m.PersonMention, signatory.person_mention_id)
    assert mention is not None and mention.canonical_person_id
    dossier = build_dossier(session, mention.canonical_person_id)
    assert dossier is not None
    assert dossier.signing_capacities, "quien firma tiene capacidad declarada"
    capacity = dossier.signing_capacities[0]
    assert capacity["issuers"] == ["DESARROLLO AGRARIO Y RIEGO"]
