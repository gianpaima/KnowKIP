"""Parser de páginas web de contexto (prensa, notas institucionales).

Estrategia:
1. Metadatos del JSON-LD que la propia página declara (schema.org NewsArticle
   y afines) — es lo que el publicador afirma de su artículo, no lo que
   nosotros interpretamos. Fallback: OpenGraph y <title>.
2. Cuerpo como lista ordenada de párrafos (<p>) del contenedor del artículo,
   con texto original conservado. La lista es reconstruible determinísticamente
   desde los bytes del CAS: los EvidenceSpan anclan por índice de párrafo y
   offsets, y la re-verificación re-extrae y compara.
3. Honestidad sobre el alcance: sin párrafos, `body_scope` degrada a
   METADATA_ONLY; con JSON-LD que declara `isAccessibleForFree: false`, a
   PARTIAL_PAYWALL. Nunca se citará como completo lo que no lo es.

No valida contra la lista blanca ni toca la red: parsea bytes que ya son
evidencia. La política vive en domain/web_sources y en la orquestación.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from datetime import datetime

from lxml import html as lxml_html

from kipu_knowledge.domain.enums import WebBodyScope, WebDocumentKind
from kipu_knowledge.domain.normalization import collapse_whitespace

WEB_PARSER_VERSION = "web-parser/1.0"

# Tipos schema.org que se aceptan como artículo periodístico o institucional.
_ARTICLE_TYPES = {
    "NewsArticle": WebDocumentKind.NEWS_ARTICLE,
    "ReportageNewsArticle": WebDocumentKind.NEWS_ARTICLE,
    "Article": WebDocumentKind.NEWS_ARTICLE,
    "SocialMediaPosting": WebDocumentKind.SOCIAL_POST,
    "ProfilePage": WebDocumentKind.SOCIAL_PROFILE,
}

# Un párrafo más corto que esto no es prosa del artículo (pies, créditos,
# "Comparte esta noticia"). El umbral es deliberadamente bajo: recortar de más
# perdería párrafos citables, y el ruido corto no hace daño — nadie lo citará.
_MIN_PARAGRAPH_CHARS = 25


class WebParseError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParsedWebPage:
    """Lo que la página declara de sí misma, más su cuerpo en párrafos."""

    kind: WebDocumentKind
    headline_raw: str | None
    published_at_raw: str | None
    published_at: datetime | None
    modified_at_raw: str | None
    author_raw: str | None
    section_raw: str | None
    language: str | None
    canonical_url_declared: str | None
    body_scope: WebBodyScope
    paragraphs: tuple[str, ...] = field(default_factory=tuple)

    @property
    def full_text(self) -> str:
        """Texto plano del cuerpo, reconstruible desde los párrafos.

        El separador doble salto es parte del contrato: los offsets de las
        citas se calculan sobre esta forma exacta.
        """
        return "\n\n".join(self.paragraphs)


def parse_web_page(content: bytes) -> ParsedWebPage:
    """Parsea los bytes capturados de una página de contexto."""
    try:
        # Se decodifica como UTF-8 explícitamente (con reemplazo) en vez de
        # dejar que lxml adivine: la adivinación convertía tildes en mojibake
        # cuando la página no declara charset, y las citas deben ser exactas.
        tree = lxml_html.fromstring(content.decode("utf-8", errors="replace"))
    except Exception as exc:  # lxml lanza variantes según el contenido
        raise WebParseError(f"HTML no parseable: {exc}") from exc

    jsonld = _first_article_jsonld(tree)
    kind = _kind_from(jsonld)
    headline = _headline(tree, jsonld)
    published_raw, modified_raw = _dates_raw(tree, jsonld)
    author = _author(jsonld, tree)
    section = _section(jsonld, tree)
    language = _language(tree)
    canonical = _canonical(tree)
    paragraphs = _paragraphs(tree)

    if not paragraphs:
        scope = WebBodyScope.METADATA_ONLY
    elif jsonld is not None and jsonld.get("isAccessibleForFree") in (False, "False", "false"):
        scope = WebBodyScope.PARTIAL_PAYWALL
    else:
        scope = WebBodyScope.FULL

    return ParsedWebPage(
        kind=kind,
        headline_raw=headline,
        published_at_raw=published_raw,
        published_at=_parse_iso(published_raw),
        modified_at_raw=modified_raw,
        author_raw=author,
        section_raw=section,
        language=language,
        canonical_url_declared=canonical,
        body_scope=scope,
        paragraphs=tuple(paragraphs),
    )


def _first_article_jsonld(tree) -> dict | None:  # noqa: ANN001 - elemento lxml
    for script in tree.xpath('//script[@type="application/ld+json"]'):
        raw = script.text_content()
        if not raw or not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for candidate in _flatten_jsonld(data):
            type_value = candidate.get("@type")
            types = type_value if isinstance(type_value, list) else [type_value]
            if any(t in _ARTICLE_TYPES for t in types if isinstance(t, str)):
                return candidate
    return None


def _flatten_jsonld(data) -> list[dict]:  # noqa: ANN001 - JSON arbitrario
    if isinstance(data, dict):
        if "@graph" in data and isinstance(data["@graph"], list):
            return [item for item in data["@graph"] if isinstance(item, dict)]
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _kind_from(jsonld: dict | None) -> WebDocumentKind:
    if jsonld is None:
        return WebDocumentKind.OTHER
    type_value = jsonld.get("@type")
    types = type_value if isinstance(type_value, list) else [type_value]
    for t in types:
        if isinstance(t, str) and t in _ARTICLE_TYPES:
            return _ARTICLE_TYPES[t]
    return WebDocumentKind.OTHER


# Los valores del JSON-LD llegan a veces con entidades HTML dentro del propio
# string JSON ("Luna-Victoria" como "Luna-Victoria", "ó" como "&oacute;";
# observado en RPP el 2026-08-18). Decodificarlas no altera lo declarado — es
# transporte, no contenido — y sin ello el titular guardado no es citable.
def _clean(value: str) -> str:
    return collapse_whitespace(html.unescape(value))


def _headline(tree, jsonld: dict | None) -> str | None:  # noqa: ANN001
    if jsonld is not None and isinstance(jsonld.get("headline"), str):
        return _clean(jsonld["headline"]) or None
    og = tree.xpath('//meta[@property="og:title"]/@content')
    if og:
        return _clean(og[0]) or None
    title = tree.xpath("//title/text()")
    return _clean(title[0]) if title else None


def _dates_raw(tree, jsonld: dict | None) -> tuple[str | None, str | None]:  # noqa: ANN001
    published = modified = None
    if jsonld is not None:
        if isinstance(jsonld.get("datePublished"), str):
            published = jsonld["datePublished"].strip()
        if isinstance(jsonld.get("dateModified"), str):
            modified = jsonld["dateModified"].strip()
    if published is None:
        meta = tree.xpath('//meta[@property="article:published_time"]/@content')
        published = meta[0].strip() if meta else None
    return published or None, modified or None


def _parse_iso(raw: str | None) -> datetime | None:
    """Fecha declarada, solo si es ISO-8601 inequívoco. Nada se adivina."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _author(jsonld: dict | None, tree) -> str | None:  # noqa: ANN001
    if jsonld is not None:
        author = jsonld.get("author")
        names: list[str] = []
        for entry in author if isinstance(author, list) else [author]:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.append(_clean(entry["name"]))
            elif isinstance(entry, str) and entry.strip():
                names.append(_clean(entry))
        if names:
            return ", ".join(names)
    meta = tree.xpath('//meta[@name="author"]/@content')
    return _clean(meta[0]) if meta and meta[0].strip() else None


def _section(jsonld: dict | None, tree) -> str | None:  # noqa: ANN001
    if jsonld is not None and isinstance(jsonld.get("articleSection"), str):
        return _clean(jsonld["articleSection"]) or None
    meta = tree.xpath('//meta[@property="article:section"]/@content')
    return _clean(meta[0]) if meta and meta[0].strip() else None


def _language(tree) -> str | None:  # noqa: ANN001
    lang = tree.xpath("//html/@lang")
    return lang[0].strip()[:10] if lang and lang[0].strip() else None


def _canonical(tree) -> str | None:  # noqa: ANN001
    href = tree.xpath('//link[@rel="canonical"]/@href')
    return href[0].strip() if href and href[0].strip() else None


# Contenedores donde vive la prosa del artículo, por orden de especificidad.
# El fallback al <body> entero arrastraría menús y artículos relacionados.
_BODY_CONTAINERS = (
    '//*[@itemprop="articleBody"]',
    "//article",
    '//*[contains(@class, "story-content") or contains(@class, "article-body") '
    'or contains(@class, "post-content") or contains(@class, "entry-content")]',
)


def _paragraphs(tree) -> list[str]:  # noqa: ANN001
    for xpath in _BODY_CONTAINERS:
        containers = tree.xpath(xpath)
        if not containers:
            continue
        paragraphs: list[str] = []
        for container in containers:
            for p in container.xpath(".//p"):
                text = collapse_whitespace(p.text_content())
                if len(text) >= _MIN_PARAGRAPH_CHARS:
                    paragraphs.append(text)
        if paragraphs:
            return _dedupe_preserving_order(paragraphs)
    return []


def _dedupe_preserving_order(paragraphs: list[str]) -> list[str]:
    """Quita duplicados exactos (maquetas que repiten el cuerpo para móvil).

    Conservar la primera aparición mantiene el orden de lectura; un duplicado
    exacto no aporta nada citable y rompería la unicidad del localizador.
    """
    seen: set[str] = set()
    unique: list[str] = []
    for text in paragraphs:
        if text not in seen:
            seen.add(text)
            unique.append(text)
    return unique
