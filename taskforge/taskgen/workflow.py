"""The task-generation workflow (DESIGN.md section 12.1) as engine steps.

Keys: "batch" for run-level steps, "pair:<id>", "pattern:<id>", "proposal:<id>", "task:<id>",
"rollout:<task>:<i>", "strong:<task>", "traj:<id>". A changed task gets a new id, so every
step after a change runs fresh (invariant 7).

Every prompt is built only from the step's input and records that existed before the step
started, so a re-executed step sends identical requests and gets cached replies.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from pydantic import ValidationError

from ..core.agent import SOLVER_TOOLS
from ..core.engine import Done, Fork, Next, Resource, Spawn, Wait, Workflow
from ..core.errors import ModelOutputError
from ..core.gateway import Gateway, PromptRegistry
from ..core.kb import MemoryKB
from ..core.review import ReviewService
from ..core.sandbox import Sandbox, section, session
from . import difficulty as diff
from . import package as pkg
from . import verify
from .acceptance import decide
from .agents import builder, hacker, judge, proposal, repairer, rubric
from .agents.llm import RoleLLM
from .config import AGENT_STEP_TIMEOUT_S, TaskgenConfig
from .contamination import BenchmarkIndex
from .hacks import STRATEGIES, scan_suspect
from .pairs import load_job, make_pairs
from .policy import EDIT_AREAS, pick, stable_fraction
from .report import build_report
from .schemas import (GATES, BadPattern, ContaminationCheck, DifficultyRecord, ExportRecord, GateResult, JudgeRecord,
                      PatternBrief, RubricItem, RubricScore, RubricVerdict, TaskDecision, TaskPackage, TaskProposal,
                      Trajectory, TrajectoryPair)
from .views import render_for_judge

PROMPTS_DIR = Path(__file__).parent / "prompts"
WORKFLOW = Workflow("taskgen", "1")
step = WORKFLOW.step


@dataclass
class Services:
    """Everything a step may use. taskforge.runtime builds it on memory or on Postgres."""
    store: Any                 # MemoryStore | PgStore
    gateway: Gateway
    sandbox: Sandbox
    reviews: ReviewService
    benchmark: BenchmarkIndex
    config: TaskgenConfig
    kb: Any = field(default_factory=MemoryKB)
    prompts: PromptRegistry = PromptRegistry(PROMPTS_DIR)


# ------------------------------------------------------------------- helpers
def _svc(ctx) -> Services:
    return ctx.services


def _llm(ctx) -> RoleLLM:
    svc = _svc(ctx)
    return RoleLLM(svc.gateway, svc.prompts, svc.config.models, ctx.call_scope, svc.config.role_params,
                   svc.config.max_tokens)


def brief(pattern: BadPattern) -> PatternBrief:
    return PatternBrief(pattern_id=pattern.id, category=pattern.category, description=pattern.description)


def files_of(svc: Services, task_id: str) -> dict[str, str]:
    return svc.store.get_json_blob(svc.store.get(task_id, TaskPackage).files_blob)


def make_task(svc: Services, files: Mapping[str, str], *, proposal_id: str, pattern_id: str, origin: str,
              parents: Iterable[str], produced_by: str, builder_ok: bool | None = None) -> TaskPackage:
    blob = svc.store.put_json_blob(dict(sorted(files.items())))
    return TaskPackage(proposal_id=proposal_id, target_pattern_id=pattern_id, files_blob=blob, origin=origin,
                       builder_oracle_ok=builder_ok, parents=tuple(parents), produced_by=produced_by)


def gate_map(svc: Services, task_id: str) -> dict[str, bool]:
    return {g.gate: g.passed for g in svc.store.find(GateResult, task_id=task_id)}


def record_gate(ctx, task_id: str, gate: str, outcome: verify.GateOutcome) -> None:
    _svc(ctx).store.put(GateResult(task_id=task_id, gate=gate, passed=outcome.passed, reasons=outcome.reasons,
                                   details=outcome.details, parents=(task_id,), produced_by=ctx.step_run_id))


CARRIED = ("proposal_id", "pattern_id", "task_id", "hack_fix_round", "repair_round", "flags")


def carry(inp: Mapping[str, Any], **changes) -> dict:
    """The fields that travel with a task from step to step, with some changed."""
    out = {k: inp[k] for k in CARRIED if k in inp}
    out.update(changes)
    return out


def finish(ctx, inp: Mapping[str, Any], decision: str, reasons: Iterable[str], *,
           pass_traj_ids: Iterable[str] = ()) -> Done:
    """End a proposal's branch with its final outcome (and a TaskDecision record when a task exists)."""
    svc = _svc(ctx)
    task_id = inp.get("task_id")
    reasons = tuple(reasons)
    if task_id:
        svc.store.put(TaskDecision(task_id=task_id, decision=decision, reasons=reasons, gates=gate_map(svc, task_id),
                                   parents=(task_id,), produced_by=ctx.step_run_id))
    return Done({"proposal_id": inp["proposal_id"], "pattern_id": inp["pattern_id"], "task_id": task_id,
                 "decision": decision, "reasons": list(reasons), "pass_traj_ids": list(pass_traj_ids)})


def _review(ctx, stage: str, item_id: str, auto: str, material: Mapping[str, Any], parents=()) -> Wait:
    rid = _svc(ctx).reviews.open(ctx.run_id, stage, item_id, auto, material, parents=tuple(parents))
    return Wait(ReviewService.signal_name(rid))


def _decision(ctx) -> tuple[str, dict]:
    payload = ctx.resume.payload or {}
    return payload.get("decision", ""), payload.get("payload") or {}


def _note_new_hack(ctx, ref: str) -> None:
    """A shortcut a person found (H4, H5) becomes a note the model-driven Hacker reads on later tasks."""
    signal = ctx.resume.payload or {}
    if signal.get("reason_code") in ("new_hack_found", "shortcut_found") and signal.get("notes"):
        _svc(ctx).kb.add("hack_note", signal["notes"], ref=ref)


# ------------------------------------------------------- mining bad patterns
@step("ingest")
def ingest(ctx, inp):
    svc = _svc(ctx)
    ids = []
    for cohort in ("strong", "weak"):
        for job in inp.get(f"{cohort}_jobs", []):
            for t in load_job(Path(job), cohort):
                ids.append(svc.store.put(t.model_copy(update={"produced_by": ctx.step_run_id})))
    return Next(Spawn("pair", "batch", {"trajectory_ids": ids}), output={"trajectories": len(ids)})


@step("pair")
def pair(ctx, inp):
    svc = _svc(ctx)
    pairs = make_pairs(svc.store.get(i, Trajectory) for i in inp["trajectory_ids"])
    if svc.config.max_pairs is not None:
        pairs = [p for p in pairs if p.id in set(pick([q.id for q in pairs], svc.config.max_pairs, salt="pairs"))]
    ids = [svc.store.put(p.model_copy(update={"produced_by": ctx.step_run_id})) for p in pairs]
    if not ids:
        return Done({"pairs": 0})
    return Next(Spawn("mine", "batch", {"pair_ids": ids}), output={"pairs": len(ids)})


@step("mine")
def mine(ctx, inp):
    if ctx.resume is None:
        return Fork(*[Spawn("write_rubric", f"pair:{p}", {"pair_id": p}) for p in inp["pair_ids"]])
    kept = [i for r in ctx.resume.results if r.status == "SUCCEEDED" for i in r.output["kept_item_ids"]]
    failed = sum(r.status != "SUCCEEDED" for r in ctx.resume.results)
    if not kept:
        return Done({"kept_items": 0, "failed_pairs": failed})
    return Next(Spawn("cluster_patterns", "batch", {"item_ids": kept}),
                output={"kept_items": len(kept), "failed_pairs": failed})


@step("write_rubric", resource=Resource.LLM)
def write_rubric(ctx, inp):
    svc = _svc(ctx)
    p = svc.store.get(inp["pair_id"], TrajectoryPair)
    strong, weak = svc.store.get(p.strong_traj, Trajectory), svc.store.get(p.weak_traj, Trajectory)
    deleted = [h.text for h in svc.kb.recent("rubric_counterexample", 8, before_seq=ctx.seq)]
    drafts = rubric.write_items(_llm(ctx), strong, weak, svc.config.items_per_pair, avoid=deleted)
    ids = list(dict.fromkeys(svc.store.put(RubricItem(pair_id=p.id, task_ref=p.task_ref, parents=(p.id,),
                                                      produced_by=ctx.step_run_id, **d)) for d in drafts))
    return Next(Spawn("score_rubric", ctx.key, {"pair_id": p.id, "item_ids": ids}))


@step("score_rubric", resource=Resource.LLM)
def score_rubric(ctx, inp):
    """Score every item on every imported trajectory of the task, then apply the partial-order rule."""
    svc, cfg = _svc(ctx), _svc(ctx).config
    p = svc.store.get(inp["pair_id"], TrajectoryPair)
    items = [svc.store.get(i, RubricItem) for i in inp["item_ids"]]
    trajs = sorted((t for t in svc.store.all(Trajectory) if t.task_ref == p.task_ref and t.source.startswith("import:")),
                   key=lambda t: t.id)
    scores: dict[str, dict[str, list[int]]] = {it.id: {"strong": [], "weak": []} for it in items}
    llm = _llm(ctx)
    for t in trajs:
        for item_id, value in rubric.score_trajectory(llm, items, t).items():
            svc.store.put(RubricScore(item_id=item_id, traj_id=t.id, cohort=t.cohort, score=value,
                                      parents=(item_id, t.id), produced_by=ctx.step_run_id))
            scores[item_id][t.cohort].append(value)
    kept = []
    for it in items:
        ok, s_mean, w_mean = rubric.partial_order(scores[it.id]["strong"], scores[it.id]["weak"],
                                                  margin=cfg.keep_margin, floor=cfg.keep_floor)
        svc.store.put(RubricVerdict(item_id=it.id, strong_mean=s_mean, weak_mean=w_mean,
                                    n_strong=len(scores[it.id]["strong"]), n_weak=len(scores[it.id]["weak"]),
                                    kept=ok, parents=(it.id,), produced_by=ctx.step_run_id))
        if ok:
            kept.append(it.id)
    return Done({"kept_item_ids": kept, "items": len(items)})


@step("cluster_patterns", resource=Resource.LLM)
def cluster_patterns(ctx, inp):
    svc = _svc(ctx)
    items = [svc.store.get(i, RubricItem) for i in inp["item_ids"]]
    ids = []
    for g in rubric.cluster(_llm(ctx), items):
        evidence = tuple(sorted({svc.store.get(i, RubricItem).pair_id for i in g["item_ids"]}))
        ids.append(svc.store.put(BadPattern(category=g["category"], description=g["description"],
                                            item_ids=tuple(g["item_ids"]), evidence_pairs=evidence,
                                            parents=tuple(g["item_ids"]), produced_by=ctx.step_run_id)))
    if not ids:
        return Done({"patterns": 0})
    return Next(Spawn("h1_calibrate", "batch", {"pattern_ids": ids}), output={"patterns": len(ids)})


@step("h1_calibrate", resource=Resource.HUMAN)
def h1_calibrate(ctx, inp):
    """Whole-batch stop: a person keeps, or deletes, each pattern before any task is built from it."""
    svc = _svc(ctx)
    if ctx.resume is None:
        patterns = []
        for pid in inp["pattern_ids"]:
            pat = svc.store.get(pid, BadPattern)
            evidence = [svc.store.get(e, TrajectoryPair) for e in pat.evidence_pairs[:3]]
            patterns.append({"id": pid, "category": pat.category, "description": pat.description,
                             "items": [svc.store.get(i, RubricItem).description for i in pat.item_ids],
                             "evidence": [{"task": e.task_ref, "strong": e.strong_traj, "weak": e.weak_traj}
                                          for e in evidence]})
        return _review(ctx, "H1", f"batch:{ctx.run_id}", "PROPOSED", {"patterns": patterns}, inp["pattern_ids"])
    _, payload = _decision(ctx)
    keep = [p for p in payload.get("keep", inp["pattern_ids"]) if p in inp["pattern_ids"]]
    if svc.config.max_patterns is not None:
        keep = keep[:svc.config.max_patterns]
    for pid in set(inp["pattern_ids"]) - set(keep):       # deleted patterns teach the Rubric Writer what not to write
        for item_id in svc.store.get(pid, BadPattern).item_ids:
            svc.kb.add("rubric_counterexample", svc.store.get(item_id, RubricItem).description, ref=item_id,
                       meta={"reason": (ctx.resume.payload or {}).get("reason_code")})
    if not keep:
        return Done({"patterns_kept": 0})
    return Next(Spawn("generate", "batch", {"pattern_ids": keep}), output={"patterns_kept": len(keep)})


# ------------------------------------------------------------- per pattern
@step("generate")
def generate(ctx, inp):
    if ctx.resume is None:
        return Fork(*[Spawn("propose", f"pattern:{p}", {"pattern_id": p}) for p in inp["pattern_ids"]])
    outcomes = [r.output for r in ctx.resume.results if r.status == "SUCCEEDED"]
    failed = [{"step": r.step, "key": r.key, "error": (r.error or "").strip().splitlines()[-1:]}
              for r in ctx.resume.results if r.status != "SUCCEEDED"]
    return Next(Spawn("h5_audit", "batch", {"outcomes": outcomes, "failed_branches": failed}))


@step("propose", resource=Resource.LLM)
def propose(ctx, inp):
    svc, cfg = _svc(ctx), _svc(ctx).config
    pattern = svc.store.get(inp["pattern_id"], BadPattern)
    # what already exists for this weakness, from earlier runs too; only documents older than this step,
    # so a re-executed step builds the same prompt and its model call is answered from the cache
    known = svc.kb.search(("proposal", "accepted_task"), f"{pattern.category} {pattern.description}", k=8,
                          before_seq=ctx.seq)
    drafts = proposal.propose(_llm(ctx), brief(pattern), cfg.proposals_per_pattern, [h.text[:600] for h in known])
    ids = list(dict.fromkeys(svc.store.put(TaskProposal(target_pattern_id=pattern.id, parents=(pattern.id,),
                                                        produced_by=ctx.step_run_id, **d)) for d in drafts))
    for pid in ids:
        prop = svc.store.get(pid, TaskProposal)
        svc.kb.add("proposal", f"{prop.title}. {prop.statement}", ref=pid, meta={"pattern": pattern.category})
    sampled = set(pick(ids, cfg.h2_sample_per_pattern, salt="H2"))
    return Next(*[Spawn("check_contamination", f"proposal:{i}",
                        {"proposal_id": i, "pattern_id": pattern.id, "h2_sample": i in sampled}) for i in ids])


@step("check_contamination")
def check_contamination(ctx, inp):
    svc = _svc(ctx)
    prop = svc.store.get(inp["proposal_id"], TaskProposal)
    c = svc.benchmark.check(f"{prop.title}\n{prop.statement}")
    svc.store.put(ContaminationCheck(target_id=prop.id, text_kind="proposal", status=c.status, score=c.score,
                                     closest=c.closest, parents=(prop.id,), produced_by=ctx.step_run_id))
    base = carry(inp)
    if c.status == "OVERLAP":
        return finish(ctx, base, "REJECT", ("contamination:proposal",))
    if c.status == "GREY" or inp["h2_sample"]:
        return Next(Spawn("h2_review", ctx.key, {**base, "auto": c.status, "score": c.score, "closest": c.closest}))
    return Next(Spawn("build_task", ctx.key, base))


@step("h2_review", resource=Resource.HUMAN)
def h2_review(ctx, inp):
    svc = _svc(ctx)
    if ctx.resume is None:
        prop = svc.store.get(inp["proposal_id"], TaskProposal)
        pat = svc.store.get(inp["pattern_id"], BadPattern)
        return _review(ctx, "H2", prop.id, inp["auto"],
                       {"title": prop.title, "statement": prop.statement, "pattern": pat.description,
                        "overlap_score": inp["score"], "closest_benchmark_item": inp["closest"]}, (prop.id,))
    decision, _ = _decision(ctx)
    if decision == "drop":
        return finish(ctx, carry(inp), "REJECT", ("h2:drop",))
    return Next(Spawn("build_task", ctx.key, carry(inp)))


@step("build_task", resource=Resource.AGENT, timeout_s=AGENT_STEP_TIMEOUT_S)
def build_task(ctx, inp):
    svc, cfg = _svc(ctx), _svc(ctx).config
    prop = svc.store.get(inp["proposal_id"], TaskProposal)
    pattern = svc.store.get(inp["pattern_id"], BadPattern)
    files, ok, _log = builder.draft(_llm(ctx), svc.sandbox, brief(pattern), prop.title, prop.statement,
                                    cfg.builder_fix_rounds)
    task = make_task(svc, files, proposal_id=prop.id, pattern_id=pattern.id, origin="builder", parents=(prop.id,),
                     produced_by=ctx.step_run_id, builder_ok=ok)
    task_id = svc.store.put(task)
    c = svc.benchmark.check(files.get("instruction.md", ""))
    svc.store.put(ContaminationCheck(target_id=task_id, text_kind="instruction", status=c.status, score=c.score,
                                     closest=c.closest, parents=(task_id,), produced_by=ctx.step_run_id))
    nxt = carry(inp, task_id=task_id, hack_fix_round=0, repair_round=0,
                flags=["grey_instruction"] if c.status == "GREY" else [])
    if c.status == "OVERLAP":
        return finish(ctx, nxt, "REJECT", ("contamination:instruction",))
    return Next(Spawn("gate_oracle", f"task:{task_id}", nxt))


# ------------------------------------------------------------ executed gates
@step("gate_oracle", resource=Resource.SANDBOX)
def gate_oracle(ctx, inp):
    res = verify.gate_oracle(_svc(ctx).sandbox, files_of(_svc(ctx), inp["task_id"]))
    record_gate(ctx, inp["task_id"], "oracle", res)
    if not res.passed:
        return finish(ctx, inp, "REJECT", ("gate:oracle", *res.reasons))
    # demand hint for the warm pool: the initial-state check and every scripted shortcut each need a
    # sandbox to work in and a fresh one to be graded in
    svc = _svc(ctx)
    svc.sandbox.prewarm(svc.sandbox.build(files_of(svc, inp["task_id"])), 2 + 2 * len(STRATEGIES))
    return Next(Spawn("gate_initial", ctx.key, carry(inp)))


@step("gate_initial", resource=Resource.SANDBOX)
def gate_initial(ctx, inp):
    res = verify.gate_initial(_svc(ctx).sandbox, files_of(_svc(ctx), inp["task_id"]))
    record_gate(ctx, inp["task_id"], "initial", res)
    if not res.passed:
        return finish(ctx, inp, "REJECT", ("gate:initial", *res.reasons))
    return Next(Spawn("gate_hack", ctx.key, carry(inp)))


@step("gate_hack", resource=Resource.AGENT, timeout_s=AGENT_STEP_TIMEOUT_S)
def gate_hack(ctx, inp):
    """A successful shortcut means the tests are weak: fix them (invariant 2), never drop the task here."""
    svc, cfg = _svc(ctx), _svc(ctx).config
    files = files_of(svc, inp["task_id"])
    report = verify.run_scripted_hacks(svc.sandbox, files)
    if cfg.use_model_hacker and not any(v["passed"] for v in report.values()):
        notes = [h.text for h in svc.kb.recent("hack_note", 5, before_seq=ctx.seq)]
        report["model_hacker"] = hacker.model_hack(_llm(ctx), svc.sandbox, svc.sandbox.build(files), files,
                                                   cfg.hacker_max_steps, notes=notes, wall_s=cfg.hacker_wall_s)
    won = sorted(k for k, v in report.items() if v["passed"])
    record_gate(ctx, inp["task_id"], "hack",
                verify.GateOutcome(not won, tuple(f"hack:{k}" for k in won), {"strategies": report}))
    if not won:
        return Next(Spawn("gate_leak", ctx.key, carry(inp)))
    if inp["hack_fix_round"] < cfg.hack_fix_rounds:
        return Next(Spawn("fix_verifier", ctx.key, carry(inp, hack_report=report)))
    return Next(Spawn("h3_fix", ctx.key, carry(inp, why=f"tests still beaten after {cfg.hack_fix_rounds} fixes: {won}")))


@step("fix_verifier", resource=Resource.AGENT, timeout_s=AGENT_STEP_TIMEOUT_S)
def fix_verifier(ctx, inp):
    svc = _svc(ctx)
    files = files_of(svc, inp["task_id"])
    try:
        changes = builder.strengthen_tests(_llm(ctx), files, inp["hack_report"])
    except ModelOutputError as e:
        return Next(Spawn("h3_fix", ctx.key, carry(inp, why=f"verifier fix unusable: {e}")))
    new_files = pkg.apply_changes(files, changes)
    changed = pkg.changed(files, new_files)
    if not changed:
        return Next(Spawn("h3_fix", ctx.key, carry(inp, why="verifier fix changed nothing")))
    rnd = inp["hack_fix_round"] + 1
    return _spawn_child(ctx, inp, new_files, origin="fix_verifier", diagnosis="exploit", mutation="strengthen_tests",
                        area="tests", changed=changed, by="agent", round_no=rnd, counters={"hack_fix_round": rnd})


@step("gate_leak", resource=Resource.LLM)
def gate_leak(ctx, inp):
    """Hard rules decide. The model's suspicion is recorded and sends the task to H4, but cannot reject it."""
    files = files_of(_svc(ctx), inp["task_id"])
    res = verify.gate_leak(files)
    suspect, reason = judge.leak_suspicion(_llm(ctx), files)
    record_gate(ctx, inp["task_id"], "leak", verify.GateOutcome(
        res.passed, res.reasons, {**res.details, "model_suspects_leak": suspect, "model_reason": reason}))
    if not res.passed:
        return finish(ctx, inp, "REJECT", ("gate:leak", *res.reasons))
    flags = list(inp.get("flags", [])) + (["model_suspects_leak"] if suspect else []) \
        + (["test_lines_in_workspace"] if res.details.get("suspect_test_lines") else [])
    svc = _svc(ctx)
    svc.sandbox.prewarm(svc.sandbox.build(files), 2 * svc.config.n_runs)     # one to work in + one to grade, per rollout
    return Next(Spawn("difficulty", ctx.key, carry(inp, flags=flags)))


# ----------------------------------------------------------- difficulty
def _rollouts(ctx, task_id: str, indexes: Iterable[int], retry: int = 0) -> list[Spawn]:
    """Keys follow the difficulty step's own key, so an operator re-run of that step gets fresh attempts.
    A retry of a failed attempt keeps its index and gets a suffix: rollout:<task>:3#r1."""
    base = ctx.key.split(":", 1)[1] if ctx.key.startswith("task:") else ctx.key
    suffix = f"#r{retry}" if retry else ""
    return [Spawn("rollout", f"rollout:{base}:{i}{suffix}", {"task_id": task_id, "index": i, "role": "weak_solver"})
            for i in indexes]


def _prewarm(ctx, task_id: str, attempts: int) -> None:
    """Tell sandboxd a batch of attempts is coming: one sandbox to work in and one to grade in, each. The
    leak gate does this for the first batch; the second batch and re-runs of failed attempts said nothing
    before P4, so all their sandboxes were cold starts."""
    svc = _svc(ctx)
    svc.sandbox.prewarm(svc.sandbox.build(files_of(svc, task_id)), 2 * attempts)


def _runs(results) -> tuple[list[dict], list[int]]:
    """Finished attempts (sorted by index) and the indexes of attempts that failed for infrastructure reasons."""
    ok = sorted((r.output for r in results if r.status == "SUCCEEDED"), key=lambda o: o["index"])
    failed = sorted(int(r.root_key.rsplit(":", 1)[1].split("#")[0]) for r in results if r.status != "SUCCEEDED")
    return ok, failed


def _repair_or_h3(ctx, inp, diagnosis: str, why: str, runs: list[dict]):
    if inp["repair_round"] < _svc(ctx).config.repair_rounds:
        return Next(Spawn("repair", ctx.key, carry(inp, diagnosis=diagnosis, weak_runs=runs)))
    return Next(Spawn("h3_fix", ctx.key, carry(inp, why=f"{why}; repair rounds used up")))


@step("difficulty")
def difficulty(ctx, inp):
    cfg = _svc(ctx).config
    task_id, n = inp["task_id"], cfg.n_runs
    if ctx.resume is None:
        return Fork(*_rollouts(ctx, task_id, range(n)), state={"phase": "first"})
    state = ctx.resume.state
    ok, failed = _runs(ctx.resume.results)
    runs = sorted(state.get("partial", []) + ok, key=lambda o: o["index"])
    if failed:
        tries = state.get("retries", 0) + 1
        if tries > cfg.rollout_retries:
            record_gate(ctx, task_id, "difficulty", verify.GateOutcome(False, ("rollouts_failed",), {"failed": failed}))
            return finish(ctx, inp, "REJECT", ("gate:difficulty", "rollouts_failed"))
        # infrastructure failures: run just those attempts again, keep the finished ones
        _prewarm(ctx, task_id, len(failed))
        return Fork(*_rollouts(ctx, task_id, failed, retry=tries), state={**state, "partial": runs, "retries": tries})
    if state["phase"] == "first":
        if diff.after_first([r["passed"] for r in runs], cfg.band_low) == "in_band":
            return _in_band(ctx, inp, runs)
        _prewarm(ctx, task_id, n)
        return Fork(*_rollouts(ctx, task_id, range(n, 2 * n)), state={"phase": "rerun", "first": runs})
    runs = state["first"] + runs
    verdict = diff.after_rerun([r["passed"] for r in runs[:n]], [r["passed"] for r in runs[n:]], cfg.band_low)
    if verdict == "in_band":
        return _in_band(ctx, inp, runs)
    k = sum(r["passed"] for r in runs)
    record_gate(ctx, task_id, "difficulty", verify.GateOutcome(False, (f"difficulty:{verdict}",),
                                                               {"passes": k, "runs": len(runs)}))
    if verdict == "all_pass":
        if diff.diagnose_all_pass([r["suspect"] for r in runs if r["passed"]]) == "exploit":
            report = {"weak_model_runs": {"passed": True, "suspect": sorted({s for r in runs for s in r["suspect"]})}}
            if inp["hack_fix_round"] < cfg.hack_fix_rounds:
                return Next(Spawn("fix_verifier", ctx.key, carry(inp, hack_report=report)))
            return Next(Spawn("h3_fix", ctx.key, carry(inp, why="weak runs pass by a shortcut; fixes used up")))
        return _repair_or_h3(ctx, inp, "too_easy", f"weak model passed {k}/{len(runs)}", runs)
    return Next(Spawn("diagnose", ctx.key, carry(inp, weak_runs=runs)))


def _in_band(ctx, inp, runs):
    k = sum(r["passed"] for r in runs)
    record_gate(ctx, inp["task_id"], "difficulty", verify.GateOutcome(True, (), {"passes": k, "runs": len(runs)}))
    return Next(Spawn("blind_judge", ctx.key, carry(inp, weak_runs=runs)))


@step("rollout", resource=Resource.AGENT, timeout_s=AGENT_STEP_TIMEOUT_S)
def rollout(ctx, inp):
    svc, cfg = _svc(ctx), _svc(ctx).config
    files = files_of(svc, inp["task_id"])
    image = svc.sandbox.build(files)
    role, instruction = inp["role"], files.get("instruction.md", "")
    with session(svc.sandbox, image, purpose="rollout") as sid:
        steps = cfg.strong_solver_max_steps if role == "strong_solver" else cfg.solver_max_steps
        result = _llm(ctx).agent(role, system_prompt="solver_system", task_prompt=instruction, sandbox=svc.sandbox,
                                 sid=sid, tools=SOLVER_TOOLS, max_steps=steps, wall_s=cfg.solver_wall_s,
                                 params=cfg.solver_params if role == "weak_solver" else None,
                                 tags={"task_id": inp["task_id"], "index": inp["index"]})
        snap = svc.sandbox.snapshot_app(sid)
    v = svc.sandbox.verify(image, section(files, "tests"), snap)
    traj = Trajectory(task_ref=inp["task_id"], cohort="strong" if role == "strong_solver" else "weak",
                      model=cfg.models[role], source="rollout", rollout_index=inp["index"], task_prompt=instruction,
                      steps=tuple(result.steps), reward=1.0 if v.passed else 0.0, exit=result.exit,
                      suspect=scan_suspect(result.steps), parents=(inp["task_id"],), produced_by=ctx.step_run_id)
    return Done({"index": inp["index"], "traj_id": svc.store.put(traj), "passed": v.passed,
                 "suspect": list(traj.suspect)})


@step("diagnose", resource=Resource.LLM)
def diagnose(ctx, inp):
    """Every weak run failed: one strong run plus a blind judgement tell 'broken' from 'truly hard'."""
    task_id = inp["task_id"]
    base = ctx.key.split(":", 1)[1] if ctx.key.startswith("task:") else ctx.key
    if ctx.resume is None:
        return Fork(Spawn("rollout", f"strong:{base}", {"task_id": task_id, "index": 0, "role": "strong_solver"}),
                    state={"retries": 0})
    strong = [r.output for r in ctx.resume.results if r.status == "SUCCEEDED"]
    tries = (ctx.resume.state or {}).get("retries", 0)
    if not strong and tries < _svc(ctx).config.rollout_retries:
        return Fork(Spawn("rollout", f"strong:{base}#r{tries + 1}", {"task_id": task_id, "index": 0,
                                                                     "role": "strong_solver"}), state={"retries": tries + 1})
    strong_passed = bool(strong and strong[0]["passed"])
    verdict, evidence = _judge(ctx, task_id, inp["weak_runs"])
    d = diff.diagnose_all_fail(strong_passed, verdict)
    if d == "unclear":
        return Next(Spawn("h3_fix", ctx.key, carry(inp, why=f"all weak runs failed; strong passed={strong_passed}, "
                                                            f"judge={verdict}")))
    return _repair_or_h3(ctx, inp, d, f"all weak runs failed; diagnosis {d}", inp["weak_runs"])


def _judge(ctx, task_id: str, runs: list[dict]) -> tuple[str, str]:
    svc = _svc(ctx)
    traj_ids = [r["traj_id"] for r in runs]
    views = [render_for_judge(svc.store.get(t, Trajectory)) for t in traj_ids]
    verdict, evidence = judge.blind_judge(_llm(ctx), files_of(svc, task_id).get("instruction.md", ""), views)
    svc.store.put(JudgeRecord(task_id=task_id, verdict=verdict, evidence=evidence, traj_ids=tuple(traj_ids),
                              parents=(task_id, *traj_ids), produced_by=ctx.step_run_id))
    return verdict, evidence


@step("repair", resource=Resource.AGENT, timeout_s=AGENT_STEP_TIMEOUT_S)
def repair(ctx, inp):
    svc = _svc(ctx)
    files = files_of(svc, inp["task_id"])
    runs = inp["weak_runs"]
    shown = [render_for_judge(svc.store.get(r["traj_id"], Trajectory)).text for r in runs[:2]]
    evidence = (f"weak model passed {sum(r['passed'] for r in runs)} of {len(runs)} runs\n\n"
                + "\n\n".join(f"### Run {i + 1}\n{t}" for i, t in enumerate(shown)))
    examples = [h.text for h in svc.kb.search("repair_example", f"{inp['diagnosis']} {files.get('instruction.md', '')[:400]}",
                                              k=3, before_seq=ctx.seq)]
    cfg = svc.config
    target = (cfg.band_low, cfg.n_runs - cfg.band_low, cfg.n_runs)
    try:
        mutation, area, changes = repairer.repair(_llm(ctx), files, inp["diagnosis"], evidence, examples,
                                                  passes=(sum(r["passed"] for r in runs), len(runs)), target=target)
    except ModelOutputError as e:
        return Next(Spawn("h3_fix", ctx.key, carry(inp, why=f"repair unusable: {e}")))
    new_files = pkg.apply_changes(files, changes)
    changed = pkg.changed(files, new_files)
    rnd = inp["repair_round"] + 1
    return _spawn_child(ctx, inp, new_files, origin="repair", diagnosis=inp["diagnosis"], mutation=mutation, area=area,
                        changed=changed, by="agent", round_no=rnd, counters={"repair_round": rnd},
                        pass_count=sum(r["passed"] for r in runs), n_runs=len(runs))


def _spawn_child(ctx, inp, new_files, *, origin, diagnosis, mutation, area, changed, by, round_no, counters,
                 pass_count=None, n_runs=None):
    """Store parent -> child with its DifficultyRecord, then send the child through every gate again (invariant 7)."""
    svc = _svc(ctx)
    parent = svc.store.get(inp["task_id"], TaskPackage)
    child = make_task(svc, new_files, proposal_id=parent.proposal_id, pattern_id=parent.target_pattern_id,
                      origin=origin, parents=(parent.id,), produced_by=ctx.step_run_id)
    try:
        record = DifficultyRecord(parent_task=parent.id, child_task=child.id, round=round_no, diagnosis=diagnosis,
                                  mutation=mutation, area=area, changed_files=tuple(changed), pass_count=pass_count,
                                  n_runs=n_runs, by=by, parents=(parent.id, child.id), produced_by=ctx.step_run_id)
    except ValidationError as e:
        if by == "human":
            return finish(ctx, inp, "REJECT", ("h3:invalid_edit",))
        return Next(Spawn("h3_fix", ctx.key, carry(inp, why=f"change breaks the one-change rule: {e.errors()[0]['msg']}")))
    svc.store.put(child)
    svc.store.put(record)
    return Next(Spawn("gate_oracle", f"task:{child.id}", carry(inp, task_id=child.id, **counters)))


@step("h3_fix", resource=Resource.HUMAN)
def h3_fix(ctx, inp):
    svc = _svc(ctx)
    task_id = inp["task_id"]
    if ctx.resume is None:
        lineage = [r for r in svc.store.ancestors(task_id) if isinstance(r, TaskPackage)]
        history = [d.model_dump(mode="json", exclude={"parents", "produced_by"})
                   for t in [task_id, *(r.id for r in lineage)] for d in svc.store.find(DifficultyRecord, child_task=t)]
        files = files_of(svc, task_id)
        return _review(ctx, "H3", task_id, "NEEDS_FIX",
                       {"why": inp.get("why", ""), "instruction": files.get("instruction.md", ""),
                        "files": sorted(files), "gates": gate_map(svc, task_id), "history": history}, (task_id,))
    decision, payload = _decision(ctx)
    if decision == "drop":
        return finish(ctx, inp, "REJECT", ("h3:drop",))
    files = files_of(svc, task_id)
    new_files = pkg.apply_changes(files, payload.get("files") or {})
    changed = pkg.changed(files, new_files)
    if not changed:
        return finish(ctx, inp, "REJECT", ("h3:invalid_edit",))
    signal = ctx.resume.payload or {}
    svc.kb.add("repair_example", f"Problem: {inp.get('why', '')}. A person chose {decision.replace('_', ' ')} "
                                 f"(reason: {signal.get('reason_code', '')}) and changed {', '.join(changed)}. "
                                 f"{signal.get('notes', '')}".strip(), ref=task_id)
    return _spawn_child(ctx, inp, new_files, origin="human", diagnosis="human_edit", mutation=decision,
                        area=EDIT_AREAS[decision], changed=changed, by="human", round_no=0, counters={})


# ------------------------------------------------------------- judging
@step("blind_judge", resource=Resource.LLM)
def blind_judge(ctx, inp):
    svc, cfg = _svc(ctx), _svc(ctx).config
    task_id = inp["task_id"]
    verdict, _ = _judge(ctx, task_id, inp["weak_runs"])
    auto, reasons = decide(gate_map(svc, task_id), verdict)
    passes = [r["traj_id"] for r in inp["weak_runs"] if r["passed"]]
    if auto == "REJECT":
        return finish(ctx, inp, auto, reasons)
    harder = any(d.diagnosis == "too_easy" for d in svc.store.find(DifficultyRecord, child_task=task_id))
    sampled = (bool(inp.get("flags")) or harder
               or stable_fraction(task_id, "H4") < cfg.h4_sample_rate)
    if auto == "ACCEPT" and not sampled:
        return finish(ctx, inp, "ACCEPT", reasons, pass_traj_ids=passes)
    return Next(Spawn("h4_review", ctx.key, carry(inp, auto=auto, verdict=verdict, pass_traj_ids=passes)))


@step("h4_review", resource=Resource.HUMAN)
def h4_review(ctx, inp):
    svc = _svc(ctx)
    task_id = inp["task_id"]
    if ctx.resume is None:
        j = svc.store.find(JudgeRecord, task_id=task_id)
        return _review(ctx, "H4", task_id, inp["auto"],
                       {"instruction": files_of(svc, task_id).get("instruction.md", ""), "flags": inp.get("flags", []),
                        "judge": {"verdict": j[-1].verdict, "evidence": j[-1].evidence} if j else None,
                        "gates": gate_map(svc, task_id)}, (task_id,))
    decision, _ = _decision(ctx)
    _note_new_hack(ctx, task_id)
    final, reasons = decide(gate_map(svc, task_id), inp["verdict"], decision)
    return finish(ctx, inp, final, reasons, pass_traj_ids=inp["pass_traj_ids"] if final == "ACCEPT" else ())


# ------------------------------------------------------ end of the batch
@step("h5_audit")
def h5_audit(ctx, inp):
    """Sample passing weak runs of accepted tasks; a person checks each for a shortcut before export."""
    svc, cfg = _svc(ctx), _svc(ctx).config
    if ctx.resume is None:
        sample = [t for o in inp["outcomes"] if o["decision"] == "ACCEPT"
                  for t in pick(o["pass_traj_ids"], cfg.h5_sample_per_task, salt="H5")]
        if sample:
            return Fork(*[Spawn("h5_check", f"traj:{t}", {"traj_id": t}) for t in sample])
        shortcuts: set[str] = set()
    else:
        shortcuts = {r.output["traj_id"] for r in ctx.resume.results
                     if r.status == "SUCCEEDED" and r.output["decision"] == "shortcut"}
    outcomes = []
    for o in inp["outcomes"]:
        if o["decision"] == "ACCEPT" and shortcuts & set(o["pass_traj_ids"]):
            o = {**o, "decision": "REJECT", "reasons": [*o["reasons"], "h5:shortcut"]}
            svc.store.put(TaskDecision(task_id=o["task_id"], decision="REJECT", reasons=tuple(o["reasons"]),
                                       gates=gate_map(svc, o["task_id"]), parents=(o["task_id"],),
                                       produced_by=ctx.step_run_id))
        outcomes.append(o)
    return Next(Spawn("h6_decide", "batch", {"outcomes": outcomes, "failed_branches": inp["failed_branches"]}))


@step("h5_check", resource=Resource.HUMAN)
def h5_check(ctx, inp):
    svc = _svc(ctx)
    if ctx.resume is None:
        t = svc.store.get(inp["traj_id"], Trajectory)
        return _review(ctx, "H5", t.id, "PASS", {"task_id": t.task_ref, "suspect": list(t.suspect),
                                                  "trajectory": render_for_judge(t).text}, (t.id,))
    decision, _ = _decision(ctx)
    _note_new_hack(ctx, inp["traj_id"])
    return Done({"traj_id": inp["traj_id"], "decision": decision})


@step("h6_decide", resource=Resource.HUMAN)
def h6_decide(ctx, inp):
    """Whole-batch stop before export."""
    if ctx.resume is None:
        report = build_report(_svc(ctx), ctx.run_id, inp["outcomes"], inp["failed_branches"])
        return _review(ctx, "H6", f"batch:{ctx.run_id}", "READY", report)
    decision, _ = _decision(ctx)
    if decision == "hold":
        return Done({"held": True})
    return Next(Spawn("export", "batch", dict(inp)))


@step("export")
def export(ctx, inp):
    svc = _svc(ctx)
    report = build_report(svc, ctx.run_id, inp["outcomes"], inp["failed_branches"])
    accepted = tuple(o["task_id"] for o in inp["outcomes"] if o["decision"] == "ACCEPT")
    for task_id in accepted:                         # later runs avoid proposing near-copies of these
        svc.kb.add("accepted_task", files_of(svc, task_id).get("instruction.md", ""), ref=task_id)
    rid = svc.store.put(ExportRecord(run_id=ctx.run_id, accepted_task_ids=accepted, funnel=report["funnel"],
                                     measures=report["measures"], parents=accepted, produced_by=ctx.step_run_id))
    return Done({"export_id": rid, "accepted": list(accepted), **report})


assert set(GATES) == {"oracle", "initial", "hack", "leak", "difficulty"}


# The P4 load test adds one operator step (load_fanout) to this workflow; it calls no paid model.
from . import loadtest  # noqa: E402,F401
