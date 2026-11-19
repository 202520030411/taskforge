"""Review console and control API (DESIGN.md section 10).

Pages for people: runs, one run, the review queue, one review with its decision form, any record with its
lineage links, and review statistics. JSON under /api for scripts. /metrics for Prometheus.
All rules about which decisions are allowed live in ReviewService, not here: a form can only offer what
the service will accept, and the service checks again on submit.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from ..core.review import ReviewRecord
from ..runtime import Platform

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
STAGE_HELP = {
    "H1": "Check each bad pattern before any task is built from it. Untick a pattern to delete it.",
    "H2": "Is this proposal aimed at the pattern, and not a copy of a benchmark task?",
    "H3": "Automatic repair gave up. Edit one area of the task, or drop it.",
    "H4": "The judge flagged this task, or it was sampled. Keep it or reject it.",
    "H5": "Did this passing attempt really solve the task, or take a shortcut?",
    "H6": "Last stop before export. Look at the funnel and coverage.",
    "H7": "Label blind, with the judge's four labels.",
}


class Decision(BaseModel):
    decision: str
    reason_code: str
    notes: str = ""
    minutes_spent: float = 0.0
    payload: dict[str, Any] = {}
    reviewer: str = "owner"


class StartRun(BaseModel):
    paid_approved: bool = False
    budget_usd: float = 0.0
    strong_jobs: list[str] = []
    weak_jobs: list[str] = []
    demo_jobs: bool = False            # write the small scripted "stored runs" and use those


def _pretty(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, indent=2, default=str)


def create_app(platform: Platform) -> FastAPI:
    engine, svc = platform.engine, platform.svc
    app = FastAPI(title="taskforge")
    TEMPLATES.env.filters["pretty"] = _pretty
    TEMPLATES.env.filters["money"] = lambda v: f"${v:,.4f}" if v is not None else "-"

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, name, {"pending_total": len(svc.reviews.pending()), **ctx})

    def run_or_404(run_id: str) -> dict:
        try:
            return engine.run_info(run_id)
        except KeyError:
            raise HTTPException(404, f"no run {run_id}") from None

    def review_or_404(review_id: str):
        try:
            return svc.reviews.get_item(review_id)
        except KeyError:
            raise HTTPException(404, f"no review {review_id}") from None

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def runs(request: Request):
        rows = [{**r, "summary": engine.summary(r["id"])} for r in engine.list_runs()[:50]]
        return page(request, "runs.html", runs=rows)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run(request: Request, run_id: str):
        info = run_or_404(run_id)
        steps = engine.all_steps(run_id)
        by_step: dict[str, dict[str, int]] = {}
        for s in steps:
            by_step.setdefault(s.step, {}).setdefault(s.status.value, 0)
            by_step[s.step][s.status.value] += 1
        export = next((s.output for s in steps if s.step == "export" and s.output), None)
        return page(request, "run.html", run=info, summary=engine.summary(run_id), by_step=by_step,
                    failed=[s for s in steps if s.status.value == "FAILED"],
                    running=[s for s in steps if s.status.value == "RUNNING"],
                    reviews=svc.reviews.pending(run_id=run_id), cost=svc.gateway.cost_by_role(run_id),
                    export=export, events=engine.recent_events(run_id, 40),
                    disagreement=svc.reviews.disagreement(run_id))

    @app.post("/runs/{run_id}/approve")
    def approve(run_id: str, budget_usd: float = Form(...)):
        run_or_404(run_id)
        engine.approve(run_id, budget_usd)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/cancel")
    def cancel(run_id: str):
        run_or_404(run_id)
        engine.cancel(run_id)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/steps/{step_run_id}/requeue")
    def requeue(step_run_id: str):
        step = engine.get_step(step_run_id)
        if step is None:
            raise HTTPException(404, "no such step run")
        try:
            engine.requeue(step_run_id)
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        return RedirectResponse(f"/runs/{step.run_id}", status_code=303)

    @app.get("/reviews", response_class=HTMLResponse)
    def reviews(request: Request, stage: str | None = None, run_id: str | None = None):
        items = svc.reviews.pending(run_id=run_id, stage=stage)
        stages = sorted({i.stage for i in svc.reviews.pending()})
        return page(request, "reviews.html", items=items, stage=stage, run_id=run_id, stages=stages)

    @app.get("/reviews/{review_id}", response_class=HTMLResponse)
    def review(request: Request, review_id: str, error: str | None = None):
        item = review_or_404(review_id)
        return page(request, "review.html", item=item, help=STAGE_HELP.get(item.stage, ""), error=error)

    @app.post("/reviews/{review_id}")
    async def submit(request: Request, review_id: str):
        item = review_or_404(review_id)
        form = await request.form()
        payload: dict[str, Any] = {}
        if item.stage == "H1":
            payload["keep"] = form.getlist("keep")
        if item.stage == "H3" and form.get("edit_path"):
            payload["files"] = {str(form["edit_path"]): str(form.get("edit_content", ""))}
        try:
            svc.reviews.submit(review_id, str(form.get("decision", "")), str(form.get("reason_code", "")),
                               notes=str(form.get("notes", "")), minutes_spent=float(form.get("minutes_spent") or 0),
                               payload=payload, reviewer=str(form.get("reviewer") or "owner"))
        except ValueError as e:
            return review(request, review_id, error=str(e))
        return RedirectResponse(f"/reviews?stage={item.stage}", status_code=303)

    @app.get("/records/{record_id:path}", response_class=HTMLResponse)
    def record(request: Request, record_id: str):
        try:
            rec = svc.store.get(record_id)
        except KeyError:
            raise HTTPException(404, f"no record {record_id}") from None
        body = rec.model_dump(mode="json", exclude={"parents", "produced_by"})
        files = None
        if rec.KIND == "task_package":
            files = svc.store.get_json_blob(rec.files_blob)
        return page(request, "record.html", rec=rec, body=body, files=files,
                    ancestors=svc.store.ancestors(record_id), children=svc.store.children(record_id))

    @app.get("/stats", response_class=HTMLResponse)
    def stats(request: Request):
        records = svc.store.all(ReviewRecord)
        reasons: dict[str, dict[str, int]] = {}
        for r in records:
            reasons.setdefault(r.stage, {}).setdefault(r.reason_code, 0)
            reasons[r.stage][r.reason_code] += 1
        return page(request, "stats.html", disagreement=svc.reviews.disagreement(), reasons=reasons, total=len(records))

    # ------------------------------------------------------------------- json
    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/metrics")
    def metrics():
        if platform.db is None:
            return Response("# metrics need the Postgres backend\n", media_type="text/plain")
        from ..core.obs.metrics import render
        return Response(render(platform.db), media_type="text/plain; version=0.0.4")

    @app.get("/api/runs")
    def api_runs():
        return [engine.summary(r["id"]) for r in engine.list_runs()[:100]]

    @app.post("/api/runs")
    def api_start(body: StartRun):
        jobs = {"strong_jobs": body.strong_jobs, "weak_jobs": body.weak_jobs}
        if body.demo_jobs:
            from ..taskgen.demo import write_fake_jobs
            jobs = write_fake_jobs(Path(tempfile.mkdtemp(prefix="taskforge-demo-jobs-")))
        if not jobs["strong_jobs"] or not jobs["weak_jobs"]:
            raise HTTPException(422, "give strong_jobs and weak_jobs, or demo_jobs=true")
        run_id = engine.start_run("ingest", "batch", jobs, paid_approved=body.paid_approved, budget_usd=body.budget_usd)
        return {"run_id": run_id}

    @app.get("/api/runs/{run_id}")
    def api_run(run_id: str):
        run_or_404(run_id)
        steps = engine.all_steps(run_id)
        export = next((s.output for s in steps if s.step == "export" and s.output), None)
        return {**engine.summary(run_id), "export": export, "cost_by_role": svc.gateway.cost_by_role(run_id),
                "failed_steps": [{"id": s.id, "step": s.step, "key": s.key, "error": (s.error or "")[-500:]}
                                 for s in steps if s.status.value == "FAILED"]}

    @app.post("/api/runs/{run_id}/approve")
    def api_approve(run_id: str, budget_usd: float):
        run_or_404(run_id)
        engine.approve(run_id, budget_usd)
        return engine.summary(run_id)

    @app.get("/api/reviews")
    def api_reviews(run_id: str | None = None, stage: str | None = None):
        return [{"review_id": i.review_id, "run_id": i.run_id, "stage": i.stage, "item_id": i.item_id,
                 "auto_decision": i.auto_decision, "allowed": list(i.allowed), "reason_codes": list(i.reason_codes),
                 "material": i.material} for i in svc.reviews.pending(run_id=run_id, stage=stage)]

    @app.post("/api/reviews/{review_id}")
    def api_submit(review_id: str, body: Decision):
        review_or_404(review_id)
        try:
            rec = svc.reviews.submit(review_id, body.decision, body.reason_code, notes=body.notes,
                                     minutes_spent=body.minutes_spent, payload=body.payload, reviewer=body.reviewer)
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        return {"record_id": rec.id}

    return app
