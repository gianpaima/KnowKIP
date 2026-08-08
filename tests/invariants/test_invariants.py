"""Pruebas de invariantes de dominio (reglas no negociables, sección 8)."""

from __future__ import annotations

import hashlib
from datetime import date

import pytest
from sqlalchemy import func, select

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.adapters.resolution.resolver import SimpleEntityResolver
from kipu_knowledge.adapters.storage.fs_store import ImmutabilityViolation
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain.extraction_models import ExtractedDate
from kipu_knowledge.domain.offices import singular_office


def test_no_accepted_assertion_without_evidence(ingested_session):
    # Regla 9 (reforzada a: ninguna afirmación sin evidencia).
    count = ingested_session.execute(
        select(func.count()).select_from(m.Assertion).where(m.Assertion.evidence_span_id.is_(None))
    ).scalar()
    assert count == 0


def test_no_artifact_version_without_sha256(ingested_session):
    # Regla 10.
    rows = ingested_session.execute(select(m.ArtifactVersion)).scalars().all()
    assert rows
    for row in rows:
        assert row.sha256 and len(row.sha256) == 64


def test_stored_bytes_never_overwritten(store):
    # Regla 11: reponer el mismo contenido es no-op; contenido distinto = objeto distinto.
    a = store.put_immutable(b"contenido original")
    again = store.put_immutable(b"contenido original")
    assert again.already_existed and again.sha256 == a.sha256
    b = store.put_immutable(b"contenido distinto")
    assert b.sha256 != a.sha256
    assert store.get(a.object_key) == b"contenido original"

    # Corrupción simulada -> violación explícita
    victim = store._path_for(a.sha256)
    victim.write_bytes(b"bytes adulterados")
    with pytest.raises(ImmutabilityViolation):
        store.put_immutable(b"contenido original")


def test_evidence_spans_stay_anchored_to_their_section(ingested_session):
    """La evidencia debe seguir siendo localizable después de persistida.

    Dos verificaciones: el sha256 registrado corresponde a la cita, y el rango
    declarado (char_start/char_end) sigue recortando exactamente esa cita del
    texto original de la sección. Si un re-parseo o una migración desplazara el
    texto, este invariante lo detecta antes de que la UI resalte un fragmento
    equivocado.
    """
    spans = ingested_session.execute(select(m.EvidenceSpan)).scalars().all()
    assert spans, "el corpus ingerido debe producir evidencia"
    for span in spans:
        digest = hashlib.sha256(span.quoted_text.encode("utf-8")).hexdigest()
        assert digest == span.quoted_text_sha256, (
            f"span {span.id}: sha256 registrado no corresponde a quoted_text"
        )
        if span.document_section_id is None or span.char_start is None or span.char_end is None:
            continue
        section = ingested_session.get(m.DocumentSection, span.document_section_id)
        assert section is not None, f"span {span.id}: sección {span.document_section_id} ausente"
        assert section.text_raw[span.char_start : span.char_end] == span.quoted_text, (
            f"span {span.id}: el rango {span.char_start}–{span.char_end} ya no "
            "recorta la cita registrada dentro de su sección"
        )


def test_no_effective_date_invented(ingested_session):
    # Regla 12: ningún evento START tiene effective_from == published_on salvo
    # que el texto lo declare (status EXPLICIT con frase fuente).
    events = (
        ingested_session.execute(
            select(m.PersonnelEvent).where(
                m.PersonnelEvent.assignment_effect == e.AssignmentEffect.START
            )
        )
        .scalars()
        .all()
    )
    assert events
    for event in events:
        if event.effective_from is None:
            assert event.effective_from_status == e.DateStatus.NOT_STATED
        else:
            assert event.effective_from_status == e.DateStatus.EXPLICIT


def test_legal_effect_never_replaces_a_stated_date(ingested_session):
    """La determinación por norma vive aparte, nunca encima de lo que la fuente dice.

    Si `legal_effect_from` pudiera coexistir con una fecha expresada, el sistema
    estaría eligiendo entre dos fechas sin que nadie lo hubiera decidido.
    """
    for event in ingested_session.execute(
        select(m.PersonnelEvent).where(m.PersonnelEvent.legal_effect_from.isnot(None))
    ).scalars():
        assert event.effective_from is None
        assert event.effective_from_status == e.DateStatus.NOT_STATED
    for ra in ingested_session.execute(
        select(m.RoleAssignment).where(m.RoleAssignment.legal_effect_from.isnot(None))
    ).scalars():
        assert ra.valid_from is None and ra.valid_from_status == e.DateStatus.NOT_STATED
    for ra in ingested_session.execute(
        select(m.RoleAssignment).where(m.RoleAssignment.legal_effect_to.isnot(None))
    ).scalars():
        assert ra.valid_to is None and ra.valid_to_status == e.DateStatus.NOT_STATED


def test_every_determined_date_cites_its_norm_and_its_evidence(ingested_session):
    """Regla 2 aplicada a la quinta señal: sin norma citada y sin cita de la
    fecha de publicación, una fecha determinada sería una fecha inventada."""
    from kipu_knowledge.application.legal_effect import PREDICATE, SPAN_KIND

    events = (
        ingested_session.execute(
            select(m.PersonnelEvent).where(m.PersonnelEvent.legal_effect_from.isnot(None))
        )
        .scalars()
        .all()
    )
    assert events, "el corpus debe producir al menos una fecha determinada por norma"
    for event in events:
        basis = (event.legal_effect_basis_json or {}).get("basis") or {}
        assert basis.get("norm") and basis.get("article"), event.id
        assertion = ingested_session.execute(
            select(m.Assertion).where(
                m.Assertion.subject_type == "personnel_event",
                m.Assertion.subject_id == event.id,
                m.Assertion.predicate == PREDICATE,
            )
        ).scalar_one()
        span = ingested_session.get(m.EvidenceSpan, assertion.evidence_span_id)
        assert span is not None and (span.locator_json or {}).get("kind") == SPAN_KIND


def test_legal_effect_dates_are_re_verifiable(ingested_session):
    """La regla vuelve a ejecutarse sobre los datos congelados y da lo mismo.

    Es lo que separa una determinación normativa de una fecha puesta a mano: un
    tercero puede repetirla desde la misma evidencia.
    """
    from kipu_knowledge.application.legal_effect import verdict_for_event

    for event in ingested_session.execute(select(m.PersonnelEvent)).scalars():
        verdict = verdict_for_event(ingested_session, event)
        assert event.legal_effect_from == (verdict.value if verdict.determined else None), (
            f"la fecha guardada del evento {event.id} ya no es la que la regla produce"
        )


def test_publication_date_spans_still_cut_their_phrase_in_the_capture(ingested_session, store):
    """El span de la fecha de publicación no cuelga de ninguna sección: su rango
    es sobre el texto del artefacto, así que se verifica contra el CAS."""
    from kipu_knowledge.application.legal_effect import SPAN_KIND

    checked = 0
    for span in ingested_session.execute(
        select(m.EvidenceSpan).where(m.EvidenceSpan.document_section_id.is_(None))
    ).scalars():
        if (span.locator_json or {}).get("kind") != SPAN_KIND:
            continue
        version = ingested_session.get(m.ArtifactVersion, span.artifact_version_id)
        page = store.get(version.object_key).decode("utf-8", errors="replace")
        assert page[span.char_start : span.char_end] == span.quoted_text, span.id
        checked += 1
    assert checked, "el corpus debe registrar la cita de la fecha de publicación"


def test_extracted_date_model_rejects_unfounded_values():
    with pytest.raises(ValueError):
        ExtractedDate(value=date(2026, 8, 6), status=e.DateStatus.NOT_STATED)
    with pytest.raises(ValueError):
        ExtractedDate(value=date(2026, 8, 6), status=e.DateStatus.EXPLICIT, source_phrase=None)


def _repeated_names(session) -> list[str]:
    return list(
        session.execute(
            select(m.PersonMention.text_normalized)
            .group_by(m.PersonMention.text_normalized)
            .having(func.count() > 1)
        )
        .scalars()
        .all()
    )


def test_no_person_merge_by_name(ingested_session):
    """Regla 13: ninguna fusión puede sostenerse solo en el nombre.

    Vincular dos menciones del mismo nombre a la misma persona exige una razón
    registrada e independiente del nombre: un identificador declarado por la
    fuente, una decisión humana, o un oficio del que hay un solo titular. La
    primera mención de un nombre no es una fusión —crea la persona— y por eso
    AUTO_LINKED solo vale para ella.
    """
    justified = {
        e.ResolutionStatus.IDENTIFIER_LINKED,
        e.ResolutionStatus.PRECEDENT_LINKED,
        e.ResolutionStatus.OFFICE_CORROBORATED,
        e.ResolutionStatus.HUMAN_CONFIRMED,
    }
    repeated = _repeated_names(ingested_session)
    assert repeated, "el corpus debe contener nombres repetidos (firmantes)"
    for normalized in repeated:
        mentions = (
            ingested_session.execute(
                select(m.PersonMention)
                .where(m.PersonMention.text_normalized == normalized)
                .order_by(m.PersonMention.id)
            )
            .scalars()
            .all()
        )
        linked = [x for x in mentions if x.canonical_person_id is not None]
        first_links = [x for x in linked if x.resolution_status == e.ResolutionStatus.AUTO_LINKED]
        assert len(first_links) <= 1, (
            f"'{normalized}': más de una mención vinculada sin corroboración; "
            "AUTO_LINKED solo puede crear la persona, nunca fusionar"
        )
        for mention in linked:
            if mention in first_links:
                continue
            assert mention.resolution_status in justified, (
                f"'{normalized}': mención {mention.id} vinculada con estado "
                f"{mention.resolution_status} sin señal corroborante"
            )


def test_office_corroboration_requires_a_singular_office(ingested_session):
    """OFFICE_CORROBORATED exige un cargo unipersonal, no cualquier cargo.

    Un cargo genérico ("Jefe Institucional") lo ostentan personas distintas en
    organismos distintos, así que no descarta la homonimia.
    """
    mentions = (
        ingested_session.execute(
            select(m.PersonMention).where(
                m.PersonMention.resolution_status == e.ResolutionStatus.OFFICE_CORROBORATED
            )
        )
        .scalars()
        .all()
    )
    assert mentions, "el corpus debe ejercitar la corroboración por oficio (firma presidencial)"
    for mention in mentions:
        assert singular_office(mention.role_context_normalized) is not None, (
            f"mención {mention.id} corroborada con un cargo no unipersonal: "
            f"{mention.role_context_normalized!r}"
        )


def test_recital_corroborated_links_are_re_verifiable(ingested_session):
    """La cuarta señal debe poder re-verificarse desde los datos almacenados.

    Para cada participante corroborado por recital, re-derivar los candidatos
    de las secciones congeladas y re-ejecutar la regla debe dar el mismo
    veredicto y la misma persona. Si el texto o la regla derivan, este
    invariante lo detecta antes de que un vínculo quede sin sustento.
    """
    from kipu_knowledge.application.corroboration import (
        RecitalOutcome,
        bare_document_number,
        corroborate_recital,
        recital_candidates_from_sections,
    )
    from kipu_knowledge.domain.normalization import normalize_person_name

    participants = (
        ingested_session.execute(
            select(m.EventParticipant).where(
                m.EventParticipant.role_in_event
                == e.ParticipantRole.AFFECTED_PERSON_RECITAL_CORROBORATED
            )
        )
        .scalars()
        .all()
    )
    assert participants, "el corpus debe ejercitar la corroboración por recital (caso D)"
    for participant in participants:
        event = ingested_session.get(m.PersonnelEvent, participant.event_id)
        ended = (
            ingested_session.execute(
                select(m.RoleAssignment).where(m.RoleAssignment.end_event_id == event.id)
            )
            .scalars()
            .all()
        )
        assert len(ended) == 1, f"evento {event.id}: corroborado sin asignación concluida única"
        candidates = recital_candidates_from_sections(ingested_session, event.legal_document_id)
        article_numbers = {
            number
            for row in ingested_session.execute(
                select(m.DocumentReference).where(
                    m.DocumentReference.source_document_id == event.legal_document_id,
                    m.DocumentReference.reference_type == e.ReferenceType.PRIOR_APPOINTMENT,
                )
            ).scalars()
            if (number := bare_document_number(row.target_number_raw))
        }
        verdict = corroborate_recital(candidates, ended[0].position_label_raw, article_numbers)
        assert verdict.outcome == RecitalOutcome.CORROBORATED, (
            f"evento {event.id}: el vínculo corroborado ya no se sostiene ({verdict.rationale})"
        )
        mention = ingested_session.get(m.PersonMention, participant.person_mention_id)
        assert normalize_person_name(verdict.candidate.name) == normalize_person_name(
            mention.text_raw
        ), f"evento {event.id}: la re-verificación señala a otra persona"


def test_identifier_linked_requires_a_declared_identifier(ingested_session):
    """IDENTIFIER_LINKED exige un PersonIdentifier con evidencia; nunca se infiere."""
    for mention in (
        ingested_session.execute(
            select(m.PersonMention).where(
                m.PersonMention.resolution_status == e.ResolutionStatus.IDENTIFIER_LINKED
            )
        )
        .scalars()
        .all()
    ):
        identifiers = (
            ingested_session.execute(
                select(m.PersonIdentifier).where(m.PersonIdentifier.person_mention_id == mention.id)
            )
            .scalars()
            .all()
        )
        assert identifiers, f"mención {mention.id} sin identificador declarado"
        for identifier in identifiers:
            assert ingested_session.get(m.EvidenceSpan, identifier.evidence_span_id) is not None


def test_sunat_produces_two_events(ingested_session):
    # Caso C / criterio 7.
    doc = _doc_by_code(ingested_session, "2540905-3")
    events = (
        ingested_session.execute(
            select(m.PersonnelEvent).where(m.PersonnelEvent.legal_document_id == doc.id)
        )
        .scalars()
        .all()
    )
    assert len(events) == 2
    assert {ev.event_type for ev in events} == {
        e.EventType.ACCEPT_RESIGNATION,
        e.EventType.DESIGNATION,
    }


def test_bcrp_three_assignments_with_mandate(ingested_session):
    # Caso G / criterio 11.
    doc = _doc_by_code(ingested_session, "2540905-2")
    event = ingested_session.execute(
        select(m.PersonnelEvent).where(m.PersonnelEvent.legal_document_id == doc.id)
    ).scalar_one()
    assignments = (
        ingested_session.execute(
            select(m.RoleAssignment).where(m.RoleAssignment.start_event_id == event.id)
        )
        .scalars()
        .all()
    )
    assert len(assignments) == 3
    for ra in assignments:
        assert ra.assignment_kind == e.AssignmentKind.BOARD_MEMBERSHIP
        assert ra.mandate_id is not None
    mandate = ingested_session.get(m.Mandate, assignments[0].mandate_id)
    assert mandate.mandate_type == e.MandateType.CONSTITUTIONAL_PERIOD


def test_inbp_conditional_end(ingested_session):
    # Caso F / criterio 10: eficacia anticipada + condición textual de término.
    doc = _doc_by_code(ingested_session, "2540702-1")
    event = ingested_session.execute(
        select(m.PersonnelEvent).where(m.PersonnelEvent.legal_document_id == doc.id)
    ).scalar_one()
    assert event.event_type == e.EventType.ADDITIONAL_RESPONSIBILITY
    assert event.effective_from == date(2026, 7, 30)
    assert event.effective_from_status == e.DateStatus.EXPLICIT
    assert event.end_condition_text and "hasta el retorno" in event.end_condition_text
    ra = ingested_session.execute(
        select(m.RoleAssignment).where(m.RoleAssignment.start_event_id == event.id)
    ).scalar_one()
    assert ra.assignment_kind == e.AssignmentKind.ADDITIONAL_RESPONSIBILITY
    assert ra.end_condition_text and ra.valid_to is None


def test_bnp_cap_slot(ingested_session):
    # Caso E / criterio 9: fecha explícita + correlativo CAP 007.
    doc = _doc_by_code(ingested_session, "2540779-1")
    event = ingested_session.execute(
        select(m.PersonnelEvent).where(m.PersonnelEvent.legal_document_id == doc.id)
    ).scalar_one()
    assert event.effective_from == date(2026, 8, 6)
    assert event.effective_from_status == e.DateStatus.EXPLICIT
    ra = ingested_session.execute(
        select(m.RoleAssignment).where(m.RoleAssignment.start_event_id == event.id)
    ).scalar_one()
    slot = ingested_session.execute(
        select(m.PositionSlot).where(m.PositionSlot.position_id == ra.position_id)
    ).scalar_one()
    assert slot.external_scheme == "CAP_PROVISIONAL"
    assert slot.external_code == "007"


def test_tribunal_fiscal_distinguishes_encargo_from_titular(ingested_session):
    # Caso D / criterio 8.
    doc = _doc_by_code(ingested_session, "2540905-4")
    events = (
        ingested_session.execute(
            select(m.PersonnelEvent).where(m.PersonnelEvent.legal_document_id == doc.id)
        )
        .scalars()
        .all()
    )
    by_type = {ev.event_type: ev for ev in events}
    assert e.EventType.END_ACTING_ASSIGNMENT in by_type
    assert e.EventType.APPOINTMENT in by_type
    end_ra = ingested_session.execute(
        select(m.RoleAssignment).where(
            m.RoleAssignment.end_event_id == by_type[e.EventType.END_ACTING_ASSIGNMENT].id
        )
    ).scalar_one()
    start_ra = ingested_session.execute(
        select(m.RoleAssignment).where(
            m.RoleAssignment.start_event_id == by_type[e.EventType.APPOINTMENT].id
        )
    ).scalar_one()
    assert end_ra.assignment_kind == e.AssignmentKind.ACTING
    assert start_ra.assignment_kind == e.AssignmentKind.TITULAR


def test_ending_encargo_does_not_end_other_assignments(ingested_session):
    # Regla 18: terminar el encargo del Tribunal Fiscal no altera ninguna otra
    # asignación de la misma persona (su puesto base, si existiera, sobrevive).
    doc = _doc_by_code(ingested_session, "2540905-4")
    end_event = ingested_session.execute(
        select(m.PersonnelEvent).where(
            m.PersonnelEvent.legal_document_id == doc.id,
            m.PersonnelEvent.event_type == e.EventType.END_ACTING_ASSIGNMENT,
        )
    ).scalar_one()
    touched = (
        ingested_session.execute(
            select(m.RoleAssignment).where(m.RoleAssignment.end_event_id == end_event.id)
        )
        .scalars()
        .all()
    )
    assert len(touched) == 1  # solo la asignación del encargo, nada más
    all_with_end = ingested_session.execute(
        select(func.count())
        .select_from(m.RoleAssignment)
        .where(m.RoleAssignment.valid_to.isnot(None))
    ).scalar()
    # Solo la renuncia CENEPRED (B1) tiene fin con fecha en el corpus.
    assert all_with_end == 1


def test_produce_distinguishes_designation_from_obligation(ingested_session):
    # Caso H / criterio 12: un evento, y el artículo 2 clasificado como obligación.
    doc = _doc_by_code(ingested_session, "2540896-1")
    events = (
        ingested_session.execute(
            select(m.PersonnelEvent).where(m.PersonnelEvent.legal_document_id == doc.id)
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    classifications = (
        ingested_session.execute(
            select(m.Assertion).where(m.Assertion.predicate == "article_classification")
        )
        .scalars()
        .all()
    )
    doc_classes = {
        a.object_value_json["article_label"]: a.object_value_json["article_class"]
        for a in classifications
        if a.subject_id == doc.id
    }
    assert doc_classes["Artículo 1.-"] == "PERSONNEL_EVENT"
    assert doc_classes["Artículo 2.-"] == "DERIVED_OBLIGATION"


def _doc_by_code(session, code: str) -> m.LegalDocument:
    return session.execute(
        select(m.LegalDocument)
        .join(m.PublicationItem, m.PublicationItem.id == m.LegalDocument.publication_item_id)
        .where(m.PublicationItem.publication_code == code)
    ).scalar_one()


def test_declared_identifiers_never_reach_the_rdf_projection(ingested_session):
    """Un DNI es dato personal: sirve para resolver identidad, no para publicar.

    kipu:Person se limita a información funcional pública (ontology/people.ttl),
    así que el valor declarado no puede aparecer en ninguna exportación.
    """
    from kipu_knowledge.adapters.rdf.projection import RdfProjection

    doc = _doc_by_code(ingested_session, "2540861-1")
    mention = (
        ingested_session.execute(
            select(m.PersonMention).where(m.PersonMention.legal_document_id == doc.id)
        )
        .scalars()
        .first()
    )
    secret = "09342789"
    ingested_session.add(
        m.PersonIdentifier(
            person_mention_id=mention.id,
            scheme=e.IdentifierScheme.DNI,
            value_raw=secret,
            value_normalized=secret,
            evidence_span_id=mention.evidence_span_id,
        )
    )
    ingested_session.flush()

    trig = RdfProjection(ingested_session).export_trig("2540861-1")
    assert secret not in trig, "el identificador declarado se filtró a la proyección RDF"


def _ambiguous_alias_precedents(session) -> list[tuple[str, int]]:
    """Alias vigentes (sin cargo) sobre grafías que designan a más de una persona.

    Un alias así vincularía homónimos futuros sin abrir tarea. El servicio de
    revisión lo impide al crearlo, pero la ambigüedad puede aparecer después, con
    una persona nueva que estrena la misma grafía.
    """
    resolver = SimpleEntityResolver(session)
    rows = (
        session.execute(
            select(m.IdentityPrecedent).where(
                m.IdentityPrecedent.subject_type == "person",
                m.IdentityPrecedent.role_context.is_(None),
                m.IdentityPrecedent.revoked_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    offending = []
    for precedent in rows:
        count = resolver.distinct_persons_for_name(precedent.name_normalized)
        if count > 1:
            offending.append((precedent.name_normalized, count))
    return offending


def test_no_alias_precedent_over_a_non_discriminant_name(ingested_session):
    assert _ambiguous_alias_precedents(ingested_session) == []


def test_the_ambiguous_alias_check_actually_detects(ingested_session):
    """El invariante anterior es vacío sobre el corpus limpio; esto prueba que
    detecta la violación cuando existe, en vez de pasar siempre."""
    mention = ingested_session.execute(select(m.PersonMention)).scalars().first()
    twins = [m.Person(preferred_name="NOMBRE REPETIDO") for _ in range(2)]
    ingested_session.add_all(twins)
    ingested_session.flush()
    for person in twins:
        ingested_session.add(
            m.PersonMention(
                legal_document_id=mention.legal_document_id,
                evidence_span_id=mention.evidence_span_id,
                text_raw="NOMBRE REPETIDO",
                text_normalized="NOMBRE REPETIDO",
                canonical_person_id=person.id,
                resolution_status=e.ResolutionStatus.AUTO_LINKED,
            )
        )
    decision = ingested_session.execute(select(m.ReviewDecision)).scalars().first()
    if decision is None:
        task = m.ReviewTask(
            task_type=e.ReviewTaskType.ENTITY_RESOLUTION,
            target_type="person_mention",
            target_id=mention.id,
            reason="siembra de prueba",
        )
        ingested_session.add(task)
        ingested_session.flush()
        decision = m.ReviewDecision(
            review_task_id=task.id, action=e.DecisionAction.LINK_ENTITY, reviewer="r"
        )
        ingested_session.add(decision)
    ingested_session.flush()
    ingested_session.add(
        m.IdentityPrecedent(
            name_normalized="NOMBRE REPETIDO",
            role_context=None,
            person_id=twins[0].id,
            source_person_mention_id=mention.id,
            review_decision_id=decision.id,
        )
    )
    ingested_session.flush()
    assert _ambiguous_alias_precedents(ingested_session) == [("NOMBRE REPETIDO", 2)]


def test_pdf_url_is_anchored_to_its_own_device(ingested_session):
    """El PDF de una publicación apunta a su dispositivo, no a un vecino.

    Una captura puede traer varios dispositivos, así que el anclaje al código es
    lo único que impide enlazar el documento equivocado. Y tiene que ser la URL
    del **archivo**, la que declara el payload: la ruta derivable
    `…/<código>/pdf` devuelve el visor HTML, no el documento (comprobado contra
    la fuente el 2026-08-07).
    """
    items = ingested_session.execute(select(m.PublicationItem)).scalars().all()
    assert items
    for item in items:
        assert item.pdf_url, f"{item.publication_code}: la publicación debe tener PDF registrado"
        assert item.pdf_url.endswith(f"/{item.publication_code}.PDF"), (
            f"{item.publication_code}: se esperaba la URL del archivo declarada por la "
            f"captura, no {item.pdf_url}"
        )


def test_every_publication_declares_who_published_it(ingested_session):
    """Sin sistema fuente no hay forma de saber qué peso jurídico tiene el texto."""
    items = ingested_session.execute(select(m.PublicationItem)).scalars().all()
    assert items
    for item in items:
        assert item.source_system_id, f"{item.publication_code}: publicación sin sistema fuente"
        source = ingested_session.get(m.SourceSystem, item.source_system_id)
        assert source.authority == e.SourceAuthority.OFFICIAL_GAZETTE, (
            f"{item.publication_code}: el corpus solo debería venir del diario oficial"
        )


def test_one_document_per_act_with_exactly_one_authoritative_source(ingested_session):
    """Dos publicaciones del mismo acto son un documento, no dos.

    Si el mismo acto entrara dos veces como dos `LegalDocument`, el grafo
    afirmaría dos designaciones donde hubo una. Y de las publicaciones de un
    documento, exactamente una puede ser la autoritativa: es de la que se extrae.
    """
    numbers = ingested_session.execute(
        select(m.LegalDocument.number_normalized, func.count())
        .group_by(m.LegalDocument.number_normalized)
        .having(func.count() > 1)
    ).all()
    assert not numbers, f"el mismo número de documento aparece más de una vez: {numbers}"

    documents = ingested_session.execute(select(m.LegalDocument)).scalars().all()
    assert documents
    for doc in documents:
        roles = (
            ingested_session.execute(
                select(m.DocumentSource.role).where(m.DocumentSource.legal_document_id == doc.id)
            )
            .scalars()
            .all()
        )
        authoritative = [r for r in roles if r == e.DocumentSourceRole.AUTHORITATIVE]
        assert len(authoritative) == 1, (
            f"{doc.number_normalized}: se esperaba exactamente una fuente autoritativa, "
            f"hay {len(authoritative)}"
        )


def test_version_chains_never_cross_representations(ingested_session):
    """`previous_version_id` es el histórico de UNA representación.

    Encadenar la primera captura de un PDF con la última del HTML mezcla dos
    series y vuelve ilegible el "¿qué cambió respecto de la vez anterior?".
    """
    versions = ingested_session.execute(select(m.ArtifactVersion)).scalars().all()
    assert versions
    for version in versions:
        if version.previous_version_id is None:
            continue
        previous = ingested_session.get(m.ArtifactVersion, version.previous_version_id)
        assert previous is not None and previous.artifact_id == version.artifact_id, (
            f"la versión {version.id} encadena con otra de un artefacto distinto"
        )


def test_every_discovered_device_leaves_a_record(session, store, daily_kit):
    """El descubrimiento automático no puede tener zonas mudas.

    Todo dispositivo que el índice declara queda en `crawl_item`, se ingiera o
    no, con su veredicto y su regla. Si el filtro pudiera descartar sin escribir,
    "se ingirieron N normas" sería indistinguible de "ese día se publicaron N".
    """
    from kipu_knowledge.application.daily_ingest import DailyIngestService

    catalogue = daily_kit.catalogue
    adapter = daily_kit.adapter([daily_kit.listing(catalogue, total=len(catalogue))])
    result = DailyIngestService(session, store, adapter=adapter).run(
        daily_kit.run_date, capture_pdf=False
    )
    rows = session.execute(select(m.CrawlItem)).scalars().all()
    assert len(rows) == result.total_declared == len(catalogue)
    for row in rows:
        assert row.relevance_rule, f"{row.publication_code} no dice con qué regla se decidió"
        assert row.relevance_rationale
        assert row.status is not e.CrawlItemStatus.DISCOVERED, (
            f"{row.publication_code} quedó sin resolver y sin motivo registrado"
        )
