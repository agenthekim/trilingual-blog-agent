"""OPS-2: the unit tests need no model, no network and no credential.

The devices are in ``conftest.py``; this file checks that they work.
"""

from __future__ import annotations

import socket

import pytest

from app.env_file import NO_DOTENV, load_env_file
from tests.unit.conftest import OutsideWorldRefused


def test_unit_tests_cannot_reach_the_network_or_a_model(
    no_network_and_no_model, tmp_path, monkeypatch
) -> None:
    attempts = no_network_and_no_model

    # The network: a TCP connection is refused before anything is sent, and
    # the attempt is remembered so that swallowing the error does not hide it.
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp,
        pytest.raises(OutsideWorldRefused, match="OPS-2"),
    ):
        tcp.connect(("192.0.2.1", 443))
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp,
        pytest.raises(OutsideWorldRefused),
    ):
        tcp.connect_ex(("192.0.2.1", 443))
    assert len(attempts) == 2 and "192.0.2.1" in attempts[0]

    # Unix sockets stay usable: the event loop and local helpers need them.
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(tmp_path / "s"))
        listener.listen(1)
        client.connect(str(tmp_path / "s"))
    finally:
        client.close()
        listener.close()
    assert len(attempts) == 2

    # A model: the client the code would call a model with cannot be made.
    from google import genai

    with pytest.raises(OutsideWorldRefused, match="a model"):
        genai.Client()
    assert len(attempts) == 3

    # ``.env``: not read while the tests run, read otherwise.
    env_file = tmp_path / ".env"
    env_file.write_text("BLOG_AGENT_TEST_ONLY_VALUE=from-dotenv\n", encoding="utf-8")
    monkeypatch.delenv("BLOG_AGENT_TEST_ONLY_VALUE", raising=False)
    import os

    assert os.environ[NO_DOTENV] == "1"
    assert load_env_file(env_file) is False
    assert "BLOG_AGENT_TEST_ONLY_VALUE" not in os.environ
    monkeypatch.delenv(NO_DOTENV)
    assert load_env_file(env_file) is True
    assert os.environ.pop("BLOG_AGENT_TEST_ONLY_VALUE") == "from-dotenv"

    # These attempts were the point of the test, not a leak.
    attempts.clear()


def test_the_page_and_the_api_server_read_env_through_the_one_loader() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for module in ("app/web_ui.py", "app/fast_api_app.py"):
        source = (root / module).read_text(encoding="utf-8")
        assert "load_env_file(" in source, module
        assert "load_dotenv" not in source, module
