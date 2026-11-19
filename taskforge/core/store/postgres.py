"""Records in Postgres, blobs in a blob store. Same methods as MemoryStore."""
from __future__ import annotations

import json
from typing import TypeVar

from psycopg.types.json import Jsonb

from ..hashing import canonical_json
from ..pg import Database
from .blobs import BlobStore
from .records import RECORD_KINDS, Record

R = TypeVar("R", bound=Record)


def _load(row: dict) -> Record:
    cls = RECORD_KINDS[row["kind"]]
    return cls.model_validate({**row["body"], "parents": row["parents"], "produced_by": row["produced_by"]})


class PgStore:
    def __init__(self, db: Database, blobs: BlobStore):
        self.db = db
        self.blobs = blobs

    # -- records -------------------------------------------------------------
    def put(self, record: Record, conn=None) -> str:
        """Store a record and return its id. Its parents must already be stored. Storing it twice is a no-op.
        Pass `conn` to write inside the caller's transaction."""
        if conn is None:
            with self.db.tx() as c:
                return self._put(c, record)
        return self._put(conn, record)

    @staticmethod
    def _put(c, record: Record) -> str:
        rid = record.id
        body = record.model_dump(mode="json", exclude={"parents", "produced_by"})
        if record.parents:
            found = c.execute("SELECT count(*) AS n FROM records WHERE id = ANY(%s)",
                              (list(set(record.parents)),)).fetchone()["n"]
            if found != len(set(record.parents)):
                raise KeyError(f"{rid}: some parents are not stored: {record.parents}")
        c.execute("INSERT INTO records (id, kind, schema_version, body, parents, produced_by) "
                  "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                  (rid, record.KIND, record.SCHEMA_VERSION, Jsonb(body), list(record.parents), record.produced_by))
        return rid

    def has(self, rid: str) -> bool:
        with self.db.tx() as c:
            return c.execute("SELECT 1 FROM records WHERE id = %s", (rid,)).fetchone() is not None

    def get(self, rid: str, cls: type[R] | None = None) -> R:
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM records WHERE id = %s", (rid,)).fetchone()
        if row is None:
            raise KeyError(rid)
        rec = _load(row)
        if cls is not None and not isinstance(rec, cls):
            raise TypeError(f"{rid} is a {type(rec).__name__}, not a {cls.__name__}")
        return rec  # type: ignore[return-value]

    def all(self, cls: type[R]) -> list[R]:
        with self.db.tx() as c:
            rows = c.execute("SELECT * FROM records WHERE kind = %s ORDER BY seq", (cls.KIND,)).fetchall()
        return [_load(r) for r in rows]  # type: ignore[misc]

    def find(self, cls: type[R], **equal) -> list[R]:
        match = json.loads(canonical_json(equal))
        with self.db.tx() as c:
            rows = c.execute("SELECT * FROM records WHERE kind = %s AND body @> %s ORDER BY seq",
                             (cls.KIND, Jsonb(match))).fetchall()
        return [_load(r) for r in rows]  # type: ignore[misc]

    def children(self, rid: str, cls: type[R] | None = None) -> list[Record]:
        with self.db.tx() as c:
            rows = c.execute("SELECT * FROM records WHERE parents @> ARRAY[%s] AND (%s::text IS NULL OR kind = %s) "
                             "ORDER BY seq", (rid, cls.KIND if cls else None, cls.KIND if cls else None)).fetchall()
        return [_load(r) for r in rows]

    def ancestors(self, rid: str) -> list[Record]:
        """Every record reachable by following `parents` upward, nearest first, each once."""
        with self.db.tx() as c:
            rows = c.execute("""
                WITH RECURSIVE up(id, depth) AS (
                    SELECT unnest(parents), 1 FROM records WHERE id = %s
                    UNION
                    SELECT unnest(r.parents), up.depth + 1 FROM records r JOIN up ON r.id = up.id)
                SELECT r.*, min(up.depth) AS depth FROM up JOIN records r ON r.id = up.id
                GROUP BY r.seq, r.id ORDER BY depth, r.seq""", (rid,)).fetchall()
        if not rows and not self.has(rid):
            raise KeyError(rid)
        return [_load(r) for r in rows]

    # -- blobs ---------------------------------------------------------------
    def put_blob(self, data: bytes) -> str:
        return self.blobs.put(data)

    def get_blob(self, sha: str) -> bytes:
        return self.blobs.get(sha)

    def put_json_blob(self, obj) -> str:
        return self.put_blob(canonical_json(obj).encode())

    def get_json_blob(self, sha: str):
        return json.loads(self.get_blob(sha))
