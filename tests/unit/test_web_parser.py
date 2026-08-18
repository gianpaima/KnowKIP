"""Parser de páginas web de contexto: metadatos declarados y alcance honesto."""

from __future__ import annotations

from kipu_knowledge.adapters.parsing.web_parser import parse_web_page
from kipu_knowledge.domain.enums import WebBodyScope, WebDocumentKind
from kipu_knowledge.domain.web_sources import spec_for_url, url_disallowed_reason


def _page(jsonld: str = "", body: str = "", head_extra: str = "") -> bytes:
    return f"""<!DOCTYPE html><html lang="es"><head><title>Título de respaldo</title>
    {jsonld}{head_extra}</head><body>{body}</body></html>""".encode()


ARTICLE_JSONLD = """<script type="application/ld+json">{
  "@context": "https://schema.org",
  "@type": "NewsArticle",
  "headline": "Titular declarado por el medio",
  "datePublished": "2026-08-13T11:34:00-05:00",
  "dateModified": "2026-08-13T12:00:00-05:00",
  "author": [{"@type": "Person", "name": "Luz Alarcón"}],
  "articleSection": "Economía"
}</script>"""


class TestMetadata:
    def test_jsonld_manda_sobre_el_resto(self) -> None:
        page = parse_web_page(
            _page(
                ARTICLE_JSONLD,
                "<article><p>"
                + "Un párrafo con longitud suficiente para contar como prosa real."
                + "</p></article>",
            )
        )
        assert page.kind == WebDocumentKind.NEWS_ARTICLE
        assert page.headline_raw == "Titular declarado por el medio"
        assert page.published_at_raw == "2026-08-13T11:34:00-05:00"
        assert page.published_at is not None and page.published_at.year == 2026
        assert page.author_raw == "Luz Alarcón"
        assert page.section_raw == "Economía"
        assert page.language == "es"
        assert page.body_scope == WebBodyScope.FULL

    def test_sin_jsonld_cae_a_opengraph_y_title(self) -> None:
        page = parse_web_page(
            _page(
                head_extra='<meta property="og:title" content="Titular OpenGraph"/>',
                body=(
                    "<article><p>Párrafo de prosa con longitud más que suficiente aquí."
                    "</p></article>"
                ),
            )
        )
        assert page.kind == WebDocumentKind.OTHER
        assert page.headline_raw == "Titular OpenGraph"

    def test_jsonld_en_grafo_tambien_se_encuentra(self) -> None:
        jsonld = """<script type="application/ld+json">{
          "@graph": [{"@type": "WebSite"}, {"@type": "NewsArticle", "headline": "En el grafo"}]
        }</script>"""
        page = parse_web_page(_page(jsonld))
        assert page.headline_raw == "En el grafo"

    def test_entidades_html_dentro_del_jsonld_se_decodifican(self) -> None:
        # Observado en RPP (2026-08-18): el string JSON trae "&oacute;" dentro.
        jsonld = """<script type="application/ld+json">{
          "@type": "NewsArticle",
          "headline": "Gobierno design&oacute; a C&eacute;sar Luna-Victoria",
          "author": {"@type": "Person", "name": "Redacci&oacute;n"}
        }</script>"""
        page = parse_web_page(_page(jsonld))
        assert page.headline_raw == "Gobierno designó a César Luna-Victoria"
        assert page.author_raw == "Redacción"

    def test_fecha_no_iso_se_conserva_cruda_sin_adivinar(self) -> None:
        jsonld = """<script type="application/ld+json">{
          "@type": "NewsArticle", "headline": "X", "datePublished": "13 de agosto de 2026"
        }</script>"""
        page = parse_web_page(_page(jsonld))
        assert page.published_at_raw == "13 de agosto de 2026"
        assert page.published_at is None


class TestBodyScope:
    def test_sin_parrafos_es_solo_metadatos(self) -> None:
        page = parse_web_page(_page(ARTICLE_JSONLD, "<article><p>corto</p></article>"))
        assert page.body_scope == WebBodyScope.METADATA_ONLY
        assert page.paragraphs == ()

    def test_muro_de_pago_declarado_degrada_el_alcance(self) -> None:
        jsonld = """<script type="application/ld+json">{
          "@type": "NewsArticle", "headline": "X", "isAccessibleForFree": false
        }</script>"""
        body = "<article><p>El primer párrafo gratuito antes del muro del medio.</p></article>"
        page = parse_web_page(_page(jsonld, body))
        assert page.body_scope == WebBodyScope.PARTIAL_PAYWALL

    def test_parrafos_duplicados_por_maquetacion_se_deduplican(self) -> None:
        paragraph = "<p>El mismo párrafo servido dos veces por la maquetación móvil.</p>"
        page = parse_web_page(_page(ARTICLE_JSONLD, f"<article>{paragraph}{paragraph}</article>"))
        assert len(page.paragraphs) == 1


class TestWhitelistRules:
    def test_hosts_admitidos_y_no_admitidos(self) -> None:
        assert spec_for_url("https://rpp.pe/economia/nota").name == "RPP Noticias"
        assert spec_for_url("https://www.larepublica.pe/economia/nota").name == "La República"
        assert spec_for_url("https://elcomercio.pe/nota") is None

    def test_rutas_prohibidas_por_robots(self) -> None:
        spec = spec_for_url("https://rpp.pe/x")
        assert url_disallowed_reason(spec, "https://rpp.pe/buscar?q=sunat") is not None
        assert url_disallowed_reason(spec, "https://rpp.pe/economia/nota-123") is None
        lr = spec_for_url("https://larepublica.pe/x")
        assert url_disallowed_reason(lr, "https://larepublica.pe/buscador/sunat") is not None
