"""Create a Google Slides deck with speaker notes from a slide plan.

The deck is created in the Drive of the Google account that granted access
once with ``scripts/authorize_google_slides.py``. That grant is stored as the
secret ``GOOGLE_SLIDES_OAUTH`` (client id, client secret, refresh token) and
is limited to the ``drive.file`` scope: the agent can touch only files it
created itself, never the rest of the Drive.

Use a personal or dedicated account for the grant, not a work or school
account: the OAuth app is your own unverified app, and the decks are created
in that account's Drive. The authorization script shows which account granted
access and stores nothing until that is confirmed; the approval request for a
new deck names the account again.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.schemas import SlideDeckPlan
from app.secrets import MissingSecretError, get_secret
from app.settings import HTTP_TIMEOUT_SECONDS

OAUTH_SECRET_NAME = "GOOGLE_SLIDES_OAUTH"
OAUTH_SCOPE = "https://www.googleapis.com/auth/drive.file"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SLIDES_API = "https://slides.googleapis.com/v1/presentations"


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
_LEFT = (0.55, 4.3)  # ... and next to an image
_RIGHT = (5.05, 4.4)  # left edge and width of the image area
_ACCENT = {"red": 0.10, "green": 0.45, "blue": 0.91}
_DARK = {"red": 0.09, "green": 0.11, "blue": 0.16}
_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
TITLE_SLIDE_ID = "slide_000"


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


def slide_images(plan: SlideDeckPlan, allowed_images: set[str]) -> dict[int, str]:
    """Image per slide number, keeping only URLs that really are in the article.

    The planner model proposes ``image_url``; anything it did not copy exactly
    from the article is dropped here, and an image is used on one slide only.
    """
    chosen: dict[int, str] = {}
    for slide in plan.slides:
        url = slide.image_url.strip()
        if url and url in allowed_images and url not in chosen.values():
            chosen[slide.number] = url
    return chosen


def slide_requests(
    plan: SlideDeckPlan,
    first_slide_id: str | None,
    images: dict[int, str] | None = None,
) -> list[dict]:
    """Requests that build a title slide and one content slide per planned slide.

    A content slide has a title sized to fit, an accent bar, and a text box
    with the bullets; the box takes the left half when the slide has an image.
    A new presentation starts with one empty slide; it is removed last so the
    deck is never empty in between.
    """
    images = images or {}
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
        {
            "updatePageProperties": {
                "objectId": TITLE_SLIDE_ID,
                "pageProperties": {
                    "pageBackgroundFill": {"solidFill": {"color": {"rgbColor": _DARK}}}
                },
                "fields": "pageBackgroundFill.solidFill.color",
            }
        },
        {"insertText": {"objectId": cover_title, "text": plan.deck_title}},
        _font(
            cover_title,
            36 if display_width(plan.deck_title) <= 60 else 28,
            bold=True,
            foregroundColor={"opaqueColor": {"rgbColor": _WHITE}},
        ),
        {"insertText": {"objectId": cover_subtitle, "text": plan.audience}},
        _font(
            cover_subtitle, 16, foregroundColor={"opaqueColor": {"rgbColor": _WHITE}}
        ),
    ]
    for index, slide in enumerate(plan.slides, start=1):
        page = _slide_id(slide.number)
        title, body, bar = f"{page}_title", f"{page}_body", f"{page}_bar"
        has_image = slide.number in images
        left, width = _LEFT if has_image else _FULL
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
            _font(title, title_points(slide.title), bold=True),
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
        bullets = [b.strip() for b in slide.bullets if b.strip()]
        if bullets:
            requests += [
                {
                    "createShape": {
                        "objectId": body,
                        "shapeType": "TEXT_BOX",
                        "elementProperties": _box(
                            page, left, _CONTENT_TOP, width, _CONTENT_HEIGHT
                        ),
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
                _font(body, body_points(bullets, has_image)),
                {
                    "updateParagraphStyle": {
                        "objectId": body,
                        "textRange": {"type": "ALL"},
                        "style": {
                            "lineSpacing": 125,
                            "spaceBelow": {"magnitude": 8, "unit": "PT"},
                        },
                        "fields": "lineSpacing,spaceBelow",
                    }
                },
            ]
    if first_slide_id:
        requests.append({"deleteObject": {"objectId": first_slide_id}})
    return requests


def image_request(slide_number: int, url: str) -> dict:
    """Request that places an article image in the right half of a slide.

    Google fetches the URL itself, so it must be publicly reachable; the image
    is scaled to fit the area and keeps its proportions.
    """
    page = _slide_id(slide_number)
    left, width = _RIGHT
    return {
        "createImage": {
            "objectId": f"{page}_image",
            "url": url,
            "elementProperties": _box(page, left, _CONTENT_TOP, width, _CONTENT_HEIGHT),
        }
    }


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
    requests = []
    for slide in plan.slides:
        shape = notes_shape.get(_slide_id(slide.number))
        text = speaker_notes_text(slide.visual_note, slide.speaker_script)
        if shape and text:
            requests.append({"insertText": {"objectId": shape, "text": text}})
    return requests


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
    plan: SlideDeckPlan, allowed_images: set[str] | None = None
) -> dict[str, Any]:
    """Create the deck and return its id, URL and what went into it.

    ``allowed_images`` are the image URLs found in the article; only those may
    be placed on slides. An image Google cannot fetch is skipped and counted,
    it does not fail the deck.
    """
    images = slide_images(plan, allowed_images or set())
    async with httpx.AsyncClient(timeout=max(HTTP_TIMEOUT_SECONDS, 60)) as client:
        headers = {"Authorization": f"Bearer {await _access_token(client)}"}
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
                json={"requests": slide_requests(plan, first, images)},
            ),
            "adding the slides",
        )
        images_failed = 0
        for number, url in images.items():
            response = await client.post(
                update_url,
                headers=headers,
                json={"requests": [image_request(number, url)]},
            )
            images_failed += response.status_code >= 400
        presentation = _checked(
            await client.get(f"{SLIDES_API}/{presentation_id}", headers=headers),
            "reading the new slides",
        )
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
        "slide_count": len(plan.slides) + 1,
        "slides_with_notes": len(notes),
        "images_inserted": len(images) - images_failed,
        "images_failed": images_failed,
    }
