"""Knowledge base (DESIGN.md section 9): searchable memory across runs.

Documents are short texts with a kind ("proposal", "accepted_task", "repair_example",
"rubric_counterexample", "hack_note"). Search is by words, not embeddings.

Stable replays: each document remembers the newest step run that existed when it was added
(`step_seq`). A step searching with `before_seq=ctx.seq` only sees documents added before the
step itself was created, so a re-executed step gets the same results and builds the same prompt.
"""
from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..hashing import content_hash

_WORD = re.compile(r"[a-z0-9]{3,}")
STOP = frozenset("the and for that with this from are was were will have has not but can any all its into "
                 "must should each when then than which their there them they you your our out one two".split())


def words(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in STOP]


@dataclass(frozen=True)
class KbHit:
    id: str
    kind: str
    text: str
    ref: str | None
    meta: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0


def doc_id(kind: str, text: str) -> str:
    return f"kb:{content_hash([kind, text])[:20]}"


class MemoryKB:
    def __init__(self, watermark: Callable[[], int] = lambda: 0):
        self._watermark = watermark
        self._docs: dict[str, tuple[KbHit, int]] = {}
        self._lock = threading.Lock()

    def bind_watermark(self, watermark: Callable[[], int]) -> None:
        self._watermark = watermark

    def add(self, kind: str, text: str, ref: str | None = None, meta: dict | None = None) -> str:
        did = doc_id(kind, text)
        with self._lock:
            self._docs.setdefault(did, (KbHit(did, kind, text, ref, dict(meta or {})), self._watermark()))
        return did

    def search(self, kinds: str | Iterable[str], query: str, k: int = 5, before_seq: int | None = None) -> list[KbHit]:
        kinds = {kinds} if isinstance(kinds, str) else set(kinds)
        q = set(words(query))
        hits = []
        with self._lock:
            docs = list(self._docs.values())
        for doc, seq in docs:
            if doc.kind not in kinds or (before_seq is not None and seq >= before_seq):
                continue
            w = words(doc.text)
            overlap = len(q & set(w))
            if overlap:
                hits.append(KbHit(doc.id, doc.kind, doc.text, doc.ref, doc.meta, overlap / math.sqrt(len(w) + 1)))
        return sorted(hits, key=lambda h: (-h.score, h.id))[:k]

    def recent(self, kind: str, k: int = 5, before_seq: int | None = None) -> list[KbHit]:
        """The newest k documents of a kind (for small sets where every document is relevant)."""
        with self._lock:
            docs = [d for d, seq in self._docs.values() if d.kind == kind and (before_seq is None or seq < before_seq)]
        return docs[-k:][::-1]

    def count(self, kind: str | None = None) -> int:
        with self._lock:
            return sum(1 for d, _ in self._docs.values() if kind is None or d.kind == kind)


class PgKB:
    """Postgres full-text search. A query matches a document that shares any of its words; documents
    sharing more (and rarer) words rank higher."""

    def __init__(self, db):
        self.db = db

    def add(self, kind: str, text: str, ref: str | None = None, meta: dict | None = None) -> str:
        from psycopg.types.json import Jsonb
        did = doc_id(kind, text)
        with self.db.tx() as c:
            c.execute("INSERT INTO kb_docs (id, kind, text, ref, meta, step_seq) "
                      "VALUES (%s, %s, %s, %s, %s, (SELECT COALESCE(max(seq), 0) FROM step_runs)) "
                      "ON CONFLICT (id) DO NOTHING", (did, kind, text, ref, Jsonb(dict(meta or {}))))
        return did

    def search(self, kinds: str | Iterable[str], query: str, k: int = 5, before_seq: int | None = None) -> list[KbHit]:
        kinds = [kinds] if isinstance(kinds, str) else list(kinds)
        terms = sorted(set(words(query)))[:60]
        if not terms:
            return []
        with self.db.tx() as c:
            rows = c.execute(
                "SELECT id, kind, text, ref, meta, ts_rank(tsv, q) AS score "
                "FROM kb_docs, to_tsquery('english', %(q)s) AS q "
                "WHERE kind = ANY(%(kinds)s) AND tsv @@ q AND (%(b)s::bigint IS NULL OR step_seq < %(b)s) "
                "ORDER BY score DESC, id LIMIT %(k)s",
                {"q": " | ".join(terms), "kinds": kinds, "b": before_seq, "k": k}).fetchall()
        return [KbHit(r["id"], r["kind"], r["text"], r["ref"], r["meta"], float(r["score"])) for r in rows]

    def recent(self, kind: str, k: int = 5, before_seq: int | None = None) -> list[KbHit]:
        with self.db.tx() as c:
            rows = c.execute("SELECT id, kind, text, ref, meta FROM kb_docs WHERE kind = %(kind)s "
                             "AND (%(b)s::bigint IS NULL OR step_seq < %(b)s) ORDER BY seq DESC LIMIT %(k)s",
                             {"kind": kind, "b": before_seq, "k": k}).fetchall()
        return [KbHit(r["id"], r["kind"], r["text"], r["ref"], r["meta"]) for r in rows]

    def count(self, kind: str | None = None) -> int:
        with self.db.tx() as c:
            return c.execute("SELECT count(*) AS n FROM kb_docs WHERE (%(k)s::text IS NULL OR kind = %(k)s)",
                             {"k": kind}).fetchone()["n"]


__all__ = ["KbHit", "MemoryKB", "PgKB", "doc_id", "words"]
