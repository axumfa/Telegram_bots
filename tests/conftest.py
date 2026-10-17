import os
import sys
from pathlib import Path

os.environ.update(
    BOT_TOKEN="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    CHAT_ID="1",
    ICAL_URL="https://example.invalid/feed.ics",
    DB_PATH=":memory:",
    MOODLE_TOKEN="",
    ECLASS_USERNAME="",
    ECLASS_PASSWORD="",
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import bot as botmod  # noqa: E402


@pytest.fixture
def b(monkeypatch):
    """bot module with a fresh in-memory DB and a recording send()."""
    monkeypatch.setattr(botmod, "db", botmod.open_db(":memory:"))
    sent = []

    async def fake_send(text):
        sent.append(text)

    monkeypatch.setattr(botmod, "send", fake_send)
    botmod._fails.clear()
    botmod.sent = sent
    return botmod


@pytest.fixture
def ics():
    return (Path(__file__).parent / "sample.ics").read_bytes()
