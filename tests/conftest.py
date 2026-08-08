"""Fixtures compartidos: BD SQLite en memoria, store temporal, ingesta completa.

Ninguna prueba requiere red ni servicios externos (regla de CI sin fuentes live).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from kipu_knowledge.adapters.db.models import Base
from kipu_knowledge.adapters.storage.fs_store import FilesystemArtifactStore
from kipu_knowledge.application.ingest import IngestService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = PROJECT_ROOT / "fixtures"

ALL_CODES = [
    "2540861-1",  # A: designación MIDAGRI sin fecha efectiva
    "2540903-1",  # B: renuncia CENEPRED con eficacia 2026-08-04
    "2540903-2",  # B: designación CENEPRED sin fecha
    "2540905-3",  # C: renuncia + designación SUNAT (dos eventos)
    "2540905-4",  # D: fin de encargo + nombramiento Tribunal Fiscal
    "2540779-1",  # E: designación BNP con fecha explícita y correlativo CAP
    "2540702-1",  # F: encargatura INBP con eficacia anticipada y condición final
    "2540905-2",  # G: directorio BCRP colectivo (3 personas)
    "2540896-1",  # H: designación PRODUCE + obligación de declaraciones juradas
]


# --- Recolección diaria: índice sintético y adaptador sin red ----------------
#
# Viven aquí, y no en el módulo de pruebas del recolector, porque los invariantes
# también necesitan ejercitar una corrida completa. El parser del índice se
# prueba aparte contra las capturas reales (tests/unit/test_listing_parser.py);
# esto sirve para probar la orquestación.

DAILY_CATALOGUE = [
    ("2540861-1", "Designan Jefa de la Oficina de Gestión Documental", "DESARROLLO AGRARIO"),
    ("2540903-1", "Aceptan renuncia de Jefe de la Oficina General de Administración", "CENEPRED"),
    ("2540779-1", "Designan Director de Sistema Administrativo IV", "BIBLIOTECA NACIONAL"),
    ("2540896-1", "Aprueban el Reglamento Interno de Trabajo", "PRODUCE"),
]
DAILY_RUN_DATE = date(2026, 8, 6)


def build_listing_html(entries, total: int, next_start: int | None = None) -> bytes:
    """Página del índice con la estructura mínima que el parser exige."""
    cards = "".join(
        "<div class='rounded-xl border bg-card'>"
        f"<p>{issuer}</p>"
        f"<a href='/dispositivo/NL/{code}'><p>RESOLUCIÓN MINISTERIAL</p><p>N° 1-2026</p></a>"
        f"<a href='/dispositivo/NL/{code}'>{summary}</a>"
        f"<span>{code}</span><span>jueves 06.08.2026</span>"
        "</div>"
        for code, summary, issuer in entries
    )
    pagination = (
        f"<a href='/?fechaIni=20260806&amp;start={next_start}'>siguiente</a>"
        if next_start is not None
        else ""
    )
    return (
        "<html><body><main>"
        "<p>Dispositivos del 06/08/2026 al 06/08/2026</p>"
        f"<p>{total}<!-- --> <!-- -->dispositivos encontrados</p>"
        f"{cards}{pagination}"
        "</main></body></html>"
    ).encode()


class FakeSourceAdapter:
    """Sirve el índice sintético y los fixtures locales en lugar de la red."""

    source_family = "EL_PERUANO_NL"
    base_url = "https://busquedas.elperuano.pe"

    def __init__(self, pages: list[bytes], *, fail_codes: dict[str, Exception] | None = None):
        self.pages = pages
        self.fail_codes = fail_codes or {}
        self.fetched: list[str] = []

    def iter_listing_pages(self, publication_date: date, series: str = "NL"):
        from kipu_knowledge.adapters.parsing.listing_parser import parse_listing

        for index, content in enumerate(self.pages):
            page = parse_listing(
                content,
                requested_date=publication_date,
                series=series,
                base_url=self.base_url,
                source_family=self.source_family,
            )
            url = f"{self.base_url}/?start={index * 20}"
            yield url, content, self._capture(url, len(content)), page

    def parse_source_reference(self, url_or_code: str):
        from kipu_knowledge.domain.contracts import SourceReference

        code = url_or_code.rstrip("/").rsplit("/", 1)[-1]
        return SourceReference(
            source_family=self.source_family,
            source_series="NL",
            publication_code=code,
            canonical_url=f"{self.base_url}/dispositivo/NL/{code}",
        )

    def fetch(self, reference):
        from kipu_knowledge.domain.contracts import FetchResult

        code = reference.publication_code
        self.fetched.append(code)
        if code in self.fail_codes:
            raise self.fail_codes.pop(code)
        content = (FIXTURES_DIR / "elperuano" / f"{code}.html").read_bytes()
        return FetchResult(
            reference=reference,
            content=content,
            capture=self._capture(reference.canonical_url or "", len(content)),
        )

    def _capture(self, url: str, length: int):
        from kipu_knowledge.domain.contracts import CaptureRecord

        return CaptureRecord(
            requested_url=url,
            final_url=url,
            http_status=200,
            content_type="text/html; charset=utf-8",
            byte_length=length,
            captured_at=datetime.now(UTC),
            crawler_version="fake/1.0",
        )


@pytest.fixture
def daily_kit():
    """Herramientas para ejercitar una corrida diaria completa sin red."""
    return SimpleNamespace(
        catalogue=DAILY_CATALOGUE,
        run_date=DAILY_RUN_DATE,
        listing=build_listing_html,
        adapter=FakeSourceAdapter,
    )


@pytest.fixture
def engine():
    # StaticPool + check_same_thread=False: la misma BD en memoria es visible
    # desde el hilo del TestClient de la API.
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    # SQLite ignora las claves foráneas salvo que se le pidan por conexión. Sin
    # esto la suite no puede ver un borrado en mal orden ni un huérfano —
    # exactamente lo que Postgres rechaza en producción—, así que las pruebas
    # daban por bueno lo que la BD real no acepta.
    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):  # noqa: ANN001, ANN202
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine) -> Iterator[Session]:
    with Session(engine) as s:
        yield s


@pytest.fixture
def store(tmp_path: Path) -> FilesystemArtifactStore:
    return FilesystemArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def ingest_service(session: Session, store: FilesystemArtifactStore) -> IngestService:
    return IngestService(session, store)


@pytest.fixture
def ingested_session(session: Session, ingest_service: IngestService) -> Session:
    """Sesión con los 9 casos A–H ingeridos."""
    for code in ALL_CODES:
        ingest_service.ingest_fixture(code, FIXTURES_DIR)
    session.commit()
    return session


@pytest.fixture
def api_client(engine, session, ingested_session, store):
    """TestClient de la API sobre la BD de prueba ya ingerida y su CAS."""
    from fastapi.testclient import TestClient

    from kipu_knowledge.interfaces.api.deps import get_db, get_store
    from kipu_knowledge.interfaces.api.main import app

    def _override():
        yield ingested_session

    app.dependency_overrides[get_db] = _override
    app.dependency_overrides[get_store] = lambda: store
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_store, None)
