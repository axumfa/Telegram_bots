import asyncio
import time
from datetime import datetime
from pathlib import Path

import pytest

from bot import (
    TZ,
    clean_title,
    parse_activity_grade,
    parse_attendance,
    parse_due,
    parse_grade_report,
    parse_overview,
    parse_status,
    short_course,
    week_end,
)

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
    ts = int(time.time()) + 1800
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
    assert cmds == {"upcoming", "today", "week", "missed", "done", "resources", "grades", "attendance", "sync", "status", "help"}
    assert all(len(d) <= 256 for _, d in b.MENU)
    assert "<course>" not in b.HELP  # raw angle brackets would break Telegram's HTML parsing


@pytest.mark.parametrize(
    "now, end",
    [
        ("2026-10-17 12:00", "2026-10-26 00:00"),  # Saturday -> end of Sun 25th
        ("2026-10-18 12:00", "2026-10-26 00:00"),  # Sunday -> end of the Sunday 7 days later
        ("2026-10-21 12:00", "2026-11-02 00:00"),  # Wednesday -> end of Sun 1 Nov
    ],
)
def test_week_end(now, end):
    ts = datetime.strptime(now, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp()
    assert datetime.fromtimestamp(week_end(ts), TZ).strftime("%Y-%m-%d %H:%M") == end


def test_week_includes_next_weekend(b):
    now = time.time()
    for uid, off in [("in", 7 * 86400 + 3600), ("out", 40 * 86400)]:
        b.db.execute("INSERT INTO events(uid,title,course,start_ts,status) VALUES(?,?,?,?,?)", (uid, uid, COURSE, now + off, "todo"))
    # whatever weekday the test runs on, a deadline 7d 1h away is inside the window
    assert [r[0] for r in b.upcoming(until=b.week_end(now), hide_done=True)] == ["in"]


async def test_reminder_labels(b):
    now = time.time()
    for uid, off in [("six_days", 6 * 86400), ("five_hours", 5 * 3600)]:
        b.db.execute("INSERT INTO events(uid,title,course,start_ts,status) VALUES(?,?,?,?,?)", (uid, uid, COURSE, now + off, "todo"))
    await b.check_reminders()
    assert any("Due in 7d" in m and "six_days" in m for m in b.sent)
    assert any("Due in 6h" in m and "five_hours" in m for m in b.sent)
    b.sent.clear()
    await b.check_reminders()
    assert b.sent == []  # nothing repeats


# ---------- grades ----------
FIX = Path(__file__).parent / "fixtures"  # real eclass pages with name, id and sesskey scrubbed


def fixture(name):
    return (FIX / name).read_text(encoding="utf-8")


def test_parse_grade_report_real_page():
    rows = parse_grade_report(fixture("grades_os.html"))
    items = [r for r in rows if r[4] == "item"]
    totals = [r for r in rows if r[4] == "total"]
    assert not any("SUM" in r[0] or "Include empty" in r[0] for r in rows)  # category sub-totals are skipped
    assert len(totals) == 1 and totals[0][:4] == ("Course total", None, "100", None)
    graded = {r[0]: r[1:4] for r in items if r[1] is not None}
    assert graded == {
        "Offline Attendance": ("26.00", "26", "100.00 %"),
        "Attendance (10%)": ("10.00", "10", "100.00 %"),
        "[SOC3010-001] Quiz 2 (1%)": ("100.00", "100", "100.00 %"),
        "[SOC3010-001] Quiz 3 (1%)": ("100.00", "100", "100.00 %"),
    }
    assert ("[SOC3010-001] Quiz 1 (1%)", None, "100", None, "item") in rows  # '-' means not graded


def test_parse_grade_report_hidden():
    assert parse_grade_report(fixture("grades_hidden.html")) is None  # "Can not view grades."


def test_parse_overview_real_page():
    ov = parse_overview(fixture("grades_overview.html"))
    assert len(ov) == 14  # the other 16 table rows are empty padding
    assert ov["2603"] == ("Operating System", None)
    assert ov["1907"] == ("Calculus 2", "886.92")


def test_grade_text(b):
    assert b.grade_text("85", "100", "85.00 %") == "85/100 (85.00%)"
    assert b.grade_text("85", None, None) == "85"
    assert b.grade_text(None, "100", None) == "—"


def test_render_grades(b):
    rows = [
        (COURSE, "Course total", "50", "100", "50.00 %", "total"),
        (COURSE, "Quiz 1 <b>", "5", "10", "50.00 %", "item"),
        (COURSE, "Quiz 2", None, "10", None, "item"),
        ("Hidden[001]", "Course total", None, None, None, "total"),
    ]
    short = b.render_grades(rows, detail=False)
    assert "Electronic Circuit</b> · total 50/100 (50.00%) · 1/2 graded" in short
    assert "Hidden</b> · total —" in short
    detail = b.render_grades(rows[:3], detail=True)
    assert "✅ Quiz 1 &lt;b&gt; · 5/10 (50.00%)" in detail  # escaped
    assert "⏳ 1 not graded yet" in detail and "Quiz 2" not in detail


def _scrape(monkeypatch, b, rows):
    monkeypatch.setattr(b, "ECLASS_USERNAME", "u")
    monkeypatch.setattr(b, "ECLASS_PASSWORD", "p")
    monkeypatch.setattr(b, "scrape_grades", lambda: {"1": (COURSE, rows), "2": ("Hidden[001]", [("Course total", None, None, None, "total")])})


def items(*grades):
    return [(f"Quiz {i}", g, "10", None, "item") for i, g in enumerate(grades, 1)] + [("Course total", None, "100", None, "total")]


async def test_first_grade_sync_is_silent_then_new_and_changed_alert_once(b, monkeypatch):
    _scrape(monkeypatch, b, items("5", None))
    await b.sync_grades()
    assert b.sent == []  # first run only records what's there

    _scrape(monkeypatch, b, items("5", "7"))  # Quiz 2 newly graded
    await b.sync_grades()
    assert len(b.sent) == 1 and "Quiz 2" in b.sent[0] and "Quiz 1" not in b.sent[0] and "7/10" in b.sent[0]

    await b.sync_grades()
    assert len(b.sent) == 1  # same grades again: nothing

    _scrape(monkeypatch, b, items("6", "7"))  # Quiz 1 corrected
    await b.sync_grades()
    assert len(b.sent) == 2 and "Quiz 1" in b.sent[1] and "(was 5)" in b.sent[1]


async def test_total_alert_only_when_course_hides_items(b, monkeypatch):
    monkeypatch.setattr(b, "ECLASS_USERNAME", "u")
    monkeypatch.setattr(b, "ECLASS_PASSWORD", "p")
    state = {"1": ("Shown[001]", [("Quiz 1", None, "10", None, "item"), ("Course total", None, "100", None, "total")]),
             "2": ("Hidden[001]", [("Course total", None, None, None, "total")])}
    monkeypatch.setattr(b, "scrape_grades", lambda: state)
    await b.sync_grades()  # first run
    state["1"] = ("Shown[001]", [("Quiz 1", None, "10", None, "item"), ("Course total", "40", "100", None, "total")])
    state["2"] = ("Hidden[001]", [("Course total", "88.5", None, None, "total")])
    await b.sync_grades()
    assert len(b.sent) == 1
    assert "Hidden" in b.sent[0] and "88.5" in b.sent[0] and "Shown" not in b.sent[0]


async def test_failed_course_keeps_old_grades(b, monkeypatch):
    _scrape(monkeypatch, b, items("5", None))
    await b.sync_grades()
    monkeypatch.setattr(b, "scrape_grades", lambda: {})  # every course failed this time
    await b.sync_grades()
    assert b.db.execute("SELECT COUNT(*) FROM grades WHERE uid LIKE '1:%'").fetchone()[0] == 3


async def test_grades_checked_every_third_poll(b, monkeypatch):
    calls = {"events": 0, "news": 0, "attendance": 0, "grades": 0}

    def counter(name):
        async def job():
            calls[name] += 1
        job.__name__ = f"sync_{name}"
        return job

    for name in calls:
        monkeypatch.setattr(b, f"sync_{name}", counter(name))
    sleeps = []

    async def fake_sleep(secs):
        sleeps.append(secs)
        if len(sleeps) == 7:
            raise asyncio.CancelledError

    monkeypatch.setattr(b.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await b.poll_loop()
    assert calls == {"events": 7, "news": 7, "attendance": 7, "grades": 3}  # polls 1, 4 and 7


# ---------- grades from assignment / quiz pages (courses that hide their gradebook) ----------
def test_parse_activity_grade_real_pages():
    assert parse_activity_grade("assign", fixture("assign_graded.html")) == ("63.00", "100.00")
    assert parse_activity_grade("assign", fixture("assign_ungraded.html")) is None  # "Not graded"
    assert parse_activity_grade("quiz", fixture("quiz_attempt.html")) == ("1.00", "1.00")


def test_fixtures_hold_no_grader_name():
    assert "Anon Grader" in fixture("assign_graded.html")  # the 'Graded by' name is scrubbed


def quiz_page(*attempts):
    rows = "".join(f"<tr><td>{state}</td><td>{grade}</td><td>Review</td></tr>" for state, grade in attempts)
    return f'<table class="quizattemptsummary"><thead><tr><th>State</th><th>Grade / 10.00</th><th>Review</th></tr></thead><tbody>{rows}</tbody></table>'


def test_parse_quiz_grade_takes_best_finished_attempt():
    assert parse_activity_grade("quiz", quiz_page(("Finished", "4.50"), ("Finished", "7.25"), ("Finished", "6.00"))) == ("7.25", "10.00")
    assert parse_activity_grade("quiz", quiz_page(("Finished", "3.00"), ("In progress", "-"))) == ("3.00", "10.00")
    assert parse_activity_grade("quiz", quiz_page(("In progress", "-"))) is None
    assert parse_activity_grade("quiz", "<p>No attempts yet</p>") is None


def test_render_grades_without_course_total(b):
    rows = [("Signals[001]", "HW 1", "63", "100", "63.00 %", "item"), ("Signals[001]", "HW 2", None, "100", None, "item")]
    assert b.render_grades(rows, detail=False) == "📊 <b>Signals</b> · 1/2 graded"  # no 'total —' for a missing total
    detail = b.render_grades(rows, detail=True)
    assert "total" not in detail.split("\n")[0] and "✅ HW 1 · 63/100 (63.00%)" in detail
    assert "no course total" in detail


def _fake_eclass(monkeypatch, b, activity_error=None):
    from types import SimpleNamespace

    pages = {
        "overview": "<table></table>",
        "user/index.php?id=1": fixture("grades_os.html"),  # gradebook visible
        "user/index.php?id=2": fixture("grades_hidden.html"),  # "Can not view grades."
        "act/a1": fixture("assign_graded.html"),
        "act/a2": fixture("assign_ungraded.html"),
        "act/q1": fixture("quiz_attempt.html"),
    }

    def web_get(url, **kw):
        if activity_error and url.endswith(activity_error):
            raise ValueError("boom")
        return SimpleNamespace(text=next(v for k, v in pages.items() if k in url))

    monkeypatch.setattr(b, "web_get", web_get)
    monkeypatch.setattr(b, "list_courses", lambda: {"1": "Visible", "2": "Hidden"})
    monkeypatch.setattr(
        b, "course_activities",
        lambda cid, fb: ("Hidden[001]", [("assign", "1", "HW 1 (Assignment closes)", "act/a1"), ("assign", "2", "HW 2", "act/a2"), ("quiz", "3", "Quiz 1", "act/q1")], []),
    )


def test_scrape_grades_reads_activity_pages_for_hidden_gradebook(b, monkeypatch):
    _fake_eclass(monkeypatch, b)
    out = b.scrape_grades()
    assert set(out) == {"1", "2"}
    assert any(r[4] == "total" for r in out["1"][1])  # visible gradebook: unchanged
    name, rows = out["2"]
    assert name == "Hidden[001]"
    assert rows == [
        ("HW 1", "63.00", "100.00", "63.00 %", "item"),
        ("HW 2", None, None, None, "item"),
        ("Quiz 1", "1.00", "1.00", "100.00 %", "item"),
    ]


def test_scrape_grades_skips_whole_course_if_one_activity_fails(b, monkeypatch):
    _fake_eclass(monkeypatch, b, activity_error="a2")
    out = b.scrape_grades()
    assert set(out) == {"1"}  # a half-read course would later alert already-known grades as new


async def test_hidden_course_grade_alert_end_to_end(b, monkeypatch):
    monkeypatch.setattr(b, "ECLASS_USERNAME", "u")
    monkeypatch.setattr(b, "ECLASS_PASSWORD", "p")
    state = {"2": ("Hidden[001]", [("HW 1", None, None, None, "item"), ("HW 2", "5", "10", "50.00 %", "item")])}
    monkeypatch.setattr(b, "scrape_grades", lambda: state)
    await b.sync_grades()
    assert b.sent == []
    state["2"] = ("Hidden[001]", [("HW 1", "9", "10", "90.00 %", "item"), ("HW 2", "5", "10", "50.00 %", "item")])
    await b.sync_grades()
    assert len(b.sent) == 1 and "HW 1" in b.sent[0] and "9/10 (90.00%)" in b.sent[0] and "HW 2" not in b.sent[0]


# ---------- offline attendance ----------
def test_parse_attendance_real_page():
    name, rows = parse_attendance(fixture("attendance_os.html"))
    assert name == "Operating System[202602-SOC3010-001]"
    assert len(rows) == 18
    assert rows[0] == ("2026-09-07", "1Class", "present")
    assert [r[0] for r in rows if r[2] == "absent"] == ["2026-11-02", "2026-11-04"]
    assert sum(r[2] == "present" for r in rows) == 16  # matches the page's own "Attendance : 16"


def test_parse_attendance_unmarked_classes_are_pending():
    _, rows = parse_attendance(fixture("attendance_pending.html"))
    assert len(rows) == 26
    assert sum(r[2] == "present" for r in rows) == 16
    assert sum(r[2] is None for r in rows) == 10  # later weeks: not taken yet


def test_parse_attendance_course_without_offline_attendance():
    name, rows = parse_attendance(fixture("attendance_none.html"))  # "There is no week found."
    assert rows == [] and name


def test_parse_attendance_late_and_other_columns():
    cell = lambda mark: f"<td>{mark}</td>"
    page = ('<table class="attendance_my"><tbody>'
            "<tr><td>2026-09-01</td><td>1Class</td>" + cell("") * 2 + cell("○") + cell("") + "</tr>"
            "<tr><td>2026-09-02</td><td>2Class</td>" + cell("") * 3 + cell("○") + "</tr></tbody></table>")
    assert [r[2] for r in parse_attendance(page)[1]] == ["late", "other"]


def test_fixtures_hold_no_student_identity():
    for f in ("attendance_os.html", "attendance_pending.html", "attendance_none.html"):
        assert "Fullname" not in fixture(f) and "U2410045" not in fixture(f)


def att(*classes):
    """{course id: (name, rows)} with one course 'Operating System' and the given (date, status) classes."""
    return {"1": ("Operating System[001]", [(d, f"{i}Class", st) for i, (d, st) in enumerate(classes, 1)])}


def _scrape_att(monkeypatch, b, state):
    monkeypatch.setattr(b, "ECLASS_USERNAME", "u")
    monkeypatch.setattr(b, "ECLASS_PASSWORD", "p")
    monkeypatch.setattr(b, "scrape_attendance", lambda: state)


def test_render_attendance(b):
    rows = [("Operating System[001]", "2026-11-04", "2Class", "absent"), ("Operating System[001]", "2026-11-02", "1Class", "present"),
            ("Operating System[001]", "2026-11-09", "3Class", None), ("Algorithms[002]", "2026-11-02", "1Class", "present")]
    out = b.render_attendance(rows, detail=False).split("\n")
    assert out == ["🗓 <b>Algorithms</b> · 1/1 attended", "🗓 <b>Operating System</b> · 1/2 attended · ❌ 1 absent · 1 to come"]
    detail = b.render_attendance(rows[:3], detail=True)
    assert detail.index("✅ Mon 02 Nov") < detail.index("❌ Wed 04 Nov")  # chronological
    assert "⏳ next: Mon 09 Nov" in detail


async def test_attendance_first_sync_silent_then_alerts_on_absence_only_once(b, monkeypatch):
    b.db.execute("DELETE FROM attendance")
    _scrape_att(monkeypatch, b, att(("2026-11-02", "present"), ("2026-11-04", None)))
    await b.sync_attendance()
    assert b.sent == []  # first run only records

    _scrape_att(monkeypatch, b, att(("2026-11-02", "present"), ("2026-11-04", "present")))
    await b.sync_attendance()
    assert b.sent == []  # being present is not worth a message

    _scrape_att(monkeypatch, b, att(("2026-11-02", "present"), ("2026-11-04", "absent")))
    await b.sync_attendance()
    assert len(b.sent) == 1 and "Operating System" in b.sent[0] and "Wed 04 Nov" in b.sent[0] and "absent" in b.sent[0]

    await b.sync_attendance()
    assert len(b.sent) == 1  # same state again: nothing


async def test_attendance_only_stores_courses_with_classes_and_keeps_failed_ones(b, monkeypatch):
    b.db.execute("DELETE FROM attendance")
    state = att(("2026-11-02", "present"))
    state["2"] = ("Online course[001]", [])  # menu link but "no week found"
    _scrape_att(monkeypatch, b, state)
    await b.sync_attendance()
    assert {r[0] for r in b.db.execute("SELECT course FROM attendance")} == {"Operating System[001]"}
    monkeypatch.setattr(b, "scrape_attendance", lambda: {})  # every course failed this time
    await b.sync_attendance()
    assert b.db.execute("SELECT COUNT(*) FROM attendance").fetchone()[0] == 1


# ---------- health ----------
def failing(exc):
    async def job():
        raise exc
    job.__name__ = "sync_grades"
    return job


def working():
    async def job():
        pass
    job.__name__ = "sync_grades"
    return job


async def test_transient_failures_alert_once_after_three_then_recovery(b):
    job = failing(ValueError("boom"))
    for _ in range(2):
        assert isinstance(await b.run_job(job), ValueError)
    assert b.sent == []  # a hiccup or two is not worth a message
    await b.run_job(job)
    assert len(b.sent) == 1 and "grades" in b.sent[0] and "boom" in b.sent[0]
    await b.run_job(job)
    await b.run_job(job)
    assert len(b.sent) == 1  # still failing: no repeat
    assert await b.run_job(working()) is None
    assert len(b.sent) == 2 and "updating again" in b.sent[1]
    await b.run_job(working())
    assert len(b.sent) == 2


async def test_login_style_error_alerts_at_once(b):
    await b.run_job(failing(RuntimeError("eclass login failed")))
    assert len(b.sent) == 1 and "login failed" in b.sent[0]


async def test_run_job_without_alert_stays_quiet_but_counts(b):
    for _ in range(4):
        await b.run_job(failing(RuntimeError("x")), alert=False)
    assert b.sent == []
    await b.run_job(failing(RuntimeError("x")))  # the next background failure still alerts
    assert len(b.sent) == 1


async def test_telegram_failure_does_not_escape_run_job(b, monkeypatch):
    async def broken(*a, **k):
        raise OSError("telegram down")
    monkeypatch.setattr(b.bot, "send_message", broken)
    assert isinstance(await b.run_job(failing(RuntimeError("x"))), RuntimeError)


async def test_status_shows_ok_failing_and_not_run(b):
    await b.run_job(working())
    out = b.render_status()
    assert "✅ grades · last ok just now" in out and "⏳ deadlines · not run yet" in out
    await b.run_job(failing(ValueError("boom <b>")), alert=False)
    out = b.render_status()
    assert "❌ grades · failing ×1" in out and "boom &lt;b&gt;" in out


def test_empty_home_page_is_an_error(b, monkeypatch):
    class R:
        text = "<html><body>nothing here</body></html>"
    monkeypatch.setattr(b, "web_get", lambda url, **kw: R)
    with pytest.raises(RuntimeError, match="no courses"):
        b.list_courses()


# ---------- missed deadlines ----------
def add_event(b, uid, off_days, status, kind="assign"):
    ts = time.time() + off_days * 86400
    b.db.execute("INSERT INTO events(uid,title,course,start_ts,status,kind) VALUES(?,?,?,?,?,?)", (uid, uid, COURSE, ts, status, kind))


def test_missed_lists_recent_unsubmitted_newest_first(b):
    add_event(b, "old", -40, "todo")  # beyond the look-back
    add_event(b, "a", -10, "todo")
    add_event(b, "b", -2, "todo")
    add_event(b, "done", -3, "done")
    add_event(b, "unknown", -3, "?")  # can't tell: not claimed as missed
    add_event(b, "future", 2, "todo")
    assert [r[0] for r in b.missed()] == ["b", "a"]


def _stub_site(monkeypatch, b, opened):
    monkeypatch.setattr(b, "list_courses", lambda: {"1": "C"})
    monkeypatch.setattr(b, "course_activities", lambda cid, name: ("C", [("assign", "9", "HW", "https://e/9")], []))
    monkeypatch.setattr(b, "web_get", lambda url, **kw: opened.append(url) or type("R", (), {"text": ASSIGN})())


def test_recently_passed_unsubmitted_items_are_still_rechecked(b, monkeypatch):
    opened = []
    _stub_site(monkeypatch, b, opened)
    now = time.time()
    b.scrape_courses({"assign-9": (now - 3600, "todo")})  # due an hour ago: a late submission may have happened
    assert opened == ["https://e/9"]
    opened.clear()
    b.scrape_courses({"assign-9": (now - 5 * 86400, "todo")})  # long past: left alone
    b.scrape_courses({"assign-9": (now + 3600, "done")})  # submitted: left alone
    assert opened == []


# ---------- /done and mute buttons ----------
class FakeQuery:
    def __init__(self, data, markup):
        self.data, self.answers = data, []
        self.message = type("M", (), {"chat": type("C", (), {"id": 1})(), "reply_markup": markup})()
        self.edited = None

        async def edit(reply_markup):
            self.edited = reply_markup

        self.message.edit_reply_markup = edit

    async def answer(self, text=None):
        self.answers.append(text)


def buttons(markup):
    return [[(x.text, x.callback_data) for x in row] for row in markup.inline_keyboard]


async def test_reminder_comes_with_buttons_and_done_override_wins(b):
    add_event(b, "assign-1", 0.02, "?")  # ~30 min away, status unknown
    await b.check_reminders()
    kb = buttons(b.sent_kw[0]["reply_markup"])
    assert kb == [[("✅ assign-1", "done:assign-1"), ("🔕 mute", "mute:assign-1")]]

    q = FakeQuery("done:assign-1", b.sent_kw[0]["reply_markup"])
    await b.on_action(q)
    assert q.answers == ["Marked done ✅"]
    assert buttons(q.edited) == [[("↩️ undo · ✅ assign-1", "undo:assign-1")]]
    row = b.db.execute(f"SELECT {b.STATUS} FROM events WHERE uid='assign-1'").fetchone()
    assert row == ("done",)
    assert b.upcoming(hide_done=True) == []  # hidden from /today and /week
    assert b.missed() == []

    q = FakeQuery("undo:assign-1", q.edited)
    await b.on_action(q)
    assert buttons(q.edited)[0][0][1] == "done:assign-1"  # buttons are back
    assert len(b.upcoming(hide_done=True)) == 1


async def test_pressing_one_button_leaves_the_other_items_alone(b):
    for i in (1, 2):
        add_event(b, f"assign-{i}", 0.02, "todo")
    rows = b.upcoming(hide_done=True)
    q = FakeQuery("mute:assign-2", b.action_keyboard(rows))
    await b.on_action(q)
    kb = buttons(q.edited)
    assert kb[0][0][1] == "done:assign-1" and kb[1] == [("↩️ undo · 🔕 assign-2", "undo:assign-2")]


async def test_muted_item_gets_no_reminders_but_stays_listed(b):
    add_event(b, "assign-1", 0.02, "todo")
    b.db.execute("UPDATE events SET override='mute' WHERE uid='assign-1'")
    await b.check_reminders()
    assert b.sent == []
    assert len(b.upcoming(hide_done=True)) == 1


async def test_override_survives_a_resync_and_unknown_item_is_handled(b, monkeypatch):
    monkeypatch.setattr(b, "ECLASS_USERNAME", "u")
    monkeypatch.setattr(b, "ECLASS_PASSWORD", "p")
    ts = int(time.time()) + 86400
    monkeypatch.setattr(b, "scrape_courses", lambda known: ([("assign-1", "HW", COURSE, ts, "u", "assign", "todo")], []))
    await b.sync_events()
    b.db.execute("UPDATE events SET override='done' WHERE uid='assign-1'")
    await b.sync_events()  # the scraper still says todo
    assert b.db.execute("SELECT override FROM events").fetchone() == ("done",)
    q = FakeQuery("done:nope", None)
    await b.on_action(q)
    assert q.answers == ["That item is gone"] and q.edited is None


def test_upcoming_match_filters_course_or_title(b):
    add_event(b, "assign-1", 1, "todo")
    add_event(b, "quiz-2", 1, "todo")
    assert [r[0] for r in b.upcoming(match="circuit")] == ["assign-1", "quiz-2"]  # course name
    assert [r[0] for r in b.upcoming(match="quiz-2")] == ["quiz-2"]
