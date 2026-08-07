"""La regla que determina el inicio de efectos: qué decide y qué se niega a decidir."""

from __future__ import annotations

from datetime import date

from kipu_knowledge.domain.enums import DateStatus, EventType, SourceAuthority
from kipu_knowledge.domain.legal_effect import (
    BASIS_APPOINTMENT,
    BASIS_TERMINATION,
    RULE_VERSION,
    LegalEffectOutcome,
    determine_legal_effect,
    find_postponement_clause,
)

PUBLISHED = date(2026, 8, 6)


def _verdict(**overrides):
    kwargs = {
        "event_type": EventType.DESIGNATION,
        "stated_status": DateStatus.NOT_STATED,
        "published_on": PUBLISHED,
        "source_authority": SourceAuthority.OFFICIAL_GAZETTE,
        "postponement_clause": None,
    }
    kwargs.update(overrides)
    return determine_legal_effect(**kwargs)


class TestWhatTheRuleDetermines:
    def test_designation_takes_effect_the_day_it_is_published(self):
        verdict = _verdict()
        assert verdict.outcome == LegalEffectOutcome.DETERMINED
        assert verdict.value == PUBLISHED  # el mismo día, no el siguiente
        assert verdict.basis is BASIS_APPOINTMENT
        assert "Ley N.º 27594" in verdict.rationale

    def test_the_rule_never_uses_the_issue_date(self):
        """La resolución se emitió el 05 y se publicó el 06: manda la publicación."""
        assert _verdict().value == date(2026, 8, 6)

    def test_terminations_cite_their_own_norm(self):
        verdict = _verdict(event_type=EventType.ACCEPT_RESIGNATION)
        assert verdict.outcome == LegalEffectOutcome.DETERMINED
        assert verdict.basis is BASIS_TERMINATION

    def test_the_basis_travels_with_the_verdict(self):
        payload = _verdict().as_dict()
        assert payload["rule"] == RULE_VERSION
        assert payload["status"] == "DERIVED"
        assert payload["legal_effect_from"] == "2026-08-06"
        assert payload["basis"]["article"] == "6"
        assert payload["basis"]["quote_kind"] == "verbatim"
        assert payload["basis"]["source_url"].startswith("https://")


class TestWhatTheRuleRefusesToDecide:
    def test_a_stated_date_is_not_touched(self):
        verdict = _verdict(stated_status=DateStatus.EXPLICIT)
        assert verdict.outcome == LegalEffectOutcome.NOT_APPLICABLE
        assert verdict.value is None

    def test_uncatalogued_event_types_stay_unstated(self):
        """Un encargo o una responsabilidad adicional no están cubiertos: no hay
        norma catalogada que fije su inicio, así que la fecha sigue sin determinar."""
        for event_type in (
            EventType.ACTING_ASSIGNMENT,
            EventType.ADDITIONAL_RESPONSIBILITY,
            EventType.DELEGATION,
            EventType.OTHER_PERSONNEL_ACTION,
        ):
            verdict = _verdict(event_type=event_type)
            assert verdict.outcome == LegalEffectOutcome.NOT_APPLICABLE, event_type
            assert verdict.value is None

    def test_without_publication_in_the_official_gazette_there_are_no_effects(self):
        """Caso Wasimikuna: una designación no publicada en El Peruano carece de
        efectos jurídicos. La copia de la entidad emisora no los produce."""
        for authority in (SourceAuthority.ISSUING_ENTITY, SourceAuthority.MIRROR, None):
            verdict = _verdict(source_authority=authority)
            assert verdict.outcome == LegalEffectOutcome.VETOED, authority
            assert verdict.value is None

    def test_without_a_publication_date_there_is_nothing_to_determine(self):
        verdict = _verdict(published_on=None)
        assert verdict.outcome == LegalEffectOutcome.VETOED

    def test_a_clause_postponing_the_vigencia_hands_the_case_back(self):
        """Es la excepción que el propio artículo 6 reserva; decide un humano."""
        verdict = _verdict(
            postponement_clause=(
                "La presente resolución rige a partir del día siguiente de su publicación."
            )
        )
        assert verdict.outcome == LegalEffectOutcome.VETOED
        assert "posterga la vigencia" in verdict.rationale
        assert verdict.as_dict()["postponement_clause"]


class TestPostponementDetection:
    def test_detects_the_forms_that_displace_the_general_rule(self):
        for text in (
            "Artículo 3.- La presente Resolución Ministerial entrará en vigencia el 15 de "
            "agosto de 2026.",
            "Artículo 3.- La designación surte efectos a partir del día siguiente de su "
            "publicación.",
            "Artículo 3.- Se dispone postergar su vigencia hasta la instalación del Directorio.",
        ):
            assert find_postponement_clause([text]) is not None, text

    def test_returns_the_whole_sentence_so_a_reviewer_can_read_it(self):
        clause = find_postponement_clause(
            [
                "Artículo 2.- Notifíquese la presente. Artículo 3.- La presente "
                "resolución entrará en vigencia el 15 de agosto de 2026. Artículo 4.- "
                "Publíquese."
            ]
        )
        assert clause is not None
        assert clause.startswith("Artículo 3.-")
        assert clause.endswith("2026.")

    def test_an_explicit_date_is_not_a_postponement(self):
        """«a partir del 30 de julio de 2026» es una fecha expresa que el extractor
        ya recoge como EXPLICIT; confundirla con una postergación vetaría la regla
        justo donde no hace falta."""
        assert (
            find_postponement_clause(
                [
                    "Artículo Único.- ENCARGAR, con eficacia anticipada a partir del 30 de "
                    "julio de 2026, las funciones..."
                ]
            )
            is None
        )

    def test_ordinary_publication_orders_are_not_postponements(self):
        assert (
            find_postponement_clause(
                [
                    "Artículo 2.- Disponer la publicación de la presente Resolución "
                    "Ministerial en el portal institucional del Ministerio."
                ]
            )
            is None
        )
