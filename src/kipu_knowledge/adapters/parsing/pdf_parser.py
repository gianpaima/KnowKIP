"""Extracción de capa de texto de PDFs (PyMuPDF, dependencia opcional `pdf`).

El PDF es respaldo/evidencia: los bytes originales se conservan intactos en el
ArtifactStore y aquí solo se extrae la capa textual con paginación. No se aplica
OCR (extra opcional futuro) ni normalización destructiva.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PdfPageText:
    page_number: int  # 1-indexado
    text: str


def pymupdf_available() -> bool:
    try:
        import fitz  # noqa: F401

        return True
    except ImportError:
        return False


def extract_text_layer(content: bytes) -> list[PdfPageText]:
    """Extrae el texto por página. Lanza RuntimeError si PyMuPDF no está instalado."""
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError(
            "PyMuPDF no está instalado; instala el extra 'pdf' (uv sync --extra pdf)"
        ) from exc

    pages: list[PdfPageText] = []
    with fitz.open(stream=content, filetype="pdf") as doc:
        for index, page in enumerate(doc, start=1):
            pages.append(PdfPageText(page_number=index, text=page.get_text("text")))
    return pages


def has_usable_text_layer(pages: list[PdfPageText], min_chars: int = 50) -> bool:
    return sum(len(p.text.strip()) for p in pages) >= min_chars
