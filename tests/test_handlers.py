"""Run the real aiogram dispatcher against a stubbed Bot session (no network)."""
from datetime import datetime

import pytest
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User

NOW = 1_800_000_000


class Recorder:
    def __init__(self):
        self.out = []


@pytest.fixture
def feed(b, monkeypatch):
    out = []

    async def fake_call(self, bot, method, timeout=None):
        if isinstance(method, SendMessage):
            out.append(method.text)
            return Message(message_id=1, date=datetime.now(), chat=Chat(id=method.chat_id, type="private"))
        raise AssertionError(method)

    from aiogram.client.session.aiohttp import AiohttpSession

    monkeypatch.setattr(AiohttpSession, "make_request", fake_call)
    n = [0]

    async def go(text, chat_id=1):
        n[0] += 1
        msg = Message(
            message_id=n[0], date=datetime.now(), chat=Chat(id=chat_id, type="private"),
            from_user=User(id=chat_id, is_bot=False, first_name="u"), text=text,
            entities=[{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}],
        )
        await b.dp.feed_update(b.bot, Update(update_id=n[0], message=msg))
        return list(out)

    go.out = out
    return go


async def test_help_and_ignores_strangers(feed):
    assert "/upcoming" in (await feed("/help"))[-1]
    n = len(feed.out)
    await feed("/help", chat_id=999)
    assert len(feed.out) == n


async def test_upcoming_shows_marks(b, feed):
    now = datetime.now().timestamp()
    b.db.execute("INSERT INTO events(uid,title,course,start_ts,submitted) VALUES('1@x','Done','C',?,1)", (now + 3600,))
    b.db.execute("INSERT INTO events(uid,title,course,start_ts,submitted) VALUES('2@x','Todo','C',?,0)", (now + 7200,))
    b.db.execute("INSERT INTO events(uid,title,course,start_ts) VALUES('3@x','Quiz','C',?)", (now + 9000,))
    text = (await feed("/upcoming"))[-1]
    assert "✅ <b>Done" in text and "❌ <b>Todo" in text and "• <b>Quiz" in text


async def test_courses_mute_unmute(b, feed):
    for n in ("CSE3101-001", "PHY1001"):
        b.db.execute("INSERT INTO courses VALUES(?)", (n,))
    assert "🔔 CSE3101-001" in (await feed("/courses"))[-1]
    assert "Muted" in (await feed("/mute cse3101"))[-1]
    assert "🔕 CSE3101-001" in (await feed("/courses"))[-1]
    assert "Unmuted" in (await feed("/unmute CSE3101-001"))[-1]
    assert "🔔 CSE3101-001" in (await feed("/courses"))[-1]
    assert "No course" in (await feed("/mute nothing"))[-1]
    assert "Usage" in (await feed("/mute"))[-1]


async def test_sync_command_reports_failure(b, feed, monkeypatch):
    async def bad():
        raise b.FeedError("HTTP 404")

    monkeypatch.setattr(b, "JOBS", [("calendar", bad)])
    text = (await feed("/sync"))[-1]
    assert "Sync problems" in text and "404" in text
