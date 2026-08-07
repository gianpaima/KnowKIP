"""Fecha de inicio de efectos determinada por norma (quinta señal).

Un documento puede no expresar cuándo empieza a surtir efectos lo que dispone y
aun así no dejar la fecha indeterminada: hay una norma que la fija. Para las
resoluciones de designación o nombramiento, el artículo 6 de la Ley N.º 27594
establece que surten efecto **el día** de su publicación en El Peruano, salvo
disposición en contrario que postergue su vigencia.

Esto no es inferir la fecha efectiva de la fecha de publicación —lo que la regla
12 prohíbe— sino aplicar una regla jurídica cuyo supuesto de hecho es verificable
sobre datos que ya están capturados: tipo de acto, publicación en el diario
oficial y ausencia de cláusula que postergue la vigencia. Por eso la fecha
determinada NO se escribe en ``effective_from`` (que sigue diciendo lo que el
documento dice: NOT_STATED), sino en un campo propio con su fundamento citado,
igual que ``application/corroboration.py`` hace con la identidad.

La determinación se veta —y el caso vuelve a revisión humana— cuando:

- la publicación autoritativa no es el diario oficial o no consta su fecha (sin
  publicación en El Peruano la designación carece de efectos jurídicos: no es
  que empiece más tarde, es que no empieza);
- la parte resolutiva contiene una cláusula que posterga la vigencia, que es
  justo la excepción que el artículo 6 reserva.

Señales que se contradicen abren tarea, no eligen.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from kipu_knowledge.domain.enums import DateStatus, EventType, SourceAuthority

RULE_VERSION = "legal-effect-date/1.0"


@dataclass(frozen=True)
class LegalBasis:
    """Norma concreta que fija el inicio de efectos, citable en la afirmación."""

    norm: str
    article: str
    rule_text: str
    # Si el texto es la cita literal del artículo o un resumen redactado por
    # nosotros. Un revisor necesita saber cuál de las dos está leyendo antes de
    # apoyarse en ella.
    quote_kind: str  # "verbatim" | "summary"
    source_url: str

    def as_dict(self) -> dict[str, str]:
        return {
            "norm": self.norm,
            "article": self.article,
            "rule_text": self.rule_text,
            "quote_kind": self.quote_kind,
            "source_url": self.source_url,
        }

    @property
    def citation(self) -> str:
        return f"{self.norm}, artículo {self.article}"


BASIS_APPOINTMENT = LegalBasis(
    norm="Ley N.º 27594",
    article="6",
    rule_text=(
        "Todas las Resoluciones de designación o nombramiento de funcionarios en cargos "
        "de confianza surten efecto a partir del día de su publicación en el Diario "
        "Oficial El Peruano, salvo disposición en contrario de la misma que postergue "
        "su vigencia."
    ),
    quote_kind="verbatim",
    source_url=(
        "https://www2.congreso.gob.pe/sicr/cendocbib/con5_uibd.nsf/"
        "9EB91F501AA35FBA052586DC00538D8A/%24FILE/LEY-27594.pdf"
    ),
)

BASIS_TERMINATION = LegalBasis(
    norm="Reglamento General de la Ley del Servicio Civil (D.S. N.º 040-2014-PCM)",
    article="233.3",
    rule_text=(
        "Para los funcionarios públicos del Gobierno Nacional, tanto la designación como "
        "su término producen efectos desde el día de la publicación en el Diario Oficial "
        "El Peruano, salvo que el propio acto determine una fecha distinta."
    ),
    quote_kind="summary",
    source_url="https://www.gob.pe/institucion/servir/normas-legales/",
)

# Qué norma fija el inicio de efectos de cada tipo de evento. Lo que no está en
# esta tabla no se determina: sigue el camino de siempre (NOT_STATED y, si es un
# inicio, tarea de revisión). Añadir un tipo exige citar la norma que lo cubre.
BASIS_BY_EVENT_TYPE: dict[EventType, LegalBasis] = {
    EventType.DESIGNATION: BASIS_APPOINTMENT,
    EventType.APPOINTMENT: BASIS_APPOINTMENT,
    EventType.ACCEPT_RESIGNATION: BASIS_TERMINATION,
    EventType.END_DESIGNATION: BASIS_TERMINATION,
    EventType.END_ACTING_ASSIGNMENT: BASIS_TERMINATION,
    EventType.TERMINATION: BASIS_TERMINATION,
}


class LegalEffectOutcome(StrEnum):
    DETERMINED = "DETERMINED"
    VETOED = "VETOED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class LegalEffectVerdict:
    outcome: LegalEffectOutcome
    rationale: str
    value: date | None = None
    basis: LegalBasis | None = None
    postponement_clause: str | None = None

    @property
    def determined(self) -> bool:
        return self.outcome == LegalEffectOutcome.DETERMINED

    def as_dict(self) -> dict[str, Any]:
        """Fundamento serializable, tal como se guarda en la afirmación y en la fila."""
        payload: dict[str, Any] = {
            "rule": RULE_VERSION,
            "outcome": str(self.outcome),
            "rationale": self.rationale,
            "method": "determinación normativa",
            "status": str(DateStatus.DERIVED) if self.determined else str(DateStatus.NOT_STATED),
            "legal_effect_from": self.value.isoformat() if self.value else None,
        }
        if self.basis is not None:
            payload["basis"] = self.basis.as_dict()
        if self.postponement_clause:
            payload["postponement_clause"] = self.postponement_clause
        return payload


# Cláusulas que postergan la vigencia del acto y, por tanto, desplazan la regla
# general. Deliberadamente estrechas: solo formas que hablan de la vigencia o de
# los efectos del propio acto. "A partir del 30 de julio de 2026" no entra aquí
# porque eso es una fecha expresa, que el extractor ya recoge como EXPLICIT.
_POSTPONEMENT_RE = re.compile(
    r"(?:"
    r"posterg\w+\s+(?:su\s+)?vigencia"
    r"|entrar?[áa]?\s+en\s+vigencia\s+(?:a\s+partir\s+)?(?:el|del|desde)"
    r"|(?:rige|regir[áa])\s+a\s+partir\s+del?\s+d[íi]a\s+siguiente"
    r"|surt\w+\s+efectos?\s+a\s+partir\s+del?\s+d[íi]a\s+siguiente"
    r"|(?:vigencia|eficacia)\s+a\s+partir\s+del?\s+d[íi]a\s+siguiente"
    r"|a\s+partir\s+del?\s+d[íi]a\s+siguiente\s+de\s+(?:su|la)\s+publicaci[óo]n"
    r")",
    re.IGNORECASE,
)


# Fin de oración: punto seguido de espacio y mayúscula. El punto de "Artículo
# 3.-" no lo es, y cortar ahí devolvía cláusulas que empezaban por "- La...".
_SENTENCE_BREAK_RE = re.compile(r"\.\s+(?=[A-ZÁÉÍÓÚÑ¿«])")


def find_postponement_clause(dispositive_texts: Iterable[str]) -> str | None:
    """Primera cláusula de la parte resolutiva que posterga la vigencia del acto.

    Solo se examina la parte dispositiva: un considerando que discurre sobre la
    retroactividad de los actos administrativos no dispone nada, y tomarlo por
    una postergación vetaría la regla sin motivo.
    """
    for text in dispositive_texts:
        match = _POSTPONEMENT_RE.search(text)
        if not match:
            continue
        # Se devuelve la oración completa: el revisor tiene que poder leer la
        # cláusula, no solo el fragmento que disparó el patrón.
        start = 0
        end = len(text)
        for boundary in _SENTENCE_BREAK_RE.finditer(text):
            if boundary.end() <= match.start():
                start = boundary.end()
            elif boundary.start() >= match.end():
                end = boundary.start() + 1
                break
        return text[start:end].strip()
    return None


def determine_legal_effect(
    *,
    event_type: EventType,
    stated_status: DateStatus,
    published_on: date | None,
    source_authority: SourceAuthority | None,
    postponement_clause: str | None = None,
) -> LegalEffectVerdict:
    """Decide si la norma determina el inicio de efectos de este evento.

    Determinista: mismas entradas, mismo veredicto, re-ejecutable sobre los datos
    congelados (lo verifica ``test_legal_effect_dates_are_re_verifiable``).
    """
    if stated_status != DateStatus.NOT_STATED:
        return LegalEffectVerdict(
            LegalEffectOutcome.NOT_APPLICABLE,
            f"el documento expresa la fecha ({stated_status}); no hay nada que determinar",
        )

    basis = BASIS_BY_EVENT_TYPE.get(event_type)
    if basis is None:
        return LegalEffectVerdict(
            LegalEffectOutcome.NOT_APPLICABLE,
            f"no hay norma catalogada que fije el inicio de efectos de un evento "
            f"{event_type}; la fecha queda no expresada",
        )

    if source_authority != SourceAuthority.OFFICIAL_GAZETTE:
        return LegalEffectVerdict(
            LegalEffectOutcome.VETOED,
            "la publicación autoritativa de este documento no es el diario oficial "
            f"({source_authority or 'sin sistema fuente registrado'}): sin publicación en "
            "El Peruano el acto no produce efectos, y la fecha no puede determinarse "
            f"por {basis.citation}",
            basis=basis,
        )

    if published_on is None:
        return LegalEffectVerdict(
            LegalEffectOutcome.VETOED,
            "la publicación autoritativa no registra fecha de publicación; "
            f"{basis.citation} ata los efectos a ese día y sin él no hay fecha que determinar",
            basis=basis,
        )

    if postponement_clause:
        return LegalEffectVerdict(
            LegalEffectOutcome.VETOED,
            "la parte resolutiva posterga la vigencia del acto "
            f"(«{postponement_clause}»), que es la excepción que {basis.citation} reserva: "
            "la fecha la decide un humano",
            basis=basis,
            postponement_clause=postponement_clause,
        )

    return LegalEffectVerdict(
        LegalEffectOutcome.DETERMINED,
        f"{basis.citation}: los efectos corren desde el día de la publicación en El "
        f"Peruano ({published_on.isoformat()}), no desde el día siguiente ni desde la "
        "fecha de emisión; el documento no posterga la vigencia",
        value=published_on,
        basis=basis,
    )
