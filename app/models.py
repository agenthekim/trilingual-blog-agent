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
    "refine_localization",
    "summarize_history",
    "revise_seed",
    "critique_localization",
    "plan_slides",
]

# The routing table. Keeping it as data makes the decision reviewable in one
# place instead of scattered across agent definitions.
MODEL_ROUTES: dict[TaskKind, str] = {
    "coordinate": FAST_MODEL,
    "localize": FAST_MODEL,
    "refine_localization": FAST_MODEL,
    "summarize_history": FAST_MODEL,
    "revise_seed": REASONING_MODEL,
    "critique_localization": REASONING_MODEL,
    "plan_slides": REASONING_MODEL,
}


def model_for(task: TaskKind) -> Gemini:
    """Return the configured model for a kind of task, with transient-error retries."""
    return Gemini(
        model=MODEL_ROUTES[task],
        retry_options=types.HttpRetryOptions(attempts=3),
    )
