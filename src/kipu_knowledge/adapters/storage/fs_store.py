"""ArtifactStore sobre filesystem local, direccionado por contenido.

Layout: <root>/sha256/ab/cd/<hash completo>
Los bytes almacenados nunca se sobrescriben (regla 11): si el hash ya existe,
la escritura es un no-op verificado.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from kipu_knowledge.domain.contracts import StoredObject


class ImmutabilityViolation(RuntimeError):
    pass


class FilesystemArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, sha256: str) -> Path:
        return self._root / "sha256" / sha256[:2] / sha256[2:4] / sha256

    @staticmethod
    def _key_for(sha256: str) -> str:
        return f"sha256/{sha256[:2]}/{sha256[2:4]}/{sha256}"

    def put_immutable(self, content: bytes) -> StoredObject:
        digest = hashlib.sha256(content).hexdigest()
        path = self._path_for(digest)
        if path.exists():
            existing = path.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest:
                raise ImmutabilityViolation(
                    f"El objeto {digest} existe con contenido distinto: almacenamiento corrupto"
                )
            return StoredObject(digest, self._key_for(digest), len(content), already_existed=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(content)
        tmp.replace(path)
        return StoredObject(digest, self._key_for(digest), len(content), already_existed=False)

    def get(self, object_key: str) -> bytes:
        path = self._root / Path(object_key)
        return path.read_bytes()

    def exists_by_hash(self, sha256: str) -> bool:
        return self._path_for(sha256).exists()
