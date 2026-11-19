"""Canonical JSON and content hashes. Ids of records, blobs and idempotency keys all come from here."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any


def _default(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, bytes):
        return obj.hex()
    return str(obj)


def canonical_json(obj: Any) -> str:
    """The same value always gives the same string: sorted keys, no spaces."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default)


def content_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def jsonable(obj: Any) -> Any:
    """Round-trip through JSON. Raises TypeError-like errors early for values that cannot be stored."""
    return json.loads(canonical_json(obj))
