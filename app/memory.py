"""Long-term memory: remember the author's preferences across sessions.

Writing to Memory Bank triggers model-side extraction and consolidation, which
is slow compared with a chat turn. ``schedule_memory_consolidation`` therefore
only *schedules* the work as a background task and returns immediately, so the
user's reply is never held up by it.

Only the turn that just finished is sent, and only its conversational text:
tool calls and tool results (which carry whole articles) are left out, and the
text is redacted first. Recall happens through ``PreloadMemoryTool`` on the
coordinator.
"""

from __future__ import annotations

import asyncio
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.events import Event
from google.genai import types

from app.observability import log_event
from app.redaction import redact_text

# Strong references so pending tasks are not garbage-collected mid-flight.
_background_tasks: set[asyncio.Task[Any]] = set()


def _memory_events(callback_context: CallbackContext) -> list[Event]:
    """Redacted, text-only copies of the events of the current invocation."""
    events: list[Event] = []
    for event in callback_context.session.events:
        if event.invocation_id != callback_context.invocation_id:
            continue
        if not event.content or not event.content.parts:
            continue
        texts = [
            types.Part.from_text(text=redact_text(part.text))
            for part in event.content.parts
            if part.text and not part.thought
        ]
        if not texts:
            continue
        copy = event.model_copy(deep=True)
        copy.content = types.Content(role=event.content.role, parts=texts)
        events.append(copy)
    return events


async def _consolidate(callback_context: CallbackContext, events: list[Event]) -> None:
    try:
        await callback_context.add_events_to_memory(events=events)
        log_event(
            "memory consolidation finished", event_phase="MEMORY", events=len(events)
        )
    except ValueError:
        # No memory service configured for this runner; nothing to do.
        log_event(
            "memory consolidation skipped: no memory service", event_phase="MEMORY"
        )
    except Exception as exc:
        log_event(
            "memory consolidation failed",
            event_phase="MEMORY",
            error_code=type(exc).__name__,
        )


async def schedule_memory_consolidation(callback_context: CallbackContext) -> None:
    """``after_agent_callback``: queue memory consolidation without awaiting it."""
    events = _memory_events(callback_context)
    if not events:
        return None
    task = asyncio.create_task(_consolidate(callback_context, events))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return None
