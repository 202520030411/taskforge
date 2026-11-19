"""Shared fixtures. Tests that take `db` run twice: once on memory (db is None) and once on Postgres.
The Postgres half is skipped unless TASKFORGE_PG_DSN is set (the development laptop has no Postgres)."""
import os
import uuid

import pytest

PG_DSN = os.environ.get("TASKFORGE_PG_DSN")


@pytest.fixture
def pg_db():
    if not PG_DSN:
        pytest.skip("set TASKFORGE_PG_DSN to run the Postgres tests")
    from taskforge.core.pg import Database
    db = Database(PG_DSN, schema=f"t_{uuid.uuid4().hex[:12]}", max_size=40)
    db.init_schema()
    yield db
    db.close()
    db.drop_schema()


@pytest.fixture(params=["memory", "postgres"])
def db(request):
    """None for the in-memory backend, a fresh Database (its own schema) for Postgres."""
    return None if request.param == "memory" else request.getfixturevalue("pg_db")


@pytest.fixture
def make_engine(db):
    """make_engine(workflow, services=None) -> an engine on the current backend."""
    def make(workflow, services=None, **kw):
        if db is None:
            from taskforge.core.engine import InMemoryEngine
            return InMemoryEngine(workflow, services, **kw)
        from taskforge.core.engine.postgres import PgEngine
        return PgEngine(workflow, services, db, **kw)
    return make


@pytest.fixture
def world(tmp_path, db):
    """world(WorldConfig(...), **kw) -> a finished scripted run on the current backend."""
    from taskforge.taskgen.demo import WorldConfig, run_world
    counter = iter(range(1000))

    def run(world_config=None, **kw):
        return run_world(tmp_path / f"w{next(counter)}", world_config or WorldConfig(), db=db, **kw)
    return run
