"""Keep the text of a Google Slides deck and its Markdown slide plan in step.

The same words live in two places: the Markdown file (``slides.<lang>.md``) and
the deck made from it. A person may edit either. Nothing is copied on its own;
a person asks for one direction and approves it:

- push: take what was edited in the file into the existing deck;
- pull: take what was edited in the deck into the file.

Only text moves: slide titles, bullets and speaker notes. Pictures, sizes and
positions in the deck are never touched, and slides are never added or removed.

To know which side was edited, both are compared with what they last agreed
on (the baseline, kept in the session). A push therefore carries only the
fields the file changed and leaves an edit made only in the deck alone; a pull
does the reverse. A field edited on both sides is a conflict: the direction
that was asked for wins, and the person is told how many there are before
approving.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.google_slides import (
    CLOSING_SLIDE_NUMBER,
    TITLE_SLIDE_ID,
    VISUAL_MARK,
    speaker_notes_text,
)
from app.schemas import SlideDeckPlan


def _key(number: int, field: str) -> str:
    return f"{number}/{field}"  # 0 is the title slide, 999 the thank-you slide


def _clean(text: str) -> str:
    # Google Slides stores a line break inside a paragraph as a vertical tab.
    return "\n".join(
        line.rstrip() for line in text.replace("\x0b", "\n").split("\n")
    ).strip()


def _lines(text: str) -> str:
    return "\n".join(line.strip() for line in _clean(text).split("\n") if line.strip())


def plan_text(plan: SlideDeckPlan) -> dict[str, str]:
    """The text a deck made from ``plan`` carries, by field."""
    view = {
        _key(0, "title"): _clean(plan.deck_title),
        _key(0, "notes"): _clean(plan.opening_script),
        # The words on the thank-you slide are fixed; only what is said is planned.
        _key(CLOSING_SLIDE_NUMBER, "notes"): _clean(plan.closing_script),
    }
    for slide in plan.slides:
        view[_key(slide.number, "title")] = _clean(slide.title)
        view[_key(slide.number, "bullets")] = _lines("\n".join(slide.bullets))
        view[_key(slide.number, "notes")] = _clean(
            speaker_notes_text(slide.visual_note, slide.speaker_script)
        )
    return view


def _shape_text(element: dict[str, Any]) -> str:
    runs = (element.get("shape") or {}).get("text", {}).get("textElements", [])
    return "".join(run.get("textRun", {}).get("content", "") for run in runs)


@dataclass(frozen=True)
class Holder:
    """The text box that holds one field, and how much text is in it now."""

    object_id: str
    length: int  # in UTF-16 units, without the final line break that cannot go


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2  # the unit Google's text indexes use


def _holder(object_id: str, raw: str) -> Holder:
    return Holder(object_id, max(0, _utf16_length(raw) - 1))


def deck_text(presentation: dict[str, Any]) -> tuple[dict[str, str], dict[str, Holder]]:
    """The text now in the deck, by field, and the box that holds each field.

    Slides are recognised by the ids this application gave them; a slide a
    person added by hand has another id and is not part of the plan.
    """
    view: dict[str, str] = {}
    objects: dict[str, Holder] = {}
    for page in presentation.get("slides", []):
        page_id = page.get("objectId", "")
        if page_id == TITLE_SLIDE_ID:
            number = 0
        elif page_id.startswith("slide_") and page_id[6:].isdigit():
            number = int(page_id[6:])
        else:
            continue
        shapes = {
            element.get("objectId"): element for element in page.get("pageElements", [])
        }
        for field, clean in (("title", _clean), ("bullets", _lines)):
            object_id = f"{page_id}_{'body' if field == 'bullets' else field}"
            if number == CLOSING_SLIDE_NUMBER or (number == 0 and field == "bullets"):
                continue
            if object_id in shapes:
                raw = _shape_text(shapes[object_id])
                view[_key(number, field)] = clean(raw)
                objects[_key(number, field)] = _holder(object_id, raw)
        notes_page = page.get("slideProperties", {}).get("notesPage", {})
        notes_id = notes_page.get("notesProperties", {}).get("speakerNotesObjectId")
        if notes_id:
            holder = next(
                (
                    element
                    for element in notes_page.get("pageElements", [])
                    if element.get("objectId") == notes_id
                ),
                {},
            )
            raw = _shape_text(holder)
            view[_key(number, "notes")] = _clean(raw)
            objects[_key(number, "notes")] = _holder(notes_id, raw)
    return view, objects


@dataclass(frozen=True)
class Difference:
    """One field whose text differs between the file and the deck."""

    key: str
    file: str
    deck: str
    file_changed: bool  # since the two last agreed
    deck_changed: bool

    @property
    def number(self) -> int:
        return int(self.key.split("/")[0])

    @property
    def field(self) -> str:
        return self.key.split("/")[1]


def differences(
    file_view: dict[str, str],
    deck_view: dict[str, str],
    baseline: dict[str, str] | None,
) -> list[Difference]:
    """Fields present on both sides whose text differs.

    Without a baseline for a field nobody can tell which side was edited, so
    both count as changed: either direction may then carry it.
    """
    found = []
    for key, file in file_view.items():
        deck = deck_view.get(key)
        if deck is None or deck == file:
            continue
        known = baseline is not None and key in baseline
        found.append(
            Difference(
                key,
                file,
                deck,
                file_changed=not known or file != baseline[key],
                deck_changed=not known or deck != baseline[key],
            )
        )
    return found


def to_carry(found: list[Difference], direction: str) -> list[Difference]:
    """The differences a push (file to deck) or a pull (deck to file) moves."""
    if direction == "push":
        return [d for d in found if d.file_changed]
    # A title emptied in the deck is not pulled: a plan cannot hold a slide
    # without a title, and an emptied box is far more often a slip than a wish.
    return [
        d for d in found if d.deck_changed and not (d.field == "title" and not d.deck)
    ]


def describe(
    direction: str,
    carried: list[Difference],
    file_view: dict[str, str],
    deck_view: dict[str, str],
    baseline: dict[str, str] | None,
) -> dict[str, Any]:
    """What an approval dialog needs to say before anything is written."""
    known = baseline is not None
    numbers = {int(key.split("/")[0]) for key in file_view}
    in_deck = {int(key.split("/")[0]) for key in deck_view}
    return {
        "direction": direction,
        "slides": sorted({d.number for d in carried}),
        "titles": sum(d.field == "title" for d in carried),
        "bullets": sum(d.field == "bullets" for d in carried),
        "notes": sum(d.field == "notes" for d in carried),
        # Edited on both sides: the direction asked for overwrites the other.
        "conflicts": sorted(
            {d.number for d in carried if known and d.file_changed and d.deck_changed}
        ),
        "baseline_known": known,
        "only_in_file": sorted(numbers - in_deck),
        "only_in_deck": sorted(in_deck - numbers),
    }


def update_requests(
    carried: list[Difference], objects: dict[str, Holder]
) -> list[dict]:
    """Requests that put the file's text into the deck, one field at a time.

    The new text is inserted in front of the old, which is then removed, so
    it takes over the font, size and bullets of the text it replaces instead
    of falling back to the theme's defaults.
    """
    requests: list[dict] = []
    for change in carried:
        holder = objects[change.key]
        if change.file:
            requests.append(
                {
                    "insertText": {
                        "objectId": holder.object_id,
                        "insertionIndex": 0,
                        "text": change.file,
                    }
                }
            )
        if holder.length:
            start = _utf16_length(change.file)
            requests.append(
                {
                    "deleteText": {
                        "objectId": holder.object_id,
                        "textRange": {
                            "type": "FIXED_RANGE",
                            "startIndex": start,
                            "endIndex": start + holder.length,
                        },
                    }
                }
            )
    return requests


def split_notes(notes: str) -> tuple[str, str | None]:
    """Speaker notes as the script and, if it is there, the '[Visual]' note."""
    script, mark, visual = notes.rpartition(VISUAL_MARK)
    return (script.strip(), visual.strip()) if mark else (notes.strip(), None)


def merged_plan(plan: SlideDeckPlan, carried: list[Difference]) -> SlideDeckPlan:
    """``plan`` with the deck's text taken over for the carried fields."""
    data = plan.model_dump()
    slides = {slide["number"]: slide for slide in data["slides"]}
    for change in carried:
        if change.number == 0:
            name = "deck_title" if change.field == "title" else "opening_script"
            data[name] = change.deck
        elif change.number == CLOSING_SLIDE_NUMBER:
            data["closing_script"] = change.deck
        elif change.field == "title":
            slides[change.number]["title"] = change.deck
        elif change.field == "bullets":
            slides[change.number]["bullets"] = change.deck.split("\n")
        else:
            script, visual = split_notes(change.deck)
            slides[change.number]["speaker_script"] = script
            # No '[Visual]' line in the deck means the person removed it.
            slides[change.number]["visual_note"] = visual or "text only"
    return SlideDeckPlan.model_validate(data)


def agreed(
    file_view: dict[str, str],
    deck_view: dict[str, str],
    baseline: dict[str, str] | None,
) -> dict[str, str]:
    """The new baseline: every field on which file and deck now agree."""
    same = {key: text for key, text in file_view.items() if deck_view.get(key) == text}
    return {**(baseline or {}), **same}
