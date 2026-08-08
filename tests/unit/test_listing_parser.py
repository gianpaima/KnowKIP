"""Parser del índice diario: sobre las capturas reales del 2026-08-07."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from kipu_knowledge.adapters.parsing.listing_parser import ListingParseError, parse_listing

LISTING_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "elperuano" / "listing"
BASE_URL = "https://busquedas.elperuano.pe"


def _parse(name: str, requested: date):
    return parse_listing(
        (LISTING_DIR / f"{name}.html").read_bytes(),
        requested_date=requested,
        series="NL",
        base_url=BASE_URL,
        source_family="EL_PERUANO_NL",
    )


def test_first_page_declares_total_and_yields_its_cards() -> None:
    page = _parse("NL-20260807-start0", date(2026, 8, 7))
    assert page.total_declared == 32
    assert page.range_from == page.range_to == date(2026, 8, 7)
    assert len(page.entries) == 20
    # La paginación enlaza la página siguiente, no la actual.
    assert page.pagination_starts == (20,)


def test_second_page_completes_the_declared_total() -> None:
    first = _parse("NL-20260807-start0", date(2026, 8, 7))
    second = _parse("NL-20260807-start20", date(2026, 8, 7))
    codes = {entry.reference.publication_code for entry in first.entries + second.entries}
    assert len(codes) == first.total_declared == 32
    assert second.pagination_starts == (0,)


def test_entry_carries_what_the_index_declares() -> None:
    page = _parse("NL-20260807-start0", date(2026, 8, 7))
    entry = next(e for e in page.entries if e.reference.publication_code == "2540832-1")
    assert entry.reference.canonical_url == f"{BASE_URL}/dispositivo/NL/2540832-1"
    assert entry.reference.source_series == "NL"
    assert entry.issuer_raw == "CONTRALORÍA GENERAL"
    assert entry.document_type_raw == "RESOLUCIÓN"
    assert entry.number_raw == "N° 430-2026-CG"
    assert entry.summary_raw == (
        "Designan Jefa del Órgano de Control Institucional del Ministerio de "
        "Justicia y Derechos Humanos"
    )
    assert entry.listed_date_raw == "viernes 07.08.2026"


def test_listing_of_another_date_is_rejected() -> None:
    """La portada redirige a la fecha de hoy; sin esta guarda se ingeriría el día
    equivocado en silencio."""
    with pytest.raises(ListingParseError, match="02.08.2026|2026-08-02"):
        _parse("NL-20260802-start0", date(2026, 8, 7))


def test_page_without_results_header_fails_explicitly() -> None:
    with pytest.raises(ListingParseError, match="rango de fechas"):
        parse_listing(
            b"<html><body><p>otra cosa</p></body></html>",
            requested_date=date(2026, 8, 7),
            series="NL",
            base_url=BASE_URL,
            source_family="EL_PERUANO_NL",
        )


def test_page_declaring_results_without_cards_fails_explicitly() -> None:
    html = (
        b"<html><body><p>Dispositivos del 07/08/2026 al 07/08/2026</p>"
        b"<p>32 dispositivos encontrados</p></body></html>"
    )
    with pytest.raises(ListingParseError, match="no se pudo extraer"):
        parse_listing(
            html,
            requested_date=date(2026, 8, 7),
            series="NL",
            base_url=BASE_URL,
            source_family="EL_PERUANO_NL",
        )


def test_card_whose_footer_code_differs_is_rejected() -> None:
    """Misma política que `div#x<código>`: si la tarjeta no se confirma a sí
    misma, el HTML está contaminado y no se adivina."""
    html = (
        "<html><body><p>Dispositivos del 07/08/2026 al 07/08/2026</p>"
        "<p>1 dispositivos encontrados</p>"
        "<div class='rounded-xl border bg-card'>"
        "<p>CONTRALORÍA GENERAL</p>"
        "<a href='/dispositivo/NL/2540832-1'>Designan Jefa</a>"
        "<span>2540999-9</span><span>viernes 07.08.2026</span>"
        "</div></body></html>"
    ).encode()
    with pytest.raises(ListingParseError, match="no repite su código"):
        parse_listing(
            html,
            requested_date=date(2026, 8, 7),
            series="NL",
            base_url=BASE_URL,
            source_family="EL_PERUANO_NL",
        )
