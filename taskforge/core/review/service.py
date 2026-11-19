"""Human review points (DESIGN.md section 10).

The workflow supplies the policy: which decisions are allowed at a stage given the
automatic decision, and which reason codes exist. The service enforces it on every
submit, so the rules hold no matter which interface (console, files, tests) is used.

`ReviewService` keeps its queue in memory. `PgReviewService` keeps it in Postgres and
stores a decision, marks the review decided, and delivers the wake-up signal to the
waiting step in one transaction.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..hashing import content_hash
from ..store import Record


class ReviewRecord(Record):
    KIND = "review"
    run_id: str
    review_id: str
    stage: str
    item_id: str
    reviewer: str
    auto_decision: str
    decision: str
    reason_code: str
    notes: str = ""
    minutes_spent: float = 0.0
    payload: dict[str, Any] = {}


@dataclass
class PendingReview:
    review_id: str
    run_id: str
    stage: str
    item_id: str
    auto_decision: str
    allowed: tuple[str, ...]
    reason_codes: tuple[str, ...]
    material: dict[str, Any]
    record_id: str | None = None   # set once decided
    parents: tuple[str, ...] = field(default_factory=tuple)


class ReviewPolicy:
    """allowed(stage, auto_decision) -> decisions a person may pick; () means never queue the item.
    agrees(stage, auto_decision, decision) -> whether the person confirmed the automatic decision."""

    def __init__(self, allowed: Callable[[str, str], tuple[str, ...]], reason_codes: Mapping[str, tuple[str, ...]],
                 agrees: Callable[[str, str, str], bool]):
        self.allowed = allowed
        self.reason_codes = dict(reason_codes)
        self.agrees = agrees


class ReviewRefused(Exception):
    """The item may not be shown to a person (for example: a gate already rejected it)."""


class ReviewService:
    def __init__(self, policy: ReviewPolicy, store, signal: Callable[..., None] | None = None):
        self.policy = policy
        self.store = store
        self.signal = signal
        self._items: dict[str, PendingReview] = {}

    def bind_signal(self, signal: Callable[..., None]) -> None:
        self.signal = signal

    @staticmethod
    def signal_name(review_id: str) -> str:
        return f"review:{review_id}"

    # -- storage (overridden by PgReviewService) ----------------------------------
    def _create(self, item: PendingReview) -> None:
        self._items.setdefault(item.review_id, item)

    def get_item(self, review_id: str) -> PendingReview:
        return self._items[review_id]

    def all_items(self, run_id: str | None = None) -> list[PendingReview]:
        return [r for r in self._items.values() if run_id is None or r.run_id == run_id]

    def _decide(self, item: PendingReview, record: ReviewRecord, signal_payload: dict) -> str:
        if self._items[item.review_id].record_id is not None:
            raise ValueError(f"{item.review_id} was already decided")
        record_id = self.store.put(record)
        self._items[item.review_id].record_id = record_id
        if self.signal is not None:
            self.signal(item.run_id, self.signal_name(item.review_id), {**signal_payload, "record_id": record_id})
        return record_id

    # -- behaviour shared by both backends -----------------------------------------
    def open(self, run_id: str, stage: str, item_id: str, auto_decision: str, material: Mapping[str, Any],
             parents: tuple[str, ...] = ()) -> str:
        """Queue an item. Idempotent: a re-executed step gets the same review back."""
        allowed = self.policy.allowed(stage, auto_decision)
        if not allowed:
            raise ReviewRefused(f"{stage}: items with automatic decision {auto_decision!r} are never queued")
        review_id = f"{stage}-{content_hash([run_id, stage, item_id])[:12]}"
        self._create(PendingReview(review_id, run_id, stage, item_id, auto_decision, tuple(allowed),
                                   tuple(self.policy.reason_codes[stage]), json.loads(json.dumps(dict(material), default=str)),
                                   parents=tuple(parents)))
        return review_id

    def pending(self, run_id: str | None = None, stage: str | None = None) -> list[PendingReview]:
        return [r for r in self.all_items(run_id) if r.record_id is None and (stage is None or r.stage == stage)]

    def submit(self, review_id: str, decision: str, reason_code: str, *, reviewer: str = "owner",
               minutes_spent: float = 0.0, notes: str = "", payload: Mapping[str, Any] | None = None) -> ReviewRecord:
        item = self.get_item(review_id)
        if item.record_id is not None:
            raise ValueError(f"{review_id} was already decided")
        if decision not in item.allowed:
            raise ValueError(f"{item.stage}: {decision!r} is not allowed here; allowed: {item.allowed}")
        if reason_code not in item.reason_codes:
            raise ValueError(f"{item.stage}: unknown reason code {reason_code!r}; use one of {item.reason_codes}")
        record = ReviewRecord(run_id=item.run_id, review_id=review_id, stage=item.stage, item_id=item.item_id,
                              reviewer=reviewer, auto_decision=item.auto_decision, decision=decision,
                              reason_code=reason_code, notes=notes, minutes_spent=minutes_spent,
                              payload=dict(payload or {}), parents=item.parents)
        self._decide(item, record, {"decision": decision, "reason_code": reason_code, "payload": dict(payload or {}),
                                    "notes": notes})
        return record

    # -- the file-based flow from the spec -----------------------------------------
    def export_pending(self, directory: Path, stage: str | None = None, run_id: str | None = None) -> list[Path]:
        """One JSON file per pending review, with `decision` and `reason_code` left blank to fill in."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        paths = []
        for item in self.pending(run_id=run_id, stage=stage):
            path = directory / f"{item.review_id}.json"
            path.write_text(json.dumps({
                "review_id": item.review_id, "stage": item.stage, "item_id": item.item_id,
                "auto_decision": item.auto_decision, "allowed_decisions": list(item.allowed),
                "reason_codes": list(item.reason_codes), "material": item.material,
                "decision": "", "reason_code": "", "notes": "", "minutes_spent": 0, "payload": {},
            }, indent=2, default=str))
            paths.append(path)
        return paths

    def import_decisions(self, directory: Path, stage: str | None = None) -> list[str]:
        """Submit every filled-in file. Files still blank, and reviews already decided, are skipped."""
        done = []
        for path in sorted(Path(directory).glob("*.json")):
            data = json.loads(path.read_text())
            if stage and data.get("stage") != stage:
                continue
            if not data.get("decision"):
                continue
            try:
                item = self.get_item(data["review_id"])
            except KeyError:
                continue
            if item.record_id is not None:
                continue
            self.submit(data["review_id"], data["decision"], data["reason_code"], notes=data.get("notes", ""),
                        minutes_spent=float(data.get("minutes_spent") or 0), payload=data.get("payload") or {},
                        reviewer=data.get("reviewer", "owner"))
            done.append(data["review_id"])
        return done

    # -- measures ---------------------------------------------------------------------
    def disagreement(self, run_id: str | None = None) -> dict[str, dict[str, float]]:
        """Per stage: how often the person decided differently from the automatic decision."""
        by_stage: dict[str, list[tuple[bool, float]]] = {}
        for rec in self.store.all(ReviewRecord):
            if run_id is not None and rec.run_id != run_id:
                continue
            differs = not self.policy.agrees(rec.stage, rec.auto_decision, rec.decision)
            by_stage.setdefault(rec.stage, []).append((differs, rec.minutes_spent))
        return {s: {"n": len(v), "rate": sum(d for d, _ in v) / len(v), "minutes": round(sum(m for _, m in v), 2)}
                for s, v in sorted(by_stage.items())}


class PgReviewService(ReviewService):
    def __init__(self, policy: ReviewPolicy, store, db, signal: Callable[..., None] | None = None):
        super().__init__(policy, store, signal)
        self.db = db

    @staticmethod
    def _item(row: dict) -> PendingReview:
        return PendingReview(row["review_id"], row["run_id"], row["stage"], row["item_id"], row["auto_decision"],
                             tuple(row["allowed"]), tuple(row["reason_codes"]), row["material"], row["record_id"],
                             tuple(row["parents"]))

    def _create(self, item: PendingReview) -> None:
        from psycopg.types.json import Jsonb
        with self.db.tx() as c:
            c.execute("INSERT INTO reviews (review_id, run_id, stage, item_id, auto_decision, allowed, reason_codes, "
                      "material, parents) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (review_id) DO NOTHING",
                      (item.review_id, item.run_id, item.stage, item.item_id, item.auto_decision, list(item.allowed),
                       list(item.reason_codes), Jsonb(item.material), list(item.parents)))

    def get_item(self, review_id: str) -> PendingReview:
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM reviews WHERE review_id = %s", (review_id,)).fetchone()
        if row is None:
            raise KeyError(review_id)
        return self._item(row)

    def all_items(self, run_id: str | None = None) -> list[PendingReview]:
        with self.db.tx() as c:
            rows = c.execute("SELECT * FROM reviews WHERE (%(r)s::text IS NULL OR run_id = %(r)s) ORDER BY seq",
                             {"r": run_id}).fetchall()
        return [self._item(r) for r in rows]

    def _decide(self, item: PendingReview, record: ReviewRecord, signal_payload: dict) -> str:
        """One transaction: the decision record, the review marked decided, and the signal that wakes the step."""
        with self.db.tx() as c:
            row = c.execute("SELECT record_id FROM reviews WHERE review_id = %s FOR UPDATE", (item.review_id,)).fetchone()
            if row["record_id"] is not None:
                raise ValueError(f"{item.review_id} was already decided")
            record_id = self.store.put(record, conn=c)
            c.execute("UPDATE reviews SET record_id = %s, decided_at = now() WHERE review_id = %s",
                      (record_id, item.review_id))
            if self.signal is not None:
                self.signal(item.run_id, self.signal_name(item.review_id), {**signal_payload, "record_id": record_id},
                            conn=c)
        return record_id
