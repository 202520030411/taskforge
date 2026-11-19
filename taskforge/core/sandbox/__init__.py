"""Sandbox interface and implementations (DESIGN.md section 5).

One isolated container per run; grading in a second, fresh container.
`FakeSandbox` is scripted, for unit tests. `DockerSandbox` calls Docker directly.
In the real stack workers use `SandboxdClient` (core.sandbox.client) to talk to
sandboxd (core.sandbox.service), which owns Docker and keeps a warm pool.
"""
from .base import (CANARY, DECOY_CONTENT, DECOY_PATH, BuildError, ExecResult, Sandbox, SandboxBusy, SandboxError, SandboxUnavailable,
                   VerifyResult,
                   section, session, truncate_tail, workspace_files)
from .docker import DockerSandbox
from .fake import FakeSandbox

__all__ = ["BuildError", "CANARY", "DECOY_CONTENT", "DECOY_PATH", "DockerSandbox", "ExecResult", "FakeSandbox",
           "Sandbox", "SandboxBusy", "SandboxError", "SandboxUnavailable", "VerifyResult", "section", "session", "truncate_tail", "workspace_files"]
