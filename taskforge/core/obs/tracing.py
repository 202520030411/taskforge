"""Tracing (DESIGN.md section 11). One trace per run, one span per step execution, child spans for
model calls and sandbox commands.

Without `configure()` everything still works: spans are not exported, but trace and span ids are
still generated, so the ids stored on step runs are always filled in.
"""
from __future__ import annotations

import contextvars
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

_tracer = None
_current: contextvars.ContextVar["SpanHandle | None"] = contextvars.ContextVar("taskforge_span", default=None)


@dataclass
class SpanHandle:
    trace_id: str            # 32 hex characters
    span_id: str             # 16 hex characters
    _span: Any = None

    def set(self, **attrs) -> None:
        if self._span is not None:
            for k, v in _clean(attrs).items():
                self._span.set_attribute(k, v)

    def error(self, message: str) -> None:
        if self._span is not None:
            from opentelemetry.trace import Status, StatusCode
            self._span.set_status(Status(StatusCode.ERROR, message[:200]))

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-01"


def configure(service: str, otlp_endpoint: str | None) -> bool:
    """Export spans to an OTLP/HTTP collector (Jaeger). Returns False when no endpoint is given."""
    global _tracer
    if not otlp_endpoint:
        return False
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint.rstrip("/") + "/v1/traces")))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("taskforge")
    return True


def _clean(attrs: Mapping[str, Any] | None) -> dict[str, Any]:
    return {k: (v if isinstance(v, (str, bool, int, float)) else str(v)) for k, v in (attrs or {}).items()
            if v is not None}


@contextmanager
def span(name: str, attrs: Mapping[str, Any] | None = None, *, trace_id: str | None = None,
         parent_span_id: str | None = None) -> Iterator[SpanHandle]:
    """Open a span. With `trace_id` + `parent_span_id` it continues a trace started elsewhere
    (another step, another process); otherwise it nests under the span open in this thread."""
    outer = _current.get()
    if _tracer is None:
        handle = SpanHandle(trace_id or (outer.trace_id if outer else secrets.token_hex(16)), secrets.token_hex(8))
        token = _current.set(handle)
        try:
            yield handle
        finally:
            _current.reset(token)
        return
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
    context = None
    if trace_id and parent_span_id:
        parent = SpanContext(int(trace_id, 16), int(parent_span_id, 16), is_remote=True, trace_flags=TraceFlags(1))
        context = trace.set_span_in_context(NonRecordingSpan(parent))
    with _tracer.start_as_current_span(name, context=context, attributes=_clean(attrs)) as sp:
        sc = sp.get_span_context()
        handle = SpanHandle(f"{sc.trace_id:032x}", f"{sc.span_id:016x}", sp)
        token = _current.set(handle)
        try:
            yield handle
        except BaseException as e:
            handle.error(f"{type(e).__name__}: {e}")
            raise
        finally:
            _current.reset(token)


def current() -> SpanHandle | None:
    return _current.get()


def parse_traceparent(header: str | None) -> tuple[str | None, str | None]:
    parts = (header or "").split("-")
    if len(parts) == 4 and len(parts[1]) == 32 and len(parts[2]) == 16:
        return parts[1], parts[2]
    return None, None


def flush() -> None:
    if _tracer is not None:
        from opentelemetry import trace
        trace.get_tracer_provider().force_flush()
