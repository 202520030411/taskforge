"""Prometheus metrics read from Postgres on every scrape (DESIGN.md section 11).

Because every worker writes its state to the database, one exporter (in the api process) can
report for all of them; no worker needs its own metrics port.
"""
from __future__ import annotations

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily


class DbCollector:
    def __init__(self, db):
        self.db = db

    def collect(self):
        with self.db.tx() as c:
            q = lambda sql, *a: c.execute(sql, a).fetchall()   # noqa: E731

            g = GaugeMetricFamily("taskforge_steps", "Step runs by resource class and status", labels=["resource", "status"])
            for r in q("SELECT resource, status, count(*) AS n FROM step_runs GROUP BY 1, 2"):
                g.add_metric([r["resource"], r["status"]], r["n"])
            yield g

            g = GaugeMetricFamily("taskforge_queue_oldest_seconds", "Age of the oldest step waiting to be picked up",
                                  labels=["resource"])
            for r in q("SELECT s.resource, EXTRACT(EPOCH FROM now() - min(s.available_at)) AS age FROM step_runs s "
                       "JOIN runs r ON r.id = s.run_id WHERE s.status = 'PENDING' AND s.available_at <= now() "
                       "AND r.status = 'ACTIVE' AND (r.paid_approved OR s.resource NOT IN ('llm', 'agent')) GROUP BY 1"):
                g.add_metric([r["resource"]], float(r["age"]))
            yield g

            m = CounterMetricFamily("taskforge_step_transitions", "State changes of step runs", labels=["to_status"])
            for r in q("SELECT to_status, count(*) AS n FROM events GROUP BY 1"):
                m.add_metric([r["to_status"]], r["n"])
            yield m

            row = q("SELECT count(*) FILTER (WHERE detail LIKE 'lease expired%%') AS expired, "
                    "count(*) FILTER (WHERE to_status = 'FENCED') AS fenced, "
                    "count(*) FILTER (WHERE detail LIKE 'retry in%%') AS retries FROM events")[0]
            for name, help_, key in (("taskforge_lease_expired", "Steps taken over after their worker stopped renewing", "expired"),
                                     ("taskforge_fenced_commits", "Commits refused because the worker had lost its lease", "fenced"),
                                     ("taskforge_step_retries", "Retries after an infrastructure fault", "retries")):
                m = CounterMetricFamily(name, help_)
                m.add_metric([], row[key])
                yield m

            g = GaugeMetricFamily("taskforge_step_duration_seconds", "Step run time over the last 15 minutes",
                                  labels=["step", "quantile"])
            for r in q("SELECT step, percentile_cont(0.5) WITHIN GROUP (ORDER BY d) AS p50, "
                       "percentile_cont(0.95) WITHIN GROUP (ORDER BY d) AS p95 FROM ("
                       "SELECT step, EXTRACT(EPOCH FROM finished_at - started_at) AS d FROM step_runs "
                       "WHERE status = 'SUCCEEDED' AND finished_at > now() - interval '15 minutes') t GROUP BY 1"):
                g.add_metric([r["step"], "0.5"], float(r["p50"]))
                g.add_metric([r["step"], "0.95"], float(r["p95"]))
            yield g

            g = GaugeMetricFamily("taskforge_runs", "Runs by status", labels=["status"])
            for r in q("SELECT status, count(*) AS n FROM runs GROUP BY 1"):
                g.add_metric([r["status"]], r["n"])
            yield g

            money = q("SELECT COALESCE(sum(spent_usd), 0) AS spent, COALESCE(sum(reserved_usd), 0) AS reserved, "
                      "COALESCE(sum(budget_usd) FILTER (WHERE paid_approved), 0) AS budget FROM runs")[0]
            m = CounterMetricFamily("taskforge_spent_usd", "Dollars spent on model calls")
            m.add_metric([], float(money["spent"]))
            yield m
            for name, key in (("taskforge_reserved_usd", "reserved"), ("taskforge_budget_usd", "budget")):
                g = GaugeMetricFamily(name, f"Dollars {key} across runs")
                g.add_metric([], float(money[key]))
                yield g

            calls = CounterMetricFamily("taskforge_llm_calls", "Model calls", labels=["role", "cached"])
            cost = CounterMetricFamily("taskforge_llm_cost_usd", "Dollars by role", labels=["role"])
            toks = CounterMetricFamily("taskforge_llm_tokens", "Tokens of paid calls", labels=["role", "direction"])
            for r in q("SELECT role, cached, count(*) AS n, sum(cost_usd) AS cost, sum(input_tokens) AS i, "
                       "sum(output_tokens) AS o FROM llm_calls GROUP BY 1, 2"):
                calls.add_metric([r["role"], str(r["cached"]).lower()], r["n"])
                if not r["cached"]:
                    cost.add_metric([r["role"]], float(r["cost"]))
                    toks.add_metric([r["role"], "input"], r["i"])
                    toks.add_metric([r["role"], "output"], r["o"])
            yield calls
            yield cost
            yield toks

            g = GaugeMetricFamily("taskforge_double_charges", "Requests paid for more than once (must stay 0)")
            g.add_metric([], q("SELECT count(*) AS n FROM (SELECT idem_key FROM llm_calls WHERE NOT cached "
                               "GROUP BY 1 HAVING count(*) > 1) t")[0]["n"])
            yield g

            g = GaugeMetricFamily("taskforge_reviews_pending", "Items waiting for a person", labels=["stage"])
            d = CounterMetricFamily("taskforge_reviews_decided", "Items a person decided", labels=["stage"])
            for r in q("SELECT stage, count(*) FILTER (WHERE record_id IS NULL) AS pending, "
                       "count(*) FILTER (WHERE record_id IS NOT NULL) AS decided FROM reviews GROUP BY 1"):
                g.add_metric([r["stage"]], r["pending"])
                d.add_metric([r["stage"]], r["decided"])
            yield g
            yield d

            g = GaugeMetricFamily("taskforge_task_decisions", "Final decisions on tasks", labels=["decision"])
            for r in q("SELECT body->>'decision' AS decision, count(*) AS n FROM records WHERE kind = 'task_decision' GROUP BY 1"):
                g.add_metric([r["decision"]], r["n"])
            yield g

            g = GaugeMetricFamily("taskforge_gate_results", "Executed gate results", labels=["gate", "passed"])
            for r in q("SELECT body->>'gate' AS gate, body->>'passed' AS passed, count(*) AS n FROM records "
                       "WHERE kind = 'gate_result' GROUP BY 1, 2"):
                g.add_metric([r["gate"], r["passed"]], r["n"])
            yield g


def render(db) -> bytes:
    registry = CollectorRegistry()
    registry.register(DbCollector(db))
    return generate_latest(registry)
