"""Dependencias de FastAPI: sesión de BD por request y ArtifactStore."""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy.orm import Session

from kipu_knowledge.adapters.db.session import get_session_factory
from kipu_knowledge.adapters.storage.minio_store import build_store_from_settings
from kipu_knowledge.domain.contracts import ArtifactStore


@lru_cache
def _store_singleton() -> ArtifactStore:
    return build_store_from_settings()


def get_store() -> ArtifactStore:
    return _store_singleton()


def get_db() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
