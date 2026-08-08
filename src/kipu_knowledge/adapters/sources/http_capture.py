"""Captura HTTP con la política de la casa, común a todas las fuentes.

Vive fuera del adaptador de El Peruano porque el mismo acto se publica en varios
sitios (el diario oficial y el portal de la entidad emisora), y todos merecen el
mismo trato: User-Agent identificable, rate limit conservador, backoff
exponencial, respeto de Retry-After y cero evasión de bloqueos. Ver
docs/source-policy.md.

Cada instancia lleva su propio reloj de rate limit, así que conviene reutilizar
una por host en lugar de crear una por petición.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx

from kipu_knowledge import CRAWLER_VERSION
from kipu_knowledge.config import Settings, get_settings
from kipu_knowledge.domain.contracts import CaptureRecord

_KEPT_HEADERS = {"content-type", "etag", "last-modified", "cache-control", "date"}


class LiveSourceDisabled(RuntimeError):
    pass


class CaptureHttpError(RuntimeError):
    """La fuente respondió, pero con un estado que no permite guardar nada.

    Lleva el código para que quien orquesta pueda distinguir lo transitorio de
    lo definitivo. Importa porque en esta fuente se observó un 404 pasajero en
    la ruta del PDF (2026-08-07): tratarlo como error final perdería el
    documento en silencio, y reintentarlo en el acto castigaría al servidor.
    """

    def __init__(self, message: str, *, status_code: int | None, url: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url


class CaptureNetworkError(RuntimeError):
    """No hubo respuesta (DNS, timeout, conexión): siempre reintentable."""

    def __init__(self, message: str, *, url: str) -> None:
        super().__init__(message)
        self.url = url


class PoliteFetcher:
    """Descarga un recurso y devuelve sus bytes junto al acta de la captura."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._last_request_at: float = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self._settings.crawler_rate_limit_seconds - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def get(self, url: str) -> tuple[bytes, CaptureRecord]:
        if not self._settings.live_source_enabled:
            raise LiveSourceDisabled(
                "LIVE_SOURCE_ENABLED=false: la captura en vivo está deshabilitada. "
                "Usa fixtures (kipu ingest-fixture) o habilita la bandera tras revisar "
                "docs/source-policy.md"
            )

        headers = {"User-Agent": self._settings.crawler_user_agent}
        retries = 0
        last_error: str | None = None
        with httpx.Client(follow_redirects=True, timeout=30.0) as client:
            while retries <= self._settings.crawler_max_retries:
                self._throttle()
                try:
                    response = client.get(url, headers=headers)
                except httpx.HTTPError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    retries += 1
                    time.sleep(min(2**retries, 30))
                    continue
                if response.status_code in (429, 503):
                    retry_after = response.headers.get("Retry-After")
                    delay = (
                        float(retry_after) if retry_after and retry_after.isdigit() else 2**retries
                    )
                    retries += 1
                    time.sleep(min(delay, 60))
                    continue
                capture = CaptureRecord(
                    requested_url=url,
                    final_url=str(response.url),
                    http_status=response.status_code,
                    content_type=response.headers.get("Content-Type"),
                    byte_length=len(response.content),
                    captured_at=datetime.now(UTC),
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                    response_headers={
                        k: v for k, v in response.headers.items() if k.lower() in _KEPT_HEADERS
                    },
                    crawler_version=CRAWLER_VERSION,
                    retries=retries,
                )
                if response.status_code != 200:
                    raise CaptureHttpError(
                        f"Captura fallida ({response.status_code}) para {url}; "
                        f"reintentos: {retries}",
                        status_code=response.status_code,
                        url=url,
                    )
                return response.content, capture
        raise CaptureNetworkError(
            f"Captura fallida para {url} tras {retries} reintentos: {last_error}", url=url
        )
