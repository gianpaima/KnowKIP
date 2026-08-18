"""Nomenclatura de cargos estructurales del Clasificador de Cargos.

Las entidades peruanas designan por el *cargo estructural* de su Clasificador de
Cargos / CAP ("Director de Sistema Administrativo II de la Unidad de Recursos
Humanos…"), no por el puesto funcional con que la propia entidad presenta a la
persona ("Directora de la Unidad de Recursos Humanos"). Son dos nombres del
mismo hecho: el cargo estructural con nivel romano es la denominación del
clasificador, y la unidad de la ruta dice qué jefatura ejerce.

Reconocer la nomenclatura sirve solo para explicárselo al revisor: no cambia la
identidad del puesto ni produce afirmaciones. El repertorio es el clásico del
clasificador (Sistema Administrativo / Programa Sectorial con nivel romano),
el mismo que ya recorta `patterns._STRUCTURAL_POSITION_RE` cuando viaja pegado
tras el nombre de la entidad.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Sobre etiquetas ya normalizadas (mayúsculas, sin tildes). El nivel romano es
# obligatorio: "Director del Sistema Administrativo" sin nivel no es la
# denominación del clasificador sino una jefatura descrita en prosa.
_STRUCTURAL_LABEL_RE = re.compile(
    r"^(?:DIRECTOR(?:A)?|JEF[EA]|ASESOR(?:A)?|ESPECIALISTA|EJECUTIV[OA]|GERENTE)\s+"
    r"DE(?:L)?\s+(?P<family>SISTEMA\s+ADMINISTRATIVO|PROGRAMA\s+SECTORIAL)\s+"
    r"(?P<level>[IVXL]+)\b"
)


@dataclass(frozen=True)
class StructuralCargo:
    family: str  # "SISTEMA ADMINISTRATIVO" | "PROGRAMA SECTORIAL"
    level: str  # nivel romano tal como lo declara la etiqueta


def structural_cargo(label_normalized: str | None) -> StructuralCargo | None:
    """Cargo estructural del clasificador si la etiqueta empieza por uno.

    >>> structural_cargo("DIRECTORA DEL SISTEMA ADMINISTRATIVO II").level
    'II'
    >>> structural_cargo("MINISTRO DE EDUCACION") is None
    True
    """
    if not label_normalized:
        return None
    match = _STRUCTURAL_LABEL_RE.match(label_normalized.strip())
    if match is None:
        return None
    family = re.sub(r"\s+", " ", match.group("family"))
    return StructuralCargo(family=family, level=match.group("level"))
