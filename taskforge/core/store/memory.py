"""In-memory record and blob store. The Postgres + MinIO version (P2) keeps the same methods."""
from __future__ import annotations

import json
from collections import deque
from typing import TypeVar

from ..hashing import canonical_json
from .blobs import BlobStore, MemoryBlobStore
from .records import Record

R = TypeVar("R", bound=Record)


class MemoryStore:
    def __init__(self, blobs: BlobStore | None = None) -> None:
        self._records: dict[str, Record] = {}
        self.blobs: BlobStore = blobs or MemoryBlobStore()

    # -- records -------------------------------------------------------------
    def put(self, record: Record) -> str:
        """Store a record and return its id. Its parents must already be stored."""
        rid = record.id
        if rid not in self._records:
            missing = [p for p in record.parents if p not in self._records]
            if missing:
                raise KeyError(f"{rid}: unknown parents {missing}")
            self._records[rid] = record
        return rid

    def has(self, rid: str) -> bool:
        return rid in self._records

    def get(self, rid: str, cls: type[R] | None = None) -> R:
        rec = self._records[rid]
        if cls is not None and not isinstance(rec, cls):
            raise TypeError(f"{rid} is a {type(rec).__name__}, not a {cls.__name__}")
        return rec  # type: ignore[return-value]

    def all(self, cls: type[R]) -> list[R]:
        """Records of one kind, in the order they were first stored."""
        return [r for r in self._records.values() if isinstance(r, cls)]

    def find(self, cls: type[R], **equal) -> list[R]:
        return [r for r in self.all(cls) if all(getattr(r, k) == v for k, v in equal.items())]

    def children(self, rid: str, cls: type[R] | None = None) -> list[Record]:
        out = [r for r in self._records.values() if rid in r.parents]
        return [r for r in out if cls is None or isinstance(r, cls)]

    def ancestors(self, rid: str) -> list[Record]:
        """Every record reachable by following `parents` upward, nearest first, each once."""
        seen: set[str] = set()
        out: list[Record] = []
        queue = deque(self._records[rid].parents)
        while queue:
            pid = queue.popleft()
            if pid in seen:
                continue
            seen.add(pid)
            parent = self._records[pid]
            out.append(parent)
            queue.extend(parent.parents)
        return out

    # -- blobs ---------------------------------------------------------------
    def put_blob(self, data: bytes) -> str:
        return self.blobs.put(data)

    def get_blob(self, sha: str) -> bytes:
        return self.blobs.get(sha)

    def put_json_blob(self, obj) -> str:
        return self.put_blob(canonical_json(obj).encode())

    def get_json_blob(self, sha: str):
        return json.loads(self.get_blob(sha))
