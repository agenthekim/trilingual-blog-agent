"""The texts of the page are kept in one catalog per language (WEB-13).

The page, the editor and the previews show nothing of their own that is not
in ``app/static/i18n/<language>.json``. These tests keep the three catalogs in
step with each other, with the scripts and pages that ask for their texts,
and with the server values some of the texts are named after.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

from app import policy, web_ui
from app.pipeline import SLIDE_PHASES
from app.settings import LANGUAGES

STATIC = web_ui.STATIC_DIR
SCRIPTS = ("i18n.js", "app.js", "edit.js")
PAGES = ("index.html", "edit.html")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
# Hangul, kana and Chinese characters: a text in Korean or Japanese.
_KOREAN_OR_JAPANESE = re.compile(
    r"[\u1100-\u11ff\u3040-\u30ff\u3130-\u318f\u4e00-\u9fff\uac00-\ud7a3]"
)
# Names, not sentences: the product, and the title of the editor's tab until
# the script has named the file.
_NAMES_IN_PAGES = {"Trilingual Blog Agent", "Edit"}


def _catalog(language: str) -> dict[str, str]:
    return json.loads((STATIC / "i18n" / f"{language}.json").read_text("utf-8"))


def _script(name: str) -> str:
    """A script as it is checked: without the part WP-13 takes out, which
    reads requests typed in three languages and so holds words of them."""
    text = (STATIC / name).read_text(encoding="utf-8")
    return re.sub(
        r"// i18n:legacy-begin\n.*?// i18n:legacy-end\n", "", text, flags=re.S
    )


def _without_comments(script: str) -> str:
    return re.sub(r"(?m)(^|\s)//.*$", "", script)


class _Texts(HTMLParser):
    """What a page shows before any script runs: the text of each element and
    the attributes a person reads, each with the key it is declared under."""

    VOID = frozenset({"meta", "link", "input", "br", "hr", "img"})
    READ = ("placeholder", "title", "aria-label")

    def __init__(self) -> None:
        super().__init__()
        self.open: list[dict[str, str | None]] = []
        self.shown: list[tuple[str | None, str, str | None]] = []  # key, text, params

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        for name in self.READ:
            if found.get(name) or f"data-i18n-{name}" in found:
                self.shown.append(
                    (
                        found.get(f"data-i18n-{name}"),
                        found.get(name) or "",
                        found.get("data-i18n-params"),
                    )
                )
        if "data-i18n" in found:
            # Declared even when the page holds no text for it yet.
            self.shown.append((found["data-i18n"], "", found.get("data-i18n-params")))
        if tag not in self.VOID:
            self.open.append(found)

    def handle_endtag(self, tag: str) -> None:
        if tag not in self.VOID:
            self.open.pop()

    def handle_data(self, data: str) -> None:
        if data.strip() and self.open:
            here = self.open[-1]
            self.shown.append(
                (here.get("data-i18n"), data.strip(), here.get("data-i18n-params"))
            )


def _shown(page: str) -> list[tuple[str | None, str, str | None]]:
    reader = _Texts()
    reader.feed((STATIC / page).read_text(encoding="utf-8"))
    assert not reader.open, f"{page} has an element that is never closed"
    return reader.shown


def _asked_by_scripts() -> tuple[set[str], set[str]]:
    """Keys the scripts ask for by name, and the beginnings of the keys they
    put together from a server value (``"status." + row.status``)."""
    keys: set[str] = set()
    families: set[str] = set()
    for name in SCRIPTS:
        script = _without_comments(_script(name))
        keys.update(re.findall(r'\b(?:t|textIn)\((?:\w+, )?"([^"]+)"[,)]', script))
        for counted in re.findall(r'\btn\("([^"]+)"', script):
            keys.update({f"{counted}.one", f"{counted}.other"})
        families.update(
            re.findall(r'\b(?:t|textIn)\((?:\w+, )?"([\w.]+\.)" \+', script)
        )
        # Every call is one of the forms above, so a key is never worked out
        # in a way this test cannot follow. i18n.js is where the functions
        # are: it takes its keys from the attributes of the pages, which
        # test_every_text_the_page_asks_for_exists reads itself.
        if name == "i18n.js":
            continue
        for call in re.finditer(r"\b(?:t|tn|textIn)\((.{0,40})", script):
            known = re.match(r'(?:\w+, )?"[\w.]+"', call.group(1))
            assert known, f"{name}: {call.group(0)}"
    return keys, families


def test_every_language_has_the_same_texts() -> None:
    english = _catalog("en")
    assert len(english) > 150
    for language in ("ko", "ja"):
        other = _catalog(language)
        # The same keys, in the same order, so two files can be read side by side.
        assert list(other) == list(english), language
        for key, text in english.items():
            assert isinstance(other[key], str) and other[key].strip(), (language, key)
            assert sorted(_PLACEHOLDER.findall(other[key])) == sorted(
                _PLACEHOLDER.findall(text)
            ), (language, key)
    for key, text in english.items():
        assert isinstance(text, str) and text.strip(), key
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(\.[A-Za-z0-9_]+)*", key), key
    # A text that counts has both forms.
    for key in english:
        if key.endswith(".one"):
            assert key[:-4] + ".other" in english, key
    # A language is named in its own writing, the same in every catalog.
    own = {"lang.en": "English", "lang.ko": "한국어", "lang.ja": "日本語"}
    for language in LANGUAGES:
        assert {key: _catalog(language)[key] for key in own} == own
    # English is English: no text of it was left in another language.
    for key, text in english.items():
        if key not in own:
            assert not _KOREAN_OR_JAPANESE.search(text), key


def test_every_text_the_page_asks_for_exists() -> None:
    english = _catalog("en")
    keys, families = _asked_by_scripts()
    assert len(keys) > 100 and families
    asked = set(keys)

    # Texts written into the pages: the key exists, and what the page holds
    # until the catalogs are read is the English text, or nothing.
    for page in PAGES:
        for key, text, params in _shown(page):
            if key is None:
                continue
            asked.add(key)
            assert key in english, (page, key)
            filled = english[key]
            for name, value in json.loads(params or "{}").items():
                filled = filled.replace("{" + name + "}", str(value))
            assert not _PLACEHOLDER.search(filled), (page, key)
            assert text in ("", filled), (page, key, text)

    # The few words the server writes into a preview come from the same files.
    server = (Path(web_ui.__file__)).read_text(encoding="utf-8")
    asked.update(re.findall(r"page_text\(lang, ['\"](\w+)['\"]\)", server))
    assert {"previewEdit", "previewRaw"} <= asked

    missing = sorted(key for key in asked if key not in english)
    assert not missing, f"asked for but not in en.json: {missing}"

    # Keys put together from a value of the server: every value has its text.
    pipeline = (Path(web_ui.__file__).parent / "pipeline.py").read_text("utf-8")
    steps = set(re.findall(r'"name": "([A-Z][^"]+)"', pipeline))
    assert len(steps) == 10
    expected = {
        "status.": {
            "not_posted",
            "exported_for_manual_post",
            "draft",
            "published",
            "existing_source",
        },
        "step.": {re.sub(r"[^a-z]+", "_", step.lower()) for step in steps},
        "lang.": set(LANGUAGES),
        "tagRule.": set(policy.API_PLATFORMS),
        "prompt.": {str(number) for number in range(6)},
        "slidePhase.": set(SLIDE_PHASES),
    }
    assert families == set(expected)
    for family, values in expected.items():
        have = {key[len(family) :] for key in english if key.startswith(family)}
        assert have == values, family

    # And nothing is kept that nobody asks for.
    unused = sorted(
        key
        for key in english
        if key not in asked and not any(key.startswith(family) for family in families)
    )
    assert not unused, f"in en.json but never asked for: {unused}"


def test_no_sentence_for_people_is_written_in_the_scripts() -> None:
    # 1. No script and no page holds a word of Korean or Japanese.
    for name in (*SCRIPTS, *PAGES):
        text = (
            _script(name)
            if name.endswith(".js")
            else (STATIC / name).read_text("utf-8")
        )
        found = _KOREAN_OR_JAPANESE.findall(text)
        assert not found, f"{name} holds {''.join(found[:20])}"

    # 2. No script holds an English sentence either: no quoted text in a
    # script has three words in a row. Class names, addresses and keys of the
    # catalog do not; a sentence does.
    quoted = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|`(?:[^`\\]|\\.)*`')
    sentence = re.compile(r"[A-Za-z][A-Za-z']*(?: [A-Za-z][A-Za-z']*){2,}")
    for name in SCRIPTS:
        for literal in quoted.findall(_without_comments(_script(name))):
            # Inside ${...} is code, not text.
            words = re.sub(r"\$\{[^}]*\}", "", literal)
            # A style written on an element is not for reading.
            if re.fullmatch(r"(?:[a-z-]+:[^;:]+;? ?)+", words[1:-1]):
                continue
            assert not sentence.search(words), f"{name}: {literal}"

    # 3. What a page shows before its script runs is declared under a key (and
    # equals the English text: test_every_text_the_page_asks_for_exists), or
    # is a name.
    for page in PAGES:
        for key, text, _ in _shown(page):
            if key is None and re.search(r"[A-Za-z]{2}", text):
                assert text in _NAMES_IN_PAGES, (
                    f"{page}: {text!r} is not in the catalog"
                )

    # 4. The part of app.js left out above is still there to be taken out.
    assert "// i18n:legacy-begin" in (STATIC / "app.js").read_text("utf-8")
