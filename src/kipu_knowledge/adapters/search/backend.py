"""Backends de búsqueda tras la interfaz SearchBackend.

MVP: PostgreSQL Full Text Search (español). Fallback LIKE para SQLite (pruebas).
OpenSearch puede añadirse después implementando el mismo contrato.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


class PostgresFtsBackend:
    def __init__(self, session: Session) -> None:
        self._session = session

    def index_document(self, document_id: str) -> None:
        # El FTS se evalúa sobre las tablas canónicas; no hay índice separado en el MVP.
        return None

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        sql = text(
            """
            SELECT DISTINCT ld.id AS document_id,
                   pi.publication_code,
                   ld.title_raw,
                   ts_rank(to_tsvector('spanish', ds.text_normalized),
                           websearch_to_tsquery('spanish', :q)) AS rank
            FROM document_section ds
            JOIN legal_document ld ON ld.id = ds.legal_document_id
            JOIN publication_item pi ON pi.id = ld.publication_item_id
            WHERE to_tsvector('spanish', ds.text_normalized)
                  @@ websearch_to_tsquery('spanish', :q)
            ORDER BY rank DESC
            LIMIT :limit
            """
        )
        rows = self._session.execute(sql, {"q": query, "limit": limit}).mappings().all()
        return [dict(r) for r in rows]


class SqlLikeBackend:
    """Fallback portable (SQLite en pruebas): LIKE case-insensitive."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def index_document(self, document_id: str) -> None:
        return None

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        sql = text(
            """
            SELECT DISTINCT ld.id AS document_id,
                   pi.publication_code,
                   ld.title_raw
            FROM document_section ds
            JOIN legal_document ld ON ld.id = ds.legal_document_id
            JOIN publication_item pi ON pi.id = ld.publication_item_id
            WHERE lower(ds.text_normalized) LIKE lower(:pattern)
            LIMIT :limit
            """
        )
        rows = (
            self._session.execute(sql, {"pattern": f"%{query}%", "limit": limit}).mappings().all()
        )
        return [dict(r) for r in rows]


def search_backend_for(session: Session):  # noqa: ANN201
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        return PostgresFtsBackend(session)
    return SqlLikeBackend(session)
