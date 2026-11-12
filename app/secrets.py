"""Credential lookup. No credential is ever written in source or committed.

Resolution order for a secret named ``NAME``:

1. Environment variable ``NAME``. In deployment this is how Agent Runtime
   injects Secret Manager values (``agents-cli deploy --secrets``).
2. Secret Manager ``projects/<project>/secrets/NAME/versions/latest``, read
   with the caller's Application Default Credentials. This is the normal path
   for local development.
3. A dotenv-style file whose path is given by ``BLOG_AGENT_LOCAL_SECRETS_FILE``.
   The file lives outside the repository; only its path is configured here.

If none of these has the secret, ``MissingSecretError`` is raised with an
explanation the calling tool can pass back to the model.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

from app.observability import log_event

QIITA_ACCESS_TOKEN = "QIITA_ACCESS_TOKEN"
DEVTO_API_KEY = "DEVTO_API_KEY"
GOOGLE_SLIDES_OAUTH = "GOOGLE_SLIDES_OAUTH"


class MissingSecretError(LookupError):
    """Raised when a credential cannot be found in any configured source."""


def _secret_project() -> str | None:
    return os.environ.get("SECRET_MANAGER_PROJECT") or os.environ.get(
        "GOOGLE_CLOUD_PROJECT"
    )


def _from_secret_manager(name: str) -> str | None:
    project = _secret_project()
    if not project:
        return None
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(
            request={"name": f"projects/{project}/secrets/{name}/versions/latest"}
        )
        return response.payload.data.decode("utf-8").strip()
    except Exception as exc:
        # The exception text can contain the resource path but never the value.
        log_event(
            "Secret Manager lookup failed",
            event_phase="SECRET",
            secret_name=name,
            error_type=type(exc).__name__,
        )
        return None


def _from_local_file(name: str) -> str | None:
    path = os.environ.get("BLOG_AGENT_LOCAL_SECRETS_FILE")
    if not path:
        return None
    file = Path(path).expanduser()
    if not file.is_file():
        return None
    for line in file.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == name:
            return value.strip().strip("\"'")
    return None


@functools.lru_cache(maxsize=16)
def get_secret(name: str) -> str:
    """Return the credential called ``name`` or raise ``MissingSecretError``."""
    value = os.environ.get(name) or _from_secret_manager(name) or _from_local_file(name)
    if not value:
        raise MissingSecretError(
            f"Credential {name} is not configured. Store it in Secret Manager as "
            f"'{name}' (or inject it as an environment variable at deploy time)."
        )
    return value
