"""Postgres connection pool and the schema of every table (DESIGN.md sections 4 and 7).

Each test uses its own Postgres schema (a namespace inside the database) through
`search_path`, so tests cannot see each other's rows.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id               text PRIMARY KEY,
    seq              bigserial,
    workflow         text NOT NULL,
    workflow_version text NOT NULL,
    status           text NOT NULL DEFAULT 'ACTIVE',        -- ACTIVE | PAUSED_BUDGET | CANCELLED
    paid_approved    boolean NOT NULL DEFAULT false,
    budget_usd       double precision NOT NULL DEFAULT 0,
    spent_usd        double precision NOT NULL DEFAULT 0,
    reserved_usd     double precision NOT NULL DEFAULT 0,
    trace_id         text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS step_runs (
    id               text PRIMARY KEY,
    seq              bigserial,
    run_id           text NOT NULL REFERENCES runs(id),
    step             text NOT NULL,
    key              text NOT NULL,
    input            jsonb NOT NULL,
    resource         text NOT NULL,
    status           text NOT NULL DEFAULT 'PENDING',
    attempt          integer NOT NULL DEFAULT 0,
    max_attempts     integer NOT NULL,
    timeout_s        double precision NOT NULL,
    available_at     timestamptz NOT NULL DEFAULT now(),
    output           jsonb,
    error            text,
    join_parent_id   text,              -- who is waiting for this branch to end
    branch_root_id   text,              -- the child a Fork created at the top of this branch
    open_branches    integer NOT NULL DEFAULT 0,
    fork_gen         integer NOT NULL DEFAULT 0,   -- how many times this step has forked
    join_gen         integer NOT NULL DEFAULT 0,   -- which fork of the parent this branch belongs to
    wait_signal      text,
    state            jsonb,
    resume           jsonb,
    lease_owner      text,
    lease_token      bigint NOT NULL DEFAULT 0,
    lease_expires_at timestamptz,
    started_at       timestamptz,
    finished_at      timestamptz,
    trace_id         text,
    parent_span_id   text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, step, key)
);
CREATE INDEX IF NOT EXISTS step_runs_ready   ON step_runs (resource, seq) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS step_runs_leases  ON step_runs (lease_expires_at) WHERE status = 'RUNNING';
CREATE INDEX IF NOT EXISTS step_runs_by_run  ON step_runs (run_id, status);
CREATE INDEX IF NOT EXISTS step_runs_waiting ON step_runs (run_id, wait_signal) WHERE status = 'WAITING';

CREATE TABLE IF NOT EXISTS branch_results (
    id         bigserial PRIMARY KEY,
    parent_id  text NOT NULL,
    root_step  text NOT NULL,
    root_key   text NOT NULL,
    step       text NOT NULL,
    key        text NOT NULL,
    status     text NOT NULL,
    output     jsonb,
    error      text
);
CREATE INDEX IF NOT EXISTS branch_results_parent ON branch_results (parent_id);

CREATE TABLE IF NOT EXISTS signals (            -- signals that arrived before anyone waited for them
    run_id  text NOT NULL,
    name    text NOT NULL,
    payload jsonb,
    PRIMARY KEY (run_id, name)
);

CREATE TABLE IF NOT EXISTS events (             -- append-only history of every state change
    seq         bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    run_id      text NOT NULL,
    step_run_id text NOT NULL,
    step        text NOT NULL,
    key         text NOT NULL,
    from_status text,
    to_status   text NOT NULL,
    detail      text NOT NULL DEFAULT '',
    worker      text
);
CREATE INDEX IF NOT EXISTS events_by_run ON events (run_id, seq);

CREATE TABLE IF NOT EXISTS reservations (       -- money set aside for model calls in flight
    id          bigserial PRIMARY KEY,
    run_id      text NOT NULL,
    step_run_id text NOT NULL,
    amount      double precision NOT NULL
);
CREATE INDEX IF NOT EXISTS reservations_by_step ON reservations (step_run_id);

CREATE TABLE IF NOT EXISTS records (
    seq            bigserial,
    id             text PRIMARY KEY,
    kind           text NOT NULL,
    schema_version integer NOT NULL,
    body           jsonb NOT NULL,
    parents        text[] NOT NULL DEFAULT '{}',
    produced_by    text,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS records_kind    ON records (kind, seq);
CREATE INDEX IF NOT EXISTS records_body    ON records USING gin (body jsonb_path_ops);
CREATE INDEX IF NOT EXISTS records_parents ON records USING gin (parents);

CREATE TABLE IF NOT EXISTS llm_cache (          -- one row per idempotency key
    idem_key   text PRIMARY KEY,
    status     text NOT NULL,                   -- IN_FLIGHT | DONE
    response   jsonb,
    owner      text,
    claimed_at timestamptz NOT NULL DEFAULT now(),
    done_at    timestamptz
);
ALTER TABLE llm_cache ADD COLUMN IF NOT EXISTS step_run_id text;   -- the step run that made the claim

CREATE TABLE IF NOT EXISTS llm_calls (          -- the cost ledger
    id             bigserial PRIMARY KEY,
    at             timestamptz NOT NULL DEFAULT now(),
    idem_key       text NOT NULL,
    run_id         text NOT NULL,
    step_run_id    text NOT NULL,
    scope_id       text NOT NULL,
    call_no        integer NOT NULL,
    role           text NOT NULL,
    model          text NOT NULL,
    prompt_version text NOT NULL DEFAULT '',
    input_tokens   integer NOT NULL DEFAULT 0,
    output_tokens  integer NOT NULL DEFAULT 0,
    cost_usd       double precision NOT NULL DEFAULT 0,
    cached         boolean NOT NULL,
    latency_s      double precision NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS llm_calls_by_run ON llm_calls (run_id);
CREATE INDEX IF NOT EXISTS llm_calls_by_key ON llm_calls (idem_key) WHERE NOT cached;

CREATE TABLE IF NOT EXISTS rate_buckets (       -- shared token buckets, one per provider
    name         text PRIMARY KEY,
    tokens       double precision NOT NULL,
    capacity     double precision NOT NULL,
    refill_per_s double precision NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reviews (
    review_id     text PRIMARY KEY,
    seq           bigserial,
    run_id        text NOT NULL,
    stage         text NOT NULL,
    item_id       text NOT NULL,
    auto_decision text NOT NULL,
    allowed       text[] NOT NULL,
    reason_codes  text[] NOT NULL,
    material      jsonb NOT NULL,
    parents       text[] NOT NULL DEFAULT '{}',
    record_id     text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    decided_at    timestamptz
);
CREATE INDEX IF NOT EXISTS reviews_pending ON reviews (stage, seq) WHERE record_id IS NULL;

CREATE TABLE IF NOT EXISTS kb_docs (
    id         text PRIMARY KEY,
    seq        bigserial,
    kind       text NOT NULL,
    text       text NOT NULL,
    ref        text,
    meta       jsonb NOT NULL DEFAULT '{}',
    step_seq   bigint NOT NULL DEFAULT 0,       -- newest step run when the doc was added (for stable replays)
    tsv        tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS kb_docs_tsv  ON kb_docs USING gin (tsv);
CREATE INDEX IF NOT EXISTS kb_docs_kind ON kb_docs (kind, seq);
"""


class Database:
    def __init__(self, dsn: str, *, schema: str = "public", min_size: int = 1, max_size: int = 16):
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema):
            raise ValueError(f"bad schema name {schema!r}")
        self.dsn = dsn
        self.schema = schema
        self.pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size, open=True, timeout=30,
                                   kwargs={"row_factory": dict_row, "options": f"-c search_path={schema}"})

    @contextmanager
    def tx(self) -> Iterator[psycopg.Connection]:
        """One transaction: committed when the block ends normally, rolled back if it raises."""
        with self.pool.connection() as conn:
            yield conn

    def init_schema(self) -> None:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema}")
        with self.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(727274)")     # two processes starting at once
            conn.execute(SCHEMA_SQL)

    def drop_schema(self) -> None:
        if self.schema == "public":
            raise ValueError("refusing to drop the public schema")
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {self.schema} CASCADE")

    def close(self) -> None:
        self.pool.close()
