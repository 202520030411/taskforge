"""Generate deploy/grafana/dashboards/taskforge.json. Run after changing panels: python deploy/grafana/make_dashboard.py"""
import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "prometheus"}
panels, y = [], 0


def add(title, exprs, *, kind="timeseries", w=12, h=7, unit="short", desc=""):
    global y
    x = sum(p["gridPos"]["w"] for p in panels if p["gridPos"]["y"] == y)
    if x + w > 24:
        y += max(p["gridPos"]["h"] for p in panels if p["gridPos"]["y"] == y)
        x = 0
    targets = [{"datasource": DS, "expr": e, "legendFormat": legend, "refId": chr(65 + i)}
               for i, (e, legend) in enumerate(exprs)]
    panel = {"id": len(panels) + 1, "title": title, "type": kind, "datasource": DS, "description": desc,
             "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets,
             "fieldConfig": {"defaults": {"unit": unit, "min": 0}, "overrides": []}}
    if kind == "stat":
        panel["options"] = {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "none",
                            "textMode": "value_and_name"}
    panels.append(panel)


add("Steps waiting, by resource class", [('sum by (resource) (taskforge_steps{status="PENDING"})', "{{resource}}")],
    desc="Queue length. Paid classes also count steps of runs that are not approved yet.")
add("Steps running, by resource class", [('sum by (resource) (taskforge_steps{status="RUNNING"})', "{{resource}}")])
add("Steps finished per second", [('sum(rate(taskforge_step_transitions_total{to_status="SUCCEEDED"}[1m]))', "succeeded"),
                                   ('sum(rate(taskforge_step_transitions_total{to_status="FAILED"}[1m]))', "failed")], unit="ops")
add("Oldest step waiting to be picked up", [("taskforge_queue_oldest_seconds", "{{resource}}")], unit="s",
    desc="If this keeps growing for one class, that class needs more worker threads or more sandbox capacity.")
add("Leases expired", [("taskforge_lease_expired_total", "taken over")], kind="stat", w=6, h=4,
    desc="Steps whose worker stopped renewing its claim; another worker ran them again.")
add("Commits refused (fenced)", [("taskforge_fenced_commits_total", "fenced")], kind="stat", w=6, h=4,
    desc="A worker tried to write a result after losing its claim. Refusing it is the correct outcome.")
add("Retries after faults", [("taskforge_step_retries_total", "retries")], kind="stat", w=6, h=4)
add("Requests paid twice", [("taskforge_double_charges", "must be 0")], kind="stat", w=6, h=4)
add("Sandboxes", [("taskforge_sandbox_in_use", "in use"), ("taskforge_sandbox_idle", "pre-started, idle"),
                  ("taskforge_sandbox_creating", "starting"), ("taskforge_sandbox_capacity", "capacity")])
add("Sandbox hand-out time (95% under)", [
    ('histogram_quantile(0.95, sum by (le, source) (rate(taskforge_sandbox_start_seconds_bucket[1m])))', "{{source}}")], unit="s",
    desc="pool = handed a pre-started container; cold = had to start one.")
add("Sandbox hand-outs per second", [('sum by (source) (rate(taskforge_sandbox_start_seconds_count[1m]))', "{{source}}"),
                                      ("rate(taskforge_sandbox_busy_total[1m])", "refused: full")], unit="ops")
add("Command time in sandboxes (95% under)", [
    ('histogram_quantile(0.95, sum by (le) (rate(taskforge_sandbox_exec_seconds_bucket[1m])))', "exec")], unit="s")
add("Dollars spent", [("taskforge_spent_usd_total", "spent"), ("taskforge_reserved_usd", "reserved for calls in flight"),
                      ("taskforge_budget_usd", "approved budget")], unit="currencyUSD")
add("Dollars by role", [("taskforge_llm_cost_usd_total", "{{role}}")], unit="currencyUSD")
add("Model calls per second", [('sum by (cached) (rate(taskforge_llm_calls_total[1m]))', "from cache: {{cached}}")], unit="ops")
add("Waiting for a person, by stage", [("taskforge_reviews_pending", "{{stage}}")])
add("Gate results", [('taskforge_gate_results', "{{gate}} passed={{passed}}")], kind="stat", w=12, h=5)
add("Task decisions", [("taskforge_task_decisions", "{{decision}}")], kind="stat", w=12, h=5)
add("Step run time, last 15 min (95% under)", [('taskforge_step_duration_seconds{quantile="0.95"}', "{{step}}")], unit="s", w=24)

dashboard = {"uid": "taskforge", "title": "TaskForge", "timezone": "America/New_York", "schemaVersion": 39, "version": 1,
             "refresh": "5s", "time": {"from": "now-30m", "to": "now"}, "panels": panels, "tags": ["taskforge"]}
out = Path(__file__).parent / "dashboards" / "taskforge.json"
out.write_text(json.dumps(dashboard, indent=1))
print(f"wrote {out} ({len(panels)} panels)")
