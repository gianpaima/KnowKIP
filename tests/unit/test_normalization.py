from datetime import date

from kipu_knowledge.domain.normalization import (
    normalize_document_number,
    normalize_org_name,
    normalize_person_name,
    normalize_position_label,
    parse_ddmmyyyy,
    parse_issue_line,
    parse_spanish_date,
    person_name_is_variant,
)


class TestDocumentNumber:
    def test_strips_prefix_and_uppercases(self):
        assert normalize_document_number("Nº D000284-2026-MIDAGRI-DM") == "D000284-2026-MIDAGRI-DM"
        assert normalize_document_number("nº 027-2026-ef") == "027-2026-EF"
        assert normalize_document_number("N° 053-2026-DE") == "053-2026-DE"

    def test_no_prefix(self):
        assert normalize_document_number("000066-2026-BNP") == "000066-2026-BNP"


class TestDates:
    def test_spanish_date_variants(self):
        assert parse_spanish_date("5 de agosto del 2026") == date(2026, 8, 5)
        assert parse_spanish_date("05 de agosto de 2026") == date(2026, 8, 5)
        assert parse_spanish_date("30 de julio de 2026") == date(2026, 7, 30)
        assert parse_spanish_date("1 de setiembre de 2025") == date(2025, 9, 1)

    def test_no_date_returns_none(self):
        # Regla: nunca inventar fechas.
        assert parse_spanish_date("en el cargo de Jefe") is None
        assert parse_spanish_date("") is None

    def test_invalid_date_returns_none(self):
        assert parse_spanish_date("32 de agosto de 2026") is None

    def test_issue_line(self):
        place, dt = parse_issue_line("Jesús María, 5 de agosto del 2026")
        assert place == "Jesús María"
        assert dt == date(2026, 8, 5)

    def test_ddmmyyyy(self):
        assert parse_ddmmyyyy("Fecha de publicación: 06/08/2026") == date(2026, 8, 6)


class TestNames:
    def test_person_name_normalization(self):
        assert normalize_person_name("Inés  Marylin Choy Chong") == "INES MARYLIN CHOY CHONG"

    def test_same_name_still_distinct_entities(self):
        # La normalización NO implica identidad (regla 13); solo compara formas.
        a = normalize_person_name("CARLOS MANUEL YAÑEZ LAZO")
        b = normalize_person_name("Carlos Manuel Yañez Lazo")
        assert a == b  # misma forma ≠ misma persona; la identidad la decide revisión


class TestOrgAndPosition:
    def test_hyphen_spacing_variants_collapse(self):
        base = "Centro Nacional de Estimación, Prevención y Reducción del Riesgo de Desastres"
        a = normalize_org_name(f"{base} -CENEPRED")
        b = normalize_org_name(f"{base} - CENEPRED")
        assert a == b

    def test_position_label_keeps_gender(self):
        # No se fusiona Jefa/Jefe a nivel de etiqueta; la identidad de puesto usa además org+unidad.
        assert normalize_position_label("Jefa de Presupuesto") != normalize_position_label(
            "Jefe de Presupuesto"
        )


class TestPersonNameVariants:
    """Detección conservadora de grafías del mismo nombre (regla 13: solo señala)."""

    def test_omitted_given_name_is_variant(self):
        assert person_name_is_variant("ELMER CUBA BUSTINZA", "ELMER RAFAEL CUBA BUSTINZA")
        assert person_name_is_variant("ELMER RAFAEL CUBA BUSTINZA", "ELMER CUBA BUSTINZA")

    def test_identical_names_are_not_variants(self):
        # Coinciden de forma exacta: los resuelve la vía de homónimos, no esta.
        assert not person_name_is_variant("ELMER CUBA BUSTINZA", "ELMER CUBA BUSTINZA")

    def test_different_surnames_are_not_variants(self):
        assert not person_name_is_variant(
            "JUAN CARLOS MORALES CARPIO", "JUAN CARLOS REQUEJO ALEMAN"
        )
        assert not person_name_is_variant("ELMER CUBA BUSTINZA", "ELMER CUBA QUISPE")

    def test_disjoint_given_names_are_not_variants(self):
        # Hermanos: mismos apellidos, nombres de pila sin relación de contención.
        assert not person_name_is_variant("ANA MARIA CUBA BUSTINZA", "ELMER RAFAEL CUBA BUSTINZA")

    def test_surname_particles_are_handled(self):
        assert person_name_is_variant("JUAN DE LA CRUZ PEREZ", "JUAN CARLOS DE LA CRUZ PEREZ")

    def test_two_token_names_are_never_variants(self):
        # Sin los dos apellidos no hay señal suficiente para siquiera preguntar.
        assert not person_name_is_variant("ELMER CUBA", "ELMER RAFAEL CUBA")
