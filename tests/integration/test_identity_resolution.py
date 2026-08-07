"""Resolución de identidad: cuándo el sistema puede vincular sin preguntar.

La regla 13 prohíbe fusionar menciones *solo por el nombre*, no fusionar. Estas
pruebas cubren las tres señales que sí autorizan vincular —identificador
declarado por la fuente, decisión humana previa, oficio unipersonal— y los casos
en que ninguna aplica y el caso debe llegar a un revisor.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.adapters.resolution.resolver import SimpleEntityResolver
from kipu_knowledge.application.review import ReviewError, ReviewService
from kipu_knowledge.domain import enums as e

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"

KEIKO = "KEIKO SOFIA FUJIMORI HIGUCHI"
PRESIDENCIA = "PRESIDENTA DE LA REPUBLICA"
# Resoluciones Supremas firmadas por la misma Presidenta: 053 y 054-2026-DE
# (Defensa) y la del Directorio del BCRP.
KEIKO_DOCS = ["2540903-1", "2540903-2", "2540905-2"]

# Firmante cuyo cargo NO es unipersonal: cualquier organismo tiene un jefe
# institucional, así que la corroboración por oficio no aplica y el caso depende
# de la decisión humana.
YANGALI = "JUAN YANGALI QUINTANILLA"
YANGALI_ROLE = "JEFE INSTITUCIONAL"
YANGALI_DOC = "2540779-1"


def _mentions(session, normalized: str) -> list[m.PersonMention]:
    return list(
        session.execute(
            select(m.PersonMention).where(m.PersonMention.text_normalized == normalized)
        )
        .scalars()
        .all()
    )


def _tasks_for(session, mention_id: str) -> list[m.ReviewTask]:
    return list(
        session.execute(select(m.ReviewTask).where(m.ReviewTask.target_id == mention_id))
        .scalars()
        .all()
    )


@pytest.fixture
def ingest_codes(ingest_service, session):
    def _run(*codes: str) -> None:
        for code in codes:
            ingest_service.ingest_fixture(code, FIXTURES_DIR)
        session.flush()

    return _run


@pytest.fixture
def anchor(ingest_codes, session):
    """Documento y evidencia reales a los que colgar menciones de prueba.

    `legal_document_id` y `evidence_span_id` no admiten null: toda mención cuelga
    de evidencia (regla 2). Las menciones sintéticas de estas pruebas reutilizan
    las de un documento realmente ingerido en vez de inventar la cadena.
    """
    ingest_codes("2540861-1")
    mention = session.execute(select(m.PersonMention)).scalars().first()
    return mention.legal_document_id, mention.evidence_span_id


def _seed_mention(session, anchor, *, name: str, role: str | None, person_id: str | None):
    document_id, evidence_id = anchor
    mention = m.PersonMention(
        legal_document_id=document_id,
        evidence_span_id=evidence_id,
        text_raw=name,
        text_normalized=name,
        role_context_normalized=role,
        canonical_person_id=person_id,
        resolution_status=e.ResolutionStatus.AUTO_LINKED,
    )
    session.add(mention)
    session.flush()
    return mention


def _decide_link(session, mention, person_id: str, **payload):
    task = m.ReviewTask(
        task_type=e.ReviewTaskType.ENTITY_RESOLUTION,
        target_type="person_mention",
        target_id=mention.id,
        reason="homónimo detectado (siembra de prueba)",
    )
    session.add(task)
    session.flush()
    return ReviewService(session).decide(
        task.id,
        e.DecisionAction.LINK_ENTITY,
        reviewer=payload.pop("reviewer", "revisor@example.org"),
        payload={"entity_id": person_id, **payload},
    )


class TestOfficeCorroboration:
    """Nombre idéntico + oficio del que hay un solo titular a la vez."""

    def test_signatory_mention_records_role_context(self, ingest_codes, session):
        ingest_codes(KEIKO_DOCS[0])
        mention = _mentions(session, KEIKO)[0]
        assert mention.role_context_raw == "Presidenta de la República"
        assert mention.role_context_normalized == PRESIDENCIA

    def test_recurring_signatory_links_without_asking(self, ingest_codes, session):
        ingest_codes(*KEIKO_DOCS)
        mentions = _mentions(session, KEIKO)
        assert [x.resolution_status for x in mentions] == [
            e.ResolutionStatus.AUTO_LINKED,  # la primera crea la persona
            e.ResolutionStatus.OFFICE_CORROBORATED,
            e.ResolutionStatus.OFFICE_CORROBORATED,
        ]
        assert len({x.canonical_person_id for x in mentions}) == 1
        for mention in mentions:
            assert not _tasks_for(session, mention.id)

    def test_corroboration_is_recorded_as_evidence(self, ingest_codes, session):
        ingest_codes(*KEIKO_DOCS[:2])
        second = _mentions(session, KEIKO)[1]
        assertion = session.execute(
            select(m.Assertion).where(
                m.Assertion.subject_id == second.id,
                m.Assertion.predicate == "mention_resolves_to",
            )
        ).scalar_one()
        assert assertion.object_value_json["resolution"] == "OFFICE_CORROBORATED"
        assert PRESIDENCIA in assertion.object_value_json["rationale"]

    def test_gender_variant_of_the_same_office_corroborates(self, session, anchor):
        """ "Ministro" y "Ministra" son el mismo oficio; la normalización conserva
        el género a propósito, así que el catálogo debe tolerarlo."""
        person = m.Person(preferred_name="ANA PEREZ SILVA")
        session.add(person)
        session.flush()
        _seed_mention(
            session,
            anchor,
            name="ANA PEREZ SILVA",
            role="MINISTRA DE EDUCACION",
            person_id=person.id,
        )
        found = SimpleEntityResolver(session).office_corroborated_persons(
            "ANA PEREZ SILVA", "MINISTRO DE EDUCACION"
        )
        assert [x.id for x in found] == [person.id]

    def test_generic_role_never_corroborates(self, ingest_codes, session):
        """Un cargo genérico no descarta la homonimia: lo tiene cualquier organismo."""
        ingest_codes(YANGALI_DOC)
        resolver = SimpleEntityResolver(session)
        assert resolver.office_corroborated_persons(YANGALI, YANGALI_ROLE) == []

    def test_mention_without_role_never_corroborates(self, ingest_codes, session):
        ingest_codes(KEIKO_DOCS[0])
        resolver = SimpleEntityResolver(session)
        assert resolver.office_corroborated_persons(KEIKO, None) == []


class TestIdentityPrecedent:
    """Decisión humana reutilizada donde la corroboración automática no llega."""

    def _seed_precedent(self, session, anchor, person_id: str):
        """Registra una decisión humana previa sobre (nombre, cargo genérico)."""
        seed = _seed_mention(session, anchor, name=YANGALI, role=YANGALI_ROLE, person_id=person_id)
        return seed, _decide_link(session, seed, person_id)

    def test_precedent_resolves_a_role_that_office_corroboration_cannot(
        self, ingest_codes, session, anchor
    ):
        person = m.Person(preferred_name="JUAN YANGALI QUINTANILLA")
        session.add(person)
        session.flush()
        seed, decision = self._seed_precedent(session, anchor, person.id)

        ingest_codes(YANGALI_DOC)
        ingested = [x for x in _mentions(session, YANGALI) if x.id != seed.id]
        assert len(ingested) == 1
        mention = ingested[0]
        assert mention.resolution_status == e.ResolutionStatus.PRECEDENT_LINKED
        assert mention.canonical_person_id == person.id
        assert not _tasks_for(session, mention.id)

        precedent = session.get(m.IdentityPrecedent, mention.identity_precedent_id)
        assert precedent.review_decision_id == decision.id
        assert precedent.reviewer == "revisor@example.org"

        assertion = session.execute(
            select(m.Assertion).where(
                m.Assertion.subject_id == mention.id,
                m.Assertion.predicate == "mention_resolves_to",
            )
        ).scalar_one()
        assert assertion.review_status == e.ReviewStatus.HUMAN_ACCEPTED
        assert assertion.object_value_json["review_decision_id"] == decision.id

    def test_no_precedent_without_role_context(self, session, anchor):
        """El nombre por sí solo nunca autoriza vincular menciones futuras."""
        person = m.Person(preferred_name="SIN CARGO DECLARADO")
        session.add(person)
        session.flush()
        seed = _seed_mention(
            session, anchor, name="SIN CARGO DECLARADO", role=None, person_id=person.id
        )
        _decide_link(session, seed, person.id)
        assert not session.execute(select(m.IdentityPrecedent)).scalars().all()

    def test_reviewer_can_opt_out(self, ingest_codes, session, anchor):
        person = m.Person(preferred_name="JUAN YANGALI QUINTANILLA")
        session.add(person)
        session.flush()
        seed = _seed_mention(session, anchor, name=YANGALI, role=YANGALI_ROLE, person_id=person.id)
        _decide_link(session, seed, person.id, create_precedent=False)
        assert not session.execute(select(m.IdentityPrecedent)).scalars().all()

        ingest_codes(YANGALI_DOC)
        ingested = [x for x in _mentions(session, YANGALI) if x.id != seed.id][0]
        assert ingested.resolution_status == e.ResolutionStatus.CANDIDATE_MATCH

    def test_split_revokes_the_precedent(self, ingest_codes, session, anchor):
        person = m.Person(preferred_name="JUAN YANGALI QUINTANILLA")
        session.add(person)
        session.flush()
        seed, _ = self._seed_precedent(session, anchor, person.id)
        ingest_codes(YANGALI_DOC)
        mention = [x for x in _mentions(session, YANGALI) if x.id != seed.id][0]

        split_task = m.ReviewTask(
            task_type=e.ReviewTaskType.ENTITY_RESOLUTION,
            target_type="person_mention",
            target_id=mention.id,
            reason="el precedente vinculó a la persona equivocada",
        )
        session.add(split_task)
        session.flush()
        ReviewService(session).decide(split_task.id, e.DecisionAction.SPLIT_ENTITY)

        precedent = session.execute(select(m.IdentityPrecedent)).scalars().one()
        assert precedent.revoked_at is not None
        assert precedent.revoked_reason
        assert mention.identity_precedent_id is None
        assert SimpleEntityResolver(session).person_precedent(YANGALI, YANGALI_ROLE) is None


class TestGlobalAliasPrecedent:
    """Alias declarado sobre la grafía: vale con cualquier cargo.

    El caso corriente del diario es una misma persona escrita con y sin segundo
    nombre. Atar esa decisión al cargo obliga a re-decidirla cada vez que cambia
    de puesto, que es justo lo que un registro de nombramientos hace todo el
    tiempo. El alias resuelve la grafía de una vez; la contrapartida es que
    renuncia a la segunda señal, y por eso exige un nombre discriminante.
    """

    SHORT = "ELMER CUBA BUSTINZA"
    LONG = "ELMER RAFAEL CUBA BUSTINZA"
    # Cargo del firmante en el documento del Directorio del BCRP.
    MEF = "MINISTRO DE ECONOMIA Y FINANZAS"
    # Cargo distinto y no unipersonal: aísla el alias de la corroboración por oficio.
    OTHER_ROLE = "DIRECTOR DEL BANCO CENTRAL DE RESERVA DEL PERU"
    LONG_DOC = "2540905-3"  # SUNAT, firmado por el ministro con el nombre completo
    SHORT_DOC = "2540905-2"  # BCRP, firmado por el mismo ministro sin el "RAFAEL"

    def _person_for(self, session, normalized: str):
        return session.get(m.Person, _mentions(session, normalized)[0].canonical_person_id)

    def _seed_alias(self, session, anchor, person_id: str, **payload):
        seed = _seed_mention(
            session, anchor, name=self.SHORT, role=self.OTHER_ROLE, person_id=person_id
        )
        return seed, _decide_link(session, seed, person_id, **payload)

    def test_alias_precedent_is_recorded_without_role_context(self, ingest_codes, session, anchor):
        ingest_codes(self.LONG_DOC)
        person = self._person_for(session, self.LONG)
        self._seed_alias(session, anchor, person.id, scope="global")

        precedent = session.execute(select(m.IdentityPrecedent)).scalars().one()
        assert precedent.role_context is None
        assert precedent.name_normalized == self.SHORT
        assert precedent.person_id == person.id

    def test_alias_links_a_mention_whose_cargo_is_different(self, ingest_codes, session, anchor):
        """El pago del alias: la grafía se resuelve aunque el cargo no coincida."""
        ingest_codes(self.LONG_DOC)
        person = self._person_for(session, self.LONG)
        seed, decision = self._seed_alias(session, anchor, person.id, scope="global")

        ingest_codes(self.SHORT_DOC)
        ingested = [x for x in _mentions(session, self.SHORT) if x.id != seed.id]
        assert len(ingested) == 1
        mention = ingested[0]
        # El cargo del documento no es el del precedente: solo el alias explica el vínculo.
        assert mention.role_context_normalized == self.MEF
        assert mention.resolution_status == e.ResolutionStatus.PRECEDENT_LINKED
        assert mention.canonical_person_id == person.id
        assert not _tasks_for(session, mention.id)

        precedent = session.get(m.IdentityPrecedent, mention.identity_precedent_id)
        assert precedent.role_context is None
        assert precedent.review_decision_id == decision.id

    def test_office_scope_does_not_reach_a_different_cargo(self, ingest_codes, session, anchor):
        """Control negativo: con alcance por cargo el mismo caso sigue yendo a revisión."""
        ingest_codes(self.LONG_DOC)
        person = self._person_for(session, self.LONG)
        seed, _ = self._seed_alias(session, anchor, person.id)  # scope por defecto

        ingest_codes(self.SHORT_DOC)
        mention = [x for x in _mentions(session, self.SHORT) if x.id != seed.id][0]
        # La grafía sembrada ya es un candidato exacto, así que la tarea es de
        # resolución de entidad; lo que importa es que ninguna señal vincula sola.
        assert mention.resolution_status == e.ResolutionStatus.CANDIDATE_MATCH
        assert [t.task_type for t in _tasks_for(session, mention.id)] == [
            e.ReviewTaskType.ENTITY_RESOLUTION
        ]

    def test_alias_is_rejected_on_a_non_discriminant_name(self, session, anchor):
        """Una grafía que ya designa a dos personas no puede sostener un alias."""
        first = m.Person(preferred_name="JOSE GARCIA PEREZ")
        second = m.Person(preferred_name="JOSE GARCIA PEREZ")
        session.add_all([first, second])
        session.flush()
        for person in (first, second):
            _seed_mention(session, anchor, name="JOSE GARCIA PEREZ", role=None, person_id=person.id)
        target = _seed_mention(
            session, anchor, name="JOSE GARCIA PEREZ", role=YANGALI_ROLE, person_id=first.id
        )
        with pytest.raises(ReviewError, match="homónimos"):
            _decide_link(session, target, first.id, scope="global")

    def test_rejected_alias_does_not_resolve_the_task(self, session, anchor):
        """La ReviewDecision se persiste antes del manejador (para que el precedente
        pueda citarla). Si el manejador rechaza, la tarea debe seguir pendiente y no
        quedar precedente; descartar la decisión es responsabilidad del rollback del
        llamador (ver interfaces/api/deps.get_db)."""
        first = m.Person(preferred_name="JOSE GARCIA PEREZ")
        second = m.Person(preferred_name="JOSE GARCIA PEREZ")
        session.add_all([first, second])
        session.flush()
        for person in (first, second):
            _seed_mention(session, anchor, name="JOSE GARCIA PEREZ", role=None, person_id=person.id)
        target = _seed_mention(
            session, anchor, name="JOSE GARCIA PEREZ", role=YANGALI_ROLE, person_id=first.id
        )
        task = m.ReviewTask(
            task_type=e.ReviewTaskType.ENTITY_RESOLUTION,
            target_type="person_mention",
            target_id=target.id,
            reason="alias inadmisible (siembra de prueba)",
        )
        session.add(task)
        session.flush()
        with pytest.raises(ReviewError):
            ReviewService(session).decide(
                task.id,
                e.DecisionAction.LINK_ENTITY,
                payload={"entity_id": first.id, "scope": "global"},
            )
        assert task.status == e.ReviewTaskStatus.PENDING
        assert task.resolved_at is None
        assert not session.execute(select(m.IdentityPrecedent)).scalars().all()

        session.rollback()
        assert not session.execute(select(m.ReviewDecision)).scalars().all()

    def test_unknown_scope_is_rejected(self, ingest_codes, session, anchor):
        ingest_codes(self.LONG_DOC)
        person = self._person_for(session, self.LONG)
        seed = _seed_mention(
            session, anchor, name=self.SHORT, role=self.OTHER_ROLE, person_id=person.id
        )
        with pytest.raises(ReviewError, match="Alcance de precedente no soportado"):
            _decide_link(session, seed, person.id, scope="siempre")

    def test_split_revokes_the_alias(self, ingest_codes, session, anchor):
        """Sin esto el alias equivocado sobreviviría a su propia corrección."""
        ingest_codes(self.LONG_DOC)
        person = self._person_for(session, self.LONG)
        seed, _ = self._seed_alias(session, anchor, person.id, scope="global")
        ingest_codes(self.SHORT_DOC)
        mention = [x for x in _mentions(session, self.SHORT) if x.id != seed.id][0]

        split_task = m.ReviewTask(
            task_type=e.ReviewTaskType.ENTITY_RESOLUTION,
            target_type="person_mention",
            target_id=mention.id,
            reason="el alias vinculó a la persona equivocada",
        )
        session.add(split_task)
        session.flush()
        ReviewService(session).decide(split_task.id, e.DecisionAction.SPLIT_ENTITY)

        precedent = session.execute(select(m.IdentityPrecedent)).scalars().one()
        assert precedent.revoked_at is not None
        assert precedent.revoked_reason
        assert mention.identity_precedent_id is None
        # Deja de aplicar con cualquier cargo, no solo con el de esta mención.
        resolver = SimpleEntityResolver(session)
        assert resolver.person_precedent(self.SHORT, self.MEF) is None
        assert resolver.person_precedent(self.SHORT, None) is None

    def test_contradictory_scopes_do_not_link(self, ingest_codes, session, anchor):
        """Un alias y un precedente por cargo que discrepan vuelven a revisión."""
        ingest_codes(self.LONG_DOC)
        person = self._person_for(session, self.LONG)
        other = m.Person(preferred_name="OTRO CUBA")
        session.add(other)
        session.flush()
        self._seed_alias(session, anchor, person.id, scope="global")
        conflicting = _seed_mention(
            session, anchor, name=self.SHORT, role=self.MEF, person_id=other.id
        )
        _decide_link(session, conflicting, other.id)  # precedente por cargo, otra persona

        assert SimpleEntityResolver(session).person_precedent(self.SHORT, self.MEF) is None

    def test_alias_applies_to_a_mention_without_any_cargo(self, session, anchor):
        """El alias no depende del cargo, así que alcanza a menciones que no lo declaran."""
        person = m.Person(preferred_name="ANA MARIA QUISPE ROJAS")
        session.add(person)
        session.flush()
        seed = _seed_mention(
            session, anchor, name="ANA QUISPE ROJAS", role=YANGALI_ROLE, person_id=person.id
        )
        _decide_link(session, seed, person.id, scope="global")
        found = SimpleEntityResolver(session).person_precedent("ANA QUISPE ROJAS", None)
        assert found is not None and found.person_id == person.id


class TestDeclaredIdentifier:
    """Un DNI declarado por la fuente identifica; no se infiere nada."""

    def _mention_with_dni(self, session, anchor, person_id: str, value: str, name: str):
        mention = _seed_mention(session, anchor, name=name, role=None, person_id=person_id)
        session.add(
            m.PersonIdentifier(
                person_mention_id=mention.id,
                scheme=e.IdentifierScheme.DNI,
                value_raw=value,
                value_normalized=value,
                evidence_span_id=anchor[1],
            )
        )
        session.flush()
        return mention

    def test_same_dni_finds_the_person(self, session, anchor):
        person = m.Person(preferred_name="LUIS TORRES RAMOS")
        session.add(person)
        session.flush()
        self._mention_with_dni(session, anchor, person.id, "09342789", "LUIS TORRES RAMOS")
        found = SimpleEntityResolver(session).persons_by_identifier([("DNI", "09342789")])
        assert [x.id for x in found] == [person.id]

    def test_different_dni_does_not_match(self, session, anchor):
        person = m.Person(preferred_name="LUIS TORRES RAMOS")
        session.add(person)
        session.flush()
        self._mention_with_dni(session, anchor, person.id, "09342789", "LUIS TORRES RAMOS")
        assert SimpleEntityResolver(session).persons_by_identifier([("DNI", "11223344")]) == []

    def test_same_dni_on_two_persons_is_a_conflict_not_a_choice(self, session, anchor):
        """Dos personas con el mismo documento es un error de datos: no se elige."""
        first = m.Person(preferred_name="LUIS TORRES RAMOS")
        second = m.Person(preferred_name="LUIS A. TORRES RAMOS")
        session.add_all([first, second])
        session.flush()
        self._mention_with_dni(session, anchor, first.id, "09342789", "LUIS TORRES RAMOS")
        self._mention_with_dni(session, anchor, second.id, "09342789", "LUIS A TORRES RAMOS")
        found = SimpleEntityResolver(session).persons_by_identifier([("DNI", "09342789")])
        assert len(found) == 2


class TestPersonVariantCheck:
    def test_name_variant_raises_task_instead_of_silent_duplicate(self, ingested_session):
        """'ELMER CUBA BUSTINZA' y 'ELMER RAFAEL CUBA BUSTINZA' son dos filas Person.

        Antes ese duplicado se creaba sin señal alguna. Ahora sigue creándose
        —no se fusiona sin humano— pero queda marcado para revisión.
        """
        short = _mentions(ingested_session, "ELMER CUBA BUSTINZA")[0]
        tasks = [
            t
            for t in _tasks_for(ingested_session, short.id)
            if t.task_type == e.ReviewTaskType.PERSON_VARIANT_CHECK
        ]
        assert len(tasks) == 1
        assert "ELMER RAFAEL CUBA BUSTINZA" in tasks[0].reason
        assert short.resolution_status == e.ResolutionStatus.AUTO_LINKED

    def test_variant_task_carries_candidate_assertion(self, ingested_session):
        short = _mentions(ingested_session, "ELMER CUBA BUSTINZA")[0]
        assertion = ingested_session.execute(
            select(m.Assertion).where(
                m.Assertion.subject_id == short.id,
                m.Assertion.predicate == "mention_possibly_duplicates",
            )
        ).scalar_one()
        assert assertion.review_status == e.ReviewStatus.CANDIDATE
        assert assertion.confidence < 1.0

    def test_linking_a_variant_absorbs_the_duplicate_person(self, ingested_session):
        short = _mentions(ingested_session, "ELMER CUBA BUSTINZA")[0]
        long = _mentions(ingested_session, "ELMER RAFAEL CUBA BUSTINZA")[0]
        duplicate_id = short.canonical_person_id
        survivor_id = long.canonical_person_id
        assert duplicate_id != survivor_id

        task = [
            t
            for t in _tasks_for(ingested_session, short.id)
            if t.task_type == e.ReviewTaskType.PERSON_VARIANT_CHECK
        ][0]
        ReviewService(ingested_session).decide(
            task.id,
            e.DecisionAction.LINK_ENTITY,
            reviewer="revisor@example.org",
            payload={"entity_id": survivor_id},
        )

        duplicate = ingested_session.get(m.Person, duplicate_id)
        assert duplicate.status == "MERGED"
        assert duplicate.merged_into_person_id == survivor_id
        # La fila no se borra: los identificadores ya publicados siguen resolviendo.
        assert ingested_session.get(m.Person, duplicate_id) is not None

    def test_absorbed_person_stops_being_proposed(self, ingested_session):
        short = _mentions(ingested_session, "ELMER CUBA BUSTINZA")[0]
        long = _mentions(ingested_session, "ELMER RAFAEL CUBA BUSTINZA")[0]
        duplicate_id = short.canonical_person_id
        task = [
            t
            for t in _tasks_for(ingested_session, short.id)
            if t.task_type == e.ReviewTaskType.PERSON_VARIANT_CHECK
        ][0]
        ReviewService(ingested_session).decide(
            task.id,
            e.DecisionAction.LINK_ENTITY,
            payload={"entity_id": long.canonical_person_id},
        )
        resolver = SimpleEntityResolver(ingested_session)
        proposed = {p.entity_id for p in resolver.variant_person_candidates("ELMER CUBA BUSTINZA")}
        assert duplicate_id not in proposed


class TestConflictingSignals:
    def test_contradictory_signals_do_not_link(self, ingest_codes, session, anchor):
        """Un precedente humano y una corroboración por oficio que discrepan
        producen conflicto, no una elección silenciosa de la señal más fuerte."""
        wrong = m.Person(preferred_name="OTRA PERSONA")
        session.add(wrong)
        session.flush()
        ingest_codes(KEIKO_DOCS[0])
        first = _mentions(session, KEIKO)[0]

        # Precedente humano que apunta a otra persona para la misma clave.
        seed = _seed_mention(session, anchor, name=KEIKO, role=PRESIDENCIA, person_id=wrong.id)
        _decide_link(session, seed, wrong.id)

        ingest_codes(KEIKO_DOCS[1])
        conflicted = _mentions(session, KEIKO)[-1]
        assert conflicted.canonical_person_id is None
        assert conflicted.resolution_status == e.ResolutionStatus.CANDIDATE_MATCH
        conflicts = [
            t
            for t in _tasks_for(session, conflicted.id)
            if t.task_type == e.ReviewTaskType.EXTRACTION_CONFLICT
        ]
        assert len(conflicts) == 1
        assert conflicts[0].priority == 1
        assert first.canonical_person_id != wrong.id
