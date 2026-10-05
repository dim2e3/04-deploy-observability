import pytest
from fastapi.testclient import TestClient

from responder import agent, collect, config, main

TEST_ALERT = {
    "status": "firing",
    "labels": {"alertname": "ResponderTest", "test": "true"},
    "annotations": {"summary": "Test notification; no incident to fix"},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INCIDENTS_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(main, "collect", lambda alert, d: (d / "context.md").write_text("ctx"))

    def fake_agent(incident_dir):
        calls.append(incident_dir)
        (incident_dir / "response.md").write_text("Test received.\nSTATUS: TEST_ACKNOWLEDGED\n")
        return "Test received.\nSTATUS: TEST_ACKNOWLEDGED"

    monkeypatch.setattr(main, "run_agent", fake_agent)
    with TestClient(main.app) as test_client:
        test_client.calls = calls
        yield test_client


def test_firing_alert_runs_agent(client):
    response = client.post("/alerts", json={"alerts": [TEST_ALERT]})
    assert response.status_code == 202
    incident = response.json()["incidents"][0]
    assert incident["state"] == "queued"
    main.jobs.join()

    detail = client.get(f"/incidents/{incident['id']}").json()
    assert detail["state"] == "done"
    assert detail["last_line"] == "STATUS: TEST_ACKNOWLEDGED"
    assert len(client.calls) == 1


def test_resolved_alert_is_recorded_but_not_investigated(client):
    response = client.post("/alerts", json={"alerts": [{**TEST_ALERT, "status": "resolved"}]})
    assert response.json()["incidents"][0]["state"] == "skipped"
    main.jobs.join()
    assert client.calls == []


def test_endpoint_comes_from_annotation_or_route_label():
    assert collect.alert_endpoint({"annotations": {"endpoint": "GET /x"}}) == "GET /x"
    assert collect.alert_endpoint({"labels": {"http_route": "/api/orders/{order_id}"}}) == "/api/orders/{order_id}"
    assert collect.alert_endpoint(TEST_ALERT) is None


def test_context_includes_exception_and_trace():
    start, end = collect.time_window(TEST_ALERT)
    context = collect.render_context(
        {**TEST_ALERT, "labels": {"alertname": "5xx", "http_route": "/api/orders/{order_id}"}},
        "GET /api/orders/{order_id}", start, end,
        {"window_minutes": 15, "rows": [{"route": "/api/orders/{order_id}", "status_code": "500", "count": 1.0}]},
        [{"time": "t", "level": "ERROR", "message": "Order lookup failed", "trace_id": "abc",
          "exception_type": "ValueError", "exception_message": "day is out of range for month"}],
        {"query": "{}", "traces": [{"trace_id": "abc", "root": "GET /api/orders/{order_id}", "spans": []}]},
        [],
    )
    assert "| /api/orders/{order_id} | 500 | 1.0 |" in context
    assert "ValueError: day is out of range for month" in context
    assert "### abc" in context


def test_agent_command_is_headless_and_not_nested(tmp_path, monkeypatch):
    command = agent.build_command(tmp_path)
    assert command[:2] == [config.CLAUDE_BIN, "-p"]
    assert "--output-format" in command
    tools = command[command.index("--allowedTools") + 1].split(",")
    assert "Bash(uv run --frozen pytest:*)" in tools
    assert not any(t.startswith(("Bash(git commit", "Bash(docker compose up")) for t in tools)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert "CLAUDECODE" not in agent.agent_env()
