"""Structured logging, intent/outcome capture, and tracing helpers.

Logging
    One logger, ``blog_agent``, writes single-line JSON through
    ``google.cloud.logging``'s ``StructuredLogHandler``. Every record passes a
    redaction filter first, so credentials and personal data are scrubbed
    before they reach any log sink. The handler adds the active OpenTelemetry
    trace and span IDs, which lets a log line be opened next to its trace.

Intent versus outcome
    ``ObservabilityPlugin`` is registered on the ``App`` and therefore sees
    every tool call made by every agent. Before a tool runs it logs the
    *intent* (which tool, with which arguments); afterwards it logs the
    *outcome* (status, error code, latency). The two records share the
    function-call ID so they can be joined.

Tracing
    ADK already opens spans for agent runs, model calls and tool calls, and the
    serving layer exports them to Cloud Trace when telemetry is enabled.
    ``traced_span`` adds child spans around the outbound HTTP calls made by the
    tools, so a slow publish can be attributed to the platform that was slow.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.cloud.logging_v2.handlers import StructuredLogHandler
from opentelemetry import trace

from app.redaction import redact_text, redact_value

LOGGER_NAME = "blog_agent"
_tracer = trace.get_tracer(LOGGER_NAME)


class _RedactionFilter(logging.Filter):
    """Scrub the message and structured fields of every record before it is emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = None
        fields = getattr(record, "json_fields", None)
        if fields:
            record.json_fields = redact_value(fields)
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


def log_event(message: str, **fields: Any) -> None:
    """Write one structured record. ``fields`` become top-level JSON keys."""
    get_logger().info(message, extra={"json_fields": fields}, stacklevel=2)


@contextmanager
def traced_span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    """Open a child span. Attribute values are stringified and never include bodies."""
    with _tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            span.set_attribute(key, str(value))
        yield span


class ObservabilityPlugin(BasePlugin):
    """Logs what each tool call intended to do and what actually happened."""

    def __init__(self) -> None:
        super().__init__(name="observability")
        self._started: dict[str, float] = {}

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
        return None
