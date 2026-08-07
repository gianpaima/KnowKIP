"""PDF (cuadernillo) y política de fuente viva."""

from __future__ import annotations

from pathlib import Path

import pytest

from kipu_knowledge.adapters.parsing.pdf_parser import extract_text_layer, pymupdf_available
from kipu_knowledge.adapters.sources.elperuano import (
    ElPeruanoSourceAdapter,
    LiveSourceDisabled,
)
from kipu_knowledge.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _find_issue_pdf() -> Path | None:
    """Localiza NL20260806.pdf sin asumir ruta fija."""
    for candidate in PROJECT_ROOT.rglob("NL20260806.pdf"):
        return candidate
    return None


ISSUE_PDF = _find_issue_pdf()


@pytest.mark.pdf
@pytest.mark.skipif(
    ISSUE_PDF is None,
    reason="El cuadernillo NL20260806.pdf no está disponible en el workspace "
    "(documentado en fixtures/manifest.json); no se inventa su contenido.",
)
@pytest.mark.skipif(not pymupdf_available(), reason="PyMuPDF no instalado (extra 'pdf')")
def test_issue_pdf_text_layer():
    pages = extract_text_layer(ISSUE_PDF.read_bytes())
    assert pages
    assert any("2540861-1" in p.text for p in pages)


class TestSourcePolicy:
    def test_live_fetch_disabled_by_default(self):
        adapter = ElPeruanoSourceAdapter(Settings(live_source_enabled=False))
        ref = adapter.parse_source_reference("2540861-1")
        with pytest.raises(LiveSourceDisabled):
            adapter.fetch(ref)

    def test_parse_reference_from_url(self):
        adapter = ElPeruanoSourceAdapter(Settings(live_source_enabled=False))
        ref = adapter.parse_source_reference(
            "https://busquedas.elperuano.pe/dispositivo/NL/2540861-1"
        )
        assert ref.source_series == "NL"
        assert ref.publication_code == "2540861-1"

    def test_parse_reference_rejects_garbage(self):
        adapter = ElPeruanoSourceAdapter(Settings(live_source_enabled=False))
        with pytest.raises(ValueError):
            adapter.parse_source_reference("https://ejemplo.com/otra-cosa")

    def test_discover_not_yet_implemented_returns_empty(self):
        from datetime import date

        adapter = ElPeruanoSourceAdapter(Settings(live_source_enabled=False))
        assert list(adapter.discover(date(2026, 8, 6))) == []
