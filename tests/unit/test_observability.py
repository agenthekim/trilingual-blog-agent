"""OPS-1: one action a person starts is one trace, and its log records say so.

The agent is a real ADK ``Runner`` with the observability plugin; only the
model is replaced by one that says what was scripted. The spans are those the
session's in-memory collection holds (``collected_spans`` in conftest.py):
nothing is exported, and no test sets a tracer provider of its own.
"""

from __future__ import annotations

import inspect
import io
import json
import logging
from typing import Any

import pytest
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.sessions import InMemorySessionService
from google.cloud.logging_v2.handlers import StructuredLogHandler
from opentelemetry import trace

from app import google_slides, llm, observability, runs, web_ui
from app.observability import ObservabilityPlugin, _RedactionFilter, traced_span
from app.pipeline import ArticlePipeline
from tests.unit.test_approvals import ScriptedModel, _calls
from tests.unit.test_runs import _until

RUN_ID = "c" * 32


async def look_something_up(topic: str) -> dict[str, Any]:
    """Stands for a tool that calls a platform.

    Args:
        topic: What to look up.

    Returns:
        A dict with ``status``.
    """
    with traced_span("platform.stand_in", topic=topic):
        return {"status": "success"}


def note_a_decision(language: str, tool_context: Any) -> dict[str, Any]:
    """A person's action that needs no model."""
    tool_context.state["decision"] = language
    return {"status": "success"}


def _job() -> runs.Job:
    agent = Agent(
        name="trilingual_blog_agent",
        model=ScriptedModel(
            model="scripted",
            script=[_calls(("look_something_up", {"topic": "tracing"}))],
        ),
        instruction="Do what the user asks for.",
        tools=[look_something_up],
    )
    pipeline = ArticlePipeline(
        log=lambda _: None,
        session_service=InMemorySessionService(),
        agent_app=App(name="app", root_agent=agent, plugins=[ObservabilityPlugin()]),
    )
    return runs.Job(id=RUN_ID, pipeline=pipeline)


async def _one_action(job: runs.Job, monkeypatch) -> None:
    """An action with everything under it: an agent turn with a tool call that
    reaches a platform, a tool call for the person, and a direct model call."""

    async def answer(model: str, instruction: str, contents: str) -> str:
        return "an answer"

    monkeypatch.setattr(llm, "_call_model", answer)

    async def work() -> None:
        await job.pipeline.say("Look it up.")
        await job.pipeline.act(note_a_decision, "en")
        await llm.generate_text("structure_source", "Give it headings.", "some text")

    runs.start(job, runs.MAIN, work, "message")
    await _until(lambda: not job.busy)
    assert job.error is None and job.notice is None


def _by_name(spans: list[Any]) -> dict[str, Any]:
    return {span.name: span for span in spans}


@pytest.mark.asyncio
async def test_an_action_and_what_runs_under_it_share_one_trace(
    isolated_workspace, monkeypatch, collected_spans
) -> None:
    collected_spans.clear()
    job = _job()
    await _one_action(job, monkeypatch)

    spans = collected_spans.get_finished_spans()
    named = _by_name(spans)
    root = named["run.main.message"]
    assert root.parent is None
    assert root.attributes["run_id"] == RUN_ID and root.attributes["lane"] == "main"
    assert root.resource is not None

    # Everything the action did is in its trace: the agent's own spans, the
    # platform call its tool made, and the direct model call.
    trace_id = root.context.trace_id
    assert {span.context.trace_id for span in spans} == {trace_id}
    assert all(span.parent is not None for span in spans if span is not root)
    assert {"platform.stand_in", "model.generate_text"} <= set(named)
    from_adk = set(named) - {
        "run.main.message",
        "platform.stand_in",
        "model.generate_text",
    }
    assert any("look_something_up" in name for name in from_adk), from_adk
    # The platform call hangs under the agent's tool span, not beside it.
    by_id = {span.context.span_id: span for span in spans}
    above = by_id[named["platform.stand_in"].parent.span_id]
    assert "look_something_up" in above.name

    # Another action is another trace, also when it is started from inside
    # an action, as the slides of a run are started by its posts.
    collected_spans.clear()

    async def slides() -> None:
        with traced_span("slides.stand_in"):
            return None

    async def posts() -> None:
        runs.start(job, runs.SLIDES, slides, "run_slides")

    runs.start(job, runs.MAIN, posts, "run_posts")
    await _until(lambda: not job.busy)
    named = _by_name(collected_spans.get_finished_spans())
    first, second = named["run.main.run_posts"], named["run.slides.run_slides"]
    assert second.parent is None
    assert second.context.trace_id not in (trace_id, first.context.trace_id)
    assert named["slides.stand_in"].context.trace_id == second.context.trace_id
    assert second.attributes["lane"] == "slides"

    # An action whose result the caller waits for has its root span too.
    collected_spans.clear()
    with runs.claim(job, runs.SLIDES, "sync_deck"), traced_span("slides.stand_in"):
        pass
    named = _by_name(collected_spans.get_finished_spans())
    claimed = named["run.slides.sync_deck"]
    assert claimed.parent is None and claimed.attributes["run_id"] == RUN_ID
    assert named["slides.stand_in"].parent.span_id == claimed.context.span_id


@pytest.mark.asyncio
async def test_log_records_carry_the_trace_of_the_action(
    isolated_workspace, monkeypatch, collected_spans
) -> None:
    stream = io.StringIO()
    handler = StructuredLogHandler(stream=stream)
    handler.addFilter(_RedactionFilter())
    logger = observability.get_logger()
    logger.addHandler(handler)
    collected_spans.clear()
    try:
        await _one_action(_job(), monkeypatch)
    finally:
        logger.removeHandler(handler)

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    root = _by_name(collected_spans.get_finished_spans())["run.main.message"]
    trace_id = trace.format_trace_id(root.context.trace_id)
    assert len(records) >= 6
    for record in records:
        # No record of the action is without the action's trace.
        assert record["logging.googleapis.com/trace"].endswith(trace_id), record
        assert record["logging.googleapis.com/spanId"], record

    # Every intent has its outcome, joined by an id nothing has changed: the
    # agent's tool call, the tool call for the person, the direct model call.
    def call_ids(phase: str) -> list[str]:
        return sorted(
            record.get("function_call_id") or record["call_id"]
            for record in records
            if record.get("event_phase") == phase
        )

    intents = call_ids("INTENT")
    assert len(intents) == 3 and len(set(intents)) == 3
    assert call_ids("OUTCOME") == intents
    assert not any("REDACTED" in call_id for call_id in intents)


@pytest.mark.asyncio
async def test_a_tool_that_raises_still_leaves_an_outcome_for_its_intent(
    isolated_workspace,
) -> None:
    stream = io.StringIO()
    handler = StructuredLogHandler(stream=stream)
    logger = observability.get_logger()
    logger.addHandler(handler)

    def breaks(tool_context: Any) -> dict[str, Any]:
        raise RuntimeError("the disk is full")

    try:
        with pytest.raises(RuntimeError):
            await _job().pipeline.act(breaks)
    finally:
        logger.removeHandler(handler)
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    intent, outcome = records
    assert (intent["event_phase"], outcome["event_phase"]) == ("INTENT", "OUTCOME")
    assert intent["call_id"] == outcome["call_id"]
    assert outcome["status"] == "exception"
    assert outcome["error_code"] == "RuntimeError"


def test_spans_leave_the_process_only_when_export_is_asked_for(monkeypatch) -> None:
    import opentelemetry.exporter.cloud_trace as cloud_trace
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    made: list[Any] = []

    class StandInExporter(SpanExporter):
        def __init__(self, project_id: str | None = None) -> None:
            self.project_id, self.exported = project_id, []
            made.append(self)

        def export(self, spans: Any) -> SpanExportResult:
            self.exported.extend(spans)
            return SpanExportResult.SUCCESS

    monkeypatch.setattr(cloud_trace, "CloudTraceSpanExporter", StandInExporter)

    def one_span() -> None:
        provider = observability._tracer_provider()
        assert provider.resource.attributes["service.name"] == observability.SERVICE
        with provider.get_tracer("test").start_as_current_span("an action"):
            pass
        provider.shutdown()  # flushes what waits to be exported

    # Not asked for: ids are made, and no exporter exists at all.
    monkeypatch.delenv("BLOG_AGENT_TRACE_EXPORT", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "a-project")
    one_span()
    assert made == []

    # Asked for without a project: said in the log, and nothing is exported.
    monkeypatch.setenv("BLOG_AGENT_TRACE_EXPORT", "cloud")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT")
    one_span()
    assert made == []

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "a-project")
    one_span()
    (exporter,) = made
    assert exporter.project_id == "a-project"
    assert [span.name for span in exporter.exported] == ["an action"]


def test_the_page_and_the_script_set_up_tracing_and_outbound_calls_have_spans() -> None:
    from scripts import run_pipeline

    assert "setup_tracing()" in inspect.getsource(web_ui.main)
    script = inspect.getsource(run_pipeline.main)
    assert "setup_tracing()" in script
    assert 'root_span("run.main.run_posts"' in script
    assert 'root_span("run.slides.run_slides"' in script
    for function, span in (
        (google_slides.create_deck, "slides.create_deck"),
        (google_slides.read_presentation, "slides.read_presentation"),
        (google_slides.update_presentation, "slides.update_presentation"),
        (google_slides.export_deck_pdf, "slides.export_pdf"),
        (web_ui._draw_diagram, "diagram.draw"),
        (llm.generate_text, "model.generate_text"),
    ):
        source = inspect.getsource(function)
        assert "with traced_span(" in source and f'"{span}"' in source, span


def test_a_logger_is_not_needed_to_trace() -> None:
    # The handler reads the ids from the current span; outside a span a
    # record has none, and that is not an error.
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "x", None, None)
    assert _RedactionFilter().filter(record) is True
