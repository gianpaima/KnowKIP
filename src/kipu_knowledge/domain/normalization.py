"""Normalización determinista de textos del dominio.

Reglas clave (ver docs/adr y AGENTS.md):
- Siempre se conserva el texto original; la normalización produce un campo adicional.
- Nunca se inventan fechas: si un texto no contiene una fecha, se devuelve None.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

_WS_RE = re.compile(r"\s+")

SPANISH_MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}

# "5 de agosto de 2026", "05 de agosto del 2026"
_SPANISH_DATE_RE = re.compile(
    r"(?P<day>\d{1,2})\s+de\s+(?P<month>[a-záéíóú]+)\s+del?\s+(?P<year>\d{4})",
    re.IGNORECASE,
)

# "Jesús María, 5 de agosto del 2026"
_ISSUE_LINE_RE = re.compile(
    r"^(?P<place>[^,]+),\s*(?P<rest>\d{1,2}\s+de\s+[a-záéíóú]+\s+del?\s+\d{4})\s*$",
    re.IGNORECASE,
)

# Prefijos de numeración: "Nº", "N°", "N.°", "No."
_NUMBER_PREFIX_RE = re.compile(r"^\s*(?:n\s*[º°oO]\.?|n\.\s*[º°]|num(?:ero)?\.?)\s*", re.IGNORECASE)


def collapse_whitespace(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def normalize_person_name(raw: str) -> str:
    """Nombre normalizado para comparación: mayúsculas, sin tildes, espacios colapsados.

    Un nombre escrito "APELLIDOS, NOMBRES" —la forma registral con que las
    tablas de designaciones colectivas encabezan su columna— se reordena a
    "NOMBRES APELLIDOS", que es como lo escribe el resto del corpus. Reordenar
    no es inferir: la coma es la convención que la propia fuente usa para
    separar ambas partes, y el resto del sistema asume ese orden
    (`person_name_is_variant`). Sin esto, la misma persona nombrada en una
    tabla y en un párrafo produce dos grafías que nunca se encuentran.

    No implica identidad: dos menciones con igual forma normalizada siguen siendo
    menciones distintas hasta que una señal independiente del nombre las vincule
    (regla 13). Normalizar solo decide qué se compara, nunca qué se fusiona.
    """
    text = collapse_whitespace(strip_accents(raw)).upper()
    surnames, comma, given = text.partition(",")
    if not comma:
        return text
    surnames, given = surnames.strip(), given.strip()
    # Conservador: solo se reordena lo que parece un nombre partido en dos. Ante
    # cualquier otra cosa (coma suelta, más de dos partes) se deja tal cual, que
    # a lo sumo no encuentra nada; reordenar mal fabricaría una grafía inexistente.
    if not surnames or not given or "," in given:
        return text
    if not 2 <= len(person_name_tokens(f"{given} {surnames}")) <= 8:
        return text
    return f"{given} {surnames}"


def person_name_tokens(normalized: str) -> tuple[str, ...]:
    """Tokens de un nombre ya normalizado."""
    return tuple(t for t in normalized.split(" ") if t)


def person_name_is_variant(a_normalized: str, b_normalized: str) -> bool:
    """¿Son dos grafías del mismo nombre por omisión de nombres de pila?

    Convención registral peruana: ``NOMBRES APELLIDO_PATERNO APELLIDO_MATERNO``.
    Se considera variante cuando ambos apellidos coinciden y el conjunto de
    nombres de pila de uno está estrictamente contenido en el del otro
    ("ELMER CUBA BUSTINZA" vs "ELMER RAFAEL CUBA BUSTINZA").

    Comparación exacta por tokens: no hay fuzzy matching ni distancia de edición,
    así que no detecta erratas. Devolver True NO afirma identidad — solo que el
    par merece revisión humana (regla 13); la fusión sigue siendo decisión humana.
    """
    a, b = person_name_tokens(a_normalized), person_name_tokens(b_normalized)
    if len(a) < 3 or len(b) < 3 or a == b:
        return False
    if a[-2:] != b[-2:]:  # los dos apellidos deben coincidir
        return False
    given_a, given_b = set(a[:-2]), set(b[:-2])
    return bool(given_a < given_b or given_b < given_a)


def normalize_identifier(raw: str) -> str:
    """Canoniza un documento de identidad para comparación exacta.

    Solo mayúsculas y eliminación de separadores de presentación (espacios,
    puntos, guiones). No se rellenan ceros ni se corrigen longitudes: alterar el
    valor declarado sería inventar un identificador.
    """
    return re.sub(r"[\s.\-]", "", raw).upper()


def normalize_org_name(raw: str) -> str:
    """Nombre de organización normalizado. Igual que el de puesto, colapsa guiones
    con espaciado irregular ("Desastres -CENEPRED" vs "Desastres - CENEPRED")
    frecuentes en publicaciones reales de la misma entidad."""
    text = collapse_whitespace(strip_accents(raw)).upper()
    return re.sub(r"\s*-\s*", "-", text).rstrip(".")


# Fragmentos que delatan una coletilla administrativa dentro de un nombre de
# organización ya normalizado. Ningún órgano del Estado se llama "bajo el
# régimen" de nada: si uno de estos aparece, la extracción arrastró la cláusula
# de la contratación y el nombre no es un nombre. La lista sale de casos
# reales (RM 299-2026-VIVIENDA y pares); ampliarla es barato y su único costo
# es una tarea de revisión de más.
_ORG_NAME_CONTAMINANTS = (
    "BAJO EL REGIMEN",
    "BAJO LA MODALIDAD",
    "PUESTO CONSIDERADO DE CONFIANZA",
    "CARGO CONSIDERADO DE CONFIANZA",
    "PERTENECIENTE AL GRUPO",
    "CODIGO DE PUESTO",
    "DE SISTEMA ADMINISTRATIVO",
    "DE PROGRAMA SECTORIAL",
)


def org_name_contamination(name_normalized: str) -> str | None:
    """Primer fragmento administrativo hallado en un nombre de organización.

    Devuelve el fragmento —para citarlo en la tarea de revisión— o None si el
    nombre está limpio. Detectar no corrige: la corrección es del extractor o
    del revisor; esto solo impide que el nombre contaminado pase en silencio.
    """
    for fragment in _ORG_NAME_CONTAMINANTS:
        if fragment in name_normalized:
            return fragment
    return None


def normalize_position_label(raw: str) -> str:
    """Etiqueta de puesto normalizada.

    Además de mayúsculas/tildes/espacios, se normalizan los guiones con espaciado
    irregular ("... Desastres -CENEPRED" vs "... Desastres - CENEPRED") que aparecen
    en publicaciones reales del mismo puesto.
    """
    text = collapse_whitespace(strip_accents(raw)).upper()
    text = re.sub(r"\s*-\s*", "-", text)
    # Femenino/masculino de cabeceras frecuentes no se normaliza: se conserva tal cual
    # y la identidad de puesto se decide con organización + unidad + etiqueta.
    return text.rstrip(".")


def normalize_document_number(raw: str) -> str:
    """Canoniza un número de resolución: sin prefijo Nº, mayúsculas, sin espacios internos.

    "Nº D000284-2026-MIDAGRI-DM" -> "D000284-2026-MIDAGRI-DM"
    "nº 027-2026-ef" -> "027-2026-EF"
    """
    text = _NUMBER_PREFIX_RE.sub("", collapse_whitespace(raw))
    return text.replace(" ", "").upper()


def parse_spanish_date(text: str) -> date | None:
    """Extrae la primera fecha en español del texto. Devuelve None si no hay fecha."""
    m = _SPANISH_DATE_RE.search(text)
    if not m:
        return None
    month = SPANISH_MONTHS.get(strip_accents(m.group("month").lower()))
    if month is None:
        return None
    try:
        return date(int(m.group("year")), month, int(m.group("day")))
    except ValueError:
        return None


def parse_issue_line(text: str) -> tuple[str | None, date | None]:
    """Parsea la línea de emisión "Lugar, D de mes de AAAA" -> (lugar, fecha)."""
    m = _ISSUE_LINE_RE.match(collapse_whitespace(text))
    if not m:
        return None, None
    return m.group("place").strip(), parse_spanish_date(m.group("rest"))


def parse_ddmmyyyy(text: str) -> date | None:
    """Parsea fechas "06/08/2026" (formato del visor de El Peruano)."""
    m = re.search(r"\b(\d{2})/(\d{2})/(\d{4})\b", text)
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None
