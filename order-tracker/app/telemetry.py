import logging
import os

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    ConsoleLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor


SERVICE_NAME = "order-tracker"
METRIC_EXPORT_INTERVAL_MS = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "10000"))


def setup():
    """Send traces, metrics and logs to the OpenTelemetry Collector.

    The OTLP exporters read OTEL_EXPORTER_OTLP_ENDPOINT. Without it, signals go to
    the console instead (visible in `docker compose logs app`).
    """
    if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
        return
    resource = Resource.create({"service.name": SERVICE_NAME})
    use_otlp = bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter())
        if use_otlp
        else SimpleSpanProcessor(ConsoleSpanExporter())
    )
    trace.set_tracer_provider(tracer_provider)

    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter() if use_otlp else ConsoleMetricExporter(),
        export_interval_millis=METRIC_EXPORT_INTERVAL_MS,
    )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter())
        if use_otlp
        else SimpleLogRecordProcessor(ConsoleLogRecordExporter())
    )
    set_logger_provider(logger_provider)
    app_logger = logging.getLogger("app")
    app_logger.setLevel(logging.INFO)
    app_logger.addHandler(LoggingHandler(logger_provider=logger_provider))


setup()

tracer = trace.get_tracer("app.orders")
meter = metrics.get_meter("app.orders")
logger = logging.getLogger("app.orders")

lookup_requests = meter.create_counter(
    "order_lookup.requests",
    unit="{request}",
    description="Order lookup requests by route and HTTP status code",
)
lookup_duration = meter.create_histogram(
    "order_lookup.duration",
    unit="s",
    description="Order lookup duration by route and HTTP status code",
)
