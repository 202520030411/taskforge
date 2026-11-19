"""A thin Docker client (DESIGN.md section 5). P1 calls the `docker` command line directly;
P2 moves this behind the sandboxd service so workers never touch Docker.

Isolation for every container: no network; CPU, memory and process-count limits; all
Linux capabilities dropped except three that root needs for our own setup work (CHOWN,
DAC_OVERRIDE, FOWNER); no privilege escalation; a time limit after which it removes itself.
Everything a model asks for runs as `agent` (uid 1000), which holds no capabilities at all.
"""
from __future__ import annotations

import io
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from typing import Mapping

from ..hashing import content_hash, sha256_bytes
from .base import DECOY_CONTENT, DECOY_PATH, BuildError, ExecResult, SandboxError, VerifyResult, section, truncate_tail

AGENT_UID = 1000
GRADER_UID = 1001
USERS = {"agent": AGENT_UID, "grader": GRADER_UID, "root": 0}

# Appended to every task Dockerfile, so tasks need not know about our users.
USER_SETUP = f"""
# --- appended by taskforge ---
RUN (id -u agent >/dev/null 2>&1 || useradd -m -u {AGENT_UID} agent) \\
 && (id -u grader >/dev/null 2>&1 || useradd -m -u {GRADER_UID} grader) \\
 && mkdir -p /app && chown -R agent:agent /app
WORKDIR /app
"""


class DockerSandbox:
    def __init__(self, *, cpus: float = 1.0, memory: str = "1g", pids: int = 256, ttl_s: int = 3600,
                 build_timeout_s: int = 1200, docker: str = "docker", snapshot_dir: str | None = None,
                 owner: str = "local"):
        self.cpus, self.memory, self.pids, self.ttl_s = cpus, memory, pids, ttl_s
        self.build_timeout_s = build_timeout_s
        self.docker = docker
        self.owner = owner          # label on every container, so one owner never removes another's
        self.snapshot_dir = Path(snapshot_dir or tempfile.mkdtemp(prefix="taskforge-snapshots-"))
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.created = 0
        self.destroyed = 0
        self.start_latencies: list[float] = []

    # ------------------------------------------------------------ helpers
    def _run(self, args: list[str], *, input: bytes | None = None, timeout: float = 120,
             check: bool = True) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run([self.docker, *args], input=input, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise SandboxError(f"docker {args[0]} timed out after {timeout}s") from e
        except FileNotFoundError as e:
            raise SandboxError("docker command not found") from e
        if check and proc.returncode != 0:
            raise SandboxError(f"docker {' '.join(args[:2])} failed: {proc.stderr.decode(errors='replace')[-500:]}")
        return proc

    # -------------------------------------------------------------- images
    def build(self, files: Mapping[str, str]) -> str:
        env = section(files, "environment")
        if "Dockerfile" not in env:
            raise BuildError("environment/Dockerfile is missing")
        tag = "taskforge-task:" + content_hash(sorted(env.items()))[:16]
        if self._run(["image", "inspect", tag], check=False).returncode == 0:
            return tag
        with tempfile.TemporaryDirectory() as tmp:
            for rel, content in env.items():
                path = Path(tmp) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            (Path(tmp) / "Dockerfile").write_text(env["Dockerfile"].rstrip() + "\n" + USER_SETUP)
            try:
                proc = subprocess.run([self.docker, "build", "-q", "-t", tag, tmp], capture_output=True,
                                      timeout=self.build_timeout_s)
            except subprocess.TimeoutExpired as e:
                raise BuildError(f"image build took longer than {self.build_timeout_s}s") from e
        if proc.returncode != 0:
            raise BuildError(proc.stderr.decode(errors="replace")[-3000:])
        return tag

    # ---------------------------------------------------------- containers
    def create(self, image: str, *, purpose: str) -> str:
        started = time.monotonic()
        name = f"tf-{purpose}-{uuid.uuid4().hex[:10]}"
        expires = int(time.time()) + self.ttl_s
        self._run(["run", "-d", "--name", name, "--network", "none", "--hostname", "sandbox",
                   "--cpus", str(self.cpus), "--memory", self.memory, "--pids-limit", str(self.pids),
                   "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER",
                   "--security-opt", "no-new-privileges",
                   "--label", "taskforge=1", "--label", f"taskforge.purpose={purpose}",
                   "--label", f"taskforge.owner={self.owner}", "--label", f"taskforge.expires={expires}",
                   "--entrypoint", "sleep", image, str(self.ttl_s)], timeout=120)
        self.created += 1
        try:
            if purpose != "verify":
                # one command: create the decoy and make it readable; then give / and the decoy's folders
                # the image's /app time, so `ls -la` prints the same in every container of this image. Without
                # it, `..` in `ls -la /app` shows when the container was made, and a re-run step stops
                # matching its cached model calls after its first `ls` (P4 drill kill_worker, D-035).
                d = DECOY_PATH.rsplit("/", 1)[0]
                self._run(["exec", "-i", "-u", "0", name, "sh", "-c",
                           f'mkdir -p {d} && cat > {DECOY_PATH} && chmod a+r {DECOY_PATH} && '
                           f'(touch -r /app / /opt {d} {DECOY_PATH} 2>/dev/null || true)'],
                          input=DECOY_CONTENT.encode())
        except BaseException:
            self.destroy(name)
            raise
        self.start_latencies.append(time.monotonic() - started)
        return name

    def exec(self, sid: str, command: str, *, timeout_s: float = 120, user: str = "agent") -> ExecResult:
        uid = USERS[user]
        try:
            proc = subprocess.run(
                [self.docker, "exec", "-u", str(uid), "-w", "/app", sid,
                 "timeout", "--kill-after=5", str(int(timeout_s)), "bash", "-c", command],
                capture_output=True, timeout=timeout_s + 30)
        except subprocess.TimeoutExpired as e:
            raise SandboxError(f"docker exec did not return within {timeout_s + 30}s") from e
        output = (proc.stdout + proc.stderr).decode(errors="replace").replace("\x00", "\\0")
        if proc.returncode in (125, 126) and "Error response from daemon" in output:
            raise SandboxError(output[-500:])
        text, cut = truncate_tail(output)
        return ExecResult(proc.returncode, text, timed_out=proc.returncode in (124, 137), truncated=cut)

    def write_files(self, sid: str, files: Mapping[str, str], *, user: str = "agent") -> dict[str, str]:
        errors: dict[str, str] = {}
        for path, content in files.items():
            proc = self._run(["exec", "-i", "-u", str(USERS[user]), sid, "sh", "-c",
                              'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", path],
                             input=content.encode(), check=False)
            if proc.returncode != 0:
                errors[path] = proc.stderr.decode(errors="replace").strip()[-300:] or f"exit {proc.returncode}"
        return errors

    def read_file(self, sid: str, path: str, *, user: str = "agent") -> str:
        proc = self._run(["exec", "-u", str(USERS[user]), sid, "cat", path], check=False)
        if proc.returncode != 0:
            raise FileNotFoundError(f"{path}: {proc.stderr.decode(errors='replace').strip()}")
        return proc.stdout.decode(errors="replace").replace("\x00", "\\0")

    def snapshot_app(self, sid: str) -> str:
        """Save /app as a tar file named by its hash; return that name."""
        data = self._run(["exec", "-u", "0", sid, "tar", "-C", "/app", "-cf", "-", "."], timeout=300).stdout
        ref = sha256_bytes(data)
        path = self.snapshot_dir / f"{ref}.tar"
        if not path.exists():
            tmp = self.snapshot_dir / f".{ref}.{uuid.uuid4().hex[:6]}"
            tmp.write_bytes(data)
            tmp.replace(path)
        return ref

    def prewarm(self, image: str, n: int) -> None:
        """No pool in the direct client; sandboxd overrides this."""

    def destroy(self, sid: str) -> None:
        self._run(["rm", "-f", sid], check=False, timeout=60)
        self.destroyed += 1

    # --------------------------------------------------------------- grading
    def verify(self, image: str, tests: Mapping[str, str], snapshot: str, *,
               timeout_s: float = 300, sid: str | None = None) -> VerifyResult:
        """Grade in a fresh container: restore /app from the snapshot, add the tests, run them as `grader`.

        The reward is the exit code of `bash /tests/test.sh`. Nothing the agent wrote outside /app
        exists here, and /tests is owned by root and read-only for the grader.
        `sid` lets sandboxd pass in an unused container from its warm pool.
        """
        try:
            app_snapshot = (self.snapshot_dir / f"{snapshot}.tar").read_bytes()
        except FileNotFoundError:
            raise SandboxError(f"snapshot {snapshot} is gone") from None
        own = sid is None                      # a container passed in is the caller's to destroy
        sid = sid or self.create(image, purpose="verify")
        try:
            self._run(["exec", "-u", "0", sid, "sh", "-c", f"rm -rf /app {DECOY_PATH} && mkdir -p /app /tests"])
            self._run(["exec", "-i", "-u", "0", sid, "tar", "-C", "/app", "-xf", "-"], input=app_snapshot,
                      timeout=300)
            self._run(["exec", "-i", "-u", "0", sid, "tar", "-C", "/tests", "-xf", "-"], input=_tar(tests),
                      timeout=120)
            self._run(["exec", "-u", "0", sid, "sh", "-c",
                       f"chown -R {GRADER_UID}:{GRADER_UID} /app && chown -R 0:0 /tests && chmod -R a+rX,go-w /tests"])
            result = self.exec(sid, "bash /tests/test.sh", timeout_s=timeout_s, user="grader")
        finally:
            if own:
                self.destroy(sid)
        return VerifyResult(result.exit_code == 0 and not result.timed_out, result.exit_code, result.output,
                            result.timed_out)

    # ------------------------------------------------------------- cleanup
    def destroy_many(self, sids) -> None:
        """Remove many containers with one docker command per 50 (one at a time took ~70 s for 70 containers
        when sandboxd stopped: P4 drill restart_sandboxd)."""
        sids = list(sids)
        for i in range(0, len(sids), 50):
            self._run(["rm", "-f", *sids[i:i + 50]], check=False, timeout=300)

    def list_owned(self) -> list[str]:
        proc = self._run(["ps", "-a", "--filter", f"label=taskforge.owner={self.owner}", "--format", "{{.Names}}"],
                         check=False)
        return proc.stdout.decode().split()

    def sweep_snapshots(self, max_age_s: float = 6 * 3600) -> int:
        removed, cutoff = 0, time.time() - max_age_s
        for path in self.snapshot_dir.glob("*.tar"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def sweep_expired(self) -> int:
        """Remove our containers whose time limit has passed (e.g. left behind by a crashed worker)."""
        proc = self._run(["ps", "-a", "--filter", f"label=taskforge.owner={self.owner}", "--format",
                          '{{.Names}} {{.Label "taskforge.expires"}}'], check=False)
        now = int(time.time())
        removed = 0
        for line in proc.stdout.decode().splitlines():
            name, _, expires = line.partition(" ")
            if expires.isdigit() and int(expires) < now:
                self.destroy(name)
                removed += 1
        return removed


def _tar(files: Mapping[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel, content in sorted(files.items()):
            data = content.encode()
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o755 if rel.endswith(".sh") else 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()
