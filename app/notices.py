"""Sentences the server says to a person, each with a code (WEB-13).

A notice of a lane and a refusal of the web layer are English sentences. The
page shows them in the interface language: every sentence made here carries
its code and the values it was filled with, and the page looks up
``server.<code>`` in its catalogs, falling back to the English sentence.

``TEMPLATES`` is the one list of codes. ``tests/unit/test_i18n.py`` keeps the
catalogs in step with it: every code has its text in every language, with the
same placeholders, and the English text is the sentence written here.

A ``Notice`` is a string, the English sentence, so everything that reads,
compares or stores sentences goes on working. It also has ``parts``: one
entry per coded sentence. Several notices are put together with ``join``,
which keeps the parts; a plain string joined in becomes a part without a
code, shown as it is.

What is not translated is passed through as a value (4.7 of the design): what
a platform or a tool answered, a title the author wrote. Two names of values
mean something to the page: ``language`` is a language code and a list is a
list of language codes; the page writes them as it names languages.

This module imports only ``app.settings``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from app.settings import LANGUAGE_NAMES

TEMPLATES: dict[str, str] = {
    # --- lanes (app/runs.py) ---
    "lane_busy_main": (
        "The main lane (posts and requests to the agent) is still working. "
        "Try again when it has finished."
    ),
    "lane_busy_slides": (
        "The slides lane (plans, decks and PDF) is still working. "
        "Try again when it has finished."
    ),
    "interrupted_main": (
        "The server stopped while this run was working. What was finished is "
        "below; start a new run for the rest."
    ),
    "interrupted_slides": (
        "The server stopped while the slides of this run were being made. "
        "What was finished is below; ask for the missing slides again."
    ),
    "unknown_run": "Unknown run. Its record is not on this server.",
    "slides_not_started": "No slides were started.",
    "approved_slides_not_started": "The slides that were approved were not started.",
    "ask_for_slides_again": "Then ask for them again.",
    # --- the source document (app/pipeline.py) ---
    "post_not_read": "The post could not be read. {reason}",
    "no_article_stored": (
        "No article was stored. See the agent's reply in the activity log."
    ),
    "address_unreadable": (
        "That address cannot be read: {sources}. Hand in a supported URL, a "
        ".md or .txt file, or the Markdown text itself."
    ),
    "gdoc_not_read": "The Google Docs document could not be read. {reason}",
    "medium_not_read": "The Medium post could not be read. {reason}",
    "draft_too_short": (
        "The draft could not be stored. It is shorter than {min} characters; "
        "hand in the whole article."
    ),
    "draft_too_long": (
        "The draft could not be stored. It has {chars} characters; the limit "
        "for one article is {limit}."
    ),
    "draft_not_stored": "The draft could not be stored. {reason}",
    "naver_rebuilt_structured": (
        "The text was rebuilt from the Naver page, so images and links may be missing."
    ),
    "naver_rebuilt_plain": (
        "The text was rebuilt from the Naver page, so headings, lists, images "
        "and links may be missing."
    ),
    "check_korean_file": "Check the Korean file before using the results.",
    "draft_no_heading": "The draft has no Markdown heading.",
    "review_file": "Review the file before using the results.",
    "structuring_used": (
        "A model gave the text Markdown structure (headings, lists, tables, "
        "code blocks). Code confirmed that the letters and digits are the "
        "same as in the text as supplied, which is kept as {file}."
    ),
    "structuring_letters_differ": (
        "A model was asked to give the text Markdown structure, but its copy "
        "was not used because its letters and digits were not the same as in "
        "the text as supplied. The article is the text as supplied, also kept "
        "as {file}."
    ),
    "structuring_no_answer": (
        "A model was asked to give the text Markdown structure, but its copy "
        "was not used because the model gave no answer. The article is the "
        "text as supplied, also kept as {file}."
    ),
    "structuring_not_stored": (
        "A model was asked to give the text Markdown structure, but its copy "
        "was not used because it could not be stored. The article is the text "
        "as supplied, also kept as {file}."
    ),
    "draft_no_title_line": (
        "The draft has no '# Title' line, so this was taken as the title: "
        "'{title}'. If that is not the title, add a '# Title' line at the top "
        "and run again."
    ),
    # --- the posts ---
    "no_text_written": (
        "No {languages} text was written, so nothing else was done. See the "
        "agent's reply in the activity log."
    ),
    "structure_check_failed": (
        "The structure check failed for {languages}, so nothing was exported "
        "and no slides were made. Review the files; the activity log lists "
        "what differs."
    ),
    "naver_file_not_written": "The Naver Blog file was not written. {reason}",
    "not_posted": "Nothing was posted on {platform}. {reason}",
    "address_not_recorded": "The address was not recorded. {reason}",
    "address_did_not_answer": (
        "The address was recorded, but it did not answer when checked "
        "({problem}). Open it to make sure it is right."
    ),
    # --- the slides ---
    "slides_not_made": "The {language} slides were not made. {reason}",
    "no_slide_plan": "No {language} slide plan was made. {reason}",
    "no_slide_plan_see_log": (
        "No {language} slide plan was made. See the activity log for what the "
        "planner said."
    ),
    "slide_plan_missed_target": (
        "The {language} slide plan missed the one-slide-per-minute target "
        "after two attempts; its speaker notes say how long each slide runs."
    ),
    "deck_not_created": "The {language} Google Slides deck was not created. {reason}",
    "pdf_not_exported": "The {language} PDF was not exported. {reason}",
    # --- editing and deck text sync ---
    "file_not_editable": "This file cannot be edited here.",
    "post_read_from_platform": (
        "This post was read from its blog platform and cannot be edited here."
    ),
    "post_no_longer_editable": "This post can no longer be edited here.",
    "text_empty": "The text is empty.",
    "no_deck_yet": "There is no Google Slides deck for this language yet.",
    "slide_plan_file_missing": "The slide plan file of this language is missing.",
    # --- the web layer (app/web_ui.py) ---
    "needs_access_token": (
        "This server needs its access token. Open the address that its log "
        "showed when it started."
    ),
    "post_needs_approval": "A post is sent only with a person's approval.",
    "deck_needs_approval": "A deck is created only with a person's approval.",
    "slides_need_approval": "Slides are made only with a person's approval.",
    "tag_rule_qiita": (
        "Qiita takes one to five tags without spaces, separated by commas "
        '(Good: "GoogleCloud, Lustre, HPC, Storage, GCP" · '
        'Bad: "GoogleCloud Lustre HPC Storage GCP").'
    ),
    "tag_rule_devto": (
        "DEV Community takes up to four tags, lowercase letters and digits only, "
        'separated by commas (Good: "googlecloud, lustre, hpc, storage" · '
        'Bad: "GoogleCloud, google-cloud" or "gcp lustre hpc").'
    ),
    "ask_before_approving": "Ask what would change before approving it.",
    "approval_not_waiting": "That approval request is no longer waiting.",
    "requests_left_unanswered": (
        "{count} request(s) the agent was waiting on were left unanswered by "
        "this message and are gone. Ask again for those."
    ),
    "other_requests_declined": (
        "{count} other request(s) the agent was waiting on were declined: one "
        "answer goes to one request. Ask again for those."
    ),
}

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
# How a list of languages reads in the English sentence of a code.
_LIST_WORD = {"no_text_written": " or "}


def _english(code: str, name: str, value: Any) -> str:
    """A value as the English sentence writes it."""
    if isinstance(value, list | tuple):
        names = [LANGUAGE_NAMES.get(item, str(item)) for item in value]
        return _LIST_WORD.get(code, " and ").join(names)
    if name == "language":
        return LANGUAGE_NAMES.get(value, str(value))
    return str(value)


class Notice(str):
    """An English sentence, or several, with the coded parts it is made of."""

    parts: tuple[dict[str, Any], ...]

    def __new__(cls, text: str, parts: Iterable[dict[str, Any]]) -> Notice:
        made = super().__new__(cls, text)
        made.parts = tuple(parts)
        return made


def notice(code: str, **params: Any) -> Notice:
    """The sentence of ``code`` filled with ``params``, carrying both."""
    template = TEMPLATES[code]
    wanted = set(_PLACEHOLDER.findall(template))
    assert wanted == set(params), f"{code} takes {sorted(wanted)}"
    text = _PLACEHOLDER.sub(
        lambda match: _english(code, match.group(1), params[match.group(1)]), template
    )
    sent = {
        name: list(value) if isinstance(value, list | tuple) else str(value)
        for name, value in params.items()
    }
    return Notice(text, [{"code": code, "params": sent, "text": text}])


def parts_of(text: str | None) -> list[dict[str, Any]]:
    """The parts of a sentence for the page. A sentence without a code (an
    error of a platform, a sentence from an older record) is one part that
    is shown as it is."""
    if not text:
        return []
    if isinstance(text, Notice):
        return [dict(part) for part in text.parts]
    return [{"code": None, "params": {}, "text": str(text)}]


def join(*items: str | None) -> Notice | None:
    """Several sentences as one notice, in order; None when there is none."""
    parts = [part for item in items for part in parts_of(item)]
    if not parts:
        return None
    return Notice(" ".join(part["text"] for part in parts), parts)


def restore(text: str | None, parts: Any) -> str | None:
    """A sentence read back from a run record, with its parts when the record
    kept them and they still spell the sentence."""
    if not text or not isinstance(parts, list):
        return text
    try:
        if " ".join(part["text"] for part in parts) != text:
            return text
        kept = [
            {
                "code": part["code"] if part.get("code") in TEMPLATES else None,
                "params": dict(part.get("params") or {}),
                "text": str(part["text"]),
            }
            for part in parts
        ]
    except (KeyError, TypeError, ValueError):
        return text
    return Notice(text, kept)
