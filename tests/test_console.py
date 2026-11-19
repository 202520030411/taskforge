"""The console and control API, on both backends (Postgres half skipped without TASKFORGE_PG_DSN)."""
from fastapi.testclient import TestClient

from taskforge.api.app import create_app
from taskforge.runtime import Platform
from taskforge.taskgen.demo import WorldConfig, build_world


def make(tmp_path, db, world=WorldConfig()):
    engine, svc, reviewer, provider, jobs = build_world(tmp_path, world, db=db)
    return engine, svc, jobs, TestClient(create_app(Platform(engine, svc, db)), follow_redirects=False)


def drive(engine, client, run, decide):
    """Run to the end, answering every review through the HTTP API."""
    for _ in range(60):
        engine.run_until_idle(run)
        pending = client.get("/api/reviews", params={"run_id": run}).json()
        if not pending:
            return
        for item in pending:
            decision, reason, payload = decide(item)
            r = client.post(f"/api/reviews/{item['review_id']}", json={"decision": decision, "reason_code": reason,
                                                                       "payload": payload, "minutes_spent": 1.5})
            assert r.status_code == 200, r.text
    raise AssertionError("run did not finish")


DEFAULTS = {"H1": ("approve", "patterns_ok"), "H2": ("keep", "on_target"), "H4": ("keep", "looks_fine"),
            "H5": ("clean", "clean"), "H6": ("export", "ready")}


def test_full_run_through_the_api_and_every_page_renders(tmp_path, db):
    engine, svc, jobs, client = make(tmp_path, db)
    run = client.post("/api/runs", json={"demo_jobs": True}).json()["run_id"]
    engine.run_until_idle(run)
    assert client.get(f"/api/runs/{run}").json()["blocked_paid_steps"] == 2        # nothing paid ran yet
    page = client.get(f"/runs/{run}").text
    assert "Approve paid steps" in page and "paid step(s) are waiting for approval" in page

    assert client.post(f"/runs/{run}/approve", data={"budget_usd": "5"}).status_code == 303
    engine.run_until_idle(run)
    [h1] = client.get("/api/reviews", params={"run_id": run, "stage": "H1"}).json()
    html = client.get(f"/reviews/{h1['review_id']}").text
    assert "no-self-check" in html and 'name="keep"' in html and "patterns ok" in html

    drive(engine, client, run, lambda item: (*DEFAULTS[item["stage"]], {}))
    result = client.get(f"/api/runs/{run}").json()
    assert result["finished"] and len(result["export"]["accepted"]) == 3 and not result["failed_steps"]

    task = result["export"]["accepted"][0]
    record_page = client.get(f"/records/{task}").text
    assert "bad pattern" in record_page and "task proposal" in record_page and "instruction.md" in record_page
    for path in ("/", f"/runs/{run}", "/reviews", "/stats"):
        assert client.get(path).status_code == 200
    assert "Accepted tasks" in client.get(f"/runs/{run}").text
    assert "H1" in client.get("/stats").text
    assert client.get("/metrics").status_code == 200


def test_the_console_cannot_submit_what_the_rules_forbid(tmp_path, db):
    engine, svc, jobs, client = make(tmp_path, db)
    run = engine.start_run("ingest", "batch", jobs, paid_approved=True, budget_usd=5)
    engine.run_until_idle(run)
    [h1] = client.get("/api/reviews", params={"run_id": run}).json()
    rid = h1["review_id"]
    assert client.post(f"/api/reviews/{rid}", json={"decision": "reject_everything", "reason_code": "patterns_ok"}).status_code == 409
    assert client.post(f"/api/reviews/{rid}", json={"decision": "approve", "reason_code": "because"}).status_code == 409
    bad = client.post(f"/reviews/{rid}", data={"decision": "approve", "reason_code": "nope"})
    assert bad.status_code == 200 and "unknown reason code" in bad.text            # the form comes back with the error
    assert client.post(f"/reviews/{rid}", data={"decision": "approve", "reason_code": "patterns_ok",
                                                "keep": h1["material"]["patterns"][0]["id"]}).status_code == 303
    assert client.post(f"/api/reviews/{rid}", json={"decision": "approve", "reason_code": "patterns_ok"}).status_code == 409  # once only
    assert client.get("/api/reviews/none").status_code in (404, 405) and client.get("/runs/run-nope").status_code == 404


def test_deleting_every_pattern_at_h1_ends_the_run_without_building_anything(tmp_path, db):
    engine, svc, jobs, client = make(tmp_path, db)
    run = engine.start_run("ingest", "batch", jobs, paid_approved=True, budget_usd=5)
    engine.run_until_idle(run)
    [h1] = client.get("/api/reviews", params={"run_id": run}).json()
    assert client.post(f"/reviews/{h1['review_id']}", data={"decision": "approve", "reason_code": "too_generic"}).status_code == 303
    engine.run_until_idle(run)
    assert engine.summary(run)["finished"] and not engine.find(run, "propose")


def test_failed_step_can_be_run_again_from_the_console(tmp_path, db):
    engine, svc, jobs, client = make(tmp_path, db)
    run = engine.start_run("ingest", "batch", {"strong_jobs": ["/nonexistent"], "weak_jobs": ["/nonexistent"]})
    engine.run_until_idle(run)
    assert engine.summary(run)["finished"]                                   # no trajectories -> no pairs -> ends
    assert client.post("/steps/sr-nope/requeue").status_code == 404
    done = engine.get(run, "pair", "batch")
    assert client.post(f"/steps/{done.id}/requeue").status_code == 409        # only FAILED steps can be requeued
