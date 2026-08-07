"""Parser de dispositivos HTML de El Peruano.

Estrategia:
1. Delimitar el contenedor real del dispositivo: div#x<codigo> (nunca el body completo;
   la página del buscador incluye navegación, publicidad y hasta títulos incrustados de
   OTROS dispositivos, como se observó en fixtures reales).
2. Validar campos obligatorios: sumilla, tipo, número y código de publicación.
3. Segmentar en secciones tipadas conservando el texto original y el orden.

Falla de forma explícita (ParseError) si el contenedor no existe o faltan campos.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from lxml import html as lxml_html

from kipu_knowledge.domain.contracts import SourceReference
from kipu_knowledge.domain.enums import DocumentTypeCode, SectionType
from kipu_knowledge.domain.normalization import (
    collapse_whitespace,
    normalize_document_number,
    parse_ddmmyyyy,
    parse_issue_line,
    strip_accents,
)
from kipu_knowledge.domain.parsed import ParsedDocument, ParsedSection

_DOC_TYPE_MAP = {
    "RESOLUCION MINISTERIAL": DocumentTypeCode.RESOLUCION_MINISTERIAL,
    "RESOLUCION SUPREMA": DocumentTypeCode.RESOLUCION_SUPREMA,
    "RESOLUCION JEFATURAL": DocumentTypeCode.RESOLUCION_JEFATURAL,
    "RESOLUCION DE INTENDENCIA": DocumentTypeCode.RESOLUCION_DE_INTENDENCIA,
    "RESOLUCION DIRECTORAL": DocumentTypeCode.RESOLUCION_DIRECTORAL,
}

_ARTICLE_LABEL_RE = re.compile(
    r"^(?P<label>Art[íi]culo\s+(?:[ÚU]nico|\d+)\s*[º°]?\s*[.-]*)\s*",
    re.IGNORECASE,
)
_CLOSING_RE = re.compile(r"^Reg[íi]strese[,.]?\s", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"^-\s+\S")
_PUBLICATION_DATE_RE = re.compile(
    r"Fecha de publicaci[oó]n:\s*(?:<!--\s*-->)?\s*(\d{2}/\d{2}/\d{4})"
)
_UPPER_NAME_RE = re.compile(r"^[A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ\s.]+$")

# El token del path admite base62 más '_' y '-' (visto en fixtures reales:
# '41m_ySmF4OIAVBAIt5vud7', '5IL1gMpvK7HAMgIuW_-Mnp'). Restringirlo a
# alfanuméricos produce un falso negativo silencioso —"este dispositivo no
# declara PDF"— en vez de un fallo visible, así que el charset va explícito.
_PDF_PATH_TEMPLATE = r"/api/archivo/file/[A-Za-z0-9_-]+/[^\"'\\\s]*/{code}\.PDF"


class ParseError(ValueError):
    pass


def declared_pdf_url(page_text: str, code: str, canonical_url: str | None) -> str | None:
    """URL del PDF que la propia captura declara para este dispositivo.

    El visor la publica en un `<script>` del payload global (`"urlPDF"`), fuera
    de `div#x<código>`: es de la página, no del dispositivo, y una captura puede
    traer varios dispositivos. Por eso se ancla al código —solo se acepta si el
    archivo se llama `<código>.PDF`— y ante cualquier otra cosa se devuelve None
    en lugar de arriesgar un enlace al documento equivocado.

    El path es relativo; el origen se toma de la URL canónica de la referencia,
    nunca de una constante, para que apunte al mismo host del que se capturó.
    """
    if not canonical_url:
        return None
    match = re.search(_PDF_PATH_TEMPLATE.format(code=re.escape(code)), page_text, re.IGNORECASE)
    if match is None:
        return None
    origin = urlsplit(canonical_url)
    if not origin.scheme or not origin.netloc:
        return None
    return f"{origin.scheme}://{origin.netloc}{match.group(0)}"


def publication_date_phrase(page_text: str) -> tuple[str, int, int] | None:
    """Frase de la captura que declara la fecha de publicación, con su rango.

    Está fuera del contenedor del dispositivo —la pone la página del buscador—,
    así que su localización es un rango sobre el texto del artefacto decodificado
    y no sobre una sección. Se expone aparte del parseo completo porque la
    reparación de filas antiguas la necesita releyendo los bytes del CAS.
    """
    match = _PUBLICATION_DATE_RE.search(page_text)
    if match is None:
        return None
    return match.group(0), match.start(), match.end()


def _classify_doc_type(raw: str) -> DocumentTypeCode:
    key = collapse_whitespace(strip_accents(raw)).upper()
    return _DOC_TYPE_MAP.get(key, DocumentTypeCode.OTHER)


class ElPeruanoHtmlParser:
    """DocumentParser para el HTML del visor de El Peruano."""

    def parse(self, content: bytes, reference: SourceReference) -> ParsedDocument:
        page_text = content.decode("utf-8", errors="replace")
        tree = lxml_html.fromstring(page_text)

        code = reference.publication_code
        containers = tree.xpath(f"//div[@id='x{code}']")
        if not containers:
            raise ParseError(
                f"No se encontró el contenedor div#x{code}: el HTML no corresponde al "
                f"dispositivo esperado o está contaminado"
            )
        container = containers[0]

        story = container.xpath(".//div[@class='story']")
        root = story[0] if story else container

        # Fecha de publicación: aparece en la página del buscador, fuera del contenedor.
        published_on = None
        published_on_phrase = None
        published_on_char_start = None
        published_on_char_end = None
        m = _PUBLICATION_DATE_RE.search(page_text)
        if m:
            published_on = parse_ddmmyyyy(m.group(1))
            # El rango es sobre el texto del artefacto decodificado, no sobre una
            # sección: de ahí sale la evidencia citable de la fecha de publicación.
            published_on_phrase, published_on_char_start, published_on_char_end = (
                m.group(0),
                m.start(),
                m.end(),
            )

        title_raw: str | None = None
        doc_type_raw: str | None = None
        number_raw: str | None = None
        publication_code_seen: str | None = None
        issue_place: str | None = None
        issued_on = None

        sections: list[ParsedSection] = []
        order = 0
        state = "HEADER"  # HEADER -> VISTOS/CONSIDERANDO -> RESOLVE -> CLOSING/SIGNATURE

        def add(section_type: SectionType, text: str, label: str | None = None) -> None:
            nonlocal order
            sections.append(
                ParsedSection(
                    section_type=section_type,
                    label_raw=label,
                    order_index=order,
                    text_raw=text,
                    text_normalized=collapse_whitespace(text),
                )
            )
            order += 1

        for el in root.iter():
            if el.tag not in {"h1", "h2", "p"}:
                continue
            text = collapse_whitespace(el.text_content())
            if not text:
                continue
            upper = strip_accents(text).upper()

            if el.tag == "h1":
                title_raw = text
                add(SectionType.SUMMARY, text)
                continue
            if el.tag == "h2":
                if doc_type_raw is None:
                    doc_type_raw = text
                    add(SectionType.DOC_TYPE, text)
                elif number_raw is None:
                    number_raw = text
                    add(SectionType.DOC_NUMBER, text)
                else:
                    add(SectionType.OTHER, text)
                continue

            # <p>: depende del estado
            if text == code:
                publication_code_seen = text
                add(SectionType.PUBLICATION_CODE, text)
                continue
            if upper.startswith("VISTOS") or upper.startswith("VISTO:"):
                state = "VISTOS"
                add(SectionType.VISTOS, text, label=text)
                continue
            if upper.startswith("CONSIDERANDO") or upper.startswith("Y CONSIDERANDO"):
                state = "CONSIDERANDO"
                add(SectionType.CONSIDERANDO, text, label=text)
                continue
            if upper.startswith("SE RESUELVE"):
                state = "RESOLVE"
                add(SectionType.RESOLVE_HEADER, text, label=text)
                continue
            if _CLOSING_RE.match(text):
                state = "CLOSING"
                add(SectionType.CLOSING, text)
                continue

            if state == "HEADER":
                place, dt = parse_issue_line(text)
                if place is not None or dt is not None:
                    issue_place, issued_on = place, dt
                    add(SectionType.ISSUE_LINE, text)
                else:
                    add(SectionType.OTHER, text)
                continue
            if state == "VISTOS":
                add(SectionType.VISTOS, text)
                continue
            if state == "CONSIDERANDO":
                add(SectionType.CONSIDERANDO, text)
                continue
            if state == "RESOLVE":
                am = _ARTICLE_LABEL_RE.match(text)
                if am:
                    add(SectionType.ARTICLE, text, label=collapse_whitespace(am.group("label")))
                elif _LIST_ITEM_RE.match(text):
                    add(SectionType.ARTICLE_LIST_ITEM, text)
                else:
                    add(SectionType.OTHER, text)
                continue
            if state == "CLOSING":
                add(SectionType.SIGNATURE, text)
                continue
            add(SectionType.OTHER, text)

        missing = [
            name
            for name, value in {
                "sumilla": title_raw,
                "tipo de documento": doc_type_raw,
                "número": number_raw,
                "código de publicación": publication_code_seen,
            }.items()
            if not value
        ]
        if missing:
            raise ParseError(
                f"Campos obligatorios ausentes en {code}: {', '.join(missing)}. "
                f"HTML posiblemente contaminado o incompleto."
            )
        assert title_raw and doc_type_raw and number_raw  # para el type checker

        return ParsedDocument(
            publication_code=code,
            source_series=reference.source_series,
            canonical_url=reference.canonical_url,
            pdf_url=declared_pdf_url(page_text, code, reference.canonical_url),
            title_raw=title_raw,
            document_type_raw=doc_type_raw,
            document_type_code=_classify_doc_type(doc_type_raw),
            number_raw=number_raw,
            number_normalized=normalize_document_number(number_raw),
            issue_place_raw=issue_place,
            issued_on=issued_on,
            published_on=published_on,
            published_on_phrase=published_on_phrase,
            published_on_char_start=published_on_char_start,
            published_on_char_end=published_on_char_end,
            sections=sections,
        )
