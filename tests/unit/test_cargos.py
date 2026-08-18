"""Nomenclatura de cargos estructurales del clasificador: reconocimiento, no afirmación."""

from __future__ import annotations

from kipu_knowledge.domain.cargos import structural_cargo
from kipu_knowledge.domain.normalization import normalize_position_label


def test_reconoce_sistema_administrativo_con_nivel():
    cargo = structural_cargo(normalize_position_label("Directora del Sistema Administrativo II"))
    assert cargo is not None
    assert cargo.family == "SISTEMA ADMINISTRATIVO"
    assert cargo.level == "II"


def test_reconoce_programa_sectorial_y_variante_masculina():
    cargo = structural_cargo(normalize_position_label("Director de Programa Sectorial IV"))
    assert cargo is not None
    assert cargo.family == "PROGRAMA SECTORIAL"
    assert cargo.level == "IV"


def test_sin_nivel_romano_no_es_cargo_del_clasificador():
    assert structural_cargo(normalize_position_label("Director del Sistema Administrativo")) is None


def test_una_jefatura_comun_no_se_reconoce():
    assert structural_cargo(normalize_position_label("Jefa de la Oficina de Presupuesto")) is None
    assert structural_cargo(None) is None
