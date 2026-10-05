"""Collect the telemetry around an alert: metrics, logs and traces."""

import json
from datetime import datetime, timedelta, timezone

import httpx

from responder import config

LOG_DETAIL_KEYS = ("exception_type", "exception_message", "exception_stacktrace", "trace_id")


def parse_time(value):
    if not value or value.startswith("0001-"):
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def alert_endpoint(alert):
    annotations, labels = alert.get("annotations", {}), alert.get("labels", {})
    if annotations.get("endpoint"):
        return annotations["endpoint"]
    if labels.get("http_route"):
        return labels["http_route"]
    return None


def time_window(alert, now=None):
    now = now or datetime.now(timezone.utc)
    lookback = timedelta(minutes=config.LOOKBACK_MINUTES)
    started = parse_time(alert.get("startsAt"))
    start = min(started - lookback, now - lookback) if started else now - lookback
    return start, now


class Collector:
    def __init__(self, client=None):
        self.client = client or httpx.Client(base_url=config.GRAFANA_URL, timeout=15)
        self.errors = []

    def _get(self, source, path, params):
        try:
            response = self.client.get(f"/api/datasources/proxy/uid/{source}{path}", params=params,
                                       headers={"Accept": "application/json"})
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.errors.append(f"{source} {path}: {exc}")
            return None

    def metrics(self, start, end):
        minutes = max(1, int((end - start).total_seconds() // 60))
        query = (f'sum by (http_route, http_response_status_code) '
                 f'(increase(order_lookup_requests_total{{service_name="{config.SERVICE_NAME}"}}[{minutes}m]))')
        data = self._get("prometheus", "/api/v1/query", {"query": query, "time": end.timestamp()})
        rows = []
        for item in (data or {}).get("data", {}).get("result", []):
            rows.append({
                "route": item["metric"].get("http_route"),
                "status_code": item["metric"].get("http_response_status_code"),
                "count": round(float(item["value"][1]), 1),
            })
        return {"query": query, "window_minutes": minutes, "rows": rows}

    def logs(self, start, end, limit=200):
        data = self._get("loki", "/loki/api/v1/query_range", {
            "query": f'{{service_name="{config.SERVICE_NAME}"}}',
            "start": int(start.timestamp() * 1e9),
            "end": int(end.timestamp() * 1e9),
            "limit": limit,
            "direction": "backward",
        })
        entries = []
        for stream in (data or {}).get("data", {}).get("result", []):
            labels = stream.get("stream", {})
            for ts, line in stream.get("values", []):
                entries.append({
                    "time": datetime.fromtimestamp(int(ts) / 1e9, timezone.utc).isoformat(),
                    "level": labels.get("severity_text") or labels.get("detected_level"),
                    "message": line,
                    **{k: labels[k] for k in LOG_DETAIL_KEYS if k in labels},
                })
        entries.sort(key=lambda e: e["time"], reverse=True)
        return entries

    def traces(self, start, end, route=None, limit=5):
        conditions = [f'resource.service.name="{config.SERVICE_NAME}"', "(status=error || span.http.response.status_code>=500)"]
        if route:
            conditions.append(f'span.http.route="{route}"')
        query = "{" + " && ".join(conditions) + "}"
        found = self._get("tempo", "/api/search", {
            "q": query, "start": int(start.timestamp()), "end": int(end.timestamp()), "limit": limit,
        })
        traces = []
        for item in (found or {}).get("traces", [])[:limit]:
            detail = self._get("tempo", f"/api/traces/{item['traceID']}", {})
            traces.append({"trace_id": item["traceID"], "root": item.get("rootTraceName"),
                           "spans": summarize_spans(detail or {})})
        return {"query": query, "traces": traces}


def _attr_value(value):
    return next(iter(value.values()), None) if isinstance(value, dict) else value


def summarize_spans(trace):
    spans = []
    for batch in trace.get("batches") or trace.get("resourceSpans") or []:
        for scope in batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []:
            for span in scope.get("spans", []):
                start, end = int(span.get("startTimeUnixNano", 0)), int(span.get("endTimeUnixNano", 0))
                spans.append({
                    "name": span.get("name"),
                    "status": span.get("status", {}),
                    "duration_ms": round((end - start) / 1e6, 2),
                    "attributes": {a["key"]: _attr_value(a.get("value")) for a in span.get("attributes", [])},
                    "events": [
                        {"name": e.get("name"),
                         **{a["key"]: _attr_value(a.get("value")) for a in e.get("attributes", [])}}
                        for e in span.get("events", [])
                    ],
                })
    return spans


def collect(alert, incident_dir, collector=None):
    """Write alert.json, metrics.json, logs.json, traces.json and context.md to incident_dir."""
    collector = collector or Collector()
    start, end = time_window(alert)
    endpoint = alert_endpoint(alert)
    route = alert.get("labels", {}).get("http_route")

    metrics = collector.metrics(start, end)
    logs = collector.logs(start, end)
    traces = collector.traces(start, end, route)

    for name, data in (("alert", alert), ("metrics", metrics), ("logs", logs), ("traces", traces)):
        (incident_dir / f"{name}.json").write_text(json.dumps(data, indent=2, default=str))
    context = render_context(alert, endpoint, start, end, metrics, logs, traces, collector.errors)
    (incident_dir / "context.md").write_text(context)
    return context


def render_context(alert, endpoint, start, end, metrics, logs, traces, errors):
    labels, annotations = alert.get("labels", {}), alert.get("annotations", {})
    out = [
        f"# Alert: {labels.get('alertname', 'unknown')}",
        "",
        f"- Status: {alert.get('status', 'unknown')}",
        f"- Started: {alert.get('startsAt') or 'unknown'}",
        f"- Endpoint: {endpoint or 'not specified'}",
        f"- Window collected: {start.isoformat()} to {end.isoformat()}",
        f"- Summary: {annotations.get('summary', '')}",
        f"- Description: {annotations.get('description', '')}",
        f"- Dashboard: {annotations.get('dashboard_url') or alert.get('dashboardURL') or alert.get('panelURL') or ''}",
        f"- Labels: {json.dumps(labels)}",
        "",
        f"## Lookup requests in the last {metrics['window_minutes']} minutes",
        "",
    ]
    if metrics["rows"]:
        out += ["| Route | Status | Count |", "| --- | --- | --- |"]
        out += [f"| {r['route']} | {r['status_code']} | {r['count']} |" for r in metrics["rows"]]
    else:
        out.append("No lookup requests recorded.")

    errors_logged = [e for e in logs if (e.get("level") or "").upper() in {"ERROR", "CRITICAL", "FATAL"}]
    out += ["", f"## Error logs ({len(errors_logged)})", ""]
    for entry in errors_logged[:10]:
        out.append(f"- {entry['time']} {entry['message']} (trace_id={entry.get('trace_id', '-')})")
        if entry.get("exception_type"):
            out.append(f"  - {entry['exception_type']}: {entry.get('exception_message', '')}")
        if entry.get("exception_stacktrace"):
            out += ["", "```", entry["exception_stacktrace"].strip(), "```", ""]
    if not errors_logged:
        out.append("None.")

    out += ["", "## Recent logs", ""]
    out += [f"- {e['time']} [{e.get('level') or '-'}] {e['message']}" for e in logs[:20]] or ["None."]

    out += ["", f"## Failing traces ({len(traces['traces'])})", "", f"TraceQL: `{traces['query']}`", ""]
    for trace in traces["traces"]:
        out.append(f"### {trace['trace_id']} {trace['root'] or ''}")
        for span in trace["spans"]:
            out.append(f"- span `{span['name']}` {span['duration_ms']} ms, status={span['status']}, "
                       f"attributes={json.dumps(span['attributes'])}")
            for event in span["events"]:
                out.append(f"  - event {event.get('name')}: {event.get('exception.type', '')} "
                           f"{event.get('exception.message', '')}")
    if not traces["traces"]:
        out.append("None.")

    if errors:
        out += ["", "## Collection errors", ""] + [f"- {e}" for e in errors]
    return "\n".join(out) + "\n"
