"""Immutable, content-addressed records (DESIGN.md section 7).

A record's id is the hash of its content, so writing the same record twice is
a no-op, and changing anything - including its parents - gives a new id.
`produced_by` (which step run made it) is provenance and is left out of the id.
"""
from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from ..hashing import content_hash

RECORD_KINDS: dict[str, type["Record"]] = {}


class Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    KIND: ClassVar[str] = ""
    SCHEMA_VERSION: ClassVar[int] = 1

    parents: tuple[str, ...] = ()
    produced_by: str | None = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        kind = cls.__dict__.get("KIND")
        if kind:
            existing = RECORD_KINDS.get(kind)
            if existing is not None and existing.__qualname__ != cls.__qualname__:
                raise TypeError(f"record kind {kind!r} is already used by {existing.__qualname__}")
            RECORD_KINDS[kind] = cls

    @property
    def id(self) -> str:
        body = self.model_dump(mode="json", exclude={"produced_by"})
        return f"{self.KIND}:{content_hash([self.KIND, self.SCHEMA_VERSION, body])[:20]}"
