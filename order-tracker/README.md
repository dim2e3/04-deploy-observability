# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It has a web page, an API, tests, and a Docker Compose setup, plus the Homework 4 additions:

- OpenTelemetry metrics, logs, and traces for order lookups
- A telemetry pipeline: OpenTelemetry Collector, Prometheus, Loki, Tempo, and Grafana
- A Grafana dashboard and an alert for 5xx responses
- An incident responder that receives the alert and starts a coding agent to investigate and fix the problem

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

```
app ──OTLP──> Collector ──> Prometheus (metrics)
                        ──> Loki       (logs)
                        ──> Tempo      (traces)
                                 │
                              Grafana ── 5xx alert ──webhook──> incident responder ──> claude -p
```

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
docker compose up --build -d --wait
```

This starts the app and the observability stack. Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Data is stored in Docker volumes and survives container recreation.

| Service | URL | Purpose |
| --- | --- | --- |
| Order Tracker | <http://127.0.0.1:8000> | App and API |
| Grafana | <http://127.0.0.1:3000> | Dashboard, alerts, Explore (no login) |
| Prometheus | <http://127.0.0.1:9090> | Metrics |

All ports are bound to 127.0.0.1. If one is occupied, override it with `ORDER_TRACKER_PORT`, `GRAFANA_PORT`, or `PROMETHEUS_PORT`, for example:

```bash
ORDER_TRACKER_PORT=18080 docker compose up --build -d --wait
```

Run tests with `uv run --frozen pytest -q`. Telemetry export is turned off in tests (`tests/conftest.py` sets `OTEL_SDK_DISABLED=true`). Stop everything with `docker compose down`. Add `-v` only if you also want to delete the order data and stored telemetry.

## Telemetry

`app/telemetry.py` sets up OpenTelemetry. The order lookup endpoint, `GET /api/orders/{order_id}`, records:

| Signal | What | Details |
| --- | --- | --- |
| Metric | `order_lookup_requests_total` | Counter labelled `http_route` and `http_response_status_code` |
| Metric | `order_lookup_duration_seconds` | Histogram with the same labels |
| Trace | `GET /api/orders/{order_id}` span | `order.id`, route, and status code; unexpected exceptions mark the span as an error. A 404 does not. |
| Log | `Order lookup <id> returned <code>` | Unexpected exceptions are logged with their stack trace; every log carries `trace_id` and `span_id` |

In Compose, `OTEL_EXPORTER_OTLP_ENDPOINT` points the app at the Collector. Without it, for example when running `uvicorn` locally, signals are printed to the console instead. Metrics are exported every 10 seconds (`OTEL_METRIC_EXPORT_INTERVAL`).

## Observability stack

Configuration lives in `observability/`:

| File | Purpose |
| --- | --- |
| `otel-collector.yaml` | Receives OTLP from the app; sends metrics to Prometheus, logs to Loki, traces to Tempo |
| `prometheus.yaml` | OTLP metrics receiver; `service.name` becomes the `service_name` label |
| `loki.yaml` | Single-node Loki; OTLP log attributes such as `trace_id` are kept as structured metadata |
| `tempo.yaml` | Single-node Tempo with local storage |
| `grafana/provisioning/datasources/` | Prometheus, Loki, and Tempo, linked so a log opens its trace and a trace shows its logs and metrics |
| `grafana/dashboards/order-tracker.json` | The **Order Tracker** dashboard (Grafana's home page) |
| `grafana/provisioning/alerting/` | The 5xx alert rule, the webhook contact point, and the notification policy |

Prometheus runs with `created-timestamp-zero-ingestion`, so the first 5xx after a restart is counted by `increase()` and the alert does not miss it.

The dashboard shows lookup counts, lookups by status code, 4xx and 5xx counts and rates, p50/p95 latency, recent traces, and app logs. The top count panels show totals since the app last restarted.

## Alerting

The **Order lookup 5xx errors** rule (folder *Order Tracker*) runs every 30 seconds:

- It counts 5xx lookup responses per endpoint over the last 5 minutes and fires when the count is above 0, with no pending period.
- No 5xx data, for example right after a restart, counts as Normal, not No data.
- The alert carries the endpoint, the 5-minute window, a description, and a link to the dashboard's Error rate panel. It is labelled `severity=critical`.

Every alert goes to the `incident-responder` webhook contact point at `http://host.docker.internal:8001/alerts`. Alerts are grouped by alert name and endpoint and sent after 10 seconds. A still-firing alert is re-sent only after 4 hours, so one incident starts one investigation.

## Incident responder

`incident-response/` is a separate small service. It runs on the host, not in Docker, because it starts the `claude` CLI against this repository. Start it after the stack:

```bash
cd incident-response
uv run uvicorn responder.main:app --host 127.0.0.1 --port 8001
```

On a firing alert it:

1. saves the alert, plus metrics, logs with stack traces, and failing traces from the 15 minutes before it, to `incident-response/incidents/<id>/`;
2. runs `claude -p` headless in this repository;
3. lets the agent fix small, clear code bugs and add a regression test. The agent never commits, restarts containers, or deploys.

Check progress with `curl localhost:8001/incidents`. See [incident-response/README.md](incident-response/README.md) for details and configuration.

With Docker Desktop, Grafana reaches a responder bound to 127.0.0.1. With Docker Engine on Linux, bind the responder to the Docker bridge address instead.

## End-to-end check

```bash
curl -i http://localhost:8000/api/orders/express-1002   # 500 before the fix
```

Within about a minute, the alert fires, Grafana posts to the responder, and the agent investigates. It writes its answer to `incidents/<id>/response.md`, ending with `STATUS: FIXED`, `ROOT_CAUSE_FOUND`, `ESCALATE`, or `TEST_ACKNOWLEDGED`. Review the diff, then rebuild with `docker compose up --build -d --wait app` and repeat the request.

The seeded incident was the express delivery estimate. It used `placed_at.replace(day=placed_at.day + 2)`, which fails for orders placed in the last two days of a month: `express-1002` is dated the last day of the previous month. It now uses `placed_at + timedelta(days=2)`, covered by regression tests in `tests/test_api.py`.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order (instrumented) |
| PATCH | `/api/orders/{id}` | Change an order status |

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.
