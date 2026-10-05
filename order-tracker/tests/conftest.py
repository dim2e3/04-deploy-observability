import os

# Keep console exporters quiet during tests; telemetry calls become no-ops.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
