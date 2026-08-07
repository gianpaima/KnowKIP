"""Parser del índice diario de dispositivos del buscador de El Peruano.

El cuadernillo (`/cuadernillo/NL/YYYYMMDD`) **no** es el índice del día: su
página solo declara el PDF de la edición completa. Se comprobó sobre los bytes
ya capturados de NL20260806, que no contienen ni un solo enlace a un
dispositivo. El índice real es la búsqueda por rango de fechas que el propio
sitio usa en su portada:

    /?fechaIni=YYYYMMDD&fechaFin=YYYYMMDD&tipoPublicacion=NL&ci=ONLY&start=N

La página se sirve renderizada en el servidor, así que se parsea como HTML sin
ejecutar JavaScript.

Tres guardas, porque un descubrimiento equivocado no falla de forma ruidosa
—simplemente ingiere el día que no era, o menos normas de las que hubo—:

1. La página declara el rango consultado ("Dispositivos del dd/mm/aaaa al
   dd/mm/aaaa"). Se exige que coincida con la fecha pedida. Sin esta guarda, la
   redirección de la portada a la fecha de hoy pasaría inadvertida.
2. La página declara el total ("N dispositivos encontrados"). El recolector lo
   contrasta contra lo que juntó entre todas las páginas.
3. Cada tarjeta repite el código en el enlace y en su pie. Si no coinciden, el
   HTML está contaminado y se falla explícito (misma política que el parser de
   dispositivo con `div#x<código>`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from lxml import html as lxml_html

from kipu_knowledge.domain.contracts import ListingEntry, SourceReference
from kipu_knowledge.domain.normalization import collapse_whitespace

_HREF_RE = re.compile(r"^/dispositivo/(?P<series>[A-Z]{1,5})/(?P<code>\d{5,9}-\d+)/?$")
_RANGE_RE = re.compile(r"Dispositivos del\s+(\d{2}/\d{2}/\d{4})\s+al\s+(\d{2}/\d{2}/\d{4})")
# El total viaja partido por comentarios HTML ("32<!-- --> <!-- -->dispositivos
# encontrados"), igual que la fecha de publicación en la página del dispositivo;
# por eso se busca sobre el texto ya extraído por lxml, no sobre los bytes.
_TOTAL_RE = re.compile(r"(\d+)\s*dispositivos?\s+encontrados?")
_START_RE = re.compile(r"[?&]start=(\d+)")


class ListingParseError(ValueError):
    pass


@dataclass(frozen=True)
class ListingPage:
    """Una página del índice, tal como la declara la fuente."""

    entries: tuple[ListingEntry, ...]
    total_declared: int
    range_from: date
    range_to: date
    # Valores de `start` enlazados por la paginación de esta página. El
    # recolector los usa para saber qué queda por visitar sin suponer un tamaño
    # de página fijo.
    pagination_starts: tuple[int, ...]


def _text(element) -> str:  # noqa: ANN001
    return collapse_whitespace(element.text_content())


def _parse_ddmmyyyy(raw: str) -> date:
    day, month, year = (int(part) for part in raw.split("/"))
    return date(year, month, day)


def _entry_from_card(card, series_expected: str, base_url: str, source_family: str):  # noqa: ANN001, ANN202
    """Convierte una tarjeta del listado en una entrada, o None si no lo es."""
    linked: list[tuple[re.Match[str], object]] = []
    for anchor in card.xpath(".//a[@href]"):
        match = _HREF_RE.match(anchor.get("href", ""))
        if match:
            linked.append((match, anchor))
    if not linked:
        return None  # tarjeta sin dispositivo (cabecera, anuncio): no es una entrada

    codes = {match.group("code") for match, _ in linked}
    if len(codes) > 1:
        raise ListingParseError(
            f"Una tarjeta del listado enlaza {len(codes)} dispositivos distintos "
            f"({', '.join(sorted(codes))}): HTML contaminado"
        )
    match, _ = linked[0]
    code = match.group("code")
    series = match.group("series")
    if series != series_expected:
        raise ListingParseError(
            f"El listado de la serie {series_expected} devolvió un dispositivo de "
            f"la serie {series} ({code})"
        )

    footer = [collapse_whitespace(value) for value in card.xpath(".//span/text()")]
    if code not in footer:
        raise ListingParseError(
            f"La tarjeta de {code} no repite su código en el pie {footer!r}: "
            f"el HTML no tiene la estructura esperada"
        )
    listed_date_raw = next((value for value in footer if value != code), None)

    # Estructura, no clases CSS: el ancla con dos <p> lleva tipo y número; el
    # ancla sin <p> lleva la sumilla; el <p> que no está dentro de ningún ancla
    # es la entidad emisora.
    document_type_raw: str | None = None
    number_raw: str | None = None
    summary_raw: str | None = None
    for _, anchor in linked:
        paragraphs = anchor.xpath(".//p")
        if paragraphs:
            texts = [_text(p) for p in paragraphs if _text(p)]
            if texts and document_type_raw is None:
                document_type_raw = texts[0]
                number_raw = texts[1] if len(texts) > 1 else None
        else:
            text = _text(anchor)
            if text and (summary_raw is None or len(text) > len(summary_raw)):
                summary_raw = text

    issuer_raw = next(
        (_text(p) for p in card.xpath(".//p[not(ancestor::a)]") if _text(p)),
        None,
    )

    return ListingEntry(
        reference=SourceReference(
            source_family=source_family,
            source_series=series,
            publication_code=code,
            canonical_url=f"{base_url.rstrip('/')}/dispositivo/{series}/{code}",
        ),
        issuer_raw=issuer_raw,
        document_type_raw=document_type_raw,
        number_raw=number_raw,
        summary_raw=summary_raw,
        listed_date_raw=listed_date_raw,
    )


def parse_listing(
    content: bytes,
    *,
    requested_date: date,
    series: str,
    base_url: str,
    source_family: str,
) -> ListingPage:
    """Lee una página del índice y comprueba que responde a lo que se pidió."""
    text = content.decode("utf-8", errors="replace")
    tree = lxml_html.fromstring(text)
    page_text = collapse_whitespace(tree.text_content())

    range_match = _RANGE_RE.search(page_text)
    if range_match is None:
        raise ListingParseError(
            "El listado no declara el rango de fechas consultado; la página no es "
            "la de resultados o su estructura cambió"
        )
    range_from = _parse_ddmmyyyy(range_match.group(1))
    range_to = _parse_ddmmyyyy(range_match.group(2))
    if range_from != requested_date or range_to != requested_date:
        raise ListingParseError(
            f"Se pidió el listado del {requested_date.isoformat()} y la página declara "
            f"{range_from.isoformat()}..{range_to.isoformat()}: la fuente redirigió o "
            f"ignoró el filtro (no se ingiere una fecha por otra)"
        )

    total_match = _TOTAL_RE.search(page_text)
    if total_match is None:
        raise ListingParseError(
            "El listado no declara cuántos dispositivos encontró; sin ese total no "
            "hay forma de saber si la recolección quedó completa"
        )
    total_declared = int(total_match.group(1))

    entries: list[ListingEntry] = []
    seen: set[str] = set()
    for card in tree.xpath("//div[contains(@class,'bg-card')]"):
        entry = _entry_from_card(card, series, base_url, source_family)
        if entry is None:
            continue
        if entry.reference.publication_code in seen:
            continue
        seen.add(entry.reference.publication_code)
        entries.append(entry)

    if total_declared > 0 and not entries:
        raise ListingParseError(
            f"El listado declara {total_declared} dispositivos y no se pudo extraer "
            f"ninguno: la estructura de las tarjetas cambió"
        )

    starts = sorted({int(value) for value in _START_RE.findall(text)})
    return ListingPage(
        entries=tuple(entries),
        total_declared=total_declared,
        range_from=range_from,
        range_to=range_to,
        pagination_starts=tuple(starts),
    )
