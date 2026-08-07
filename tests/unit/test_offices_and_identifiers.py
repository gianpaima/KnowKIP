"""Catálogo de oficios unipersonales y extracción de documentos de identidad.

Ambos son las señales que autorizan vincular sin revisión humana, así que lo que
importa es tanto lo que reconocen como lo que deliberadamente no reconocen.
"""

from __future__ import annotations

from kipu_knowledge.adapters.extraction.patterns import (
    extract_person_identifiers,
    identifiers_for_name,
)
from kipu_knowledge.domain.normalization import normalize_identifier, normalize_position_label
from kipu_knowledge.domain.offices import singular_office


class TestSingularOffices:
    def test_constitutional_offices(self):
        assert singular_office("PRESIDENTA DE LA REPUBLICA") == "PRESIDENCIA_DE_LA_REPUBLICA"
        assert singular_office("PRESIDENTE DE LA REPUBLICA") == "PRESIDENCIA_DE_LA_REPUBLICA"
        assert (
            singular_office("PRESIDENTE DEL CONSEJO DE MINISTROS")
            == "PRESIDENCIA_DEL_CONSEJO_DE_MINISTROS"
        )

    def test_gender_variants_share_the_office(self):
        assert singular_office("MINISTRO DE ECONOMIA Y FINANZAS") == singular_office(
            "MINISTRA DE ECONOMIA Y FINANZAS"
        )
        assert singular_office("PRESIDENTE DE LA REPUBLICA") == singular_office(
            "PRESIDENTA DE LA REPUBLICA"
        )

    def test_article_and_de_del_variants_share_the_office(self):
        # "de la Producción" y "de Producción" nombran la misma cartera.
        assert singular_office("MINISTRO DE LA PRODUCCION") == singular_office(
            "MINISTRO DE PRODUCCION"
        )
        assert singular_office("MINISTRO DEL INTERIOR") == "MINISTERIO::INTERIOR"

    def test_despacho_form_is_the_same_office(self):
        assert singular_office("MINISTRO DE ESTADO EN EL DESPACHO DE DEFENSA") == singular_office(
            "MINISTRO DE DEFENSA"
        )

    def test_different_portfolios_are_different_offices(self):
        assert singular_office("MINISTRO DE DEFENSA") != singular_office("MINISTRO DE SALUD")

    def test_generic_roles_are_not_singular(self):
        """Lo que decide que el sistema pregunte en vez de fusionar."""
        for label in (
            "JEFE INSTITUCIONAL",
            "INTENDENTE NACIONAL, INTENDENCIA NACIONAL DE BOMBEROS DEL PERU",
            "JEFE DE LA OFICINA DE ADMINISTRACION",
            "GERENTA GENERAL",
            "MIEMBROS DEL DIRECTORIO DEL BANCO CENTRAL DE RESERVA DEL PERU",
            "MINISTRO CONSEJERO EN EL SERVICIO DIPLOMATICO",
            "",
        ):
            assert singular_office(label) is None, label

    def test_none_role_is_not_singular(self):
        assert singular_office(None) is None

    def test_catalog_consumes_normalized_labels(self):
        # El catálogo se aplica sobre la salida de normalize_position_label.
        assert singular_office(normalize_position_label("Presidenta de la República")) is not None
        assert singular_office(normalize_position_label("Ministro de Economía y Finanzas"))


class TestIdentifierExtraction:
    def test_labelled_dni_forms(self):
        for text in (
            "identificado con DNI N° 09342789, en el cargo",
            "D.N.I. Nº 09342789",
            "Documento Nacional de Identidad N 09342789",
        ):
            assert extract_person_identifiers(text)[0][:2] == ("DNI", "09342789"), text

    def test_bare_numbers_are_never_identifiers(self):
        """Confundir un correlativo con un DNI fabricaría una identidad."""
        assert extract_person_identifiers("Resolución Ministerial N° 00284-2026 de fecha") == []
        assert extract_person_identifiers("número correlativo 09342789") == []

    def test_wrong_dni_length_is_rejected(self):
        assert extract_person_identifiers("DNI N° 1234567") == []
        assert extract_person_identifiers("DNI N° 123456789") == []

    def test_carne_de_extranjeria(self):
        found = extract_person_identifiers("con Carné de Extranjería N° 001234567")
        assert found[0][:2] == ("CARNE_EXTRANJERIA", "001234567")

    def test_attribution_to_the_named_person(self):
        text = "Designar a JUAN PEREZ GARCIA, identificado con DNI N° 09342789, en el cargo."
        assert identifiers_for_name(text, "JUAN PEREZ GARCIA")[0][1] == "09342789"

    def test_collective_article_attributes_to_the_nearest_name_only(self):
        """En un artículo con varias personas, el documento va a quien lo declara."""
        text = (
            "Designar a JUAN PEREZ GARCIA y a MARIA LOPEZ SILVA, "
            "identificada con DNI N° 11223344, como miembros."
        )
        assert identifiers_for_name(text, "JUAN PEREZ GARCIA") == []
        assert identifiers_for_name(text, "MARIA LOPEZ SILVA")[0][1] == "11223344"

    def test_distant_identifier_is_not_attributed(self):
        text = "Designar a JUAN PEREZ GARCIA en el cargo. " + "x" * 200 + " DNI N° 09342789"
        assert identifiers_for_name(text, "JUAN PEREZ GARCIA") == []

    def test_absent_name_yields_nothing(self):
        text = "Designar a JUAN PEREZ GARCIA, con DNI N° 09342789."
        assert identifiers_for_name(text, "OTRO NOMBRE APELLIDO") == []

    def test_offsets_locate_the_literal_quote(self):
        text = "Designar a JUAN PEREZ GARCIA, con DNI N° 09342789, en el cargo."
        _, value, start, end = identifiers_for_name(text, "JUAN PEREZ GARCIA")[0]
        assert value in text[start:end]


class TestIdentifierNormalization:
    def test_separators_are_dropped(self):
        assert normalize_identifier("09.342.789") == "09342789"
        assert normalize_identifier("09 342 789") == "09342789"
        assert normalize_identifier("00-1234567") == "001234567"

    def test_leading_zeros_are_preserved(self):
        # Rellenar o recortar el valor declarado sería inventar un identificador.
        assert normalize_identifier("09342789") == "09342789"
        assert normalize_identifier("9342789") == "9342789"

    def test_case_is_folded_for_alphanumeric_schemes(self):
        assert normalize_identifier("ab123456") == "AB123456"
