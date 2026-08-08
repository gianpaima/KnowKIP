"""La regla que determina el inicio de efectos: qué decide y qué se niega a decidir."""

from __future__ import annotations

from datetime import date

import pytest

from kipu_knowledge.domain.enums import DateStatus, EventType, SourceAuthority
from kipu_knowledge.domain.legal_effect import (
    BASIS_APPOINTMENT,
    BASIS_TERMINATION,
    RULE_VERSION,
    DeferralClause,
    DeferralKind,
    LegalEffectOutcome,
    determine_legal_effect,
    find_deferral_clause,
)

PUBLISHED = date(2026, 8, 6)


def _verdict(**overrides):
    kwargs = {
        "event_type": EventType.DESIGNATION,
        "stated_status": DateStatus.NOT_STATED,
        "published_on": PUBLISHED,
        "source_authority": SourceAuthority.OFFICIAL_GAZETTE,
        "deferral": None,
    }
    kwargs.update(overrides)
    return determine_legal_effect(**kwargs)


def _deferral(kind, text="cláusula de prueba"):
    return DeferralClause(kind=kind, text=text)


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

    def test_an_indeterminate_deferral_hands_the_case_back(self):
        """Es la excepción que el propio artículo 6 reserva; decide un humano."""
        verdict = _verdict(
            deferral=_deferral(
                DeferralKind.INDETERMINATE,
                "La presente resolución entrará en vigencia con la instalación del Directorio.",
            )
        )
        assert verdict.outcome == LegalEffectOutcome.VETOED
        assert "no permiten fechar" in verdict.rationale
        assert verdict.as_dict()["deferral_clause"]
        assert verdict.as_dict()["deferral_kind"] == "INDETERMINATE"


class TestDeferralToTheDayAfterPublication:
    """ADR-0009: la cláusula que ata los efectos al día siguiente de la
    publicación no deja la fecha indeterminada, la fija con una suma."""

    def test_the_date_is_the_day_after_publication(self):
        verdict = _verdict(deferral=_deferral(DeferralKind.DAY_AFTER_PUBLICATION))
        assert verdict.outcome == LegalEffectOutcome.DETERMINED
        assert verdict.value == date(2026, 8, 7)  # publicada el 6

    def test_it_cites_both_the_clause_and_the_norm_that_allows_it(self):
        clause = (
            "Artículo 2.- La acción de personal dispuesta en el artículo 1 precedente, "
            "tendrá efectividad a partir del día siguiente de la publicación de la "
            "presente resolución en el Diario Oficial El Peruano."
        )
        verdict = _verdict(deferral=_deferral(DeferralKind.DAY_AFTER_PUBLICATION, clause))
        # La norma catalogada no cambia: es la que faculta al acto a disponer en
        # contrario. Lo que se añade es la cláusula con la que lo dispuso.
        assert verdict.basis is BASIS_APPOINTMENT
        assert clause in verdict.rationale
        payload = verdict.as_dict()
        assert payload["deferral_kind"] == "DAY_AFTER_PUBLICATION"
        assert payload["legal_effect_from"] == "2026-08-07"
        assert payload["status"] == "DERIVED"

    def test_the_vetoes_still_come_first(self):
        """Una cláusula calculable no salva un acto sin publicación oficial: sin
        publicación en El Peruano no hay día siguiente al que referirse."""
        verdict = _verdict(
            deferral=_deferral(DeferralKind.DAY_AFTER_PUBLICATION),
            source_authority=SourceAuthority.ISSUING_ENTITY,
        )
        assert verdict.outcome == LegalEffectOutcome.VETOED

    def test_an_uncatalogued_event_type_is_still_not_determined(self):
        verdict = _verdict(
            deferral=_deferral(DeferralKind.DAY_AFTER_PUBLICATION),
            event_type=EventType.DELEGATION,
        )
        assert verdict.outcome == LegalEffectOutcome.NOT_APPLICABLE


class TestDeferralClassification:
    """Qué formas se saben computar y cuáles no. La tabla es el contrato: lo que
    no está aquí no se calcula."""

    @pytest.mark.parametrize(
        "text",
        [
            "Artículo 2.- La acción de personal dispuesta en el artículo 1 precedente, "
            "tendrá efectividad a partir del día siguiente de la publicación de la "
            "presente resolución en el Diario Oficial El Peruano.",
            "Artículo 3.- La designación surte efectos a partir del día siguiente de su "
            "publicación.",
            "Artículo 3.- La presente resolución rige a partir del día siguiente al de su "
            "publicación en el Diario Oficial El Peruano.",
            "Artículo 3.- La presente entra en vigencia el día siguiente de la publicación.",
        ],
    )
    def test_day_after_publication_is_computable(self, text):
        clause = find_deferral_clause([text])
        assert clause is not None, text
        assert clause.kind == DeferralKind.DAY_AFTER_PUBLICATION
        assert clause.computable

    @pytest.mark.parametrize(
        "text",
        [
            # Fecha futura fija: la regla general no aplica y el extractor no la
            # recogió como efectiva, así que decide un humano.
            "Artículo 3.- La presente Resolución Ministerial entrará en vigencia el 15 de "
            "agosto de 2026.",
            "Artículo 3.- Se dispone postergar su vigencia hasta la instalación del Directorio.",
            # El día HÁBIL siguiente exige un calendario de feriados que este
            # sistema no tiene: computarlo sería inventar la fecha.
            "Artículo 3.- La presente resolución rige a partir del día hábil siguiente de su "
            "publicación en el Diario Oficial El Peruano.",
            # Sin ancla no hay nada que sumar: ¿siguiente a qué?
            "Artículo 3.- La designación surte efectos a partir del día siguiente.",
        ],
    )
    def test_the_rest_is_indeterminate(self, text):
        clause = find_deferral_clause([text])
        assert clause is not None, text
        assert clause.kind == DeferralKind.INDETERMINATE
        assert not clause.computable

    def test_the_business_day_wins_over_the_calendar_day(self):
        """Regresión: «día hábil siguiente ... publicación» contiene la forma
        calculable dentro. Si el patrón calculable se mirara primero, el sistema
        sumaría un día natural donde la norma manda contar hábiles."""
        clause = find_deferral_clause(
            ["La presente rige desde el día hábil siguiente de su publicación."]
        )
        assert clause is not None
        assert clause.kind == DeferralKind.INDETERMINATE

    def test_an_indeterminate_clause_anywhere_wins_over_a_computable_one(self):
        """Ante un acto que dijera las dos cosas, devolverlo al humano es más
        conservador que quedarse con la mitad que se sabe sumar."""
        clause = find_deferral_clause(
            [
                "Artículo 3.- Rige desde el día siguiente de su publicación.",
                "Artículo 4.- Se posterga su vigencia hasta la instalación del Directorio.",
            ]
        )
        assert clause is not None
        # Gana el primero encontrado recorriendo la parte resolutiva en orden;
        # dentro de una misma sección gana siempre el indeterminado.
        combined = find_deferral_clause(
            [
                "Artículo 3.- Rige desde el día siguiente de su publicación, "
                "sin perjuicio de postergar su vigencia hasta la instalación del Directorio."
            ]
        )
        assert combined is not None
        assert combined.kind == DeferralKind.INDETERMINATE

    def test_returns_the_whole_sentence_so_a_reviewer_can_read_it(self):
        clause = find_deferral_clause(
            [
                "Artículo 2.- Notifíquese la presente. Artículo 3.- La presente "
                "resolución entrará en vigencia el 15 de agosto de 2026. Artículo 4.- "
                "Publíquese."
            ]
        )
        assert clause is not None
        assert clause.text.startswith("Artículo 3.-")
        assert clause.text.endswith("2026.")

    def test_it_says_which_dispositive_section_the_clause_came_from(self):
        """El índice es lo que permite anclar la cita sin volver a buscarla."""
        clause = find_deferral_clause(
            [
                "Artículo 1.- Designar a ...",
                "Artículo 2.- Notifíquese.",
                "Artículo 3.- Rige desde el día siguiente de su publicación.",
            ]
        )
        assert clause is not None
        assert clause.source_index == 2

    def test_an_explicit_date_is_not_a_deferral(self):
        """«a partir del 30 de julio de 2026» es una fecha expresa que el extractor
        ya recoge como EXPLICIT; confundirla con un diferimiento desplazaría la
        regla justo donde no hace falta."""
        assert (
            find_deferral_clause(
                [
                    "Artículo Único.- ENCARGAR, con eficacia anticipada a partir del 30 de "
                    "julio de 2026, las funciones..."
                ]
            )
            is None
        )

    def test_ordinary_publication_orders_are_not_deferrals(self):
        assert (
            find_deferral_clause(
                [
                    "Artículo 2.- Disponer la publicación de la presente Resolución "
                    "Ministerial en el portal institucional del Ministerio."
                ]
            )
            is None
        )


class TestDispositiveSectionsCoverTheWholeResolutivePart:
    """Regresión: la cláusula que veta la regla puede vivir en un cuerpo aparte.

    Desde que un artículo puede tener el encabezado en una sección
    ("Artículo 3.- Vigencia") y lo que dispone en la siguiente, mirar solo
    ARTICLE dejaba fuera justo el texto que posterga la vigencia. Un veto que
    no se ve no es conservador: fija una fecha que la fuente contradice.
    """

    def test_article_bodies_and_table_rows_are_dispositive(self):
        from kipu_knowledge.application.legal_effect import _DISPOSITIVE
        from kipu_knowledge.domain.enums import SectionType

        assert SectionType.ARTICLE in _DISPOSITIVE
        assert SectionType.ARTICLE_BODY in _DISPOSITIVE
        assert SectionType.ARTICLE_LIST_ITEM in _DISPOSITIVE
        assert SectionType.ARTICLE_TABLE_ROW in _DISPOSITIVE

    def test_the_clause_is_found_wherever_the_resolutive_part_puts_it(self):
        body = (
            "Las acciones de personal dispuestas en los artículos 1 y 2 precedentes, "
            "tendrán efectividad a partir del día siguiente de la publicación de la "
            "presente resolución en el diario oficial El Peruano."
        )
        # Da igual que llegue como encabezado de artículo o como su cuerpo: lo
        # que se examina es el texto de la parte resolutiva.
        clause = find_deferral_clause(["Artículo 3.- Vigencia", body])
        assert clause is not None
        assert clause.kind == DeferralKind.DAY_AFTER_PUBLICATION
        assert clause.source_index == 1
