"""Model routing: which Gemini model each kind of work is sent to.

Two tiers are used on purpose:

* ``FAST_MODEL`` handles high-volume, latency-sensitive work where the task is
  well specified: routing the conversation, calling tools, and translating a
  finished article into another language.
* ``REASONING_MODEL`` handles work where judgement matters more than speed:
  revising a technical draft, criticising a translation against its source,
  and planning a slide deck with a timed speaker script.

Both names can be overridden through environment variables so the same code
runs in projects where a different model is available.
"""

from __future__ import annotations

import os
from typing import Literal

from google.adk.models import Gemini
from google.genai import types

FAST_MODEL = os.environ.get("FAST_MODEL_NAME", "gemini-3.8-flash")
REASONING_MODEL = os.environ.get("REASONING_MODEL_NAME", "gemini-4-argon")

TaskKind = Literal[
    "coordinate",
    "localize",
    "localize_titles",
    "refine_localization",
    "summarize_history",
    "revise_seed",
    "critique_localization",
    "plan_slides",
    "structure_source",
]

# The routing table. Keeping it as data makes the decision reviewable in one
# place instead of scattered across agent definitions.
MODEL_ROUTES: dict[TaskKind, str] = {
    "coordinate": FAST_MODEL,
    "localize": FAST_MODEL,
    "localize_titles": FAST_MODEL,
    "refine_localization": FAST_MODEL,
    "summarize_history": FAST_MODEL,
    "revise_seed": REASONING_MODEL,
    "critique_localization": REASONING_MODEL,
    "plan_slides": REASONING_MODEL,
    "structure_source": FAST_MODEL,
}

# Transient errors are retried the same way wherever a model is called: by the
# agents below and by the gateway for direct calls (``app.llm``).
RETRY_OPTIONS = types.HttpRetryOptions(attempts=3)


def model_for(task: TaskKind) -> Gemini:
    """Return the configured model for a kind of task, with transient-error retries."""
    return Gemini(
        model=MODEL_ROUTES[task],
        retry_options=RETRY_OPTIONS,
    )
