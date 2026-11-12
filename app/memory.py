"""Long-term memory: remember the author's preferences across sessions.

Writing to Memory Bank triggers model-side extraction and consolidation, which
is slow compared with a chat turn. ``schedule_memory_consolidation`` therefore
only *schedules* the work as a background task and returns immediately, so the
user's reply is never held up by it. The task outlives the turn that started
it, so it takes nothing from the callback's context with it: the events, the
memory service and the identifiers of the session are read before the task is
made. A strong reference keeps the task alive until it ends, and a write that
fails is a structured log record, never an exception in somebody's turn.

The memory service is the one the runner was given. Every surface gives it the
same one, ``app.app_utils.services.get_memory_service``: the page and the
script (``ArticlePipeline``), the agent API server and Agent Runtime.

Only the turn that just finished is sent, and only what the author and the
coordinator said in it. Tool calls, tool results and the output of every other
agent are left out, because that is where whole articles travel, and a memory
is later put into the coordinator's prompt as something the user said. What is
kept is redacted, cut to a fixed length and limited to the latest few events,
so of a draft pasted into a chat message no more than its opening lines can
become a memory. Recall happens through ``PreloadMemoryTool`` on the
coordinator.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.events import Event
from google.genai import types

from app.observability import log_event
from app.redaction import redact_text

# Name of the root agent in app/agent.py. Repeated here because that module
# imports this one.
COORDINATOR_AGENT_NAME = "trilingual_blog_agent"
_REMEMBERED_AUTHORS = frozenset({"user", COORDINATOR_AGENT_NAME})

# Long enough for a preference or a decision, far too short for an article.
MAX_MEMORY_TEXT_CHARS = 1000
# One turn rarely has more than a few author and coordinator messages; the cap
# bounds what a single runaway turn can add.
MAX_MEMORY_EVENTS = 20

# Strong references so pending tasks are not garbage-collected mid-flight.
_background_tasks: set[asyncio.Task[Any]] = set()


def _memory_events(callback_context: CallbackContext) -> list[Event]:
    """Redacted, shortened copies of what author and coordinator said this turn."""
    events: list[Event] = []
    for event in callback_context.session.events:
        if event.invocation_id != callback_context.invocation_id:
            continue
        if event.author not in _REMEMBERED_AUTHORS:
            continue
        if not event.content or not event.content.parts:
            continue
        said = "\n".join(
            part.text for part in event.content.parts if part.text and not part.thought
        )
        if not said.strip():
            continue
        # Redact before cutting: a credential cut in half no longer matches its
        # pattern and would be stored as it is.
        text = redact_text(said)
        if len(text) > MAX_MEMORY_TEXT_CHARS:
            text = text[:MAX_MEMORY_TEXT_CHARS] + "..."
        # A new event rather than a copy: a copy would carry the state changes
        # and function calls of the original, which can hold article text.
        events.append(
            Event(
                id=event.id,
                invocation_id=event.invocation_id,
                author=event.author,
                timestamp=event.timestamp,
                content=types.Content(
                    role=event.content.role, parts=[types.Part.from_text(text=text)]
                ),
            )
        )
    return events[-MAX_MEMORY_EVENTS:]


def _memory_service(callback_context: CallbackContext) -> Any:
    """The memory service of the runner this turn runs under, or None.

    ADK's context offers the service only through methods that use the
    context when they are called; the background task must not do that after
    the turn has ended, so the service itself is taken here. A unit test runs
    a real turn and fails if a new ADK version keeps it somewhere else.
    """
    invocation = getattr(callback_context, "_invocation_context", None)
    return getattr(invocation, "memory_service", None)


async def _consolidate(
    service: Any, *, app_name: str, user_id: str, session_id: str, events: list[Event]
) -> None:
    """Write the events to the memory service; a failure is logged, not raised.

    Args:
        service: The memory service, taken from the turn before it ended.
        app_name: The application the memories belong to.
        user_id: The user the memories belong to.
        session_id: The session the events were said in.
        events: The redacted, shortened events of the turn.
    """
    try:
        await service.add_events_to_memory(
            app_name=app_name, user_id=user_id, session_id=session_id, events=events
        )
    except Exception as exc:
        # The type only: the text of the exception could repeat what was said.
        log_event(
            "memory consolidation failed",
            level=logging.WARNING,
            event_phase="MEMORY",
            session_id=session_id,
            events=len(events),
            error_code=type(exc).__name__,
        )
        return
    log_event(
        "memory consolidation finished",
        event_phase="MEMORY",
        session_id=session_id,
        events=len(events),
    )


def _consolidation_ended(task: asyncio.Task[Any]) -> None:
    """Let go of a task that ended, and say so if it did not end by itself."""
    _background_tasks.discard(task)
    if task.cancelled():
        # The process is shutting down, or the event loop was closed.
        log_event(
            "memory consolidation cancelled",
            level=logging.WARNING,
            event_phase="MEMORY",
        )


async def schedule_memory_consolidation(callback_context: CallbackContext) -> None:
    """``after_agent_callback``: queue memory consolidation without awaiting it.

    Everything the background task needs is read from the context here, while
    the turn is still running. The task itself never touches the context.
    """
    events = _memory_events(callback_context)
    if not events:
        return None
    service = _memory_service(callback_context)
    if service is None:
        # No memory service configured for this runner; nothing to do.
        log_event(
            "memory consolidation skipped: no memory service", event_phase="MEMORY"
        )
        return None
    session = callback_context.session
    task = asyncio.create_task(
        _consolidate(
            service,
            app_name=session.app_name,
            user_id=session.user_id,
            session_id=session.id,
            events=events,
        ),
        name="memory-consolidation",
    )
    _background_tasks.add(task)
    task.add_done_callback(_consolidation_ended)
    return None
