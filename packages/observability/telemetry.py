from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from config.settings import Settings


@dataclass(frozen=True, slots=True)
class TelemetryState:
    service_name: str
    otlp_export_enabled: bool


_configured = False


def configure_telemetry(app: FastAPI, settings: Settings) -> TelemetryState:
    """Install OpenTelemetry tracing and metrics without emitting credentials or payloads."""
    global _configured
    if _configured:
        FastAPIInstrumentor.instrument_app(app)
        return TelemetryState(settings.service_name, bool(settings.otlp_endpoint))

    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": app.version,
            "deployment.environment.name": settings.environment,
        }
    )
    tracer_provider = TracerProvider(resource=resource)
    metric_readers = []
    if settings.otlp_endpoint:
        endpoint = settings.otlp_endpoint.rstrip("/")
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")))
        metric_readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"),
                export_interval_millis=15_000,
            )
        )
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=metric_readers))
    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls="health/live,health/ready",
        http_capture_headers_server_request=["x-correlation-id"],
        http_capture_headers_server_response=["x-correlation-id"],
    )
    _configured = True
    return TelemetryState(settings.service_name, bool(settings.otlp_endpoint))
