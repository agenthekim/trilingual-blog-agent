"""Structured logging, intent/outcome capture, and tracing helpers.

Logging
    One logger, ``blog_agent``, writes single-line JSON through
    ``google.cloud.logging``'s ``StructuredLogHandler``. Every record passes a
    redaction filter first, so credentials and personal data are scrubbed
    before they reach any log sink. The handler adds the active OpenTelemetry
    trace and span IDs, which lets a log line be opened next to its trace.
    Every module of ``app/`` logs through ``log_event``; none has a logger of
    its own. The one exception is the terminal line in ``app/web_ui.py`` that
    shows the address with the access token once (SEC-7): it must not be in
    the structured log.

Activity
    The lines a person reads on the page while a run works are also written
    here, as records with ``event_phase`` ``ACTIVITY`` and the run id
    (``log_activity``). Nothing in ``app/`` writes a log with ``print``.

Intent versus outcome
    ``ObservabilityPlugin`` is registered on the ``App`` and therefore sees
    every tool call made by every agent. Before a tool runs it logs the
    *intent* (which tool, with which arguments); afterwards it logs the
    *outcome* (status, error code, latency). The two records share the
    function-call ID so they can be joined.

Tracing
    One action a person starts is one trace (OPS-1). ``setup_tracing`` gives
    the process a tracer provider, so that spans get ids at all; the page and
    the script call it when they start. ``root_span`` opens the span of an
    action, ADK opens spans for agent runs, model calls and tool calls under
    it, and ``traced_span`` adds a child span around every outbound call, so a
    slow action can be attributed to the service that was slow. Spans leave
    the process only when ``BLOG_AGENT_TRACE_EXPORT=cloud``; the agent API
    server has its own switch in the serving layer.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.cloud.logging_v2.handlers import StructuredLogHandler
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.redaction import IDENTIFIER_FIELDS, redact_text, redact_value

LOGGER_NAME = "blog_agent"
SERVICE = "trilingual-blog-agent"
TRACE_EXPORT_ENV = "BLOG_AGENT_TRACE_EXPORT"
_MAX_IDENTIFIER_CHARS = 200
_tracer = trace.get_tracer(LOGGER_NAME)


def _is_identifier(key: str, value: Any) -> bool:
    """Whether a field of a record is an identifier, to be written as it is.

    Only a field the code itself names as an identifier, and only a value
    short enough to be one: anything longer is treated as text.
    """
    return (
        key in IDENTIFIER_FIELDS
        and isinstance(value, str)
        and len(value) <= _MAX_IDENTIFIER_CHARS
    )


class _RedactionFilter(logging.Filter):
    """Scrub the message and structured fields of every record before it is emitted.

    Identifier fields are left as they are (SEC-4): a record is joined to its
    counterpart and to its trace by them, and a changed id joins nothing.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = None
        fields = getattr(record, "json_fields", None)
        if fields:
            record.json_fields = {
                str(key): value if _is_identifier(key, value) else redact_value(value)
                for key, value in fields.items()
            }
        return True


def get_logger() -> logging.Logger:
    """Return the JSON logger, configuring it on first use."""
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = StructuredLogHandler(stream=sys.stderr)
        handler.addFilter(_RedactionFilter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def log_event(message: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Write one structured record. ``fields`` become top-level JSON keys.

    Args:
        message: What happened, in a few words; it is redacted like the fields.
        level: The severity, ``logging.INFO`` unless something went wrong.
        fields: Values to write next to the message, as top-level JSON keys.
    """
    get_logger().log(level, message, extra={"json_fields": fields}, stacklevel=2)


def _tracer_provider() -> TracerProvider:
    """A provider named after this service; it exports only when asked to."""
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: SERVICE}))
    if os.environ.get(TRACE_EXPORT_ENV, "").strip().lower() != "cloud":
        return provider  # spans get ids, and nothing leaves the process
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project:
        log_event(
            f"{TRACE_EXPORT_ENV}=cloud needs GOOGLE_CLOUD_PROJECT; "
            "spans are not exported.",
            level=logging.WARNING,
            event_phase="TRACING",
        )
        return provider
    # The exporter ADK's serving layer uses, attached the same way.
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

    provider.add_span_processor(
        BatchSpanProcessor(CloudTraceSpanExporter(project_id=project))
    )
    return provider


def setup_tracing() -> bool:
    """Give this process a tracer provider, unless it already has one.

    Without a provider no span is recorded and log records carry no trace id.
    The provider can be set once per process, so whoever set one first keeps
    it: the serving layer, or a test that collects spans in memory. Returns
    whether this call set the provider.
    """
    if not isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
        return False
    trace.set_tracer_provider(_tracer_provider())
    return True


def _set_attributes(span: trace.Span, attributes: dict[str, Any]) -> None:
    for key, value in attributes.items():
        span.set_attribute(key, str(value))


def log_activity(line: str, run_id: str) -> None:
    """Write one activity line, as the page shows it, as a structured record.

    The line is a field, not the message, so the redaction filter cuts a long
    one: the log says what happened, it does not repeat an article.
    """
    get_logger().info(
        "activity",
        extra={
            "json_fields": {"event_phase": "ACTIVITY", "run_id": run_id, "line": line}
        },
        stacklevel=2,
    )


@contextmanager
def traced_span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    """Open a child span. Attribute values are stringified and never include bodies."""
    with _tracer.start_as_current_span(name) as span:
        _set_attributes(span, attributes)
        yield span


@contextmanager
def root_span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    """Open the span of an action a person started: the root of a new trace.

    It has no parent even when it is opened while another action's span is
    current, as when the posts of a run start its slides: one action is one
    trace. Everything that runs inside it, and every log record written
    inside it, belongs to that trace.
    """
    with _tracer.start_as_current_span(name, context=otel_context.Context()) as span:
        _set_attributes(span, attributes)
        yield span


class ObservabilityPlugin(BasePlugin):
    """Logs what each tool call intended to do and what actually happened.

    A tool that raises gets its outcome record here, with status
    ``exception`` and the type of the exception; this plugin answers nothing,
    so ``ToolFailurePlugin`` after it can give the model a guided error.
    """

    def __init__(self) -> None:
        super().__init__(name="observability")
        self._started: dict[str, float] = {}
        # Calls whose outcome record was written when the tool raised. When a
        # later plugin answers the exception, ADK passes that answer through
        # ``after_tool_callback``; the call has its outcome already.
        self._raised: set[str] = set()

    @staticmethod
    def _call_id(tool: BaseTool, tool_context: ToolContext) -> str:
        return getattr(tool_context, "function_call_id", None) or tool.name

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, Any] | None:
        call_id = self._call_id(tool, tool_context)
        self._started[call_id] = time.perf_counter()
        log_event(
            f"intent: {tool.name}",
            event_phase="INTENT",
            tool=tool.name,
            function_call_id=call_id,
            agent=tool_context.agent_name,
            invocation_id=tool_context.invocation_id,
            intended_arguments=tool_args,
        )
        return None

    def _latency_ms(self, call_id: str) -> float | None:
        started = self._started.pop(call_id, None)
        if started is None:
            return None
        return round((time.perf_counter() - started) * 1000, 1)

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        call_id = self._call_id(tool, tool_context)
        if call_id in self._raised:
            # One intent, one outcome: the record of the exception stands, and
            # the guided error the model was given instead is not a second one.
            self._raised.discard(call_id)
            return None
        outcome = result if isinstance(result, dict) else {"result": result}
        log_event(
            f"outcome: {tool.name}",
            event_phase="OUTCOME",
            tool=tool.name,
            function_call_id=call_id,
            agent=tool_context.agent_name,
            invocation_id=tool_context.invocation_id,
            status=outcome.get("status", "ok"),
            error_code=outcome.get("error_code"),
            latency_ms=self._latency_ms(call_id),
            actual_outcome=outcome,
        )
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict[str, Any] | None:
        call_id = self._call_id(tool, tool_context)
        log_event(
            f"outcome: {tool.name} raised",
            event_phase="OUTCOME",
            tool=tool.name,
            function_call_id=call_id,
            agent=tool_context.agent_name,
            invocation_id=tool_context.invocation_id,
            status="exception",
            error_code=type(error).__name__,
            latency_ms=self._latency_ms(call_id),
        )
        if isinstance(error, Exception):  # a cancelled call has no answer
            self._raised.add(call_id)
        return None
