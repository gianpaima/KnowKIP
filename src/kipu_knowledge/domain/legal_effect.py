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
- la parte resolutiva difiere la vigencia a un momento que estos datos no
  permiten fechar.

El diferimiento no es una sola cosa (ver ADR-0009). Cuando el acto dice «a
partir del día siguiente de la publicación» está ejerciendo la disposición en
contrario que el propio artículo 6 admite, y fija una fecha **calculable con
exactitud** sobre un dato ya capturado: la publicación más un día. Eso se
determina, citando la cláusula del acto junto a la norma que lo faculta. Cuando
en cambio ata la vigencia a un hecho futuro («hasta la instalación del
Directorio») o al día **hábil** siguiente —cómputo que exige un calendario de
feriados que este sistema no tiene—, no hay nada que calcular y decide un
humano.

Señales que se contradicen abren tarea, no eligen.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from kipu_knowledge.domain.enums import DateStatus, EventType, SourceAuthority

# 1.1 distingue el diferimiento calculable del indeterminado (ADR-0009). Cambia
# respuestas que 1.0 dejaba sin determinar, así que la versión viaja en cada
# afirmación: una fecha guardada dice con qué regla se produjo.
RULE_VERSION = "legal-effect-date/1.1"


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


class DeferralKind(StrEnum):
    """Qué clase de diferimiento dispone el propio acto sobre su vigencia.

    La distinción es la razón de ser de ADR-0009: mezclarlas mandaba a revisión
    humana casos cuya respuesta era una suma.
    """

    # "a partir del día siguiente de la publicación": fecha calculable sobre un
    # dato capturado (publicación + 1 día natural).
    DAY_AFTER_PUBLICATION = "DAY_AFTER_PUBLICATION"
    # Cualquier otro diferimiento: un hecho futuro, o el día *hábil* siguiente,
    # que exige un calendario de feriados que este sistema no tiene.
    INDETERMINATE = "INDETERMINATE"


@dataclass(frozen=True)
class DeferralClause:
    """Cláusula del acto que desplaza la regla general, ya clasificada."""

    kind: DeferralKind
    text: str
    # Posición de la sección dispositiva en la que se encontró, para que la capa
    # de aplicación pueda anclar la cita sin volver a buscarla.
    source_index: int = 0

    @property
    def computable(self) -> bool:
        return self.kind == DeferralKind.DAY_AFTER_PUBLICATION


@dataclass(frozen=True)
class LegalEffectVerdict:
    outcome: LegalEffectOutcome
    rationale: str
    value: date | None = None
    basis: LegalBasis | None = None
    deferral: DeferralClause | None = None

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
        if self.deferral is not None:
            # La cláusula viaja siempre, determine o vete: es lo que un revisor
            # necesita leer para comprobar que la clasificación fue correcta.
            payload["deferral_kind"] = str(self.deferral.kind)
            payload["deferral_clause"] = self.deferral.text
        return payload


# El día *hábil* siguiente no es el día siguiente: computarlo exige el calendario
# de feriados, que este sistema no tiene. Se mira antes que nada para que la
# palabra "hábil" no se pierda dentro del patrón calculable.
_BUSINESS_DAY_RE = re.compile(r"d[íi]a\s+h[áa]bil\s+siguiente", re.IGNORECASE)

# Ancla que vuelve calculable al "día siguiente": la publicación, en cualquiera
# de las formas en que la fuente la escribe ("de la publicación", "al de su
# publicación").
_PUBLICATION_ANCHOR = r"(?:al\s+)?(?:de\s+)?(?:su\s+|la\s+|el\s+)?publicaci[óo]n"

# Diferimiento calculable: el día siguiente, anclado explícitamente a la
# publicación. Sin ese ancla no se calcula nada — "rige a partir del día
# siguiente" a secas no dice siguiente a qué.
_DAY_AFTER_PUBLICATION_RE = re.compile(
    rf"d[íi]a\s+siguiente\s+{_PUBLICATION_ANCHOR}",
    re.IGNORECASE,
)

# El mismo "día siguiente", pero sin el ancla que lo haría calculable. El
# lookahead es lo que reparte los casos entre este patrón y el anterior: sin él,
# toda cláusula calculable caería también aquí.
_UNANCHORED_NEXT_DAY = rf"d[íi]a\s+siguiente(?!\s+{_PUBLICATION_ANCHOR})"

# Diferimiento que este sistema no puede fechar. Deliberadamente estrecho: solo
# formas que hablan de la vigencia o de los efectos del propio acto. "A partir
# del 30 de julio de 2026" no entra aquí porque eso es una fecha expresa, que el
# extractor ya recoge como EXPLICIT.
_INDETERMINATE_RE = re.compile(
    r"(?:"
    r"posterg\w+\s+(?:su\s+)?vigencia"
    # "entrará en vigencia el 15 de agosto", "...con la instalación del
    # Directorio", "...una vez aprobado el reglamento": una fecha futura o un
    # hecho que la regla general no alcanza. Se excluye "el día siguiente", que
    # es el caso anclado y sí se calcula.
    r"|entrar?[áa]?\s+en\s+vigencia\s+(?:a\s+partir\s+)?"
    r"(?:el|del|desde|con|una\s+vez|cuando|tras|luego\s+de)\s+"
    r"(?!d[íi]a\s+siguiente)"
    rf"|a\s+partir\s+del?\s+{_UNANCHORED_NEXT_DAY}"
    r")",
    re.IGNORECASE,
)


# Fin de oración: punto seguido de espacio y mayúscula. El punto de "Artículo
# 3.-" no lo es, y cortar ahí devolvía cláusulas que empezaban por "- La...".
_SENTENCE_BREAK_RE = re.compile(r"\.\s+(?=[A-ZÁÉÍÓÚÑ¿«])")


def _sentence_around(text: str, start: int, end: int) -> str:
    """Oración completa que contiene el fragmento: el revisor lee la cláusula
    entera, no solo el trozo que disparó el patrón."""
    left = 0
    right = len(text)
    for boundary in _SENTENCE_BREAK_RE.finditer(text):
        if boundary.end() <= start:
            left = boundary.end()
        elif boundary.start() >= end:
            right = boundary.start() + 1
            break
    return text[left:right].strip()


def find_deferral_clause(dispositive_texts: Iterable[str]) -> DeferralClause | None:
    """Primera cláusula de la parte resolutiva que difiere la vigencia, clasificada.

    Solo se examina la parte dispositiva: un considerando que discurre sobre la
    retroactividad de los actos administrativos no dispone nada, y tomarlo por
    un diferimiento desplazaría la regla sin motivo.

    Dentro de un mismo texto se pregunta primero por lo que no se puede calcular:
    ante una resolución que dijera las dos cosas, lo conservador es devolverla al
    humano en vez de quedarse con la mitad que sí sabe sumar.
    """
    for index, text in enumerate(dispositive_texts):
        for regex, kind in (
            (_BUSINESS_DAY_RE, DeferralKind.INDETERMINATE),
            (_INDETERMINATE_RE, DeferralKind.INDETERMINATE),
            (_DAY_AFTER_PUBLICATION_RE, DeferralKind.DAY_AFTER_PUBLICATION),
        ):
            match = regex.search(text)
            if match is None:
                continue
            return DeferralClause(
                kind=kind,
                text=_sentence_around(text, match.start(), match.end()),
                source_index=index,
            )
    return None


def determine_legal_effect(
    *,
    event_type: EventType,
    stated_status: DateStatus,
    published_on: date | None,
    source_authority: SourceAuthority | None,
    deferral: DeferralClause | None = None,
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

    if deferral is not None and not deferral.computable:
        return LegalEffectVerdict(
            LegalEffectOutcome.VETOED,
            "la parte resolutiva difiere la vigencia del acto a un momento que estos "
            f"datos no permiten fechar («{deferral.text}»), amparándose en la "
            f"disposición en contrario que {basis.citation} reserva: la fecha la "
            "decide un humano",
            basis=basis,
            deferral=deferral,
        )

    if deferral is not None:
        # El acto ejerce la disposición en contrario que la norma le reconoce y
        # la ata a la publicación: la fecha no se infiere, se suma. Día natural,
        # no hábil: el patrón que llega hasta aquí excluye "día hábil siguiente".
        value = published_on + timedelta(days=1)
        return LegalEffectVerdict(
            LegalEffectOutcome.DETERMINED,
            f"el propio acto dispone que sus efectos corren desde el día siguiente de "
            f"la publicación («{deferral.text}»), que es la disposición en contrario "
            f"que {basis.citation} admite; publicado en El Peruano el "
            f"{published_on.isoformat()}, los efectos corren desde el "
            f"{value.isoformat()}",
            value=value,
            basis=basis,
            deferral=deferral,
        )

    return LegalEffectVerdict(
        LegalEffectOutcome.DETERMINED,
        f"{basis.citation}: los efectos corren desde el día de la publicación en El "
        f"Peruano ({published_on.isoformat()}), no desde el día siguiente ni desde la "
        "fecha de emisión; el documento no difiere la vigencia",
        value=published_on,
        basis=basis,
    )
