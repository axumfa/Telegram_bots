import time
from datetime import datetime

from bot import TZ, clean_title, parse_due, parse_status, short_course

COURSE = "Electronic Circuit[202602-ICE2080-001]"


def test_short_course():
    assert short_course(COURSE) == "Electronic Circuit"
    assert short_course("Plain name") == "Plain name"
    long = "The New History of Uzbekistan, Philosophy, Basics of Spirituality[202602-NTS3032-001]"
    assert short_course(long) == "The New History of Uzbekistan…"


def test_clean_title():
    assert clean_title("MCQS-5 (Quiz closes)") == "MCQS-5"
    assert clean_title("Homework 04 ") == "Homework 04"
    assert clean_title("Electronic Circuit[001]-13교시") == "13교시"
    assert clean_title("Team Formation closes") == "Team Formation"


def test_parse_assign():
    assert parse_status("assign", "<td>No submissions have been made yet</td>") == "todo"
    assert parse_status("assign", "<td>Draft (not submitted)</td>") == "todo"
    assert parse_status("assign", "<td>Submitted for grading</td>") == "done"
    assert parse_status("assign", "<p>whatever</p>") == "?"


def test_parse_quiz():
    done = '<table class="quizattemptsummary"><tbody><tr><td>Finished</td></tr></tbody></table>'
    assert parse_status("quiz", done) == "done"
    assert parse_status("quiz", "<p>Attempts allowed: 1</p>") == "todo"


ASSIGN = """<div class="submissionsummarytable"><table><tr><td>Submission status</td><td>No attempt</td></tr>
<tr><td>Due date</td><td>2026-10-18 23:55</td></tr></table></div>"""
QUIZ = '<div class="quizinfo"><p>This quiz opened at 2026-10-12 10:00</p><p>This quiz will close at 2026-10-19 23:59</p></div>'


def test_parse_due():
    ts = parse_due("assign", ASSIGN)
    assert datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M") == "2026-10-18 23:55"
    ts = parse_due("quiz", QUIZ)
    assert datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M") == "2026-10-19 23:59"
    assert parse_due("assign", "<p>Submit on Gradescope</p>") is None
    assert parse_due("quiz", "<p>nothing</p>") is None


def test_line_shows_status_and_link(b):
    ts = int(time.time()) + 3600
    out = b.line(("1@x", "Homework 04 ", COURSE, ts, "https://e/a?id=1", "todo"))
    assert out.startswith("❌ <a href=")
    assert "Electronic Circuit" in out and "ICE2080" not in out


async def test_reminders_batched_and_skip_done(b):
    ts = int(time.time()) + 3600
    for i, st in enumerate(["todo", "todo", "done"]):
        b.db.execute(
            "INSERT INTO events(uid,title,course,start_ts,status) VALUES(?,?,?,?,?)",
            (f"{i}@x", f"HW {i}", COURSE, ts + i, st),
        )
    await b.check_reminders()
    assert len(b.sent) == 1  # one message for both open items
    assert "HW 0" in b.sent[0] and "HW 1" in b.sent[0] and "HW 2" not in b.sent[0]


async def test_new_resources_announced_once(b, monkeypatch):
    rows = [("ubfile-1", "Lecture 1", COURSE, "ubfile", "https://e/1"), ("vod-2", "Lecture 2 <b>", COURSE, "vod", "https://e/2")]
    await b.send_new_resources(rows)
    assert len(b.sent) == 1
    msg = b.sent[0]
    assert "Electronic Circuit" in msg and "📄" in msg and "🎥" in msg
    assert "&lt;b&gt;" in msg  # titles are escaped


def test_menu_matches_handlers(b):
    cmds = {c for c, _ in b.MENU}
    assert cmds == {"upcoming", "today", "week", "resources", "sync", "help"}
    assert all(len(d) <= 256 for _, d in b.MENU)
    assert "<course>" not in b.HELP  # raw angle brackets would break Telegram's HTML parsing
