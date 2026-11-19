"""Structured logs: one JSON object per line, carrying run / step / trace ids set with `bind()`."""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from contextlib import contextmanager

_fields: contextvars.ContextVar[dict] = contextvars.ContextVar("taskforge_log_fields", default={})


@contextmanager
def bind(**fields):
    token = _fields.set({**_fields.get(), **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _fields.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
               "level": record.levelname, "logger": record.name, "msg": record.getMessage(), **_fields.get()}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)[-2000:]
        return json.dumps(out, default=str)


def setup(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
