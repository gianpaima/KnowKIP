"""Vocabulario y reglas del contexto web atribuido (docs/web-context-design.md).

Las afirmaciones extraídas de fuentes de contexto (prensa, redes sociales)
viven en `assertion` con predicados de este módulo, en espacio de nombres
`web:` para que ninguna consulta pueda confundirlas con hechos del registro
funcional. El vocabulario es cerrado a propósito: un tipo nuevo de afirmación
es una decisión de política, no una salida más del extractor.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

WEB_PREDICATE_PREFIX = "web:"

# El documento cita una norma; el objeto es la web_reference que la ancla.
CITES_OFFICIAL_ACT = "web:cites_official_act"
# Cubre un evento de la función pública (juramentación, asunción del cargo…).
COVERS_EVENT = "web:covers_event"
# Cargo público previo declarado por la fuente (dirige la ingesta histórica).
PRIOR_PUBLIC_ROLE = "web:prior_public_role"
# Actividad en el sector privado declarada por la fuente.
PRIVATE_SECTOR_ROLE = "web:private_sector_role"
# Formación académica declarada por la fuente.
EDUCATION = "web:education"
# Profesión declarada por la fuente.
PROFESSION = "web:profession"
# Cuenta pública que la fuente atribuye a la persona.
PUBLIC_ACCOUNT = "web:public_account"
# Declaración pública de la propia persona recogida por la fuente.
PUBLIC_STATEMENT = "web:public_statement"
# Señalamiento publicado (investigación, cuestionamiento). Registra que la
# fuente lo publicó; el sistema no lo evalúa ni lo convierte en veredicto.
REPORTED_ASSESSMENT = "web:reported_assessment"
# Contexto que no encaja en la tipología, sin forzarla.
OTHER_CONTEXT = "web:other_context"

CONTEXT_PREDICATES: frozenset[str] = frozenset(
    {
        CITES_OFFICIAL_ACT,
        COVERS_EVENT,
        PRIOR_PUBLIC_ROLE,
        PRIVATE_SECTOR_ROLE,
        EDUCATION,
        PROFESSION,
        PUBLIC_ACCOUNT,
        PUBLIC_STATEMENT,
        REPORTED_ASSESSMENT,
        OTHER_CONTEXT,
    }
)


def is_context_predicate(predicate: str) -> bool:
    """Si una afirmación pertenece a la capa de contexto atribuido.

    El prefijo basta para la separación de capas en consultas; la pertenencia
    al vocabulario cerrado se comprueba aparte porque un predicado `web:` fuera
    del catálogo es un error del extractor, no un tipo nuevo.
    """
    return predicate.startswith(WEB_PREDICATE_PREFIX)


# Serie con la que los documentos de contexto entran en publication_item. El
# UNIQUE existente (sistema, serie, código) dedupe así por URL canónica dentro
# de cada publicador.
WEB_SOURCE_SERIES = "WEB"

# Parámetros de tracking que no identifican el documento: dos URLs que solo
# difieren en ellos son la misma página, y conservarlos duplicaría capturas.
_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "yclid", "mc_cid", "mc_eid", "ref_src")


def canonicalize_url(url: str) -> str:
    """URL sin fragmento ni parámetros de tracking, con esquema y host en minúsculas.

    No persigue la canonicalización perfecta (esa la declara la propia página
    en `<link rel="canonical">` y la resuelve el adaptador de captura): asegura
    que el código de publicación no cambie por decoración de la URL.
    """
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not any(
            k.lower().startswith(prefix) or k.lower() == prefix for prefix in _TRACKING_PARAMS
        )
    ]
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            urlencode(query),
            "",  # el fragmento nunca viaja al servidor: no identifica contenido
        )
    )


# --- Formas cortas de un nombre en prosa periodística ----------------------
#
# El corpus registra la grafía registral completa ("CESAR ALFONSO LUNA
# VICTORIA LEON"); la prensa escribe "César Luna Victoria". Una forma corta
# válida es una subsecuencia ordenada de los tokens del nombre completo que
# incluye al menos un apellido (uno de los dos últimos tokens). "César
# Alfonso" solo (nombres de pila) no cuenta: sin apellido no hay mención.
#
# Encontrar NO es vincular: la mención hallada sigue sometida al guard de
# homonimia. Esto solo decide qué se compara, nunca qué se fusiona.

# Secuencia de palabras capitalizadas (tildes incluidas), con partículas de
# apellido en minúscula permitidas dentro ("de", "del", "la"). Sin "y": une
# nombres de personas distintas mucho más a menudo que apellidos reales.
NAME_RUN_RE = re.compile(
    r"\b[A-ZÁÉÍÓÚÑÜ][\w'áéíóúñü-]*"
    r"(?:\s+(?:(?:de|del|la|las|los)\s+)?[A-ZÁÉÍÓÚÑÜ][\w'áéíóúñü-]*)+"
)


def is_short_name_form(run_tokens: tuple[str, ...], full_tokens: tuple[str, ...]) -> bool:
    """¿`run_tokens` es una forma corta legítima de `full_tokens`? (normalizados)"""
    if len(run_tokens) < 2 or len(run_tokens) > len(full_tokens):
        return False
    if run_tokens == full_tokens:
        return True
    iterator = iter(full_tokens)
    if not all(token in iterator for token in run_tokens):  # subsecuencia ordenada
        return False
    return bool(set(run_tokens) & set(full_tokens[-2:]))


# --- Citas de normas en prosa periodística ---------------------------------
#
# La prensa cita la norma sin el protocolo del corpus oficial: "mediante
# Resolución Suprema 027-2026-EF", a menudo sin "Nº". Solo formas con el tipo
# escrito entero: las siglas ("RS 027-2026-EF") son ambiguas fuera del registro
# oficial y un falso anclaje es peor que ninguno.
_NORM_CITATION_RE = re.compile(
    r"(?P<kind>Resoluci[oó]n\s+(?:Suprema|Ministerial|Jefatural|Directoral|"
    r"Viceministerial|de\s+Superintendencia|de\s+Intendencia)|"
    r"Decreto\s+Supremo|Decreto\s+Legislativo|Decreto\s+de\s+Urgencia|Ley)"
    # El número empieza en dígito y termina en alfanumérico: "027-2026-EF"
    # termina en letra y "27692" en dígito; el punto final de la frase queda fuera.
    r"\s+(?:N[º°.]?\s*)?(?P<num>\d[\dA-Za-z./-]*[\dA-Za-z]|\d)",
)


@dataclass(frozen=True)
class NormCitation:
    """Una norma citada en texto libre, con offsets para anclar la evidencia."""

    kind_raw: str
    number_raw: str
    start: int
    end: int


def find_norm_citations(text: str) -> list[NormCitation]:
    """Normas citadas en el texto, deduplicadas por (tipo, número)."""
    found: list[NormCitation] = []
    seen: set[tuple[str, str]] = set()
    for match in _NORM_CITATION_RE.finditer(text):
        kind = re.sub(r"\s+", " ", match.group("kind"))
        key = (kind.lower(), match.group("num").upper())
        if key in seen:
            continue
        seen.add(key)
        found.append(
            NormCitation(
                kind_raw=kind,
                number_raw=match.group("num"),
                start=match.start(),
                end=match.end(),
            )
        )
    return found


def web_publication_code(url: str) -> str:
    """Código estable de un documento web para `publication_item`.

    Es el sha256 truncado de la URL canonicalizada: determinista, corto y sin
    pretensión de significado — el identificador con sentido es la URL, que se
    guarda íntegra en `canonical_url`.
    """
    return hashlib.sha256(canonicalize_url(url).encode("utf-8")).hexdigest()[:16]
