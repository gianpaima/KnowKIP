"""Patrones y heurísticas deterministas para resoluciones de personal.

Todos los patrones operan sobre el texto de secciones ya segmentadas. La regla
general es conservadora: si un patrón no calza con seguridad, no se extrae nada
y se deja constancia (warning / tarea de revisión), nunca se adivina.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DATE_ES = r"\d{1,2}\s+de\s+[a-záéíóúñ]+\s+del?\s+\d{4}"

# --- Cabeceras organizacionales para segmentar rutas "rol de la Unidad del Órgano" ---

ORG_HEAD_WORDS = (
    "Ministerio",
    "Biblioteca Nacional",
    "Banco Central",
    "Intendencia Nacional",
    "Centro Nacional",
    "Superintendencia Nacional",
    "Presidencia del Consejo de Ministros",
    "Archivo General",
    "Instituto Nacional",
)

UNIT_HEAD_WORDS = (
    "Oficina General",
    "Oficina",
    "Secretaría General",
    "Secretaría",
    "Gerencia General",
    "Gerencia",
    "Dirección General",
    "Dirección",
    "Despacho",
    "Unidad",
    "Jefatura",
    "Tribunal",
    "Directorio",
)

_ALL_HEADS = sorted(ORG_HEAD_WORDS + UNIT_HEAD_WORDS, key=len, reverse=True)
_HEADS_ALT = "|".join(re.escape(h) for h in _ALL_HEADS)
_BOUNDARY_RE = re.compile(r"\s+(?:de la|del|de los|de)\s+(?=(?:" + _HEADS_ALT + r")\b)")
_ANTE_ORG_RE = re.compile(
    r"\s+ante\s+(?:la|el)\s+(?P<org>(?:"
    + "|".join(re.escape(h) for h in ORG_HEAD_WORDS)
    + r")[^,;]*)"
)


@dataclass
class OrgPathSplit:
    role_label: str
    unit_chain: list[str] = field(default_factory=list)  # específica -> general
    organization: str | None = None


def split_org_path(role_text: str) -> OrgPathSplit:
    """Divide "Jefa de X de la Oficina Y de la Secretaría Z del Ministerio W".

    Devuelve rol, cadena de unidades y organización. Si no hay cabecera de órgano
    reconocible, todo queda en role_label y organization=None (decisión conservadora).
    """
    segments = _BOUNDARY_RE.split(role_text.strip())
    if len(segments) == 1:
        # Sin ruta "de la X del Y": intenta detectar la entidad tras "ante la/el ..."
        am = _ANTE_ORG_RE.search(role_text)
        if am:
            return OrgPathSplit(role_label=role_text.strip(), organization=am.group("org").strip())
        return OrgPathSplit(role_label=role_text.strip())
    role = segments[0].strip()
    rest = [s.strip() for s in segments[1:] if s.strip()]
    organization: str | None = None
    units: list[str] = []
    for seg in rest:
        if organization is None and any(seg.startswith(h) for h in ORG_HEAD_WORDS):
            organization = seg
        else:
            units.append(seg)
    return OrgPathSplit(role_label=role, unit_chain=units, organization=organization)


# --- Limpieza de textos de rol ---

_THANKS_RE = re.compile(r"[;,]?\s*d[áa]ndosele las gracias.*$", re.IGNORECASE)
_SLOT_RE = re.compile(
    r",?\s*previst[oa] en el (?P<scheme>CAP(?:\s+Provisional)?)\b.*?"
    r"n[úu]mero correlativo\s+(?P<code>\d+)\s*",
    re.IGNORECASE,
)


def strip_thanks(text: str) -> str:
    return _THANKS_RE.sub("", text).strip().rstrip(".;,")


def extract_position_slot(role_text: str) -> tuple[str, tuple[str, str, str] | None]:
    """Separa la cláusula de correlativo CAP. Devuelve (rol limpio, (scheme, code, frase))."""
    m = _SLOT_RE.search(role_text)
    if not m:
        return role_text.strip().rstrip(".;,"), None
    cleaned = (role_text[: m.start()] + role_text[m.end() :]).strip().rstrip(".;,")
    scheme_raw = m.group("scheme").upper().replace(" ", "_")
    scheme = "CAP_PROVISIONAL" if "PROVISIONAL" in scheme_raw else scheme_raw
    return cleaned, (scheme, m.group("code"), m.group(0).strip(" ,"))


# --- Verbos resolutivos ---

START_EVENT_RE = re.compile(
    r"^(?P<verb>DESIGNAR|Designar|Se designa|Des[íi]gnase|NOMBRAR|Nombrar|Se nombra)"
    r",?\s*"
    r"(?:a partir del\s+(?P<date>" + DATE_ES + r")\s*,?\s*)?"
    r"(?:a la se[ñn]ora|al se[ñn]or|a la se[ñn]orita|a)\s+"
    r"(?P<name>.+?)"
    r",?\s+(?:en el (?:puesto|cargo)(?: de confianza)? de|como|al cargo de)\s+"
    r"(?P<role>.+?)\s*\.?$"
)

COLLECTIVE_START_RE = re.compile(
    r"^(?P<verb>Designar|Nombrar|DESIGNAR|NOMBRAR)\s+como\s+"
    r"(?P<role>.+?),\s*"
    r"(?:en representaci[oó]n del?\s+(?P<representing>.+?),\s*)?"
    r"a l[oa]s siguientes se[ñn]or(?:es|as)?\s*:?$"
)

ACCEPT_RESIGNATION_RE = re.compile(
    r"^(?P<verb>Aceptar la renuncia|Se acepta la renuncia)"
    r"(?:\s*,\s*(?:con eficacia(?: anticipada)?\s+(?:al|a partir del)|a partir del)\s+"
    r"(?P<date>" + DATE_ES + r")\s*,)?"
    r"\s*(?:presentada por\s+)?"
    r"(?:del|de la|el|la|por el|por la)?\s*(?:se[ñn]ora?|se[ñn]orita)?\s*"
    r"(?P<name>.+?)"
    r"\s+(?:al cargo de|al puesto de|en el cargo de|como)\s+"
    r"(?P<role>.+?)\s*\.?$"
)

END_ACTING_RE = re.compile(
    r"^(?P<verb>Dar por concluido el encargo)(?:\s+de(?:l)?(?:\s+puesto)?(?:\s+de)?)?\s+"
    r"(?P<rest>.+?)\s*\.?$"
)

END_DESIGNATION_RE = re.compile(
    r"^(?P<verb>Dar por concluida la designaci[oó]n)\s+"
    r"(?:de la se[ñn]ora|del se[ñn]or|de)\s+"
    r"(?P<name>.+?)"
    r"\s+(?:en el cargo de|al cargo de|como)\s+"
    r"(?P<role>.+?)\s*\.?$"
)

ENCARGAR_PERSON_RE = re.compile(
    r"^(?P<verb>ENCARGAR|Encargar|Se encarga)\s*,?\s+"
    r"(?:al|a la|a)\s+"
    r"(?:servidor(?:a)?\s+civil\s+|se[ñn]ora?\s+|se[ñn]orita\s+|abogad[oa]\s+)?"
    r"(?P<name>[^,]+?)\s*,\s*"
    r"(?:con eficacia anticipada\s+a partir del\s+(?P<date>" + DATE_ES + r")\s*,\s*)?"
    r"(?P<resp>.+?)\s*\.?$"
)

# Guardas: ENCARGAR a una unidad organizacional no es un evento de personal (regla 23)
ENCARGAR_ORG_GUARD_RE = re.compile(
    r"^(?:ENCARGAR|Encargar)\s*,?\s+a\s+la\s+"
    r"(?:Oficina|Unidad|Gerencia|Direcci[oó]n|Secretar[íi]a)",
    re.IGNORECASE,
)

# --- Condiciones de término y mandatos ---

END_CONDITION_RE = re.compile(
    r"(?P<cond>(?:hasta el retorno|hasta que|mientras dure|hasta concluir)\s+.+)$",
    re.IGNORECASE,
)

RETURNING_HOLDER_RE = re.compile(
    r"hasta el retorno(?:\s+del?\s+descanso vacacional)?\s+"
    r"(?:del?|de la)\s*(?:servidor(?:a)?\s+|se[ñn]ora?\s+)?"
    r"(?P<name>[A-ZÁÉÍÓÚÑ][^,.;]+?)\s*\.?$",
)

PREDECESSOR_PERIOD_PHRASE = "hasta concluir el período de su antecesor"
CONSTITUTIONAL_PERIOD_PHRASE = "período constitucional"

# --- Recitales (considerandos) ---

# Captura el encargo completo declarado en un considerando: nombre, cargo
# sustantivo opcional y el puesto encargado. El puesto es la pieza que permite
# corroborar contra el artículo resolutivo que concluye ese mismo puesto.
RECITAL_ENCARGO_RE = re.compile(
    r"se encarga (?:a la se[ñn]ora|al se[ñn]or)\s+(?P<name>[^,]+),"
    r"(?:\s*(?P<substantive>[^,]+?),)?"
    r"\s*(?:el puesto de|el cargo de|las funciones de)\s+"
    r"(?P<position>.+?)(?:,\s*hasta\b|;|\.|$)"
)

# Instrumento que el propio considerando cita como origen del encargo
# ("conforme a lo dispuesto en la Resolución Suprema N° 044-2025-EF, se encarga…").
RECITAL_CITED_DOC_RE = re.compile(
    r"(?:conforme a(?: lo dispuesto en)?|mediante|por)\s+(?:la|el)\s*"
    r"(?P<kind>Resoluci[oó]n\s+[A-Za-zÁÉÍÓÚáéíóú]+|Decreto\s+[A-Za-z]+)\s+"
    r"N[º°]\s*(?P<num>[A-Za-z0-9./-]+)"
)

PRIOR_DOC_IN_ARTICLE_RE = re.compile(
    r",?\s*dispuest[oa] mediante\s+(?:la|el)?\s*"
    r"(?P<kind>Resoluci[oó]n\s+[A-Za-zÁÉÍÓÚáéíóú]+|Decreto\s+[A-Za-z]+)\s+"
    r"N[º°]\s*(?P<num>[A-Za-z0-9./-]+)"
)

# --- Referencias documentales ---

SEEN_DOC_RE = re.compile(
    r"(?P<kind>Prove[íi]do|Memorando|Informe(?:\s+(?:T[ée]cnico|Legal))?|Nota Informativa|Oficio)"
    r"\s+N[º°]\s*(?P<num>[A-Za-z0-9./-]*[A-Za-z0-9])"
)

PRIOR_APPOINTMENT_DOC_RE = re.compile(
    r"(?:mediante|dispuest[oa] en|lo dispuesto en)\s+(?:la\s+)?"
    r"(?P<kind>Resoluci[oó]n\s+(?:Suprema|Ministerial|Jefatural|Directoral|de Intendencia))"
    r"\s+N[º°]\s*(?P<num>[A-Za-z0-9./-]*[A-Za-z0-9])"
)

NORMATIVE_DOC_RE = re.compile(
    r"(?P<kind>Ley|Decreto Supremo|Decreto Legislativo)\s+N[º°]\s*"
    r"(?P<num>[A-Za-z0-9./-]*[A-Za-z0-9])"
)

PRIOR_APPOINTMENT_CONTEXT_RE = re.compile(r"se design[óa]|se encarga|se encarg[óo]", re.IGNORECASE)

# --- Clasificación de artículos no-evento ---

COUNTERSIGNATURE_RE = re.compile(r"es refrendada por", re.IGNORECASE)
SWORN_DECLARATION_RE = re.compile(r"Declaraci[oó]n Jurada", re.IGNORECASE)
PUBLICATION_NOTICE_RE = re.compile(r"publicaci[oó]n de la presente Resoluci[oó]n", re.IGNORECASE)
NOTIFICATION_RE = re.compile(r"se le notifique|notif[íi]quese", re.IGNORECASE)

UPPERCASE_NAME_RE = re.compile(r"^[A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ\s.'-]+$")


def looks_like_person_name(text: str) -> bool:
    words = text.split()
    return 2 <= len(words) <= 8 and text[0].isupper() and not any(ch.isdigit() for ch in text)


# --- Identificadores de persona declarados por la fuente ---

# "identificado con DNI N° 09342789", "D.N.I. Nº 09342789", "Documento Nacional
# de Identidad N° 09342789". El DNI peruano tiene exactamente 8 dígitos; exigirlo
# evita capturar números de resolución o correlativos vecinos.
_DNI_RE = re.compile(
    r"(?:D\.?\s*N\.?\s*I\.?|Documento\s+Nacional\s+de\s+Identidad)"
    r"\s*(?:N[º°.]?\s*)?(?P<value>\d{8})\b",
    re.IGNORECASE,
)

# "Carné de Extranjería N° 001234567" — longitud variable, alfanumérico.
_CE_RE = re.compile(
    r"(?:C\.?\s*E\.?|Carn[eé]\s+de\s+Extranjer[ií]a)"
    r"\s*(?:N[º°.]?\s*)?(?P<value>[A-Za-z0-9]{6,12})\b",
)


def extract_person_identifiers(text: str) -> list[tuple[str, str, int, int]]:
    """Identificadores declarados en el texto: (esquema, valor_raw, inicio, fin).

    Conservador por diseño: solo formas explícitamente etiquetadas. Un número
    suelto nunca se interpreta como documento de identidad, porque confundirlo
    con un correlativo fabricaría una identidad — exactamente lo que el sistema
    no debe hacer. Los offsets permiten anclar un EvidenceSpan a la cita literal.
    """
    found: list[tuple[str, str, int, int]] = []
    for scheme, pattern in (("DNI", _DNI_RE), ("CARNE_EXTRANJERIA", _CE_RE)):
        for match in pattern.finditer(text):
            found.append((scheme, match.group("value"), match.start(), match.end()))
    return sorted(found, key=lambda item: item[2])


# Ventana máxima entre el final de un nombre y el identificador que le pertenece.
# Cubre "NOMBRE APELLIDO, identificado con DNI N° ..." y sus variantes con cargo
# intercalado, sin alcanzar a la siguiente persona de un artículo colectivo.
_IDENTIFIER_WINDOW = 160

# Dos o más palabras capitalizadas seguidas: probable nombre de otra persona.
_OTHER_NAME_RUN_RE = re.compile(
    r"\b[A-ZÁÉÍÓÚÑÜ][\wÁÉÍÓÚÑÜáéíóúñü.'-]+(?:\s+[A-ZÁÉÍÓÚÑÜ][\wÁÉÍÓÚÑÜáéíóúñü.'-]+)+"
)


def identifiers_for_name(text: str, name: str) -> list[tuple[str, str, int, int]]:
    """Identificadores atribuibles a `name` dentro de `text`.

    Un artículo colectivo nombra a varias personas y puede declarar el documento
    de cada una. Atribuir un identificador al nombre equivocado fabricaría una
    identidad, así que se exige que el identificador siga al nombre dentro de una
    ventana corta y que en el hueco entre ambos no aparezca otro nombre. Si la
    atribución es dudosa no se extrae nada: perder un dato es recuperable, una
    identidad falsa no.
    """
    start = text.find(name)
    if start < 0:
        return []
    name_end = start + len(name)
    attributed: list[tuple[str, str, int, int]] = []
    for scheme, value, id_start, id_end in extract_person_identifiers(text):
        if not name_end <= id_start <= name_end + _IDENTIFIER_WINDOW:
            continue
        if _OTHER_NAME_RUN_RE.search(text[name_end:id_start]):
            continue
        attributed.append((scheme, value, id_start, id_end))
    return attributed
