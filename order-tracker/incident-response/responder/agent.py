"""Run the coding agent (Claude Code) headless against an incident folder."""

import json
import os
import subprocess

from responder import config

PROMPT = """\
You are the on-call engineer for the Order Tracker service. A Grafana alert just reached
the incident responder. The repository is the current working directory.

Everything collected for this alert is in {incident_dir}:
- context.md: alert details, affected endpoint, request counts, error logs, failing traces
- alert.json, metrics.json, logs.json, traces.json: the raw data behind context.md

Steps:
1. Read context.md first.
2. If the alert is a test (label test="true") or there is no incident to investigate, say so in
   one or two sentences and stop. Do not investigate further.
3. Otherwise find the root cause. Use the logs, traces and stack traces, then read the code.
   For more telemetry, query Grafana's datasource proxy with curl at
   {grafana}/api/datasources/proxy/uid/<prometheus|loki|tempo>/... (no auth needed).
4. If the cause is a bug in this repository's code and the fix is small and clear:
   - fix it with the smallest change that addresses the cause,
   - add a regression test in tests/ that fails without the fix,
   - run `uv run --frozen pytest -q` and make sure everything passes.
   Do not commit, restart containers or deploy; a human reviews and deploys the fix.
   If the fix is risky, unclear, or outside the code, do not edit anything and escalate.

Answer in Markdown with: Summary, Evidence, Root cause, Fix (the diff you applied, or the one you
suggest), Verification, and what a human needs to do next. Skip sections that do not apply to a
test alert. End with exactly one final line, one of:
STATUS: TEST_ACKNOWLEDGED
STATUS: FIXED
STATUS: ROOT_CAUSE_FOUND
STATUS: ESCALATE
"""


def agent_env():
    """Drop variables that make Claude Code think it is nested inside another session."""
    return {k: v for k, v in os.environ.items()
            if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")}


def build_prompt(incident_dir):
    return PROMPT.format(incident_dir=incident_dir, grafana=config.GRAFANA_URL)


def build_command(incident_dir):
    # The prompt goes on stdin; --add-dir and --allowedTools take several values.
    command = [
        config.CLAUDE_BIN, "-p",
        "--output-format", "json",
        "--add-dir", str(incident_dir),
        "--allowedTools", config.ALLOWED_TOOLS,
    ]
    if config.CLAUDE_MODEL:
        command += ["--model", config.CLAUDE_MODEL]
    return command


def run_agent(incident_dir):
    """Run the agent and save its answer. Returns the answer text."""
    completed = subprocess.run(
        build_command(incident_dir),
        cwd=config.REPO_DIR,
        env=agent_env(),
        input=build_prompt(incident_dir),
        capture_output=True,
        text=True,
        timeout=config.AGENT_TIMEOUT_SECONDS,
    )
    (incident_dir / "agent-output.json").write_text(completed.stdout)
    if completed.stderr:
        (incident_dir / "agent-stderr.log").write_text(completed.stderr)
    try:
        output = json.loads(completed.stdout)
    except json.JSONDecodeError:
        output = {"is_error": True, "result": completed.stdout or completed.stderr}
    answer = output.get("result") or ""
    (incident_dir / "response.md").write_text(answer + "\n")
    if completed.returncode != 0 or output.get("is_error"):
        raise RuntimeError(f"agent exited with code {completed.returncode}: {answer[:500]}")
    return answer
