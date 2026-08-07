"""Catálogo de oficios unipersonales del Estado peruano.

Un *oficio unipersonal* es un cargo del que existe, por construcción normativa, un
solo titular a la vez en todo el país: la Presidencia de la República, la
Presidencia del Consejo de Ministros, cada cartera ministerial. Sirve como señal
corroborante independiente del nombre para la resolución de identidad: dos
menciones con el mismo nombre normalizado *y* el mismo oficio unipersonal no
pueden ser dos personas distintas salvo que dos homónimos exactos hayan ocupado
el mismo cargo único, lo que se descarta por improbable.

Todo lo que no coincide con estos patrones es NO unipersonal por defecto. Esa
asimetría es deliberada: "Jefe Institucional" aparece como capacidad de firma en
las resoluciones y lo tiene cualquier organismo público, así que dos personas
distintas pueden firmar con esa misma etiqueta sin ninguna contradicción. Ante
la duda, el caso va a revisión humana (regla 13).

No existe una fuente oficial legible por máquina de la estructura del Estado, así
que este catálogo se mantiene a mano y se versiona con la ontología
(ver ontology/CHANGELOG.md). Los patrones se aplican sobre etiquetas ya
normalizadas con ``normalize_position_label`` (mayúsculas, sin tildes) y toleran
la variante de género, que la normalización conserva a propósito.
"""

from __future__ import annotations

import re

# Artículos iniciales de una cartera ministerial: "DE LA PRODUCCION" y
# "DE PRODUCCION" nombran el mismo ministerio.
_LEADING_ARTICLE_RE = re.compile(r"^(?:LA|EL|LOS|LAS)\s+")

# Cada patrón produce una clave de oficio estable. El grupo `cartera`, cuando
# existe, distingue ministerios entre sí (hay un Ministro de Defensa y un
# Ministro de Economía a la vez; no hay dos de Defensa).
_SINGULAR_OFFICE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^PRESIDENT[EA] DE LA REPUBLICA$"), "PRESIDENCIA_DE_LA_REPUBLICA"),
    (
        re.compile(r"^PRESIDENT[EA] DEL CONSEJO DE MINISTROS$"),
        "PRESIDENCIA_DEL_CONSEJO_DE_MINISTROS",
    ),
    (
        re.compile(
            r"^MINISTR[OA] DE(?:L)?\s+(?:ESTADO EN EL DESPACHO DE(?:L)?\s+)?(?P<cartera>.+)$"
        ),
        "MINISTERIO",
    ),
    (re.compile(r"^CONTRALOR(?:A)? GENERAL DE LA REPUBLICA$"), "CONTRALORIA_GENERAL"),
    (re.compile(r"^DEFENSOR(?:A)? DEL PUEBLO$"), "DEFENSORIA_DEL_PUEBLO"),
    (re.compile(r"^FISCAL DE LA NACION$"), "FISCALIA_DE_LA_NACION"),
)


def singular_office(role_context_normalized: str | None) -> str | None:
    """Clave del oficio unipersonal de una etiqueta de cargo, o None.

    Devolver None significa "no consta que sea unipersonal", nunca "no existe":
    es la respuesta segura, porque solo habilita el camino de revisión humana.

    >>> singular_office("PRESIDENTA DE LA REPUBLICA")
    'PRESIDENCIA_DE_LA_REPUBLICA'
    >>> singular_office("MINISTRO DE LA PRODUCCION")
    'MINISTERIO::PRODUCCION'
    >>> singular_office("JEFE INSTITUCIONAL") is None
    True
    """
    if not role_context_normalized:
        return None
    label = role_context_normalized.strip()
    for pattern, key in _SINGULAR_OFFICE_PATTERNS:
        match = pattern.match(label)
        if match is None:
            continue
        if "cartera" not in (match.groupdict() or {}):
            return key
        cartera = _LEADING_ARTICLE_RE.sub("", match.group("cartera").strip())
        if not cartera:
            return None
        return f"{key}::{cartera}"
    return None
