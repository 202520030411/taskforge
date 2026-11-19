"""A scripted sandbox for unit tests: files live in dicts, commands are Python functions.

Fake-task convention (only for this fake):
  running `bash /solution/solve.sh` copies every /solution/files/<rel> to /app/<rel>
  (the caller writes the solution into /solution first, as root, as with Docker)
  grading passes when, for every tests/expect/<rel>, /app/<rel> has exactly that content
The agent user may write only under /app, /tmp and /home/agent, as in the real sandbox.
"""
from __future__ import annotations

import json
import shlex
from typing import Callable, Mapping

from ..hashing import content_hash
from .base import DECOY_CONTENT, DECOY_PATH, ExecResult, VerifyResult, section, workspace_files

Program = Callable[[dict, list[str]], ExecResult]
WRITABLE = ("/app/", "/tmp/", "/home/agent/")


class FakeSandbox:
    def __init__(self, programs: Mapping[str, Program] | None = None):
        self.programs: dict[str, Program] = {"cat": _cat, "ls": _ls, "true": lambda fs, a: ExecResult(0, ""),
                                             "chmod": lambda fs, a: ExecResult(0, "")}
        self.programs.update(programs or {})
        self.images: dict[str, dict[str, str]] = {}
        self.boxes: dict[str, dict[str, str]] = {}
        self.purposes: dict[str, str] = {}
        self.snapshots: dict[str, bytes] = {}
        self.prewarmed: list[tuple[str, int]] = []
        self.created = 0
        self.destroyed = 0
        self.verify_calls = 0
        self.commands: list[str] = []

    def build(self, files):
        fs = workspace_files(files)
        image = "fake:" + content_hash(sorted(fs.items()))[:12]
        self.images[image] = fs
        return image

    def create(self, image, *, purpose):
        self.created += 1
        sid = f"fake-{self.created}"
        fs = dict(self.images[image])
        if purpose != "verify":
            fs[DECOY_PATH] = DECOY_CONTENT
        self.boxes[sid] = fs
        self.purposes[sid] = purpose
        return sid

    def exec(self, sid, command, *, timeout_s=120, user="agent"):
        self.commands.append(command)
        fs = self.boxes[sid]
        if command.strip() == "bash /solution/solve.sh":
            if "/solution/solve.sh" not in fs:
                return ExecResult(127, "bash: /solution/solve.sh: No such file or directory")
            for path, content in list(fs.items()):
                if path.startswith("/solution/files/"):
                    fs["/app/" + path[len("/solution/files/"):]] = content
            return ExecResult(0, "")
        argv = shlex.split(command)
        if not argv:
            return ExecResult(0, "")
        program = self.programs.get(argv[0])
        if program is None:
            return ExecResult(127, f"{argv[0]}: command not found")
        return program(fs, argv)

    def write_files(self, sid, files, *, user="agent"):
        errors = {}
        for path, content in files.items():
            if user == "agent" and not path.startswith(WRITABLE):
                errors[path] = "Permission denied"
                continue
            self.boxes[sid][path] = content
        return errors

    def read_file(self, sid, path, *, user="agent"):
        try:
            return self.boxes[sid][path]
        except KeyError:
            raise FileNotFoundError(path) from None

    def snapshot_app(self, sid):
        app = {p: c for p, c in self.boxes[sid].items() if p.startswith("/app/")}
        data = json.dumps(app, sort_keys=True).encode()
        ref = "snap-" + content_hash(app)[:16]
        self.snapshots[ref] = data
        return ref

    def prewarm(self, image, n):
        self.prewarmed.append((image, n))

    def destroy(self, sid):
        if self.boxes.pop(sid, None) is not None:
            self.destroyed += 1

    def verify(self, image, tests, snapshot, *, timeout_s=300):
        self.verify_calls += 1
        app = json.loads(self.snapshots[snapshot])
        expect = section(tests, "expect")
        if not expect:
            return VerifyResult(False, 2, "fake grader: no tests/expect/ files")
        wrong = [rel for rel, want in expect.items() if app.get("/app/" + rel) != want]
        return VerifyResult(not wrong, 0 if not wrong else 1, "ok" if not wrong else f"mismatch: {wrong}")

    @property
    def leaked(self) -> int:
        """Sandboxes created and never destroyed."""
        return len(self.boxes)


def _cat(fs, argv):
    out = []
    for path in argv[1:]:
        if path not in fs:
            return ExecResult(1, f"cat: {path}: No such file or directory")
        out.append(fs[path])
    return ExecResult(0, "".join(out))


def _ls(fs, argv):
    root = (argv[1] if len(argv) > 1 else "/app").rstrip("/") + "/"
    names = sorted({p[len(root):].split("/")[0] for p in fs if p.startswith(root)})
    return ExecResult(0 if names else 2, "\n".join(names))
