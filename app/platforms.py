"""HTTP clients for the three blog platforms.

What each platform actually allows (verified against the live services):

* Qiita: full read/write REST API (v2). Posts are created as private drafts.
* DEV Community (dev.to): full read/write REST API (Forem v1). Posts are
  created unpublished.
* Naver Blog: no public write API and no Markdown export. Reading is a
  best-effort extraction from the mobile web page; writing is done by a human
  pasting an exported HTML file.

Every outbound call runs inside a tracing span named after the platform and
operation, and none of them log request or response bodies.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from app import secrets
from app.observability import traced_span
from app.settings import HTTP_TIMEOUT_SECONDS

QIITA_API = "https://qiita.com/api/v2"
DEVTO_API = "https://dev.to/api"
_USER_AGENT = "trilingual-blog-agent/0.1"


class PlatformError(Exception):
    """A platform call failed. ``recovery`` says what can be done about it."""

    def __init__(self, code: str, message: str, recovery: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.recovery = recovery


def _client(headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT, **(headers or {})},
    )


def _raise_for_status(platform: str, response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    status = response.status_code
    if status in (401, 403):
        raise PlatformError(
            f"{platform.upper()}_UNAUTHORIZED",
            f"{platform} rejected the credential (HTTP {status}).",
            "Tell the user the stored credential is invalid or lacks write scope; "
            "do not retry.",
        )
    if status == 404:
        raise PlatformError(
            f"{platform.upper()}_NOT_FOUND",
            f"{platform} returned HTTP 404 for the requested article.",
            "Check the URL or article ID with the user.",
        )
    if status == 429:
        raise PlatformError(
            f"{platform.upper()}_RATE_LIMITED",
            f"{platform} rate limit reached (HTTP 429).",
            "Wait before retrying; tell the user the platform is rate limiting.",
        )
    detail = response.text[:300].replace("\n", " ")
    raise PlatformError(
        f"{platform.upper()}_HTTP_{status}",
        f"{platform} returned HTTP {status}: {detail}",
        "If the message names an invalid field (for example tags or title), fix it "
        "and retry once; otherwise report the error to the user.",
    )


def guess_language(text: str) -> str:
    """Classify text as ko, ja or en from the scripts it uses."""
    sample = text[:4000]
    hangul = len(re.findall(r"[\uac00-\ud7a3]", sample))
    kana = len(re.findall(r"[\u3040-\u30ff]", sample))
    if kana > 20 and kana >= hangul:
        return "ja"
    if hangul > 20:
        return "ko"
    return "en"


# ---------------------------------------------------------------------------
# Qiita
# ---------------------------------------------------------------------------
def parse_qiita_item_id(url: str) -> str:
    match = re.search(r"/(?:items|private)/([0-9a-f]{20})", urlparse(url).path)
    if not match:
        raise PlatformError(
            "QIITA_URL_FORMAT",
            "Not a Qiita article URL.",
            "Ask for a URL of the form https://qiita.com/<user>/items/<id>.",
        )
    return match.group(1)


def _qiita_headers(required: bool) -> dict[str, str]:
    try:
        return {
            "Authorization": f"Bearer {secrets.get_secret(secrets.QIITA_ACCESS_TOKEN)}"
        }
    except secrets.MissingSecretError as exc:
        if required:
            raise PlatformError(
                "QIITA_TOKEN_MISSING",
                str(exc),
                "Tell the user to store a Qiita access token with read_qiita and "
                "write_qiita scopes; do not retry until they confirm.",
            ) from exc
        return {}


async def qiita_get_item(item_id: str) -> dict[str, Any]:
    with traced_span("qiita.get_item", item_id=item_id):
        async with _client(_qiita_headers(required=False)) as client:
            response = await client.get(f"{QIITA_API}/items/{item_id}")
    _raise_for_status("qiita", response)
    return response.json()


async def qiita_save_item(
    *, title: str, body: str, tags: list[str], private: bool, item_id: str | None
) -> dict[str, Any]:
    """Create a Qiita item, or update it when ``item_id`` is given."""
    payload = {
        "title": title,
        "body": body,
        "tags": [{"name": tag, "versions": []} for tag in tags],
        "private": private,
        "tweet": False,
    }
    operation = "qiita.update_item" if item_id else "qiita.create_item"
    with traced_span(operation, private=private, body_chars=len(body)):
        async with _client(_qiita_headers(required=True)) as client:
            if item_id:
                payload.pop("tweet")
                response = await client.patch(
                    f"{QIITA_API}/items/{item_id}", json=payload
                )
            else:
                response = await client.post(f"{QIITA_API}/items", json=payload)
    _raise_for_status("qiita", response)
    return response.json()


# ---------------------------------------------------------------------------
# DEV Community
# ---------------------------------------------------------------------------
def _devto_headers(required: bool) -> dict[str, str]:
    headers = {"Accept": "application/vnd.forem.api-v1+json"}
    try:
        headers["api-key"] = secrets.get_secret(secrets.DEVTO_API_KEY)
    except secrets.MissingSecretError as exc:
        if required:
            raise PlatformError(
                "DEVTO_KEY_MISSING",
                str(exc),
                "Tell the user to store a DEV Community API key; do not retry "
                "until they confirm.",
            ) from exc
    return headers


def parse_devto_path(url: str) -> tuple[str, str]:
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) != 2:
        raise PlatformError(
            "DEVTO_URL_FORMAT",
            "Not a DEV Community article URL.",
            "Ask for a URL of the form https://dev.to/<username>/<slug>.",
        )
    return parts[0], parts[1]


async def devto_get_article(username: str, slug: str) -> dict[str, Any]:
    with traced_span("devto.get_article", slug=slug):
        async with _client(_devto_headers(required=False)) as client:
            response = await client.get(f"{DEVTO_API}/articles/{username}/{slug}")
    _raise_for_status("devto", response)
    return response.json()


async def devto_save_article(
    *, title: str, body: str, tags: list[str], published: bool, article_id: str | None
) -> dict[str, Any]:
    """Create a DEV article, or update it when ``article_id`` is given."""
    payload = {
        "article": {
            "title": title,
            "body_markdown": body,
            "published": published,
            "tags": tags,
        }
    }
    operation = "devto.update_article" if article_id else "devto.create_article"
    with traced_span(operation, published=published, body_chars=len(body)):
        async with _client(_devto_headers(required=True)) as client:
            if article_id:
                response = await client.put(
                    f"{DEVTO_API}/articles/{article_id}", json=payload
                )
            else:
                response = await client.post(f"{DEVTO_API}/articles", json=payload)
    _raise_for_status("devto", response)
    return response.json()


async def devto_find_own_article(article_id: str) -> dict[str, Any] | None:
    """Look an article up among the key owner's posts, including unpublished drafts."""
    with traced_span("devto.list_own_articles"):
        async with _client(_devto_headers(required=True)) as client:
            response = await client.get(
                f"{DEVTO_API}/articles/me/all", params={"per_page": 100}
            )
    _raise_for_status("devto", response)
    for article in response.json():
        if str(article.get("id")) == str(article_id):
            return article
    return None


# ---------------------------------------------------------------------------
# Naver Blog (read-only, best effort)
# ---------------------------------------------------------------------------
def parse_naver_post(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if query.get("blogId") and query.get("logNo"):
        return query["blogId"][0], query["logNo"][0]
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[-1].isdigit():
        return parts[-2], parts[-1]
    raise PlatformError(
        "NAVER_URL_FORMAT",
        "Not a Naver Blog post URL.",
        "Ask for a URL of the form https://blog.naver.com/<blogId>/<postNumber>.",
    )


class _NaverPostParser(HTMLParser):
    """Collect title, paragraphs and code from a Naver SmartEditor mobile page."""

    _BLOCK_CLASSES = ("se-text-paragraph", "se-code-source", "se-section-title")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.blocks: list[tuple[str, str]] = []
        self._stack: list[tuple[str, str | None]] = []
        self._buffer: list[str] = []
        self._kind: str | None = None
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("property") == "og:title":
            self.title = attributes.get("content") or ""
        if tag in ("br",) and self._kind:
            self._buffer.append("\n")
            return
        if tag in ("meta", "img", "input", "link", "hr"):
            return
        classes = attributes.get("class") or ""
        if self._kind is None:
            for name in self._BLOCK_CLASSES:
                if name in classes:
                    self._kind, self._depth, self._buffer = name, 0, []
                    break
        elif self._kind:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._kind is None or tag in ("br", "meta", "img", "input", "link", "hr"):
            return
        if self._depth == 0:
            text = "".join(self._buffer).strip("\n")
            if text.strip():
                self.blocks.append((self._kind, text))
            self._kind = None
        else:
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._kind:
            self._buffer.append(data)


def naver_html_to_markdown(html: str) -> tuple[str, str]:
    """Return (title, markdown) reconstructed from a Naver mobile post page."""
    parser = _NaverPostParser()
    parser.feed(html)
    lines: list[str] = []
    for kind, text in parser.blocks:
        if kind == "se-code-source":
            lines.append(f"```\n{text}\n```")
        elif kind == "se-section-title":
            lines.append(f"## {text.strip()}")
        else:
            lines.append(text.replace("\u200b", "").strip())
    markdown = "\n\n".join(line for line in lines if line)
    return parser.title.strip(), markdown


async def naver_get_post(blog_id: str, log_no: str) -> tuple[str, str]:
    url = f"https://m.blog.naver.com/{blog_id}/{log_no}"
    with traced_span("naver.get_post", log_no=log_no):
        async with _client() as client:
            response = await client.get(url)
    _raise_for_status("naver_blog", response)
    title, markdown = naver_html_to_markdown(response.text)
    if not markdown:
        raise PlatformError(
            "NAVER_EXTRACTION_EMPTY",
            "The post page was fetched but no article text could be extracted.",
            "Ask the user to paste the article text or provide the original .md file.",
        )
    return title, markdown


async def fetch_status_and_title(url: str) -> tuple[int, str | None]:
    """GET a public page and return its HTTP status and <title> / og:title."""
    with traced_span("http.verify_url", host=urlparse(url).hostname or ""):
        async with _client() as client:
            response = await client.get(url)
    title = None
    match = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        response.text,
    ) or re.search(r"<title[^>]*>([^<]+)</title>", response.text)
    if match:
        title = match.group(1).strip()
    return response.status_code, title
