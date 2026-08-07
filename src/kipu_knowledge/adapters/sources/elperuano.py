"""Adaptador de fuente para el buscador de El Peruano (busquedas.elperuano.pe).

Política de captura (docs/source-policy.md):
- LIVE_SOURCE_ENABLED=false por defecto: sin esa bandera, fetch() falla explícitamente.
- User-Agent identificable y configurable, rate limit conservador, backoff exponencial,
  respeto de Retry-After. Sin evasión de bloqueos ni CAPTCHA.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date

from kipu_knowledge.adapters.sources.http_capture import LiveSourceDisabled, PoliteFetcher
from kipu_knowledge.config import Settings, get_settings
from kipu_knowledge.domain.contracts import CaptureRecord, FetchResult, SourceReference

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

    def discover(self, publication_date: date) -> Iterable[SourceReference]:
        """Descubrimiento por fecha: interfaz preparada, aún no implementada.

        El buscador expone búsqueda por cuadernillo (p.ej. NL20260806); implementar
        el descubrimiento requiere validar la política de scraping de listados.
        El MVP ingiere URLs individuales y fixtures.
        """
        return []

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
