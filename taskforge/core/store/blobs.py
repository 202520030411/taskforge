"""Blob stores: large content addressed by its SHA-256 hash, so equal content is stored once."""
from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path
from typing import Protocol

from ..hashing import sha256_bytes


class BlobStore(Protocol):
    def put(self, data: bytes) -> str: ...
    def get(self, sha: str) -> bytes: ...
    def has(self, sha: str) -> bool: ...


class MemoryBlobStore:
    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, data: bytes) -> str:
        sha = sha256_bytes(data)
        self._blobs.setdefault(sha, data)
        return sha

    def get(self, sha: str) -> bytes:
        return self._blobs[sha]

    def has(self, sha: str) -> bool:
        return sha in self._blobs


class FsBlobStore:
    """Files under <root>/<first two hex chars>/<sha>. Written to a temp file, then renamed, so a reader
    never sees a half-written blob."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, sha: str) -> Path:
        return self.root / sha[:2] / sha

    def put(self, data: bytes) -> str:
        sha = sha256_bytes(data)
        path = self._path(sha)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        return sha

    def get(self, sha: str) -> bytes:
        try:
            return self._path(sha).read_bytes()
        except FileNotFoundError:
            raise KeyError(sha) from None

    def has(self, sha: str) -> bool:
        return self._path(sha).exists()


class S3BlobStore:
    """Any S3-compatible store (SeaweedFS in the stack), through the `minio` client library.
    Objects are named by their hash."""

    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str = "taskforge", secure: bool = False):
        from minio import Minio
        self.client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self.bucket = bucket
        if not self.client.bucket_exists(bucket):
            try:
                self.client.make_bucket(bucket)
            except Exception:  # noqa: BLE001 - another process created it first
                if not self.client.bucket_exists(bucket):
                    raise

    def put(self, data: bytes) -> str:
        sha = sha256_bytes(data)
        if not self.has(sha):
            self.client.put_object(self.bucket, sha, io.BytesIO(data), len(data))
        return sha

    def get(self, sha: str) -> bytes:
        from minio.error import S3Error
        try:
            resp = self.client.get_object(self.bucket, sha)
        except S3Error as e:
            if e.code == "NoSuchKey":
                raise KeyError(sha) from None
            raise
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    def has(self, sha: str) -> bool:
        from minio.error import S3Error
        try:
            self.client.stat_object(self.bucket, sha)
            return True
        except S3Error as e:
            if e.code in ("NoSuchKey", "NoSuchObject"):
                return False
            raise
