"""Workflows used by the concurrency tests. Module-level so worker subprocesses can import them."""
import hashlib
import time

from taskforge.core.engine import Done, Fork, Next, Spawn, Wait, Workflow
from taskforge.core.errors import Retryable, WorkerCrash

TREE = Workflow("tree", "1")


def _roll(key: str, salt: str) -> float:
    return int(hashlib.sha256(f"{salt}|{key}".encode()).hexdigest()[:8], 16) / 16 ** 8


@TREE.step("root", max_attempts=6)
def root(ctx, inp):
    if ctx.resume is None:
        return Fork(*[Spawn("mid", f"m{i}", {**inp, "i": i}) for i in range(inp["mids"])])
    assert all(r.status == "SUCCEEDED" for r in ctx.resume.results), [r for r in ctx.resume.results if r.status != "SUCCEEDED"]
    return Done({"total": sum(r.output["sum"] for r in ctx.resume.results), "branches": len(ctx.resume.results)})


@TREE.step("mid", max_attempts=6)
def mid(ctx, inp):
    if ctx.resume is None:
        if ctx.attempt == 1 and _roll(ctx.key, "mid") < inp.get("crash", 0):
            raise WorkerCrash()
        return Fork(*[Spawn("leaf", f"{ctx.key}-l{j}", {**inp, "j": j}) for j in range(inp["leaves"])])
    return Done({"sum": sum(r.output["v"] for r in ctx.resume.results)})


@TREE.step("leaf", max_attempts=6)
def leaf(ctx, inp):
    if inp.get("sleep"):
        time.sleep(inp["sleep"])
    roll = _roll(ctx.key, "leaf")
    if ctx.attempt == 1 and roll < inp.get("crash", 0):
        raise WorkerCrash()
    if ctx.attempt == 1 and roll > 1 - inp.get("flaky", 0):
        raise Retryable("pretend sandbox fault", delay_s=0.05)
    if roll < 0.5:
        return Next(Spawn("tail", ctx.key, {"v": inp["i"] * 100 + inp["j"]}))     # half the leaves continue as a chain
    return Done({"v": inp["i"] * 100 + inp["j"]})


@TREE.step("tail", max_attempts=6)
def tail(ctx, inp):
    return Done({"v": inp["v"]})


def expected_total(mids: int, leaves: int) -> int:
    return sum(i * 100 + j for i in range(mids) for j in range(leaves))


WAITS = Workflow("waits", "1")


@WAITS.step("fan")
def fan(ctx, inp):
    if ctx.resume is None:
        return Fork(*[Spawn("waiter", f"w{i}", {"i": i}) for i in range(inp["n"])])
    return Done(sorted(r.output for r in ctx.resume.results))


@WAITS.step("waiter")
def waiter(ctx, inp):
    if ctx.resume is None:
        return Wait(f"sig:{inp['i']}")
    return Done(ctx.resume.payload)
