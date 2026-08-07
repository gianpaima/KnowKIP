"""ArtifactStore sobre MinIO / S3-compatible. Mismo layout de claves que el FS."""

from __future__ import annotations

import hashlib
import io

from kipu_knowledge.domain.contracts import StoredObject


class MinioArtifactStore:
    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
    ) -> None:
        from minio import Minio  # importación diferida: dependencia solo si se usa

        self._client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self._bucket = bucket
        if not self._client.bucket_exists(bucket):
            self._client.make_bucket(bucket)

    @staticmethod
    def _key_for(sha256: str) -> str:
        return f"sha256/{sha256[:2]}/{sha256[2:4]}/{sha256}"

    def put_immutable(self, content: bytes) -> StoredObject:
        digest = hashlib.sha256(content).hexdigest()
        key = self._key_for(digest)
        if self.exists_by_hash(digest):
            return StoredObject(digest, key, len(content), already_existed=True)
        self._client.put_object(self._bucket, key, io.BytesIO(content), length=len(content))
        return StoredObject(digest, key, len(content), already_existed=False)

    def get(self, object_key: str) -> bytes:
        response = self._client.get_object(self._bucket, object_key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def exists_by_hash(self, sha256: str) -> bool:
        from minio.error import S3Error

        try:
            self._client.stat_object(self._bucket, self._key_for(sha256))
            return True
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject"}:
                return False
            raise


def build_store_from_settings():  # noqa: ANN201 - tipo depende de configuración
    """Construye el ArtifactStore configurado (fs por defecto)."""
    from kipu_knowledge.adapters.storage.fs_store import FilesystemArtifactStore
    from kipu_knowledge.config import get_settings

    settings = get_settings()
    if settings.artifact_store == "minio":
        return MinioArtifactStore(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            bucket=settings.minio_bucket,
            secure=settings.minio_secure,
        )
    return FilesystemArtifactStore(settings.artifact_fs_root)
