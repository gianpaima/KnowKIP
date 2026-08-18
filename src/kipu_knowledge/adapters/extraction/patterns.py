"""Patrones y heurísticas deterministas para resoluciones de personal.

Todos los patrones operan sobre el texto de secciones ya segmentadas. La regla
general es conservadora: si un patrón no calza con seguridad, no se extrae nada
y se deja constancia (warning / tarea de revisión), nunca se adivina.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from kipu_knowledge.domain.normalization import collapse_whitespace, strip_accents
from kipu_knowledge.domain.state_entities import catalog_acronyms

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
    # Los organismos públicos adscritos ("Organismo de Focalización e Información
    # Social") son entidades por derecho propio, no unidades de su ministerio. Sin
    # esta cabecera el segmentador no encontraba dónde terminar el nombre anterior
    # y lo extendía hasta el final del artículo.
    "Organismo",
    # Los programas nacionales ("Programa Nacional de Infraestructura Educativa
    # - PRONIED") también son entidades con puestos propios, no unidades. Se exige
    # el compuesto completo: "Programa" a secas partiría el cargo estructural
    # "Director de Programa Sectorial II" del clasificador.
    "Programa Nacional",
    # Organismos adscritos que designan en sus propias resoluciones o reciben
    # representantes del Estado. Compuestos a propósito: "Instituto" o "Fondo" a
    # secas también nombran unidades y fondos internos.
    "Seguro Social de Salud",
    "Instituto Peruano",
    "Comisión de Promoción",
    "Fondo Nacional",
    # Ampliación de la puesta al día de agosto 2026: organismos y programas que
    # designan en resoluciones propias (AGRO RURAL, SENCICO, APCI, INGEMMET,
    # ITP, ANIN, SERVIR, ANA, SENASA…). Compuestos a propósito, como arriba:
    # "Programa", "Instituto" o "Dirección" a secas nombran también unidades y
    # cargos estructurales del clasificador.
    "Programa de Desarrollo",
    "Servicio Nacional",
    "Agencia Peruana",
    "Autoridad Nacional",
    "Instituto Geológico",
    "Instituto Tecnológico",
    # La DINI es entidad por derecho propio pese a llamarse "Dirección …": sin
    # esta cabecera exacta caía como unidad y su puesto nacía sin organización.
    "Dirección Nacional de Inteligencia",
)

# Las resoluciones también nombran a la entidad solo por su sigla ("Jefe de la
# Oficina General de Asesoría Jurídica del FONDEPES"). Las siglas son dato
# declarado del catálogo curado (domain/state_entities.py, que ya excluye las
# homógrafas de palabras de nombres, como CULTURA); la comparación es sensible
# a mayúsculas, así que "FONDEPES" nunca calza con una palabra corriente.
ACRONYM_ORG_HEADS = catalog_acronyms()
_ACRONYM_ORG_RE = re.compile(r"^(?:" + "|".join(re.escape(a) for a in ACRONYM_ORG_HEADS) + r")\b")

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

_ALL_HEADS = sorted(ORG_HEAD_WORDS + UNIT_HEAD_WORDS + ACRONYM_ORG_HEADS, key=len, reverse=True)
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
        if organization is None and _is_org_segment(seg):
            organization = seg
        else:
            units.append(seg)
    return OrgPathSplit(role_label=role, unit_chain=units, organization=organization)


def _is_org_segment(seg: str) -> bool:
    return any(seg.startswith(h) for h in ORG_HEAD_WORDS) or _ACRONYM_ORG_RE.match(seg) is not None


# --- Limpieza de textos de rol ---

_THANKS_RE = re.compile(r"[;,]?\s*d[áa]ndosele las gracias.*$", re.IGNORECASE)
_SLOT_RE = re.compile(
    r",?\s*previst[oa] en el (?P<scheme>CAP(?:\s+Provisional)?)\b.*?"
    r"n[úu]mero correlativo\s+(?P<code>\d+)\s*",
    re.IGNORECASE,
)


def strip_thanks(text: str) -> str:
    return _THANKS_RE.sub("", text).strip().rstrip(".;,")


# Coletillas administrativas con que el artículo remata el cargo: el régimen
# laboral de la contratación ("bajo el régimen de la Ley N° 30057, Ley del
# Servicio Civil"), su modalidad CAS, la clasificación del puesto o su código.
# Describen cómo se contrata, no qué puesto ni qué órgano: pegadas al texto del
# cargo viajaban dentro del nombre de la organización y fabricaban entidades
# como "Ministerio de Vivienda, Construcción y Saneamiento, bajo el régimen de
# la Ley N° 30057". Se exige separador previo (coma o punto y coma) para no
# tocar un nombre que contenga estas palabras sin ser coletilla.
_ADMIN_CLAUSE_HEADS = (
    r"bajo el r[ée]gimen\b",
    r"bajo la modalidad\b",
    r"(?:puesto|cargo) considerado de confianza\b",
    r"perteneciente al grupo\b",
    r"con c[oó]digo de puesto\b",
)
_ADMIN_CLAUSE_RE = re.compile(
    r"[,;]\s*(?:y\s+)?(?:" + "|".join(_ADMIN_CLAUSE_HEADS) + r").*$",
    re.IGNORECASE,
)

# "… del Ministerio de Defensa - Director de Sistema Administrativo II": el
# cargo estructural del clasificador nacional pegado con guion tras la entidad.
# Solo se recorta el repertorio clásico del clasificador (Sistema
# Administrativo / Programa Sectorial con nivel romano): un guion genérico
# partiría siglas legítimas ("… – COFOPRI").
_STRUCTURAL_POSITION_RE = re.compile(
    r"\s*[-–—]\s*(?:Director(?:a)?|Jef[ea]|Asesor(?:a)?|Especialista|Ejecutiv[oa]|Gerente)\s+"
    r"de\s+(?:Sistema\s+Administrativo|Programa\s+Sectorial)\s+[IVXL]+\s*$",
)


def strip_admin_clauses(text: str) -> str:
    """Recorta del final del texto del cargo las coletillas administrativas."""
    cleaned = _ADMIN_CLAUSE_RE.sub("", text)
    cleaned = _STRUCTURAL_POSITION_RE.sub("", cleaned)
    return cleaned.strip().rstrip(".;,")


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

# Fórmulas con que un artículo colectivo anuncia la lista que lo sigue. La
# fuente alterna entre "a los siguientes señores/servidores" y "a los/as
# profesionales que se detallan a continuación" (y su variante "se indican"),
# y entre listas de guiones y tablas. El sustantivo no cambia el acto.
_COLLECTIVE_PEOPLE = (
    r"(?:se[ñn]or(?:es|as)?|servidor(?:es|as)?|profesionales|funcionari[oa]s|"
    r"ciudadan[oa]s|personas)"
)
_COLLECTIVE_TAIL = (
    # La preposición cambia con el verbo: se designa "a los siguientes
    # servidores" pero se deja sin efecto "de los servidores que se indican".
    r"(?:a|de)\s+l[oa]s(?:/as)?\s+(?:siguientes\s+)?" + _COLLECTIVE_PEOPLE + r"(?:\s+que se "
    r"(?:detallan|indican|se[ñn]alan|relacionan)(?:\s+a continuaci[oó]n)?)?\s*:?$"
)
# "Designar como X, a los siguientes señores:" y
# "Designar en el cargo de X a los/as profesionales que se detallan a continuación:"
COLLECTIVE_START_RE = re.compile(
    r"^(?P<verb>Designar|Nombrar|DESIGNAR|NOMBRAR)\s+"
    r"(?:como|en el cargo de|en los cargos de|en el puesto de)\s+"
    r"(?P<role>.+?),?\s*"
    r"(?:en representaci[oó]n del?\s+(?P<representing>.+?),\s*)?" + _COLLECTIVE_TAIL
)

# "Dar por terminada la designación en el cargo de X de los/as profesionales que
# se detallan a continuación:" / "Dejar sin efecto las designaciones como X, de
# los servidores que se indican a continuación:". El acto termina asignaciones
# de varias personas a la vez y su lista viene igual que la de inicio.
COLLECTIVE_END_RE = re.compile(
    r"^(?P<verb>Dar por terminada la designaci[oó]n|Dar por concluida la designaci[oó]n|"
    r"Dejar sin efecto la designaci[oó]n|Dejar sin efecto las designaciones)\s+"
    r"(?:como|en el cargo de|en los cargos de|en el puesto de)\s+"
    r"(?P<role>.+?),?\s*" + _COLLECTIVE_TAIL
)

# "…, siendo su primer día de labores el 07 de agosto de 2026." La fuente SÍ
# expresa la fecha de inicio, solo que no con la fórmula "a partir del". Sin
# reconocerla se perdían dos cosas a la vez: la fecha quedaba NOT_STATED pese a
# constar, y la frase entera contaminaba la etiqueta del puesto.
FIRST_WORKDAY_RE = re.compile(
    r",?\s*siendo su primer d[íi]a de labores\s+el\s+(?P<date>" + DATE_ES + r")\s*\.?$",
    re.IGNORECASE,
)

ACCEPT_RESIGNATION_RE = re.compile(
    r"^(?P<verb>Aceptar la renuncia|Se acepta la renuncia)"
    r"(?:\s*,\s*(?:con eficacia(?: anticipada)?\s+(?:al|a partir del)|a partir del)\s+"
    r"(?P<date>" + DATE_ES + r")\s*,)?"
    # La fuente alterna "presentada por" y "formulada por"; sin la segunda, el
    # nombre capturado arrancaba en "formulada" y el artículo no producía evento.
    r"\s*(?:(?:presentada|formulada)\s+por\s+)?"
    r"(?:del|de la|el|la|por el|por la)?\s*(?:se[ñn]ora?|se[ñn]orita)?\s*"
    # La coma que separa el nombre del cargo no forma parte del nombre; sin
    # excluirla la mención se registraba como "NOMBRE APELLIDO,".
    r"(?P<name>.+?),?"
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

# Fórmulas con que un encargo nombra lo encargado. Sirven de frontera derecha de
# la aposición: lo que va antes es el cargo que la persona YA ocupa.
_ENCARGO_OBJECT_HEADS = (
    r"las funciones del puesto de",
    r"las funciones del cargo de",
    r"las funciones de",
    r"el puesto de",
    r"el cargo de",
    r"la obligaci[oó]n de",
    r"la responsabilidad de",
)
_APPOSITIVE_RE = re.compile(
    r"^(?P<appositive>.+?),\s*(?=(?:" + "|".join(_ENCARGO_OBJECT_HEADS) + r")\b)",
    re.IGNORECASE,
)


def split_encargo_appositive(resp: str) -> tuple[str, str | None]:
    """Separa "Viceministro de X del Ministerio Y, las funciones del puesto Z".

    Devuelve (lo encargado, cargo sustantivo o None). La aposición solo se
    reconoce cuando lo que sigue arranca con una fórmula inequívoca de encargo:
    sin esa frontera declarada no hay forma de distinguir el cargo previo del
    encargado, y partir por la primera coma trocearía puestos con coma legítima.
    """
    text = resp.strip()
    m = _APPOSITIVE_RE.match(text)
    if m is None:
        return text, None
    appositive = m.group("appositive").strip().rstrip(".;,")
    if not appositive:
        return text, None
    return text[m.end() :].strip(), appositive


# Guardas: ENCARGAR a una unidad organizacional no es un evento de personal (regla 23)
ENCARGAR_ORG_GUARD_RE = re.compile(
    r"^(?:ENCARGAR|Encargar)\s*,?\s+a\s+la\s+"
    r"(?:Oficina|Unidad|Gerencia|Direcci[oó]n|Secretar[íi]a)",
    re.IGNORECASE,
)

# --- Condiciones de término y mandatos ---

# El conector que precede a la condición ("…, y en tanto dure la ausencia…") se
# consume fuera del grupo: pertenece a la frase anterior, no a la condición, y
# dejarlo colgando ensuciaba la etiqueta del puesto con una "y" final.
END_CONDITION_RE = re.compile(
    r"[,;]?\s*(?:y\s+|e\s+)?"
    r"(?P<cond>(?:hasta el retorno|hasta que|mientras dure|en tanto dure|en tanto se|"
    r"hasta concluir)\s+.+)$",
    re.IGNORECASE,
)

# "…, a partir del 7 de agosto de 2026" declarado al final del encargo en lugar
# de junto al nombre. Es la misma fecha de eficacia que ENCARGAR_PERSON_RE captura
# en la posición temprana; aquí se recorta para que no viaje dentro del puesto.
# La variante "a partir de la fecha" —sin cifra— también es coletilla de
# eficacia y también contaminaba la ruta del puesto ("… – APCI, a partir de la
# fecha"); se recorta igual, pero NO produce fecha: el grupo `date` queda vacío
# y la fecha sigue sin expresar, que la determine legal_effect o el revisor.
TRAILING_EFFECTIVE_FROM_RE = re.compile(
    r"[,;]?\s*(?:y\s+|e\s+)?(?:con eficacia(?:\s+anticipada)?\s+)?"
    r"a partir de(?:l\s+(?P<date>" + DATE_ES + r")|\s+la\s+fecha\b)",
    re.IGNORECASE,
)

# Cláusula que declara el encargo como acumulativo. No forma parte del puesto
# —pegada al nombre lo corrompía— pero sí es la afirmación de la fuente de que la
# responsabilidad se suma a las funciones propias, así que se conserva su rastro.
ADDITION_CLAUSE_RE = re.compile(
    r"[,;]?\s*(?:y\s+|e\s+)?(?P<clause>en adici[oó]n a (?:sus|las) funciones"
    r"(?:\s+(?:propias|de su cargo|que viene desempe[ñn]ando))?)",
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
PUBLICATION_NOTICE_RE = re.compile(
    # "Disponer la publicación de la presente Resolución…" y su forma directa
    # "Publicar la presente resolución en el diario oficial…", que quedaba sin
    # clasificar pese a ser el aviso de publicación por excelencia.
    r"publicaci[oó]n de la presente [Rr]esoluci[oó]n|Publicar la presente [Rr]esoluci[oó]n",
    re.IGNORECASE,
)
NOTIFICATION_RE = re.compile(r"se le notifique|notif[íi]quese|notificar la presente", re.IGNORECASE)

UPPERCASE_NAME_RE = re.compile(r"^[A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ\s.'-]+$")


def looks_like_person_name(text: str) -> bool:
    words = text.split()
    return 2 <= len(words) <= 8 and text[0].isupper() and not any(ch.isdigit() for ch in text)


# --- Columnas de las tablas de designación colectiva ---

# Qué declara cada columna, según su celda de cabecera. Es la fuente la que
# nombra sus columnas; sin leerlas no se sabe qué celda es un nombre y cuál un
# documento de identidad, y adivinarlo por posición fabricaría atribuciones.
_COLUMN_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("name", ("APELLIDOS", "NOMBRES", "NOMBRE COMPLETO")),
    ("identifier", ("DNI", "DOCUMENTO NACIONAL DE IDENTIDAD", "DOC. IDENTIDAD")),
    ("organization", ("ENTIDAD", "INSTITUCION", "ORGANISMO", "PLIEGO")),
)


def table_columns(header_cells: list[str]) -> dict[str, int]:
    """Índice de columna por tipo, leído de la cabecera declarada.

    Solo se reconocen las columnas que la cabecera nombra. Una tabla sin columna
    de nombre no produce nada: es preferible no afirmar a atribuir por posición.
    """
    columns: dict[str, int] = {}
    for index, cell in enumerate(header_cells):
        label = collapse_whitespace(strip_accents(cell)).upper()
        for kind, markers in _COLUMN_KINDS:
            if kind not in columns and any(marker in label for marker in markers):
                columns[kind] = index
                break
    return columns


# El DNI peruano tiene exactamente 8 dígitos. En una celda el valor viaja sin
# etiqueta —la etiqueta está en la cabecera— así que la forma es lo único que
# distingue un documento de identidad de un correlativo de fila.
_DNI_CELL_RE = re.compile(r"^\d{8}$")


def identifier_in_cell(cell: str) -> str | None:
    value = cell.strip()
    return value if _DNI_CELL_RE.match(value) else None


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
