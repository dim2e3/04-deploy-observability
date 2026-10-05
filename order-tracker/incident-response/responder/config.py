import os
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

# The repository the agent investigates (the Order Tracker app).
REPO_DIR = Path(os.getenv("RESPONDER_REPO_DIR", HERE.parent)).resolve()
INCIDENTS_DIR = Path(os.getenv("RESPONDER_INCIDENTS_DIR", HERE / "incidents")).resolve()

# Telemetry is read through Grafana's datasource proxy, so only Grafana needs to be reachable.
GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:3000").rstrip("/")
SERVICE_NAME = os.getenv("RESPONDER_SERVICE_NAME", "order-tracker")
LOOKBACK_MINUTES = int(os.getenv("RESPONDER_LOOKBACK_MINUTES", "15"))

CLAUDE_BIN = os.getenv("RESPONDER_CLAUDE_BIN", "claude")
CLAUDE_MODEL = os.getenv("RESPONDER_MODEL", "")
AGENT_TIMEOUT_SECONDS = int(os.getenv("RESPONDER_AGENT_TIMEOUT", "900"))
# Comma-separated. Read code, query telemetry, edit files in the repo, run tests. No commits or deploys.
ALLOWED_TOOLS = os.getenv(
    "RESPONDER_ALLOWED_TOOLS",
    ",".join([
        "Read", "Grep", "Glob", "Edit", "Write",
        "Bash(curl:*)",
        "Bash(docker compose logs:*)", "Bash(docker compose ps:*)",
        "Bash(git log:*)", "Bash(git diff:*)", "Bash(git status:*)",
        "Bash(uv run --frozen pytest:*)",
    ]),
)
