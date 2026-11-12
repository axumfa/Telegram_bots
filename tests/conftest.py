import os
import sys
from pathlib import Path

os.environ.update(
    BOT_TOKEN="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    CHAT_ID="1",
    DB_PATH=":memory:",
    ECLASS_USERNAME="",
    ECLASS_PASSWORD="",
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import bot as botmod  # noqa: E402


@pytest.fixture
def b(monkeypatch):
    """bot module with a recording send_message."""
    sent, sent_kw = [], []

    async def fake_send(chat_id, text, **kw):
        sent.append(text)
        sent_kw.append(kw)

    monkeypatch.setattr(botmod.bot, "send_message", fake_send)
    botmod.db.execute("DELETE FROM events")
    botmod.db.execute("DELETE FROM reminded")
    botmod.db.execute("DELETE FROM grades")
    botmod.db.execute("DELETE FROM attendance")
    botmod.db.execute("DELETE FROM health")
    botmod.db.execute("DELETE FROM inbox")
    botmod.db.execute("DELETE FROM undated")
    botmod.db.execute("DELETE FROM kv")
    botmod.sent = sent
    botmod.sent_kw = sent_kw
    return botmod
