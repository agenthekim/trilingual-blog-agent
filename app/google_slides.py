"""Create a Google Slides deck with speaker notes from a slide plan.

The deck is created in the Drive of the Google account that granted access
once with ``scripts/authorize_google_slides.py``. That grant is stored as the
secret ``GOOGLE_SLIDES_OAUTH`` (client id, client secret, refresh token) and
is limited to the ``drive.file`` scope: the agent can touch only files it
created itself, never the rest of the Drive.

A deck, once created, is changed again only by ``update_presentation``, which
the web front end calls after a person approved the listed text changes (see
``app/deck_sync.py``).

Use a personal or dedicated account for the grant, not a work or school
account: the OAuth app is your own unverified app, and the decks are created
in that account's Drive. The authorization script shows which account granted
access and stores nothing until that is confirmed; the approval request for a
new deck names the account again.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import zlib
from dataclasses import dataclass
from typing import Any

import httpx

from app.schemas import SlideDeckPlan
from app.secrets import MissingSecretError, get_secret
from app.settings import HTTP_TIMEOUT_SECONDS

OAUTH_SECRET_NAME = "GOOGLE_SLIDES_OAUTH"
OAUTH_SCOPE = "https://www.googleapis.com/auth/drive.file"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SLIDES_API = "https://slides.googleapis.com/v1/presentations"
MERMAID_RENDER_URL = "https://mermaid.ink/img/pako:"
MERMAID_SVG_URL = "https://mermaid.ink/svg/pako:"
MAX_IMAGE_URL_CHARS = 2000


class SlidesError(Exception):
    """A Google Slides failure the agent can explain to the user."""

    def __init__(self, code: str, message: str, recovery: str) -> None:
        super().__init__(message)
        self.code, self.message, self.recovery = code, message, recovery


def deck_url(presentation_id: str) -> str:
    return f"https://docs.google.com/presentation/d/{presentation_id}/edit"


def _slide_id(number: int) -> str:
    return f"slide_{number:03d}"


EMU_PER_INCH = 914400
# Default 16:9 page: 10 x 5.625 inches. Content sits under the title.
_CONTENT_TOP, _CONTENT_HEIGHT = 1.65, 3.6
_FULL = (0.55, 8.9)  # left edge and width of the text box without an image
_LEFT = (0.55, 4.3)  # ... and next to an image of unknown shape
_RIGHT = (5.05, 4.4)  # left edge and width of the area for such an image
# An image may use more of the page than the bullets: it starts right under a
# one-line title and ends close to the bottom edge.
_IMAGE_TOP, _PAGE_BOTTOM = 1.4, 5.4
_PANEL_TOP = 0.35  # top of an image that also takes the height of the title
# Left edge and top of the title box in the default theme, and the width its
# text can use. Used only to estimate how a title wraps.
_TITLE_BOX = (0.34, 0.51)
_TITLE_INNER_WIDTH = 9.1
_GAP = 0.2  # between the bullets and the image
_TEXT_WIDTHS = (3.3, 3.55, 3.8, 4.05, 4.3)  # columns the bullets may get
_TEXT_HEIGHT = 3.3  # the bullets should not need more height than this
_MIN_STACKED_IMAGE_HEIGHT = 1.2
_ACCENT = {"red": 0.10, "green": 0.45, "blue": 0.91}
TITLE_SLIDE_ID = "slide_000"
# The thank-you slide after the planned slides. Its number is past any a plan
# can have, so its id sorts last and can never collide with a planned slide.
CLOSING_SLIDE_NUMBER = 999
CLOSING_SLIDE_ID = "slide_999"
THANK_YOU = {"en": "Thank you", "ko": "감사합니다", "ja": "ありがとうございました"}


def _emu(inches: float) -> dict[str, Any]:
    return {"magnitude": round(inches * EMU_PER_INCH), "unit": "EMU"}


def _box(page: str, left: float, top: float, width: float, height: float) -> dict:
    return {
        "pageObjectId": page,
        "size": {"width": _emu(width), "height": _emu(height)},
        "transform": {
            "scaleX": 1,
            "scaleY": 1,
            "translateX": round(left * EMU_PER_INCH),
            "translateY": round(top * EMU_PER_INCH),
            "unit": "EMU",
        },
    }


def _font(object_id: str, points: float, **style: Any) -> dict:
    return {
        "updateTextStyle": {
            "objectId": object_id,
            "textRange": {"type": "ALL"},
            "style": {"fontSize": {"magnitude": points, "unit": "PT"}, **style},
            "fields": ",".join(["fontSize", *style]),
        }
    }


def _accent_bar(page: str) -> list[dict]:
    """The coloured bar on the left edge that every slide shares."""
    bar = f"{page}_bar"
    return [
        {
            "createShape": {
                "objectId": bar,
                "shapeType": "RECTANGLE",
                "elementProperties": _box(page, 0, 0, 0.14, 5.625),
            }
        },
        {
            "updateShapeProperties": {
                "objectId": bar,
                "shapeProperties": {
                    "shapeBackgroundFill": {
                        "solidFill": {"color": {"rgbColor": _ACCENT}}
                    },
                    "outline": {"propertyState": "NOT_RENDERED"},
                },
                "fields": "shapeBackgroundFill.solidFill.color,outline.propertyState",
            }
        },
    ]


def mermaid_reference(index: int) -> str:
    """What the slide planner writes to pick the article's N-th Mermaid diagram."""
    return f"mermaid:{index}"


def _packed_diagram(code: str) -> str:
    document = json.dumps({"code": code, "mermaid": {"theme": "default"}})
    packed = base64.urlsafe_b64encode(zlib.compress(document.encode("utf-8"), 9))
    return packed.decode("ascii")


def mermaid_image_url(code: str) -> str | None:
    """Address at which mermaid.ink renders ``code`` as a PNG, or None if too long.

    The diagram source travels inside the address (compressed), so the external
    service sees it; this is used only when BLOG_AGENT_RENDER_MERMAID is on.
    Google Slides accepts image addresses of at most 2,000 characters.
    """
    url = f"{MERMAID_RENDER_URL}{_packed_diagram(code)}?type=png&width=1200"
    return url if len(url) <= MAX_IMAGE_URL_CHARS else None


def mermaid_preview_url(code: str) -> str:
    """Address at which mermaid.ink draws ``code`` as SVG, for showing in a page.

    As with ``mermaid_image_url`` the diagram source is part of the address,
    so the same setting decides whether this may be used.
    """
    return f"{MERMAID_SVG_URL}{_packed_diagram(code)}"


def display_width(text: str) -> int:
    """Rough width of a line: Korean, Japanese and Chinese characters count double."""
    return sum(2 if ord(char) >= 0x2E80 else 1 for char in text)


def title_points(title: str) -> int:
    """Font size that keeps a slide title to at most two lines."""
    width = display_width(title)
    return 30 if width <= 40 else 26 if width <= 62 else 22 if width <= 90 else 18


def body_points(bullets: list[str], beside_image: bool) -> int:
    """Font size that keeps the bullets inside their box."""
    per_line = 34 if beside_image else 72
    lines = sum(1 + display_width(bullet) // per_line for bullet in bullets)
    if lines <= 5 and not beside_image:
        return 20
    return 18 if lines <= 6 else 16 if lines <= 8 else 14


def png_size(data: bytes) -> tuple[int, int] | None:
    """Pixel width and height read from the first bytes of a PNG file."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return (width, height) if width and height else None


def wrapped_lines(text: str, width: float, points: float, em: float = 0.54) -> int:
    """Estimated number of lines ``text`` takes in ``width`` inches of type.

    ``em`` is the average width of a Latin character as a share of the font
    size: about 0.52 for the bold titles and 0.44 for the bullets in decks
    this module created, so the defaults lean towards one line too many.
    Words move to the next line whole; a run without spaces (Korean,
    Japanese) breaks wherever the line is full.
    """
    limit = max(1.0, width * 72 / (points * em))
    lines, used = 1, 0.0
    for word in text.split():
        need = float(display_width(word))
        if used and used + 1 + need > limit:
            lines, used = lines + 1, 0.0
        while need > limit:
            lines, need = lines + 1, need - limit
        used += need + (1 if used else 0)
    return lines


def text_height(
    bullets: list[str], width: float, points: int, line_spacing: float = 1.25
) -> float:
    """Estimated height in inches of the bullets in a box ``width`` inches wide.

    Measured on decks this module created: a line is 1.2 times the font size
    times the line spacing, and the bullet indent and box padding take 0.7
    inches of the width. A tenth is added, because text that outgrows its box
    would run into the image below it.
    """
    lines = sum(wrapped_lines(b, width - 0.7, points, em=0.46) for b in bullets)
    return 0.2 + 1.1 * lines * points * 1.2 * line_spacing / 72


@dataclass(frozen=True)
class SlideLayout:
    """Where the title, the bullets and the image of one slide go, in inches.

    Boxes are (left, top, width, height). ``stacked`` means the bullets sit
    above a wide image instead of beside it, and are set more tightly.
    ``title_right`` is set when a tall image also takes the height of the
    title: the title box must then end there, and ``fallback_image`` is the
    box to use if the title cannot be narrowed.
    """

    text: tuple[float, float, float, float]
    points: int
    title_points: int
    image: tuple[float, float, float, float] | None = None
    stacked: bool = False
    title_right: float | None = None
    fallback_image: tuple[float, float, float, float] | None = None


def _rounded(*values: float) -> tuple[float, float, float, float]:
    left, top, width, height = (round(value, 3) for value in values)
    return left, top, width, height


def _fitting_points(bullets: list[str], width: float, height: float) -> int:
    return next((p for p in (18, 16) if text_height(bullets, width, p) <= height), 14)


def _text_column(bullets: list[str]) -> float:
    """Narrowest column in which the bullets still read well at 16 points.

    An image may be as wide as the page allows, but not at the price of
    bullets that fill their side of the slide from top to bottom.
    """
    return next(
        (w for w in _TEXT_WIDTHS if text_height(bullets, w, 16) <= _TEXT_HEIGHT),
        _TEXT_WIDTHS[-1],
    )


def slide_layout(
    title: str, bullets: list[str], has_image: bool, aspect: float | None = None
) -> SlideLayout:
    """Lay out one slide so that its image is as large as the page allows.

    ``aspect`` is the image's width divided by its height. Three placements
    are compared and the one with the largest image wins:

    - beside the bullets, under the title (the familiar one; it is kept
      unless another makes the image at least 15% larger);
    - under the bullets, up to the full page width, for a wide image;
    - beside the bullets over the full page height, with the title narrowed
      to the left column, for a tall image.

    Without a known shape the image gets the right half and is fitted into it.
    """
    title_size = title_points(title)
    left, full_width = _FULL
    if not has_image:
        return SlideLayout(
            (left, _CONTENT_TOP, full_width, _CONTENT_HEIGHT),
            body_points(bullets, False),
            title_size,
        )
    # A title that wraps to a second line needs the room right under it.
    one_line = wrapped_lines(title, _TITLE_INNER_WIDTH, title_size) == 1
    top = _IMAGE_TOP if one_line else _CONTENT_TOP
    height = _PAGE_BOTTOM - top
    if not aspect or aspect <= 0:
        return SlideLayout(
            (_LEFT[0], _CONTENT_TOP, _LEFT[1], _CONTENT_HEIGHT),
            body_points(bullets, True),
            title_size,
            (_RIGHT[0], top, _RIGHT[1], height),
        )

    right = left + full_width
    widest = full_width - _GAP - _text_column(bullets)
    text_bottom_height = _PAGE_BOTTOM - _CONTENT_TOP

    def beside_boxes(width: float, area_top: float, area_height: float) -> tuple:
        image_height = width / aspect
        return (
            _rounded(
                right - width,
                area_top + (area_height - image_height) / 2,
                width,
                image_height,
            ),
            full_width - _GAP - width,
        )

    beside_width = min(widest, height * aspect)
    beside_image, beside_text_width = beside_boxes(beside_width, top, height)
    beside = SlideLayout(
        _rounded(left, _CONTENT_TOP, beside_text_width, text_bottom_height),
        _fitting_points(bullets, beside_text_width, _TEXT_HEIGHT),
        title_size,
        beside_image,
    )
    candidates: list[tuple[float, SlideLayout]] = []

    # Under the bullets: pays off for an image much wider than it is tall.
    points = 16 if text_height(bullets, full_width, 16, 1.15) <= 1.8 else 14
    stacked_text = text_height(bullets, full_width, points, 1.15)
    room = height - stacked_text - 0.1
    stacked_width = min(full_width, room * aspect)
    if room >= _MIN_STACKED_IMAGE_HEIGHT:
        image_height = stacked_width / aspect
        candidates.append(
            (
                stacked_width,
                SlideLayout(
                    _rounded(left, top, full_width, stacked_text),
                    points,
                    title_size,
                    _rounded(
                        left + (full_width - stacked_width) / 2,
                        top + stacked_text + 0.1 + (room - image_height) / 2,
                        stacked_width,
                        image_height,
                    ),
                    stacked=True,
                ),
            )
        )

    # Over the full page height: pays off for an image taller than it is wide.
    # The title then shares its lines with the image and may wrap once. A
    # slightly smaller image is preferred to a much smaller title.
    panel_height = _PAGE_BOTTOM - _PANEL_TOP
    panel_width = min(widest, panel_height * aspect)
    for points, shrink in (
        (p, s) for p in (30, 26, 22) if p <= title_size for s in (1.0, 0.96, 0.92)
    ):
        width = panel_width * shrink
        title_right = right - width - _GAP
        lines = wrapped_lines(title, title_right - _TITLE_BOX[0] - 0.2, points)
        if lines > 2:
            continue
        panel_image, panel_text_width = beside_boxes(width, _PANEL_TOP, panel_height)
        title_bottom = _TITLE_BOX[1] + 0.1 + lines * points * 1.2 / 72
        text_top = max(_CONTENT_TOP, title_bottom + 0.15)
        text_box_height = _PAGE_BOTTOM - text_top
        candidates.append(
            (
                width,
                SlideLayout(
                    _rounded(left, text_top, panel_text_width, text_box_height),
                    _fitting_points(
                        bullets, panel_text_width, min(_TEXT_HEIGHT, text_box_height)
                    ),
                    points,
                    panel_image,
                    title_right=round(title_right, 3),
                    fallback_image=beside_image,
                ),
            )
        )
        break

    width, layout = max(candidates, key=lambda c: c[0], default=(0.0, beside))
    return layout if width >= 1.15 * beside_width else beside


def title_width_requests(
    layouts: dict[int, SlideLayout], presentation: dict[str, Any]
) -> dict[int, dict]:
    """Per slide number, the request that narrows its title to ``title_right``.

    The title is a placeholder whose size comes from the theme, so the new
    width is worked out from the geometry Google reports for it. A slide
    whose title cannot be read that way is left out; its image then goes
    under the title (``fallback_image``).
    """
    elements = {
        element.get("objectId"): element
        for page in presentation.get("slides", [])
        for element in page.get("pageElements", [])
    }
    requests: dict[int, dict] = {}
    for number, layout in layouts.items():
        element = elements.get(f"{_slide_id(number)}_title") or {}
        width = (element.get("size") or {}).get("width") or {}
        transform = element.get("transform") or {}
        if (
            layout.title_right is None
            or width.get("unit") != "EMU"
            or transform.get("unit") != "EMU"
            or not width.get("magnitude")
            or transform.get("scaleX", 0) <= 0
        ):
            continue
        wanted = layout.title_right * EMU_PER_INCH - transform.get("translateX", 0)
        if (
            wanted < 2 * EMU_PER_INCH
            or wanted > width["magnitude"] * transform["scaleX"]
        ):
            continue  # not a title box this layout was made for
        requests[number] = {
            "updatePageElementTransform": {
                "objectId": element["objectId"],
                "applyMode": "ABSOLUTE",
                "transform": {
                    "scaleX": wanted / width["magnitude"],
                    "scaleY": transform.get("scaleY", 1),
                    "shearX": transform.get("shearX", 0),
                    "shearY": transform.get("shearY", 0),
                    "translateX": transform.get("translateX", 0),
                    "translateY": transform.get("translateY", 0),
                    "unit": "EMU",
                },
            }
        }
    return requests


def slide_layouts(
    plan: SlideDeckPlan,
    images: dict[int, str] | None = None,
    aspects: dict[int, float] | None = None,
) -> dict[int, SlideLayout]:
    """Layout per slide number; ``aspects`` holds the image shapes that are known."""
    images, aspects = images or {}, aspects or {}
    return {
        slide.number: slide_layout(
            slide.title,
            [b.strip() for b in slide.bullets if b.strip()],
            slide.number in images,
            aspects.get(slide.number),
        )
        for slide in plan.slides
    }


def slide_images(plan: SlideDeckPlan, allowed_images: dict[str, str]) -> dict[int, str]:
    """Image address per slide number, keeping only what really is in the article.

    ``allowed_images`` maps what the planner may write (an image URL from the
    article, or a diagram reference such as ``mermaid:2``) to the address to
    fetch. Anything the model did not copy exactly is dropped here, and an
    image is used on one slide only.
    """
    chosen: dict[int, str] = {}
    for slide in plan.slides:
        url = allowed_images.get(slide.image_url.strip())
        if url and url not in chosen.values():
            chosen[slide.number] = url
    return chosen


def slide_requests(
    plan: SlideDeckPlan,
    first_slide_id: str | None,
    images: dict[int, str] | None = None,
    aspects: dict[int, float] | None = None,
) -> list[dict]:
    """Requests that build a title slide, one content slide per planned slide
    and a thank-you slide.

    A content slide has a title sized to fit, an accent bar, and a text box
    with the bullets; ``slide_layout`` decides where the box goes when the
    slide has an image. A new presentation starts with one empty slide; it is
    removed last so the deck is never empty in between.
    """
    layouts = slide_layouts(plan, images, aspects)
    offset = 1 if first_slide_id else 0
    cover_title, cover_subtitle = (
        f"{TITLE_SLIDE_ID}_title",
        f"{TITLE_SLIDE_ID}_subtitle",
    )
    requests: list[dict] = [
        {
            "createSlide": {
                "objectId": TITLE_SLIDE_ID,
                "insertionIndex": offset,
                "slideLayoutReference": {"predefinedLayout": "TITLE"},
                "placeholderIdMappings": [
                    {
                        "layoutPlaceholder": {"type": "CENTERED_TITLE"},
                        "objectId": cover_title,
                    },
                    {
                        "layoutPlaceholder": {"type": "SUBTITLE"},
                        "objectId": cover_subtitle,
                    },
                ],
            }
        },
        {"insertText": {"objectId": cover_title, "text": plan.deck_title}},
        _font(
            cover_title, 36 if display_width(plan.deck_title) <= 60 else 28, bold=True
        ),
        # The cover carries the title only; an empty subtitle box would show
        # "Click to add subtitle" while editing.
        {"deleteObject": {"objectId": cover_subtitle}},
        *_accent_bar(TITLE_SLIDE_ID),
    ]
    for index, slide in enumerate(plan.slides, start=1):
        page = _slide_id(slide.number)
        title, body = f"{page}_title", f"{page}_body"
        layout = layouts[slide.number]
        requests += [
            {
                "createSlide": {
                    "objectId": page,
                    "insertionIndex": index + offset,
                    "slideLayoutReference": {"predefinedLayout": "TITLE_ONLY"},
                    "placeholderIdMappings": [
                        {"layoutPlaceholder": {"type": "TITLE"}, "objectId": title}
                    ],
                }
            },
            {"insertText": {"objectId": title, "text": slide.title}},
            _font(title, layout.title_points, bold=True),
            *_accent_bar(page),
        ]
        bullets = [b.strip() for b in slide.bullets if b.strip()]
        if bullets:
            requests += [
                {
                    "createShape": {
                        "objectId": body,
                        "shapeType": "TEXT_BOX",
                        "elementProperties": _box(page, *layout.text),
                    }
                },
                {"insertText": {"objectId": body, "text": "\n".join(bullets)}},
                {
                    "createParagraphBullets": {
                        "objectId": body,
                        "textRange": {"type": "ALL"},
                        "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                    }
                },
                _font(body, layout.points),
                {
                    "updateParagraphStyle": {
                        "objectId": body,
                        "textRange": {"type": "ALL"},
                        "style": {
                            "lineSpacing": 115 if layout.stacked else 125,
                            "spaceBelow": {
                                "magnitude": 4 if layout.stacked else 8,
                                "unit": "PT",
                            },
                        },
                        "fields": "lineSpacing,spaceBelow",
                    }
                },
            ]
    closing_title, closing_subtitle = (
        f"{CLOSING_SLIDE_ID}_title",
        f"{CLOSING_SLIDE_ID}_subtitle",
    )
    requests += [
        {
            "createSlide": {
                "objectId": CLOSING_SLIDE_ID,
                "insertionIndex": len(plan.slides) + 1 + offset,
                "slideLayoutReference": {"predefinedLayout": "TITLE"},
                "placeholderIdMappings": [
                    {
                        "layoutPlaceholder": {"type": "CENTERED_TITLE"},
                        "objectId": closing_title,
                    },
                    {
                        "layoutPlaceholder": {"type": "SUBTITLE"},
                        "objectId": closing_subtitle,
                    },
                ],
            }
        },
        {"insertText": {"objectId": closing_title, "text": THANK_YOU[plan.language]}},
        _font(closing_title, 36, bold=True),
        {"deleteObject": {"objectId": closing_subtitle}},
        *_accent_bar(CLOSING_SLIDE_ID),
    ]
    if first_slide_id:
        requests.append({"deleteObject": {"objectId": first_slide_id}})
    return requests


def image_request(
    slide_number: int, url: str, box: tuple[float, float, float, float] | None = None
) -> dict:
    """Request that places an article image on a slide.

    ``box`` is (left, top, width, height) in inches, from ``slide_layout``;
    without it the image goes in the right half. Google fetches the URL
    itself, so it must be publicly reachable; the image is scaled to fit the
    box and keeps its proportions.
    """
    page = _slide_id(slide_number)
    if box is None:
        box = (_RIGHT[0], _CONTENT_TOP, _RIGHT[1], _CONTENT_HEIGHT)
    return {
        "createImage": {
            "objectId": f"{page}_image",
            "url": url,
            "elementProperties": _box(page, *box),
        }
    }


_PROBE_SECONDS = 45  # at most this long is spent reading diagram sizes


async def image_aspects(
    client: httpx.AsyncClient, images: dict[int, str]
) -> dict[int, float]:
    """Width / height of the diagrams rendered for the slides, where readable.

    Only addresses this module built itself (mermaid.ink renderings) are
    fetched. An image on any other host keeps the default placement: the
    server does not fetch arbitrary addresses taken from an article.

    The renderer answers 503 when asked too fast, so the diagrams are read one
    after the other and a refused one is tried again after a pause. A diagram
    that still cannot be read, or for which no time is left, is simply missing
    from the result.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _PROBE_SECONDS
    aspects: dict[int, float] = {}
    for number, url in images.items():
        if not url.startswith(MERMAID_RENDER_URL):
            continue
        for attempt in range(1, 4):
            if loop.time() >= deadline:
                return aspects
            head, retry = b"", False
            try:
                async with client.stream("GET", url, timeout=20) as response:
                    retry = response.status_code in (429, 503)
                    if response.status_code == 200:
                        async for chunk in response.aiter_bytes():
                            head += chunk
                            if len(head) >= 24:
                                break
            except httpx.HTTPError:
                break
            if size := png_size(head):
                aspects[number] = size[0] / size[1]
            if not retry:
                break
            await asyncio.sleep(2 * attempt)
    return aspects


def speaker_notes_text(visual_note: str, speaker_script: str) -> str:
    """Speaker notes for one slide: the script, then what to show."""
    notes = speaker_script.strip()
    if visual_note.strip() and visual_note.strip().lower() != "text only":
        notes += f"\n\n[Visual] {visual_note.strip()}"
    return notes


def notes_requests(plan: SlideDeckPlan, presentation: dict[str, Any]) -> list[dict]:
    """Requests that put each slide's script into its speaker notes."""
    notes_shape = {
        page["objectId"]: page.get("slideProperties", {})
        .get("notesPage", {})
        .get("notesProperties", {})
        .get("speakerNotesObjectId")
        for page in presentation.get("slides", [])
    }
    spoken = [
        (TITLE_SLIDE_ID, plan.opening_script.strip()),
        *(
            (
                _slide_id(slide.number),
                speaker_notes_text(slide.visual_note, slide.speaker_script),
            )
            for slide in plan.slides
        ),
        (CLOSING_SLIDE_ID, plan.closing_script.strip()),
    ]
    return [
        {"insertText": {"objectId": notes_shape[page], "text": text}}
        for page, text in spoken
        if notes_shape.get(page) and text
    ]


def granted_account() -> str | None:
    """Address of the account whose Drive receives the decks, if it is known."""
    try:
        account = json.loads(get_secret(OAUTH_SECRET_NAME)).get("account")
    except (MissingSecretError, ValueError, AttributeError):
        return None
    return account if isinstance(account, str) and account else None


def _credentials() -> dict[str, str]:
    try:
        grant = json.loads(get_secret(OAUTH_SECRET_NAME))
        return {
            key: grant[key] for key in ("client_id", "client_secret", "refresh_token")
        }
    except MissingSecretError as exc:
        raise SlidesError(
            "SLIDES_NOT_AUTHORIZED",
            "Google Slides access has not been set up.",
            "Tell the user to run scripts/authorize_google_slides.py once, then "
            "try again. The Markdown slide plan is available meanwhile.",
        ) from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise SlidesError(
            "SLIDES_GRANT_INVALID",
            f"The stored {OAUTH_SECRET_NAME} secret is not in the expected format.",
            "Tell the user to run scripts/authorize_google_slides.py again.",
        ) from exc


async def _access_token(client: httpx.AsyncClient) -> str:
    response = await client.post(
        TOKEN_URL, data={**_credentials(), "grant_type": "refresh_token"}
    )
    if response.status_code != 200:
        # An expired or revoked grant answers 400 invalid_grant.
        raise SlidesError(
            "SLIDES_GRANT_REJECTED",
            f"Google rejected the stored grant (HTTP {response.status_code}). "
            "It may have expired or been revoked.",
            "Tell the user to run scripts/authorize_google_slides.py again.",
        )
    return response.json()["access_token"]


def _checked(response: httpx.Response, doing: str) -> dict[str, Any]:
    if response.status_code >= 400:
        try:
            detail = response.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            detail = response.text
        raise SlidesError(
            "SLIDES_API_ERROR",
            f"Google Slides failed while {doing} (HTTP {response.status_code}): "
            f"{detail[:300]}",
            "Report this to the user. Do not retry more than once.",
        )
    return response.json()


async def create_deck(
    plan: SlideDeckPlan, allowed_images: dict[str, str] | None = None
) -> dict[str, Any]:
    """Create the deck and return its id, URL and what went into it.

    ``allowed_images`` maps the references the planner may use to the image
    addresses found in (or rendered from) the article; only those are placed. An image Google cannot fetch is skipped and counted,
    it does not fail the deck.
    """
    images = slide_images(plan, allowed_images or {})
    async with httpx.AsyncClient(timeout=max(HTTP_TIMEOUT_SECONDS, 60)) as client:
        headers = {"Authorization": f"Bearer {await _access_token(client)}"}
        # Reading the diagrams first also has them rendered before Google
        # fetches them. The token is not sent with these requests.
        aspects = await image_aspects(client, images)
        layouts = slide_layouts(plan, images, aspects)
        created = _checked(
            await client.post(
                SLIDES_API, headers=headers, json={"title": plan.deck_title}
            ),
            "creating the presentation",
        )
        presentation_id = created["presentationId"]
        update_url = f"{SLIDES_API}/{presentation_id}:batchUpdate"
        first = (created.get("slides") or [{}])[0].get("objectId")
        _checked(
            await client.post(
                update_url,
                headers=headers,
                json={"requests": slide_requests(plan, first, images, aspects)},
            ),
            "adding the slides",
        )
        presentation = _checked(
            await client.get(f"{SLIDES_API}/{presentation_id}", headers=headers),
            "reading the new slides",
        )
        # Titles next to a full-height image are narrowed first. If that is
        # refused, those images go under the title instead of over it.
        narrowing = title_width_requests(layouts, presentation)
        if narrowing:
            response = await client.post(
                update_url,
                headers=headers,
                json={"requests": list(narrowing.values())},
            )
            if response.status_code >= 400:
                narrowing = {}
        images_failed = 0
        for number, url in images.items():
            layout = layouts[number]
            box = (
                layout.image
                if layout.title_right is None or number in narrowing
                else layout.fallback_image
            )
            # A freshly rendered diagram can be slow the first time Google
            # fetches it, so a failed image gets one more try.
            for _attempt in range(2):
                response = await client.post(
                    update_url,
                    headers=headers,
                    json={"requests": [image_request(number, url, box)]},
                )
                if response.status_code < 400:
                    break
            images_failed += response.status_code >= 400
        notes = notes_requests(plan, presentation)
        if notes:
            _checked(
                await client.post(
                    update_url, headers=headers, json={"requests": notes}
                ),
                "writing the speaker notes",
            )
    return {
        "presentation_id": presentation_id,
        "url": deck_url(presentation_id),
        "title": plan.deck_title,
        "slide_count": len(plan.slides) + 2,  # with the title and thank-you slides
        "slides_with_notes": len(notes),
        "images_inserted": len(images) - images_failed,
        "images_failed": images_failed,
    }


async def read_presentation(presentation_id: str) -> dict[str, Any]:
    """The deck as Google has it now, including speaker notes. Changes nothing."""
    async with httpx.AsyncClient(timeout=max(HTTP_TIMEOUT_SECONDS, 60)) as client:
        headers = {"Authorization": f"Bearer {await _access_token(client)}"}
        return _checked(
            await client.get(f"{SLIDES_API}/{presentation_id}", headers=headers),
            "reading the deck",
        )


async def update_presentation(presentation_id: str, requests: list[dict]) -> None:
    """Apply ``requests`` to an existing deck. All of them take effect, or none."""
    async with httpx.AsyncClient(timeout=max(HTTP_TIMEOUT_SECONDS, 60)) as client:
        headers = {"Authorization": f"Bearer {await _access_token(client)}"}
        _checked(
            await client.post(
                f"{SLIDES_API}/{presentation_id}:batchUpdate",
                headers=headers,
                json={"requests": requests},
            ),
            "updating the deck",
        )
