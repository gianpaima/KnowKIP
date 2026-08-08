"""La fecha que la norma determina: de la ingesta a la API, y la reparación.

Lo que se congela aquí:
- una designación sin fecha expresa deja de abrir tarea y queda con
  `legal_effect_from` = día de publicación, con la norma citada;
- `effective_from` sigue intacto en NOT_STATED (regla 12);
- la afirmación cita la frase de la propia captura que declara esa fecha, y el
  rango recorta exactamente esa frase en los bytes capturados;
- el comando de reparación reconstruye todo eso desde el CAS, sin red, y cierra
  la tarea pendiente con una decisión atribuida a la regla;
- un acto cuya parte resolutiva posterga la vigencia vuelve al humano.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.application.legal_effect import (
    PREDICATE,
    SPAN_KIND,
    BackfillOutcome,
    backfill_legal_effect_dates,
    verdict_for_event,
)
from kipu_knowledge.application.review import ReviewError, ReviewService
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain.legal_effect import RULE_VERSION, LegalEffectOutcome

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "elperuano"
MIDAGRI = "2540861-1"  # Caso A: la designación que motivó la regla


def _midagri_event(session) -> m.PersonnelEvent:
    return session.execute(
        select(m.PersonnelEvent)
        .join(m.LegalDocument, m.LegalDocument.id == m.PersonnelEvent.legal_document_id)
        .where(m.LegalDocument.number_normalized == "D000284-2026-MIDAGRI-DM")
    ).scalar_one()


class TestIngest:
    def test_the_designation_takes_effect_the_day_it_was_published(self, ingested_session):
        event = _midagri_event(ingested_session)
        assert event.legal_effect_from.isoformat() == "2026-08-06"
        basis = event.legal_effect_basis_json["basis"]
        assert basis["norm"] == "Ley N.º 27594"
        assert basis["article"] == "6"
        assert event.legal_effect_basis_json["rule"] == RULE_VERSION

    def test_the_stated_date_is_left_alone(self, ingested_session):
        """La determinación no reescribe lo que el documento dice."""
        event = _midagri_event(ingested_session)
        assert event.effective_from is None
        assert event.effective_from_status == e.DateStatus.NOT_STATED

    def test_no_review_task_is_opened_when_the_norm_decides(self, ingested_session):
        pending = (
            ingested_session.execute(
                select(m.ReviewTask).where(
                    m.ReviewTask.task_type == e.ReviewTaskType.EFFECTIVE_DATE_UNSTATED,
                    m.ReviewTask.status == e.ReviewTaskStatus.PENDING,
                )
            )
            .scalars()
            .all()
        )
        assert not pending, (
            "ninguna de las nueve capturas debería seguir preguntando por una fecha "
            "que la norma determina: " + "; ".join(t.reason for t in pending)
        )

    def test_the_assertion_cites_the_publication_date_from_the_capture(self, ingested_session):
        event = _midagri_event(ingested_session)
        assertion = ingested_session.execute(
            select(m.Assertion).where(
                m.Assertion.subject_id == event.id, m.Assertion.predicate == PREDICATE
            )
        ).scalar_one()
        assert assertion.review_status == e.ReviewStatus.AUTO_ACCEPTED
        span = ingested_session.get(m.EvidenceSpan, assertion.evidence_span_id)
        assert span.locator_json["kind"] == SPAN_KIND
        assert "06/08/2026" in span.quoted_text

    def test_the_evidence_range_cuts_that_phrase_in_the_captured_bytes(self, ingested_session):
        """El span no cuelga de una sección —la fecha la declara la página, no el
        dispositivo—, así que su anclaje se verifica contra el artefacto."""
        event = _midagri_event(ingested_session)
        assertion = ingested_session.execute(
            select(m.Assertion).where(
                m.Assertion.subject_id == event.id, m.Assertion.predicate == PREDICATE
            )
        ).scalar_one()
        span = ingested_session.get(m.EvidenceSpan, assertion.evidence_span_id)
        page = (FIXTURES / f"{MIDAGRI}.html").read_bytes().decode("utf-8", errors="replace")
        assert page[span.char_start : span.char_end] == span.quoted_text

    def test_the_assignment_carries_the_determined_date(self, ingested_session):
        event = _midagri_event(ingested_session)
        ra = ingested_session.execute(
            select(m.RoleAssignment).where(m.RoleAssignment.start_event_id == event.id)
        ).scalar_one()
        assert ra.valid_from is None and ra.valid_from_status == e.DateStatus.NOT_STATED
        assert ra.legal_effect_from.isoformat() == "2026-08-06"

    def test_an_explicit_date_is_never_shadowed(self, ingested_session):
        """Caso E (BNP): el documento sí expresa la fecha, así que no hay nada
        que determinar y la columna queda vacía."""
        event = ingested_session.execute(
            select(m.PersonnelEvent)
            .join(m.LegalDocument, m.LegalDocument.id == m.PersonnelEvent.legal_document_id)
            .where(m.LegalDocument.number_normalized.like("%000066-2026-BNP%"))
        ).scalar_one()
        assert event.effective_from_status == e.DateStatus.EXPLICIT
        assert event.legal_effect_from is None


class TestApiPayload:
    def test_the_event_payload_separates_what_the_law_adds(self, api_client):
        event = api_client.get(f"/v1/documents/by-source/NL/{MIDAGRI}").json()["events"][0]
        assert event["effective_from"] is None  # lo que el documento dice
        assert event["legal_effect"]["legal_effect_from"] == "2026-08-06"
        assert event["legal_effect"]["status"] == "DERIVED"
        assert event["legal_effect"]["norm"] == "Ley N.º 27594"
        assert event["legal_effect"]["rule"] == RULE_VERSION

    def test_the_uncertainty_flag_says_the_norm_settled_it(self, api_client):
        doc = api_client.get(f"/v1/documents/by-source/NL/{MIDAGRI}").json()
        flag = next(u for u in doc["uncertainty"] if u["kind"] == "effective_from_not_stated")
        # La bandera sigue: el documento no expresa la fecha. Pero deja de ser
        # una laguna, y eso se dice en el mismo sitio.
        assert flag["determined_by_law"]["legal_effect_from"] == "2026-08-06"
        assert "Ley N.º 27594" in flag["determined_by_law"]["citation"]

    def test_the_rdf_projection_keeps_both_apart(self, api_client, ingested_session):
        doc = ingested_session.execute(
            select(m.LegalDocument).where(
                m.LegalDocument.number_normalized == "D000284-2026-MIDAGRI-DM"
            )
        ).scalar_one()
        ttl = api_client.get(f"/v1/exports/documents/{doc.id}.ttl").text
        assert "legalEffectFrom" in ttl
        assert '"NOT_STATED"' in ttl  # effectiveFromStatus intacto
        assert "Ley N.º 27594" in ttl
        assert RULE_VERSION in ttl


class TestRepairOfOlderRows:
    """Las filas ingeridas antes de la regla se reparan releyendo el CAS."""

    def _make_legacy(self, session) -> tuple[m.PersonnelEvent, m.ReviewTask]:
        event = _midagri_event(session)
        for assertion in session.execute(
            select(m.Assertion).where(
                m.Assertion.subject_id == event.id, m.Assertion.predicate == PREDICATE
            )
        ).scalars():
            span_id = assertion.evidence_span_id
            session.delete(assertion)
            session.flush()
            session.delete(session.get(m.EvidenceSpan, span_id))
        event.legal_effect_from = None
        event.legal_effect_basis_json = None
        for ra in session.execute(
            select(m.RoleAssignment).where(m.RoleAssignment.start_event_id == event.id)
        ).scalars():
            ra.legal_effect_from = None
        task = m.ReviewTask(
            task_type=e.ReviewTaskType.EFFECTIVE_DATE_UNSTATED,
            target_type="personnel_event",
            target_id=event.id,
            reason="El documento no expresa fecha efectiva de inicio (fila de la era anterior)",
            priority=4,
        )
        session.add(task)
        session.flush()
        return event, task

    def test_backfill_determines_the_date_and_closes_the_task(self, ingested_session, store):
        event, task = self._make_legacy(ingested_session)
        results = backfill_legal_effect_dates(ingested_session, store)
        mine = [r for r in results if r.event_id == event.id]
        assert [r.outcome for r in mine] == [BackfillOutcome.DETERMINED]
        assert mine[0].value == "2026-08-06"

        assert event.legal_effect_from.isoformat() == "2026-08-06"
        assert task.status == e.ReviewTaskStatus.RESOLVED
        decision = ingested_session.execute(
            select(m.ReviewDecision).where(m.ReviewDecision.review_task_id == task.id)
        ).scalar_one()
        assert decision.action == e.DecisionAction.APPLY_LEGAL_EFFECT_DATE
        assert decision.reviewer == f"sistema · {RULE_VERSION}"

    def test_backfill_rebuilds_the_citation_from_the_captured_bytes(self, ingested_session, store):
        event, _ = self._make_legacy(ingested_session)
        backfill_legal_effect_dates(ingested_session, store)
        assertion = ingested_session.execute(
            select(m.Assertion).where(
                m.Assertion.subject_id == event.id, m.Assertion.predicate == PREDICATE
            )
        ).scalar_one()
        span = ingested_session.get(m.EvidenceSpan, assertion.evidence_span_id)
        page = (FIXTURES / f"{MIDAGRI}.html").read_bytes().decode("utf-8", errors="replace")
        assert page[span.char_start : span.char_end] == span.quoted_text

    def test_dry_run_changes_nothing(self, ingested_session, store):
        event, task = self._make_legacy(ingested_session)
        results = backfill_legal_effect_dates(ingested_session, store, dry_run=True)
        assert any("[dry-run]" in r.detail for r in results if r.event_id == event.id)
        assert event.legal_effect_from is None
        assert task.status == e.ReviewTaskStatus.PENDING


class TestHumanPath:
    def test_a_reviewer_can_apply_the_rule_from_the_panel(self, ingested_session):
        event = _midagri_event(ingested_session)
        # Estado equivalente al de una fila antigua: la cita ya existe (la creó
        # la ingesta), pero la tarea sigue abierta.
        event.legal_effect_from = None
        event.legal_effect_basis_json = None
        task = m.ReviewTask(
            task_type=e.ReviewTaskType.EFFECTIVE_DATE_UNSTATED,
            target_type="personnel_event",
            target_id=event.id,
            reason="prueba: aplicación humana de la regla",
            priority=4,
        )
        ingested_session.add(task)
        ingested_session.flush()

        ReviewService(ingested_session).decide(
            task.id, e.DecisionAction.APPLY_LEGAL_EFFECT_DATE, reviewer="revisor@kipu"
        )
        assert event.legal_effect_from.isoformat() == "2026-08-06"
        assert task.status == e.ReviewTaskStatus.RESOLVED

    def test_the_panel_refuses_to_apply_the_rule_where_it_does_not_reach(self, ingested_session):
        """Caso F (INBP): encargatura con eficacia anticipada. Ni el tipo de acto
        está cubierto ni falta la fecha, así que la acción falla explicando por qué
        en lugar de escribir una fecha que nadie podría justificar."""
        event = ingested_session.execute(
            select(m.PersonnelEvent).where(
                m.PersonnelEvent.event_type == e.EventType.ADDITIONAL_RESPONSIBILITY
            )
        ).scalar_one()
        task = m.ReviewTask(
            task_type=e.ReviewTaskType.EFFECTIVE_DATE_UNSTATED,
            target_type="personnel_event",
            target_id=event.id,
            reason="prueba: acto fuera del catálogo",
            priority=4,
        )
        ingested_session.add(task)
        ingested_session.flush()

        service = ReviewService(ingested_session)
        try:
            service.decide(task.id, e.DecisionAction.APPLY_LEGAL_EFFECT_DATE)
        except ReviewError as exc:
            assert "no determina" in str(exc)
        else:  # pragma: no cover - el fallo es el comportamiento esperado
            raise AssertionError("aplicar la regla sobre un acto no cubierto debía fallar")
        assert event.legal_effect_from is None


class TestVeto:
    def test_a_clause_postponing_the_vigencia_returns_the_case_to_a_human(self, ingested_session):
        """Se añade la cláusula a la parte resolutiva del caso A y se re-evalúa:
        la regla general cede ante la disposición en contrario."""
        event = _midagri_event(ingested_session)
        article = (
            ingested_session.execute(
                select(m.DocumentSection).where(
                    m.DocumentSection.legal_document_id == event.legal_document_id,
                    m.DocumentSection.section_type == e.SectionType.ARTICLE,
                )
            )
            .scalars()
            .first()
        )
        article.text_raw += (
            " Artículo 2.- La presente Resolución Ministerial entrará en vigencia el 15 "
            "de agosto de 2026."
        )
        ingested_session.flush()

        verdict = verdict_for_event(ingested_session, event)
        assert verdict.outcome == LegalEffectOutcome.VETOED
        assert verdict.deferral is not None
        assert not verdict.deferral.computable
        assert "15 de agosto" in verdict.deferral.text

    def test_the_task_no_longer_claims_the_document_says_nothing(self, ingested_session):
        """Regresión de ADR-0009: un acto que difiere su vigencia sí dice cuándo
        empieza. La tarea que afirmaba lo contrario mandaba al revisor a buscar
        en la captura algo que el artículo ya decía."""
        event = _midagri_event(ingested_session)
        article = (
            ingested_session.execute(
                select(m.DocumentSection).where(
                    m.DocumentSection.legal_document_id == event.legal_document_id,
                    m.DocumentSection.section_type == e.SectionType.ARTICLE,
                )
            )
            .scalars()
            .first()
        )
        article.text_raw += (
            " Artículo 2.- La presente entrará en vigencia con la instalación del Directorio."
        )
        ingested_session.flush()
        verdict = verdict_for_event(ingested_session, event)
        assert verdict.deferral is not None
        assert "no permiten fechar" in verdict.rationale


class TestDeferralToTheDayAfterPublication:
    """Caso N (RCG N.º 431-2026-CG), el que motivó ADR-0009.

    Su artículo 3 difiere la efectividad al día siguiente de la publicación:
    fecha calculable, no laguna. Antes producía una tarea sin salida posible.
    """

    CODE = "2540891-1"

    def _ingest(self, session, ingest_service):
        ingest_service.ingest_fixture(self.CODE, FIXTURES.parent)
        session.flush()
        return (
            session.execute(
                select(m.PersonnelEvent)
                .join(m.LegalDocument, m.LegalDocument.id == m.PersonnelEvent.legal_document_id)
                .where(m.LegalDocument.number_normalized == "431-2026-CG")
                .order_by(m.PersonnelEvent.id)
            )
            .scalars()
            .all()
        )

    def test_every_event_takes_effect_the_day_after_publication(self, session, ingest_service):
        events = self._ingest(session, ingest_service)
        assert events, "el fixture debería producir eventos de personal"
        for event in events:
            assert event.legal_effect_from is not None, event.event_type
            # Publicada el 2026-08-07: los efectos corren desde el 08, no el 07.
            assert event.legal_effect_from.isoformat() == "2026-08-08", event.event_type

    def test_the_clause_that_fixed_it_travels_with_the_date(self, session, ingest_service):
        event = self._ingest(session, ingest_service)[0]
        basis = event.legal_effect_basis_json
        assert basis["deferral_kind"] == "DAY_AFTER_PUBLICATION"
        assert "día siguiente" in basis["deferral_clause"]
        # La sección de la que salió la cláusula queda anclada: la afirmación se
        # audita sin volver a correr el clasificador.
        section = session.get(m.DocumentSection, basis["deferral_clause_section_id"])
        assert section is not None
        assert "día siguiente" in section.text_raw
        # La norma catalogada sigue siendo la que faculta al acto a disponerlo.
        assert basis["basis"]["article"] in {"6", "233.3"}
        assert basis["rule"] == RULE_VERSION

    def test_the_stated_date_is_still_untouched(self, session, ingest_service):
        for event in self._ingest(session, ingest_service):
            assert event.effective_from is None
            assert event.effective_from_status == e.DateStatus.NOT_STATED

    def test_it_no_longer_opens_a_task_no_human_could_close(self, session, ingest_service):
        events = self._ingest(session, ingest_service)
        pending = (
            session.execute(
                select(m.ReviewTask).where(
                    m.ReviewTask.task_type == e.ReviewTaskType.EFFECTIVE_DATE_UNSTATED,
                    m.ReviewTask.target_id.in_([event.id for event in events]),
                    m.ReviewTask.status == e.ReviewTaskStatus.PENDING,
                )
            )
            .scalars()
            .all()
        )
        assert not pending, "; ".join(t.reason for t in pending)

    def test_the_date_reaches_the_assignment(self, session, ingest_service):
        events = self._ingest(session, ingest_service)
        starts = [ev for ev in events if ev.assignment_effect == e.AssignmentEffect.START]
        assert starts
        assignments = (
            session.execute(
                select(m.RoleAssignment).where(
                    m.RoleAssignment.start_event_id.in_([ev.id for ev in starts])
                )
            )
            .scalars()
            .all()
        )
        assert assignments
        for ra in assignments:
            assert ra.legal_effect_from.isoformat() == "2026-08-08"

    def test_the_determination_is_re_derivable_from_the_frozen_data(self, session, ingest_service):
        """La regla es determinista: re-ejecutarla sobre lo capturado tiene que
        devolver la misma fecha que se guardó."""
        for event in self._ingest(session, ingest_service):
            verdict = verdict_for_event(session, event)
            assert verdict.outcome == LegalEffectOutcome.DETERMINED
            assert verdict.value == event.legal_effect_from


class TestSettingTheDateByHand:
    """La salida para lo que sigue siendo indeterminado (ADR-0009).

    Sin ella la tarea quedaba abierta con tres acciones que o fallaban o
    afirmaban algo que el documento contradice.
    """

    def _vetoed_event_with_task(self, session) -> tuple[m.PersonnelEvent, m.ReviewTask]:
        event = _midagri_event(session)
        event.legal_effect_from = None
        event.legal_effect_basis_json = None
        article = (
            session.execute(
                select(m.DocumentSection).where(
                    m.DocumentSection.legal_document_id == event.legal_document_id,
                    m.DocumentSection.section_type == e.SectionType.ARTICLE,
                )
            )
            .scalars()
            .first()
        )
        article.text_raw += (
            " Artículo 2.- La presente entrará en vigencia con la instalación del Directorio."
        )
        task = m.ReviewTask(
            task_type=e.ReviewTaskType.EFFECTIVE_DATE_UNSTATED,
            target_type="personnel_event",
            target_id=event.id,
            reason="prueba: diferimiento indeterminado",
            priority=4,
        )
        session.add(task)
        session.flush()
        return event, task

    def test_a_reviewer_can_close_it_by_fixing_the_date(self, ingested_session):
        event, task = self._vetoed_event_with_task(ingested_session)
        ReviewService(ingested_session).decide(
            task.id,
            e.DecisionAction.SET_LEGAL_EFFECT_DATE,
            reviewer="revisor@kipu",
            payload={"legal_effect_from": "2026-09-01"},
            notes="El acta de instalación del Directorio es del 2026-09-01.",
        )
        assert task.status == e.ReviewTaskStatus.RESOLVED
        assert event.legal_effect_from.isoformat() == "2026-09-01"
        basis = event.legal_effect_basis_json
        assert basis["method"] == "decisión humana"
        assert basis["reviewer"] == "revisor@kipu"
        # Queda registrado por qué la regla no pudo: sin eso, seis meses después
        # una fecha humana es indistinguible de un capricho.
        assert "no permiten fechar" in basis["rule_declined_because"]
        assert basis["review_decision_id"]

    def test_what_the_document_says_is_still_not_overwritten(self, ingested_session):
        event, task = self._vetoed_event_with_task(ingested_session)
        ReviewService(ingested_session).decide(
            task.id,
            e.DecisionAction.SET_LEGAL_EFFECT_DATE,
            payload={"legal_effect_from": "2026-09-01"},
            notes="acta de instalación",
        )
        assert event.effective_from is None
        assert event.effective_from_status == e.DateStatus.NOT_STATED

    def test_a_date_without_a_reason_is_refused(self, ingested_session):
        _, task = self._vetoed_event_with_task(ingested_session)
        try:
            ReviewService(ingested_session).decide(
                task.id,
                e.DecisionAction.SET_LEGAL_EFFECT_DATE,
                payload={"legal_effect_from": "2026-09-01"},
            )
        except ReviewError as exc:
            assert "auditable" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("una fecha a mano sin motivo no debía aceptarse")

    def test_an_unreadable_date_is_refused(self, ingested_session):
        _, task = self._vetoed_event_with_task(ingested_session)
        try:
            ReviewService(ingested_session).decide(
                task.id,
                e.DecisionAction.SET_LEGAL_EFFECT_DATE,
                payload={"legal_effect_from": "01/09/2026"},
                notes="acta",
            )
        except ReviewError as exc:
            assert "AAAA-MM-DD" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("una fecha ilegible no debía aceptarse")

    def test_it_refuses_to_replace_what_the_norm_already_determines(self, ingested_session):
        """Si la regla sí decide, fijarla a mano perdería el fundamento citable
        y dejaría una fecha que nadie puede volver a derivar."""
        event = _midagri_event(ingested_session)
        task = m.ReviewTask(
            task_type=e.ReviewTaskType.EFFECTIVE_DATE_UNSTATED,
            target_type="personnel_event",
            target_id=event.id,
            reason="prueba: la norma sí determina",
            priority=4,
        )
        ingested_session.add(task)
        ingested_session.flush()
        try:
            ReviewService(ingested_session).decide(
                task.id,
                e.DecisionAction.SET_LEGAL_EFFECT_DATE,
                payload={"legal_effect_from": "2026-09-01"},
                notes="me lo invento",
            )
        except ReviewError as exc:
            assert "APPLY_LEGAL_EFFECT_DATE" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("no debía dejar pisar a mano lo que la norma determina")
