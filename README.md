# 04 — Deploy Observability

Homework module: add observability to a running service, alert on failures, and let a coding agent respond to incidents.

## Project: Order Tracker

[Order Tracker](order-tracker/) is the course starter: a small FastAPI + SQLite app for creating orders and checking their status, run with Docker Compose. The app itself is unchanged except for telemetry and the bug fix. Everything below was added on top of it:

- **Telemetry:** OpenTelemetry metrics, logs, and traces for order lookups
- **Pipeline:** OpenTelemetry Collector, Prometheus, Loki, Tempo, and Grafana in Docker Compose
- **Dashboard and alert:** Grafana panels for request counts and errors, plus an alert for 5xx responses
- **Incident responder:** a service that receives the alert and starts Claude Code headless to investigate and fix the problem

```
app ──OTLP──> Collector ──> Prometheus (metrics)
                        ──> Loki       (logs)
                        ──> Tempo      (traces)
                                 │
                              Grafana ── 5xx alert ──webhook──> incident responder ──> claude -p
```

- Code, setup, and configuration: [`order-tracker/README.md`](order-tracker/README.md)
- Incident responder: [`order-tracker/incident-response/README.md`](order-tracker/incident-response/README.md)

## Quick start

```sh
cd order-tracker
docker compose up --build -d --wait     # app :8000, Grafana :3000, Prometheus :9090

cd incident-response
uv run uvicorn responder.main:app --host 127.0.0.1 --port 8001
```

Run the tests with `uv run --frozen pytest -q` in `order-tracker/` (app) and in `order-tracker/incident-response/` (responder).

## What was added, step by step

### 1. Start the app

`docker compose up --build -d --wait`, then `curl http://localhost:8000/healthz` returns `{"status":"ok"}`.

### 2. Instrument one endpoint

`app/telemetry.py` sets up OpenTelemetry, and `GET /api/orders/{order_id}` records the following for each lookup:

- a span;
- a request counter (`order_lookup_requests_total`) and a duration histogram, both labelled with `http_route` and `http_response_status_code`;
- a log line.

At first the signals were printed to the console and read with `docker compose logs app`.

### 3. Telemetry pipeline

The app sends OTLP to the Collector, which forwards metrics to Prometheus, logs to Loki, and traces to Tempo. Grafana is set up from files: data sources linked to each other (a log opens its trace, and a trace shows its logs and metrics) and the **Order Tracker** dashboard. All configuration is in `order-tracker/observability/`.

### 4. Alert on 5xx

The Grafana rule **Order lookup 5xx errors** counts 5xx responses per endpoint over 5 minutes and fires when the count is above 0. The alert includes the endpoint, the time window, and a dashboard link. When there are no 5xx responses at all, the rule counts that as Normal, not No data.

### 5. Incident responder

`order-tracker/incident-response/` accepts Grafana webhooks at `POST /alerts` on port 8001. For each firing alert it saves the alert, the endpoint, metrics, logs with stack traces, and failing traces to `incidents/<id>/`. It then runs `claude -p` in the repository. Test alerts (`test="true"`) are acknowledged without an investigation.

### 6. Agent fixes the incident

Grafana's webhook contact point posts alerts to the responder. A lookup of `express-1002` returned 500, and the following happened in under two minutes:

1. the alert fired;
2. Grafana sent the webhook;
3. the agent traced the 500 to the express delivery estimate;
4. the agent fixed it, added regression tests, and ended with `STATUS: FIXED`.

After a rebuild, the same request returns 200.

## Homework answers

| # | Question | Answer |
| --- | --- | --- |
| 1 | What does the health check return? | `{"status":"ok"}` |
| 2 | Status code the metric records for `standard-1001` | `200` |
| 3 | Status code the metric shows for `standard-1002` | `404` (the order does not exist) |
| 4 | Alert state after the `standard-1002` lookup | `Normal` (a 404 is not a server error) |
| 5 | Agent's reply to the test alert (last line) | `STATUS: TEST_ACKNOWLEDGED` |
| 6 | What was the problem? | The express delivery date calculation tried to use a day that does not exist in that month |

## The incident

`order_detail()` computed the express delivery estimate with `placed_at.replace(day=placed_at.day + 2)`. The seeded order `express-1002` is dated the last day of the previous month, so this asked for, say, September 32. That raised `ValueError: day is out of range for month` and returned a 500. The fix, applied by the agent:

```diff
-        estimated_at = placed_at.replace(day=placed_at.day + 2)
+        estimated_at = placed_at + timedelta(days=2)
```

## Notes

- **Ports:** all services listen on 127.0.0.1. Ports 8000 and 8001 were used by containers from other projects and had to be freed first; Compose ports can be overridden with `ORDER_TRACKER_PORT`, `GRAFANA_PORT`, and `PROMETHEUS_PORT`.
- **Where the responder runs:** on the host, not in Docker, so it can use the logged-in `claude` CLI. With Docker Desktop, Grafana reaches it through `host.docker.internal` even though it is bound to 127.0.0.1.
- **What the agent may do:** read code, query telemetry, edit files, and run tests. It cannot commit, restart containers, or deploy; a human reviews the diff and rebuilds.
- **Grafana access:** anonymous admin with no login. That's fine for a local-only stack, but don't expose it as is.
