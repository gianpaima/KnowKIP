"""Fixtures compartidos: BD SQLite en memoria, store temporal, ingesta completa.

Ninguna prueba requiere red ni servicios externos (regla de CI sin fuentes live).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
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


@pytest.fixture
def engine():
    # StaticPool + check_same_thread=False: la misma BD en memoria es visible
    # desde el hilo del TestClient de la API.
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
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
