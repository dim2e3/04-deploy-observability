"""Incident responder: Grafana webhook in, headless agent investigation out.

POST /alerts accepts Grafana's webhook payload. Each firing alert becomes an incident folder
under incidents/ with the collected telemetry; a single worker then runs the agent on it.
"""

import json
import logging
import queue
import re
import threading
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from responder import config
from responder.agent import run_agent
from responder.collect import alert_endpoint, collect

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("responder")


class Alert(BaseModel, extra="allow"):
    status: str = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str | None = None
    fingerprint: str | None = None


class WebhookPayload(BaseModel, extra="allow"):
    alerts: list[Alert]


jobs: queue.Queue = queue.Queue()
active_fingerprints: set[str] = set()
lock = threading.Lock()


def now():
    return datetime.now(timezone.utc)


def write_status(incident_dir, **fields):
    path = incident_dir / "status.json"
    status = json.loads(path.read_text()) if path.exists() else {}
    status.update(fields, updated_at=now().isoformat())
    path.write_text(json.dumps(status, indent=2))
    return status


def new_incident(alert):
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", alert.get("labels", {}).get("alertname", "alert"))[:40]
    incident_id = f"{now():%Y%m%d-%H%M%S}-{name}-{uuid4().hex[:6]}"
    incident_dir = config.INCIDENTS_DIR / incident_id
    incident_dir.mkdir(parents=True)
    (incident_dir / "alert.json").write_text(json.dumps(alert, indent=2))
    return incident_id, incident_dir


def investigate(incident_dir, alert, fingerprint):
    try:
        write_status(incident_dir, state="collecting")
        collect(alert, incident_dir)
        write_status(incident_dir, state="investigating")
        log.info("Agent started for %s", incident_dir.name)
        answer = run_agent(incident_dir)
        last_line = answer.strip().splitlines()[-1] if answer.strip() else ""
        write_status(incident_dir, state="done", last_line=last_line)
        log.info("Agent finished for %s: %s", incident_dir.name, last_line)
    except Exception as exc:
        log.exception("Investigation failed for %s", incident_dir.name)
        write_status(incident_dir, state="failed", error=str(exc))
    finally:
        with lock:
            active_fingerprints.discard(fingerprint)


def worker():
    # One investigation at a time, so agents do not trip over each other.
    while True:
        investigate(*jobs.get())
        jobs.task_done()


app = FastAPI(title="Incident Responder")
threading.Thread(target=worker, daemon=True, name="investigator").start()


@app.get("/healthz")
def health():
    return {"status": "ok", "queued": jobs.qsize()}


@app.post("/alerts", status_code=202)
def receive_alerts(payload: WebhookPayload):
    incidents = []
    for alert_model in payload.alerts:
        alert = alert_model.model_dump()
        incident_id, incident_dir = new_incident(alert)
        endpoint = alert_endpoint(alert)
        fingerprint = alert.get("fingerprint") or json.dumps(alert["labels"], sort_keys=True)
        base = dict(alert=alert["labels"].get("alertname"), alert_status=alert["status"],
                    endpoint=endpoint, received_at=now().isoformat())

        if alert["status"] != "firing":
            state = "skipped"
            write_status(incident_dir, state=state, reason="alert is not firing", **base)
        else:
            with lock:
                duplicate = fingerprint in active_fingerprints
                active_fingerprints.add(fingerprint)
            if duplicate:
                state = "skipped"
                write_status(incident_dir, state=state, reason="already being investigated", **base)
            else:
                state = "queued"
                write_status(incident_dir, state=state, **base)
                jobs.put((incident_dir, alert, fingerprint))
        log.info("Alert %s (%s) -> incident %s [%s]", base["alert"], alert["status"], incident_id, state)
        incidents.append({"id": incident_id, "state": state, "endpoint": endpoint})
    return {"incidents": incidents}


@app.get("/incidents")
def list_incidents():
    if not config.INCIDENTS_DIR.exists():
        return []
    result = []
    for incident_dir in sorted(config.INCIDENTS_DIR.iterdir(), reverse=True):
        status_file = incident_dir / "status.json"
        if status_file.exists():
            result.append({"id": incident_dir.name, **json.loads(status_file.read_text())})
    return result


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str):
    incident_dir = config.INCIDENTS_DIR / incident_id
    if not re.fullmatch(r"[A-Za-z0-9_-]+", incident_id) or not incident_dir.is_dir():
        raise HTTPException(404, "Incident not found")
    response = incident_dir / "response.md"
    return {
        "id": incident_id,
        **json.loads((incident_dir / "status.json").read_text()),
        "response": response.read_text() if response.exists() else None,
    }
