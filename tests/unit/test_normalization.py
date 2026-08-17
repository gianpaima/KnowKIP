from datetime import date

import pytest

from kipu_knowledge.domain.normalization import (
    collapse_whitespace,
    normalize_document_number,
    normalize_org_name,
    normalize_person_name,
    normalize_position_label,
    org_name_contamination,
    parse_ddmmyyyy,
    parse_issue_line,
    parse_spanish_date,
    person_name_is_variant,
    strip_accents,
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


# ---------------------------------------------------------------------------
# Orden registral "APELLIDOS, NOMBRES"
# ---------------------------------------------------------------------------


def test_registral_order_is_reordered_to_the_corpus_order():
    """Las tablas de designación colectiva escriben "APELLIDOS, NOMBRES".

    El resto del corpus escribe "NOMBRES APELLIDOS", y el detector de variantes
    asume ese orden. Sin reordenar, la misma persona nombrada en una tabla y en
    un párrafo produce dos grafías que no se encuentran nunca.
    """
    assert normalize_person_name("YORGES AVALOS, DANTE AARON") == "DANTE AARON YORGES AVALOS"
    assert (
        normalize_person_name("PANTOJA URIZAR GARFIAS, ANA TERESA")
        == "ANA TERESA PANTOJA URIZAR GARFIAS"
    )


def test_reordering_does_not_claim_identity():
    """Reordenar decide qué se compara, nunca qué se fusiona.

    Dos grafías que coinciden tras normalizar siguen siendo menciones distintas
    hasta que una señal independiente del nombre las vincule (regla 13).
    """
    assert normalize_person_name("YORGES AVALOS, DANTE AARON") == normalize_person_name(
        "Dante Aarón Yorges Ávalos"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "JUAN PEREZ,",  # coma suelta al final
        ", ANA TERESA",  # sin apellidos
        "PEREZ, VEGA, JORGE",  # más de dos partes
        "UNA CADENA MUY LARGA, QUE NO ES UN NOMBRE DE PERSONA SINO OTRA COSA ENTERA",
    ],
)
def test_what_does_not_look_like_a_split_name_is_left_alone(raw: str):
    """Ante la duda no se reordena: una grafía inventada no la encuentra nadie."""
    assert normalize_person_name(raw) == collapse_whitespace(strip_accents(raw)).upper()


class TestOrgNameContamination:
    """El guard que impide que un nombre con coletilla pase en silencio."""

    @pytest.mark.parametrize(
        ("raw", "fragment"),
        [
            (
                "Ministerio de Vivienda, Construcción y Saneamiento, "
                "bajo el régimen de la Ley N° 30057, Ley del Servicio Civil",
                "BAJO EL REGIMEN",
            ),
            (
                "Ministerio de Energía y Minas, puesto considerado de confianza",
                "PUESTO CONSIDERADO DE CONFIANZA",
            ),
            ("OSINFOR, con código de puesto DP00102032", "CODIGO DE PUESTO"),
            (
                "Ministerio de Defensa - Directora de Sistema Administrativo II",
                "DE SISTEMA ADMINISTRATIVO",
            ),
        ],
    )
    def test_detects_administrative_clauses(self, raw: str, fragment: str):
        assert org_name_contamination(normalize_org_name(raw)) == fragment

    @pytest.mark.parametrize(
        "raw",
        [
            "Ministerio de Vivienda, Construcción y Saneamiento",
            "Organismo de Formalización de la Propiedad Informal – COFOPRI",
            "Superintendencia Nacional de Aduanas y de Administración Tributaria",
        ],
    )
    def test_clean_names_pass(self, raw: str):
        assert org_name_contamination(normalize_org_name(raw)) is None
