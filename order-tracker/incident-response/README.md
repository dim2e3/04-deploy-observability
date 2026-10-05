# Incident Responder

Receives Grafana alert webhooks, collects the telemetry around each alert, and starts Claude Code in headless mode to investigate.

## Run it

It runs on the host, not in Docker, so it can start the `claude` CLI against this repository. You need `uv`, a logged-in `claude`, and the Order Tracker stack running (it reads telemetry through Grafana).

```bash
cd incident-response
uv run uvicorn responder.main:app --host 127.0.0.1 --port 8001
```

Grafana's `incident-responder` contact point posts alerts to `http://host.docker.internal:8001/alerts`. With Docker Desktop that reaches a responder bound to 127.0.0.1; with Docker Engine on Linux, bind it to the Docker bridge address instead (for example `--host 172.17.0.1`).

Send a test alert:

```bash
curl -X POST http://localhost:8001/alerts \
  -H 'Content-Type: application/json' \
  -d '{"alerts":[{"status":"firing","labels":{"alertname":"ResponderTest","test":"true"},"annotations":{"summary":"Test notification; no incident to fix"}}]}'
```

Then follow progress with `curl localhost:8001/incidents`, and read the agent's answer with `curl localhost:8001/incidents/<id>` or in `incidents/<id>/response.md`.

## What happens on an alert

1. `POST /alerts` accepts Grafana's webhook payload and answers `202` right away. Each firing alert gets a folder in `incidents/`. Resolved alerts are recorded but not investigated, and an alert that is already being investigated is not started twice.
2. The responder collects, through Grafana's datasource proxy, the data from the alert's window (15 minutes before it started, by default):
   - `alert.json`: the alert as received
   - `metrics.json`: lookup counts by route and status code (Prometheus)
   - `logs.json`: app logs with exception details and trace ids (Loki)
   - `traces.json`: failing traces for the affected endpoint (Tempo)
   - `context.md`: a readable summary of all of the above
3. A single worker runs `claude -p` in the repository root with the incident folder attached. It runs one investigation at a time. The agent can read code, query telemetry with `curl`, read `docker compose` logs, edit files in the repository, and run the tests. When the cause is a small, clear code bug, it applies the fix and adds a regression test; it never commits, restarts containers, or deploys, so a human reviews the diff and rebuilds the app. It ends with `STATUS: TEST_ACKNOWLEDGED`, `FIXED`, `ROOT_CAUSE_FOUND`, or `ESCALATE`.
4. The answer is saved to `response.md`; `agent-output.json` holds the full CLI output and `status.json` the progress.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `GRAFANA_URL` | `http://localhost:3000` | Where to read telemetry |
| `RESPONDER_REPO_DIR` | parent folder | Repository the agent works in |
| `RESPONDER_LOOKBACK_MINUTES` | `15` | Telemetry window before the alert |
| `RESPONDER_MODEL` | CLI default | Model for the agent |
| `RESPONDER_AGENT_TIMEOUT` | `900` | Seconds before the agent is stopped |
| `RESPONDER_ALLOWED_TOOLS` | see `responder/config.py` | Comma-separated tools the agent may use without asking |

Run the tests with `uv run --frozen pytest -q`.
