import os
import sys
from pathlib import Path

os.environ.update(
    BOT_TOKEN="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    CHAT_ID="1",
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
    """bot module with a recording send_message."""
    sent = []

    async def fake_send(chat_id, text, **kw):
        sent.append(text)

    monkeypatch.setattr(botmod.bot, "send_message", fake_send)
    botmod.db.execute("DELETE FROM events")
    botmod.db.execute("DELETE FROM reminded")
    botmod.sent = sent
    return botmod
