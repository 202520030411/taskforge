"""A worker process for the kill test: python tests/pg_worker_main.py DSN SCHEMA THREADS LEASE_TTL"""
import signal
import sys
import threading

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from taskforge.core.engine.postgres import PgEngine  # noqa: E402
from taskforge.core.engine.worker import Worker  # noqa: E402
from taskforge.core.pg import Database  # noqa: E402
from tests.pgflows import TREE  # noqa: E402

dsn, schema, threads, ttl = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
db = Database(dsn, schema=schema, max_size=threads + 4)
worker = Worker(PgEngine(TREE, None, db, lease_ttl_s=ttl), {"cpu": threads}, poll_s=0.05, sweep_s=0.3).start()
stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *a: stop.set())
print("ready", flush=True)
stop.wait()
worker.stop()
