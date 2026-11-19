"""Identity of one model call, used to build its idempotency key (DESIGN.md section 4.6)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CallScope:
    run_id: str
    step_run_id: str
    scope_id: str   # "<run>/<step>/<key>": the same on every re-execution of the step
    call_no: int    # 1, 2, 3 ... in the order the step makes its calls
