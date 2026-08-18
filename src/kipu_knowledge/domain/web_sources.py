"""Lista blanca de publicadores de contexto web, con su inspección fechada.

La política (docs/source-policy.md) exige que cada medio o plataforma se
habilite individualmente tras inspeccionar su robots.txt y condiciones, y que
la inspección quede registrada con fecha. Este catálogo ES ese registro en
forma ejecutable: la captura se niega a tocar un dominio que no esté aquí, y
dentro de un dominio admitido se niega a las rutas que su robots.txt prohíbe
para `User-agent: *` y que podrían tentarnos (buscadores del sitio, tags,
archivo histórico).

Las reglas se copian de la inspección, no se consultan en vivo: una lista
estática es auditable y reproducible, y la política ya obliga a re-inspeccionar
antes de ampliar el uso. Si el robots.txt cambia, se re-inspecciona y se
actualiza este catálogo con nueva fecha — nunca se "adapta" en caliente.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

from kipu_knowledge.domain.enums import SourceAuthority


@dataclass(frozen=True)
class WebSourceSpec:
    """Un publicador admitido, con lo que su inspección permitió y prohibió."""

    name: str
    source_family: str
    base_url: str
    authority: SourceAuthority
    hosts: tuple[str, ...]
    # Prefijos de ruta que el robots.txt del publicador prohíbe a `*` y que son
    # relevantes para este uso (el resto de sus reglas —recursos internos,
    # rutas PHP— no son rutas que este sistema pediría jamás).
    disallowed_path_prefixes: tuple[str, ...]
    inspected_on: date
    inspection_notes: str


# Inspecciones del 2026-08-18 (detalle en docs/source-policy.md). Ambos medios
# prohíben su buscador interno a `User-agent: *`: el descubrimiento legítimo es
# por URL explícita, RSS o sitemaps declarados — nunca recorriendo su búsqueda.
WEB_SOURCES: tuple[WebSourceSpec, ...] = (
    WebSourceSpec(
        name="RPP Noticias",
        source_family="RPP",
        base_url="https://rpp.pe",
        authority=SourceAuthority.PRESS,
        hosts=("rpp.pe", "www.rpp.pe"),
        disallowed_path_prefixes=(
            "/buscar",
            "/tema",
            "/amp/",
            "/archivo/",
            "/alert",
            "/p/",
            "/basics/",
        ),
        inspected_on=date(2026, 8, 18),
        inspection_notes=(
            "robots.txt permite artículos a `*`; prohíbe /buscar, /tema, /amp, "
            "archivo histórico. Bloquea por nombre bots de entrenamiento de IA "
            "(GPTBot, anthropic-ai, etc.); este crawler no es uno de ellos, se "
            "identifica con su propio User-Agent y no republica cuerpos. "
            "Artículos server-rendered con JSON-LD NewsArticle."
        ),
    ),
    WebSourceSpec(
        name="La República",
        source_family="LAREPUBLICA",
        base_url="https://larepublica.pe",
        authority=SourceAuthority.PRESS,
        hosts=("larepublica.pe", "www.larepublica.pe"),
        disallowed_path_prefixes=(
            "/buscador",
            "/archive/",
            "/archivo/",
            "/envivo/",
            "/node/",
            "/taxonomy/",
        ),
        inspected_on=date(2026, 8, 18),
        inspection_notes=(
            "robots.txt permite artículos a `*` y declara Allow explícito para "
            "/sitemap, /sitemaps y /rss (vía legítima de descubrimiento futuro); "
            "prohíbe /buscador, archivo, paginación y tags. JSON-LD NewsArticle."
        ),
    ),
)


def spec_for_url(url: str) -> WebSourceSpec | None:
    """El publicador admitido al que pertenece la URL, o None si no hay ninguno."""
    host = urlsplit(url).netloc.lower().split(":", 1)[0]
    for spec in WEB_SOURCES:
        if host in spec.hosts:
            return spec
    return None


def url_disallowed_reason(spec: WebSourceSpec, url: str) -> str | None:
    """Por qué la política impide capturar esta URL del publicador, o None.

    Comprueba los prefijos que su robots.txt prohíbe a `*` según la inspección
    registrada. No es un parser de robots completo: es la transcripción de lo
    inspeccionado, y ante la duda la ruta se añade aquí, no se discute en vivo.
    """
    path = urlsplit(url).path or "/"
    for prefix in spec.disallowed_path_prefixes:
        if path.startswith(prefix):
            return (
                f"ruta prohibida por robots.txt de {spec.name} "
                f"(prefijo {prefix!r}, inspección {spec.inspected_on.isoformat()})"
            )
    return None
