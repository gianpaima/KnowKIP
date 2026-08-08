"""Qué dispositivos del índice diario entran en el alcance del sistema.

El diario oficial publica de todo: convenios, tarifarios, concesiones,
autorizaciones de viaje. El MVP solo entiende actos de personal, así que ingerir
la edición completa llenaría la base de documentos que no producen ninguna
afirmación. Este filtro decide sobre la **sumilla del catálogo** —lo que el
buscador escribe de cada norma—, nunca sobre el texto del documento, que aún no
se ha capturado cuando hay que decidir.

Tres cosas lo hacen defendible:

1. Es determinista y citable: dos catálogos explícitos de verbos, sin
   puntuaciones ni umbrales. La versión de la regla se guarda con cada decisión.
2. Lo desconocido se ingiere. Solo se descarta lo que empieza por un verbo del
   catálogo negativo; cualquier otra cosa queda UNDECIDED y entra igual. Un
   catálogo incompleto cuesta trabajo de más, nunca un documento perdido.
3. Nada se descarta en silencio: cada dispositivo visto queda registrado en
   `crawl_item` con su veredicto y su motivo, aunque no se ingiera. Recuperarlo
   después es ingerir su código, no volver a descubrir el día.

El catálogo positivo es el reverso de los verbos que el extractor sabe leer
(`adapters/extraction/patterns.py`), en la tercera persona del plural con que el
buscador redacta las sumillas ("Designan…" frente a "DESIGNAR…" del artículo).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from kipu_knowledge.domain.enums import Relevance
from kipu_knowledge.domain.normalization import collapse_whitespace, strip_accents

RULE_VERSION = "personnel-relevance/1.0"

# Actos de personal: si alguno aparece en cualquier parte de la sumilla, entra.
# Se buscan en cualquier posición a propósito, porque hay sumillas compuestas
# ("Dejan sin efecto designaciones y designan fedatarios institucionales…").
_PERSONNEL_MARKERS: tuple[tuple[str, str], ...] = (
    (r"\bDESIGNAN\b", "designación"),
    (r"\bDESIGNASE\b", "designación"),
    (r"\bNOMBRAN\b", "nombramiento"),
    (r"\bNOMBRASE\b", "nombramiento"),
    (r"\bENCARGAN\b", "encargo de funciones"),
    (r"\bACEPTAN\s+(?:LA\s+)?RENUNCIA\b", "aceptación de renuncia"),
    (r"\bDAN\s+POR\s+CONCLUID[AO]\b", "conclusión de designación o encargo"),
    (r"\bDEJAN\s+SIN\s+EFECTO\s+(?:LA\s+)?DESIGNACION(?:ES)?\b", "cese de designación"),
    (r"\bDECLARAN\s+VACANCIA\b", "vacancia del cargo"),
)

# Verbos con los que el buscador encabeza actos que no son de personal. Solo
# cuentan al principio de la sumilla: "Aprueban" abriendo la frase es un acto
# aprobatorio; en medio puede ser cualquier cosa.
_NON_PERSONNEL_PREFIXES: tuple[tuple[str, str], ...] = (
    (r"^APRUEBAN\b", "acto aprobatorio"),
    (r"^AUTORIZAN\b", "autorización (viajes, actos administrativos)"),
    (r"^RECTIFICAN\b", "rectificación de otra resolución"),
    (r"^MODIFICAN\b", "modificación de otra norma o contrato"),
    (r"^PRORROGAN\b", "prórroga de plazos o vigencias"),
    (r"^DISPONEN\s+LA\s+(?:NOTIFICACION|PUBLICACION)\b", "trámite de notificación o publicación"),
)

_COMPILED_PERSONNEL = tuple((re.compile(p), why) for p, why in _PERSONNEL_MARKERS)
_COMPILED_NON_PERSONNEL = tuple((re.compile(p), why) for p, why in _NON_PERSONNEL_PREFIXES)


@dataclass(frozen=True)
class RelevanceVerdict:
    relevance: Relevance
    rule: str
    rationale: str
    matched_text: str | None = None

    @property
    def should_ingest(self) -> bool:
        """Lo desconocido se ingiere: solo el catálogo negativo excluye."""
        return self.relevance is not Relevance.NOT_RELEVANT


def normalize_summary(summary: str) -> str:
    return collapse_whitespace(strip_accents(summary)).upper()


def classify_summary(summary: str | None) -> RelevanceVerdict:
    """Veredicto sobre la sumilla que el índice declara para un dispositivo."""
    if not summary or not summary.strip():
        return RelevanceVerdict(
            Relevance.UNDECIDED,
            RULE_VERSION,
            "el índice no declara sumilla para este dispositivo; se ingiere para "
            "decidir sobre el texto capturado",
        )
    normalized = normalize_summary(summary)

    for pattern, why in _COMPILED_PERSONNEL:
        match = pattern.search(normalized)
        if match:
            return RelevanceVerdict(
                Relevance.RELEVANT,
                RULE_VERSION,
                f"la sumilla declara un acto de personal ({why})",
                matched_text=match.group(0),
            )

    for pattern, why in _COMPILED_NON_PERSONNEL:
        match = pattern.match(normalized)
        if match:
            return RelevanceVerdict(
                Relevance.NOT_RELEVANT,
                RULE_VERSION,
                f"la sumilla encabeza con un verbo no personal ({why}) y no menciona "
                f"ningún acto de personal",
                matched_text=match.group(0),
            )

    return RelevanceVerdict(
        Relevance.UNDECIDED,
        RULE_VERSION,
        "la sumilla no encaja en ningún catálogo conocido; se ingiere y decide el "
        "extractor sobre el texto",
    )
