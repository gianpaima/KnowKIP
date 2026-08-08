"""Comprobación de migraciones: upgrade head sobre una BD limpia (SQLite archivo).

La corrida contra PostgreSQL real se hace con docker compose (ver README);
esta prueba garantiza en CI que la migración es ejecutable y completa.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

PROJECT_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_TABLES = {
    "source_system",
    "crawl_run",
    "crawl_item",
    "publication_issue",
    "publication_item",
    "artifact",
    "artifact_version",
    "legal_document",
    "document_source",
    "document_section",
    "document_reference",
    "evidence_span",
    "extraction_run",
    "assertion",
    "person",
    "person_mention",
    "organization",
    "organization_mention",
    "organizational_unit",
    "position",
    "position_slot",
    "personnel_event",
    "event_participant",
    "role_assignment",
    "mandate",
    "signatory",
    "review_task",
    "review_decision",
    "ontology_release",
}


def test_alembic_upgrade_head_creates_all_tables(tmp_path):
    db_path = tmp_path / "migration-check.db"
    url = f"sqlite:///{db_path.as_posix()}"

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    missing = EXPECTED_TABLES - tables
    assert not missing, f"tablas faltantes tras la migración: {missing}"


def test_downgrade_refuses_to_drop_alias_precedents(tmp_path):
    """Un alias sin cargo (role_context NULL) no cabe en el esquema anterior.

    La bajada debe fallar en vez de borrarlo: es una decisión humana y la regla 3
    prohíbe destruirla en silencio. Revocarlo es potestad del operador.
    """
    db_path = tmp_path / "migration-alias.db"
    url = f"sqlite:///{db_path.as_posix()}"
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO identity_precedent (id, subject_type, name_normalized, "
                "role_context, person_id, source_person_mention_id, review_decision_id, "
                "created_at) VALUES ('a'||hex(randomblob(15)), 'person', 'ELMER CUBA "
                "BUSTINZA', NULL, 'p1', 'm1', 'd1', '2026-08-07 00:00:00')"
            )
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="alcance global"):
        command.downgrade(config, "75f16770c199")


def test_downgrade_base(tmp_path):
    db_path = tmp_path / "migration-down.db"
    url = f"sqlite:///{db_path.as_posix()}"
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    engine.dispose()
    assert not tables
