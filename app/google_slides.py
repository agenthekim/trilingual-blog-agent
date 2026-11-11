"""Create a Google Slides deck with speaker notes from a slide plan.

The deck is created in the Drive of the Google account that granted access
once with ``scripts/authorize_google_slides.py``. That grant is stored as the
secret ``GOOGLE_SLIDES_OAUTH`` (client id, client secret, refresh token) and
is limited to the ``drive.file`` scope: the agent can touch only files it
created itself, never the rest of the Drive.
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


def slide_requests(plan: SlideDeckPlan, first_slide_id: str | None) -> list[dict]:
    """Requests that lay out one title-and-body slide per planned slide.

    A new presentation starts with one empty slide; it is removed last so the
    deck is never empty in between.
    """
    requests: list[dict] = []
    for index, slide in enumerate(plan.slides):
        page = _slide_id(slide.number)
        title, body = f"{page}_title", f"{page}_body"
        requests.append(
            {
                "createSlide": {
                    "objectId": page,
                    "insertionIndex": index + (1 if first_slide_id else 0),
                    "slideLayoutReference": {"predefinedLayout": "TITLE_AND_BODY"},
                    "placeholderIdMappings": [
                        {"layoutPlaceholder": {"type": "TITLE"}, "objectId": title},
                        {"layoutPlaceholder": {"type": "BODY"}, "objectId": body},
                    ],
                }
            }
        )
        requests.append({"insertText": {"objectId": title, "text": slide.title}})
        bullets = "\n".join(b.strip() for b in slide.bullets if b.strip())
        if bullets:
            requests.append({"insertText": {"objectId": body, "text": bullets}})
            requests.append(
                {
                    "createParagraphBullets": {
                        "objectId": body,
                        "textRange": {"type": "ALL"},
                        "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                    }
                }
            )
    if first_slide_id:
        requests.append({"deleteObject": {"objectId": first_slide_id}})
    return requests


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


async def create_deck(plan: SlideDeckPlan) -> dict[str, Any]:
    """Create the deck and return its id, URL and slide count."""
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
                json={"requests": slide_requests(plan, first)},
            ),
            "adding the slides",
        )
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
        "slide_count": len(plan.slides),
        "slides_with_notes": len(notes),
    }
