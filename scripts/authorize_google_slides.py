"""Grant the agent access to create Google Slides decks. Run once, by a person.

What it does:
  1. Prints a Google sign-in link. Open it in any browser and sign in with the
     account whose Drive should hold the decks.
  2. After you allow access, the browser is sent to a http://localhost address.
     When this script runs on another machine that page fails to load; that is
     expected. Copy the whole address from the address bar and paste it here.
  3. Stores the resulting grant in Secret Manager as GOOGLE_SLIDES_OAUTH.
     Nothing secret is printed.

The grant uses the drive.file scope only: the agent can see and change files
it created itself, nothing else in the Drive.

Before running: enable the Slides API, and create an OAuth client of type
"Desktop app" in the Google Cloud console; download its JSON file.

    uv run python scripts/authorize_google_slides.py --client-file ~/client_secret.json
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from app.google_slides import OAUTH_SCOPE, OAUTH_SECRET_NAME, TOKEN_URL  # noqa: E402

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REDIRECT_URI = "http://localhost:8765"


def store_secret(project: str, payload: str) -> None:
    from google.api_core.exceptions import AlreadyExists
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{project}"
    try:
        client.create_secret(
            request={
                "parent": parent,
                "secret_id": OAUTH_SECRET_NAME,
                "secret": {"replication": {"automatic": {}}},
            }
        )
    except AlreadyExists:
        pass
    client.add_secret_version(
        request={
            "parent": f"{parent}/secrets/{OAUTH_SECRET_NAME}",
            "payload": {"data": payload.encode("utf-8")},
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--client-file",
        type=Path,
        required=True,
        help="OAuth client JSON downloaded from the console (type: Desktop app).",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("SECRET_MANAGER_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT"),
        help="Project that holds the secret. Default: GOOGLE_CLOUD_PROJECT.",
    )
    args = parser.parse_args()
    if not args.project:
        parser.error("--project is required when GOOGLE_CLOUD_PROJECT is not set")

    config = json.loads(args.client_file.expanduser().read_text(encoding="utf-8"))
    client = config.get("installed")
    if not client:
        print(
            "This client file is not of type 'Desktop app'. Create a Desktop app "
            "OAuth client and download its JSON.",
            file=sys.stderr,
        )
        return 1

    state = secrets.token_urlsafe(16)
    query = urlencode(
        {
            "client_id": client["client_id"],
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": OAUTH_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    print(
        "\n1. Open this link and sign in with the account that should own the decks:\n"
    )
    print(f"{AUTH_URL}?{query}\n")
    print(
        "2. After allowing access the browser goes to a http://localhost:8765 page\n"
        "   that may fail to load. Copy the full address from the address bar.\n"
    )
    # getpass keeps the pasted address, which contains a one-time code, off the screen.
    pasted = getpass.getpass(
        "3. Paste the address here (input is hidden) and press Enter: "
    )
    answer = parse_qs(urlparse(pasted.strip()).query)
    if answer.get("error"):
        print(f"Google returned an error: {answer['error'][0]}", file=sys.stderr)
        return 1
    if answer.get("state", [""])[0] != state or not answer.get("code"):
        print(
            "That address does not belong to this sign-in. Run again.", file=sys.stderr
        )
        return 1

    response = httpx.post(
        TOKEN_URL,
        data={
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code": answer["code"][0],
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
        timeout=30,
    )
    tokens = response.json() if response.status_code == 200 else {}
    if not tokens.get("refresh_token"):
        print(
            f"No grant was issued (HTTP {response.status_code}). Run again and make "
            "sure to allow access.",
            file=sys.stderr,
        )
        return 1

    store_secret(
        args.project,
        json.dumps(
            {
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "refresh_token": tokens["refresh_token"],
            }
        ),
    )
    print(
        f"\nDone. The grant is stored in Secret Manager as {OAUTH_SECRET_NAME} "
        f"(project {args.project}).\nYou can delete the downloaded client file now."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
