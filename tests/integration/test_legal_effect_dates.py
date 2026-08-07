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
        assert "15 de agosto" in (verdict.postponement_clause or "")
