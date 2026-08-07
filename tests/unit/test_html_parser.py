from datetime import date
from pathlib import Path

import pytest

from kipu_knowledge.adapters.parsing.html_parser import ElPeruanoHtmlParser, ParseError
from kipu_knowledge.domain.contracts import SourceReference
from kipu_knowledge.domain.enums import DocumentTypeCode, SectionType

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "elperuano"


def _ref(code: str) -> SourceReference:
    return SourceReference(
        "EL_PERUANO_NL", "NL", code, f"https://busquedas.elperuano.pe/dispositivo/NL/{code}"
    )


@pytest.fixture(scope="module")
def parser() -> ElPeruanoHtmlParser:
    return ElPeruanoHtmlParser()


def test_parses_header_metadata(parser):
    doc = parser.parse((FIXTURES / "2540861-1.html").read_bytes(), _ref("2540861-1"))
    assert doc.document_type_code == DocumentTypeCode.RESOLUCION_MINISTERIAL
    assert doc.number_normalized == "D000284-2026-MIDAGRI-DM"
    assert doc.issue_place_raw == "Jesús María"
    assert doc.issued_on == date(2026, 8, 5)
    assert doc.published_on == date(2026, 8, 6)
    assert doc.title_raw.startswith("Designan Jefa de Atención al Ciudadano")


def test_segments_sections_in_order(parser):
    doc = parser.parse((FIXTURES / "2540903-1.html").read_bytes(), _ref("2540903-1"))
    types = [s.section_type for s in doc.sections]
    assert SectionType.SUMMARY in types
    assert SectionType.CONSIDERANDO in types
    assert SectionType.RESOLVE_HEADER in types
    assert SectionType.ARTICLE in types
    assert SectionType.PUBLICATION_CODE in types
    # El orden respeta el documento: sumilla antes que artículos, código al final.
    assert types.index(SectionType.SUMMARY) < types.index(SectionType.ARTICLE)
    assert types[-1] == SectionType.PUBLICATION_CODE


def test_detects_articles_with_labels(parser):
    doc = parser.parse((FIXTURES / "2540905-3.html").read_bytes(), _ref("2540905-3"))
    labels = [s.label_raw for s in doc.articles()]
    assert labels == ["Artículo 1.-", "Artículo 2.-", "Artículo 3.-"]


def test_articulo_unico(parser):
    doc = parser.parse((FIXTURES / "2540861-1.html").read_bytes(), _ref("2540861-1"))
    assert [s.label_raw for s in doc.articles()] == ["Artículo Único.-"]


def test_collective_list_items(parser):
    doc = parser.parse((FIXTURES / "2540905-2.html").read_bytes(), _ref("2540905-2"))
    items = doc.sections_of(SectionType.ARTICLE_LIST_ITEM)
    assert len(items) == 3
    assert items[0].text_raw.startswith("- Inés")


def test_vistos_separated_from_considerandos(parser):
    # Regla 20: VISTOS no se mezclan con referencias normativas.
    doc = parser.parse((FIXTURES / "2540779-1.html").read_bytes(), _ref("2540779-1"))
    vistos = " ".join(s.text_raw for s in doc.sections_of(SectionType.VISTOS))
    considerandos = " ".join(s.text_raw for s in doc.sections_of(SectionType.CONSIDERANDO))
    assert "Informe Técnico" in vistos
    assert "Ley N° 27594" in considerandos
    assert "Ley N° 27594" not in vistos


def test_wrong_container_fails_explicitly(parser):
    # El HTML de un dispositivo no debe parsear como otro (contaminación).
    with pytest.raises(ParseError, match="contenedor"):
        parser.parse((FIXTURES / "2540861-1.html").read_bytes(), _ref("9999999-9"))


def test_missing_required_fields_fail(parser):
    html = (
        b"<html><body><div id='x1234567-1'><div class='story'>"
        b"<p class='cuerpo'>texto</p></div></div></body></html>"
    )
    with pytest.raises(ParseError, match="obligatorios"):
        parser.parse(html, _ref("1234567-1"))


ALL_CODES = sorted(p.stem for p in FIXTURES.glob("*.html"))


@pytest.mark.parametrize("code", ALL_CODES)
def test_extracts_the_pdf_declared_by_the_capture(parser, code):
    doc = parser.parse((FIXTURES / f"{code}.html").read_bytes(), _ref(code))
    assert doc.pdf_url, f"{code}: la captura declara urlPDF y el parser debe recogerlo"
    assert doc.pdf_url.startswith("https://busquedas.elperuano.pe/api/archivo/file/")
    assert doc.pdf_url.endswith(f"/{code}.PDF")


def test_pdf_token_may_contain_underscores_and_dashes(parser):
    """Regresión: con `[A-Za-z0-9]+` estos tres códigos parecían no declarar PDF.

    Un charset incompleto no falla, simplemente no encuentra nada, y el falso
    negativo se lee como «esta fuente no publica PDF». Los tres tokens con `_` o
    `-` que hay en el corpus quedan congelados aquí.
    """
    for code in ("2540779-1", "2540903-2", "2540905-3"):
        doc = parser.parse((FIXTURES / f"{code}.html").read_bytes(), _ref(code))
        assert doc.pdf_url and doc.pdf_url.endswith(f"/{code}.PDF")


def test_pdf_of_another_device_is_never_adopted(parser):
    """El payload es de la página, no del dispositivo: si el único PDF presente
    es de otro código, lo correcto es no registrar ninguno."""
    from kipu_knowledge.adapters.parsing.html_parser import declared_pdf_url

    page = '"urlPDF","/api/archivo/file/9GzJQ8i5avj9po6xjVgsNJ/*/2540861-1.PDF"'
    base = "https://busquedas.elperuano.pe/dispositivo/NL"
    assert declared_pdf_url(page, "2540861-1", f"{base}/2540861-1") is not None
    assert declared_pdf_url(page, "2540905-4", f"{base}/2540905-4") is None
    assert declared_pdf_url(page, "2540861-1", None) is None
    assert declared_pdf_url("sin payload", "2540861-1", f"{base}/2540861-1") is None
