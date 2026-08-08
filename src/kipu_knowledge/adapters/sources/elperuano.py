"""Adaptador de fuente para el buscador de El Peruano (busquedas.elperuano.pe).

Política de captura (docs/source-policy.md):
- LIVE_SOURCE_ENABLED=false por defecto: sin esa bandera, fetch() falla explícitamente.
- User-Agent identificable y configurable, rate limit conservador, backoff exponencial,
  respeto de Retry-After. Sin evasión de bloqueos ni CAPTCHA.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from datetime import date

from kipu_knowledge.adapters.parsing.listing_parser import (
    ListingPage,
    ListingParseError,
    parse_listing,
)
from kipu_knowledge.adapters.sources.http_capture import LiveSourceDisabled, PoliteFetcher
from kipu_knowledge.config import Settings, get_settings
from kipu_knowledge.domain.contracts import (
    CaptureRecord,
    FetchResult,
    ListingEntry,
    SourceReference,
)

_DEVICE_URL_RE = re.compile(
    r"^https?://busquedas\.elperuano\.pe/dispositivo/(?P<series>[A-Z]{1,5})/(?P<code>\d{5,9}-\d+)/?$"
)
_CODE_RE = re.compile(r"^(?P<code>\d{5,9}-\d+)$")

SOURCE_FAMILY = "EL_PERUANO_NL"
BASE_URL = "https://busquedas.elperuano.pe"


# Reexportado por compatibilidad: la excepción vive ahora con la política HTTP.
__all__ = ["BASE_URL", "SOURCE_FAMILY", "ElPeruanoSourceAdapter", "LiveSourceDisabled"]


class ElPeruanoSourceAdapter:
    source_family = SOURCE_FAMILY

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._fetcher = PoliteFetcher(self._settings)

    def parse_source_reference(self, url_or_code: str) -> SourceReference:
        url_or_code = url_or_code.strip()
        m = _DEVICE_URL_RE.match(url_or_code)
        if m:
            return SourceReference(
                source_family=SOURCE_FAMILY,
                source_series=m.group("series"),
                publication_code=m.group("code"),
                canonical_url=f"{BASE_URL}/dispositivo/{m.group('series')}/{m.group('code')}",
            )
        m = _CODE_RE.match(url_or_code)
        if m:
            return SourceReference(
                source_family=SOURCE_FAMILY,
                source_series="NL",
                publication_code=m.group("code"),
                canonical_url=f"{BASE_URL}/dispositivo/NL/{m.group('code')}",
            )
        raise ValueError(f"No es una URL o código de dispositivo de El Peruano: {url_or_code!r}")

    def listing_url_for(self, publication_date: date, series: str = "NL", start: int = 0) -> str:
        """URL del índice de una fecha, tal como la construye el propio sitio.

        Es la misma que enlaza la portada ("Dispositivos" de cada edición), no
        una ruta adivinada: `ci=ONLY` restringe a los dispositivos de la serie y
        `start` pagina. Ver docs/source-policy.md (inspección del 2026-08-07).
        """
        stamp = publication_date.strftime("%Y%m%d")
        return (
            f"{BASE_URL}/?fechaIni={stamp}&fechaFin={stamp}"
            f"&tipoPublicacion={series}&ci=ONLY&start={start}"
        )

    def iter_listing_pages(
        self, publication_date: date, series: str = "NL"
    ) -> Iterator[tuple[str, bytes, CaptureRecord, ListingPage]]:
        """Recorre las páginas del índice devolviendo también sus bytes.

        Los bytes salen del adaptador porque el índice es evidencia: es la
        constancia de qué dijo la fuente que se publicó ese día. Quien orquesta
        decide si los archiva, pero no puede archivar lo que no ve.

        La paginación no supone un tamaño de página: se sigue el conjunto de
        valores `start` que la propia página enlaza, en orden creciente, hasta
        agotarlo. El tope de `crawler_max_listing_pages` está para que un enlace
        cíclico no convierta el descubrimiento en un bucle contra la fuente.
        """
        pending: set[int] = {0}
        visited: set[int] = set()
        pages = 0
        while pending:
            start = min(pending)
            pending.discard(start)
            visited.add(start)
            if pages >= self._settings.crawler_max_listing_pages:
                raise ListingParseError(
                    f"El índice del {publication_date.isoformat()} superó "
                    f"{self._settings.crawler_max_listing_pages} páginas; se detiene "
                    f"para no recorrer la fuente sin límite"
                )
            url = self.listing_url_for(publication_date, series, start)
            content, capture = self._fetcher.get(url)
            page = parse_listing(
                content,
                requested_date=publication_date,
                series=series,
                base_url=BASE_URL,
                source_family=self.source_family,
            )
            pages += 1
            yield url, content, capture, page
            pending |= {value for value in page.pagination_starts if value not in visited}

    def discover_entries(self, publication_date: date, series: str = "NL") -> list[ListingEntry]:
        """Dispositivos publicados en una fecha, con lo que el índice declara.

        Contrasta el total que la fuente declara contra lo recolectado: si no
        cuadra, falla en vez de devolver una lista incompleta que se leería como
        "ese día se publicó menos".
        """
        entries: dict[str, ListingEntry] = {}
        total_declared: int | None = None
        for _url, _content, _capture, page in self.iter_listing_pages(publication_date, series):
            total_declared = page.total_declared
            for entry in page.entries:
                entries.setdefault(entry.reference.publication_code, entry)
        if total_declared is not None and len(entries) != total_declared:
            raise ListingParseError(
                f"El índice del {publication_date.isoformat()} declara {total_declared} "
                f"dispositivos y se recolectaron {len(entries)}: la paginación quedó "
                f"incompleta o la fuente cambió durante el recorrido"
            )
        return list(entries.values())

    def discover(self, publication_date: date) -> Iterable[SourceReference]:
        return [entry.reference for entry in self.discover_entries(publication_date)]

    def pdf_viewer_url(self, reference: SourceReference) -> str | None:
        """Página del visor de PDF del dispositivo, derivada del código.

        Cuidado: `…/dispositivo/NL/<código>/pdf` **no** es el archivo. Devuelve
        `text/html`: es la página que muestra el PDF incrustado. Sirve para que
        una persona lo mire en la fuente; para respaldar los bytes hay que ir a
        la URL que declara el payload del visor, que es la única que responde
        `application/pdf` (comprobado el 2026-08-07).
        """
        if not reference.canonical_url:
            return None
        return f"{reference.canonical_url.rstrip('/')}/pdf"

    def issue_url_for(self, issue_code: str) -> str:
        """Cuadernillo de una edición: `NL20260806` → `…/cuadernillo/NL/20260806`."""
        m = re.match(r"^(?P<series>[A-Z]{1,5})(?P<date>\d{8})$", issue_code)
        if not m:
            raise ValueError(f"Código de cuadernillo no reconocido: {issue_code!r}")
        return f"{BASE_URL}/cuadernillo/{m.group('series')}/{m.group('date')}"

    def fetch(self, reference: SourceReference) -> FetchResult:
        url = reference.canonical_url
        if not url:
            raise ValueError("La referencia no tiene URL canónica")
        content, capture = self.fetch_url(url)
        return FetchResult(reference=reference, content=content, capture=capture)

    def fetch_url(self, url: str) -> tuple[bytes, CaptureRecord]:
        """Captura cualquier recurso de esta fuente con la política de la casa."""
        return self._fetcher.get(url)
