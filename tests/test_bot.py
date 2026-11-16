import asyncio
import time
from datetime import date, datetime, time as dtime
from pathlib import Path

import pytest

from bot import (
    TZ,
    clean_title,
    parse_activity_grade,
    parse_attendance,
    parse_messages,
    parse_notifications,
    parse_notices,
    parse_due,
    parse_grade_report,
    parse_overview,
    parse_status,
    parse_syllabus,
    parse_week_ranges,
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
    assert cmds == {"upcoming", "today", "week", "missed", "nodeadline", "done", "resources", "grades", "attendance", "inbox", "exams", "sync", "status", "help"}
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
    calls = {"events": 0, "attendance": 0, "inbox": 0, "exams": 0, "grades": 0}

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
    assert calls == {"events": 7, "attendance": 7, "inbox": 7, "exams": 1, "grades": 3}  # exams on poll 1 only  # polls 1, 4 and 7


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


# ---------- quiet vs. loud messages ----------
async def test_low_urgency_messages_are_silent_and_urgent_ones_are_not(b, monkeypatch):
    await b.send_new_resources([("ubfile-1", "L1", COURSE, "ubfile", "https://e/1")])
    assert b.sent_kw[-1]["disable_notification"] is True

    b.db.execute("DELETE FROM grades")
    state = {"1": ("C[001]", [("Quiz 1", None, "10", None, "item")])}
    monkeypatch.setattr(b, "ECLASS_USERNAME", "u")
    monkeypatch.setattr(b, "ECLASS_PASSWORD", "p")
    monkeypatch.setattr(b, "scrape_grades", lambda: state)
    await b.sync_grades()
    state["1"] = ("C[001]", [("Quiz 1", "9", "10", None, "item")])
    await b.sync_grades()
    assert "New grades" in b.sent[-1] and b.sent_kw[-1]["disable_notification"] is True

    await b.run_job(failing(RuntimeError("x")))  # a failing check is urgent
    assert b.sent_kw[-1]["disable_notification"] is False
    await b.run_job(working())
    assert "updating again" in b.sent[-1] and b.sent_kw[-1]["disable_notification"] is True


async def test_new_deadline_is_silent_only_when_far_away(b, monkeypatch):
    monkeypatch.setattr(b, "ECLASS_USERNAME", "u")
    monkeypatch.setattr(b, "ECLASS_PASSWORD", "p")
    now = int(time.time())
    b.db.execute("INSERT INTO events(uid,title,course,start_ts,status) VALUES('assign-0','seed',?,?,'todo')", (COURSE, now + 99))  # not a first run
    found = [("assign-1", "Soon", COURSE, now + 86400, "u", "assign", "todo"), ("assign-2", "Later", COURSE, now + 10 * 86400, "u", "assign", "todo")]
    monkeypatch.setattr(b, "scrape_courses", lambda known: (found, []))
    await b.sync_events()
    assert [kw["disable_notification"] for kw in b.sent_kw] == [False, True]  # Soon pings, Later doesn't


def test_like_takes_wildcards_literally(b):
    assert b.like(" 50% off_x ") == "%50\\% off\\_x%"
    for title, word, hit in [("100% done", "100%", True), ("1000 done", "100%", False), ("a_b", "a_b", True), ("axb", "a_b", False)]:
        row = b.db.execute("SELECT ? LIKE ? ESCAPE '\\'", (title, b.like(word))).fetchone()[0]
        assert bool(row) is hit


# ---------- page cache ----------
class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        return type("R", (), {"url": url, "text": f"page {url}", "ok": True})()


async def test_pages_are_fetched_once_per_cycle_and_fresh_the_next_time(b, monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(b, "_web", fake)
    seen = []

    def job(name):
        async def run():
            seen.append(await asyncio.to_thread(b.web_get, "https://e/home"))  # like the scrapers: in a thread
            await asyncio.to_thread(b.web_get, "https://e/c/1")
        run.__name__ = name
        return run

    await b.run_cycle([job("sync_events"), job("sync_grades"), job("sync_attendance")])
    assert fake.calls == ["https://e/home", "https://e/c/1"]  # not 6 requests
    assert seen[0] is seen[1] is seen[2]
    await b.run_cycle([job("sync_events")])
    assert len(fake.calls) == 4  # the next cycle starts empty: nothing is ever stale
    await asyncio.to_thread(b.web_get, "https://e/home")
    assert len(fake.calls) == 5  # outside a cycle there is no cache


async def test_cycles_run_one_at_a_time_each_with_its_own_cache(b, monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(b, "_web", fake)
    order, gate = [], asyncio.Event()

    async def slow():
        await asyncio.to_thread(b.web_get, "https://e/x")
        order.append("slow-start")
        await gate.wait()
        order.append("slow-end")
    slow.__name__ = "sync_events"

    async def quick():
        order.append("quick-start")
        await asyncio.to_thread(b.web_get, "https://e/x")
    quick.__name__ = "sync_grades"

    first = asyncio.create_task(b.run_cycle([slow]))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(b.run_cycle([quick], alert=False))  # like /sync while a poll is running
    await asyncio.sleep(0.05)
    assert order == ["slow-start"] and not second.done()  # it waits its turn
    gate.set()
    await asyncio.gather(first, second)
    assert order == ["slow-start", "slow-end", "quick-start"]
    assert fake.calls == ["https://e/x", "https://e/x"]  # the second cycle really re-read the page


# ---------- daily digest ----------
def at(hhmm, day="2026-11-12"):
    return datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


def add_at(b, uid, when, status="todo", override=None):
    b.db.execute("INSERT INTO events(uid,title,course,start_ts,status,override) VALUES(?,?,?,?,?,?)",
                 (uid, uid, COURSE, when.timestamp(), status, override))


async def test_digest_is_off_by_default(b):
    assert b.DIGEST_AT is None
    add_at(b, "assign-1", at("20:00"))
    await b.check_digest(now=at("08:01"))
    assert b.sent == []


async def test_digest_lists_today_and_tomorrow_only_unfinished_and_unmuted(b, monkeypatch):
    monkeypatch.setattr(b, "DIGEST_AT", dtime(8, 0))
    add_at(b, "today-todo", at("20:00"))
    add_at(b, "tomorrow-unknown", at("10:00", "2026-11-13"), "?")
    add_at(b, "done", at("21:00"), "done")
    add_at(b, "marked-done", at("21:00"), "todo", "done")
    add_at(b, "muted", at("21:00"), "todo", "mute")
    add_at(b, "later", at("10:00", "2026-11-14"))  # day after tomorrow
    add_at(b, "earlier-today", at("07:00"))  # already passed this morning
    await b.check_digest(now=at("08:01"))
    assert len(b.sent) == 1
    msg = b.sent[0]
    assert "today-todo" in msg and "tomorrow-unknown" in msg
    for gone in ("done", "marked-done", "muted", "later", "earlier-today"):
        assert gone not in msg.replace("Due today and tomorrow", "")
    assert msg.index("Thu 12 Nov") < msg.index("Fri 13 Nov")


async def test_digest_sent_once_a_day_and_not_when_nothing_is_due(b, monkeypatch):
    monkeypatch.setattr(b, "DIGEST_AT", dtime(8, 0))
    b.db.execute("DELETE FROM kv")
    await b.check_digest(now=at("08:01"))
    assert b.sent == []  # nothing due: silence
    add_at(b, "assign-1", at("20:00"))
    await b.check_digest(now=at("08:06"))
    assert b.sent == []  # today's decision was already made
    b.db.execute("UPDATE events SET start_ts=? WHERE uid='assign-1'", (at("20:00", "2026-11-13").timestamp(),))
    await b.check_digest(now=at("08:01", "2026-11-13"))  # next morning: a new decision
    assert len(b.sent) == 1 and "assign-1" in b.sent[0]
    await b.check_digest(now=at("08:06", "2026-11-13"))
    assert len(b.sent) == 1  # once a day


async def test_digest_window_and_retry_after_a_failed_send(b, monkeypatch):
    monkeypatch.setattr(b, "DIGEST_AT", dtime(8, 0))
    b.db.execute("DELETE FROM kv")
    add_at(b, "assign-1", at("20:00"))
    await b.check_digest(now=at("07:55"))  # too early
    await b.check_digest(now=at("11:30"))  # bot started too late for a morning summary
    assert b.sent == []

    real = b.bot.send_message

    async def broken(*a, **k):
        raise OSError("telegram down")

    monkeypatch.setattr(b.bot, "send_message", broken)
    with pytest.raises(OSError):
        await b.check_digest(now=at("08:01"))
    monkeypatch.setattr(b.bot, "send_message", real)
    await b.check_digest(now=at("08:06"))  # the next tick tries again
    assert len(b.sent) == 1


def test_status_hides_digest_when_off(b):
    assert "daily digest" not in b.render_status()


# ---------- inbox: messages and notices ----------
def test_parse_messages_real_page_classifies_each_kind():
    rows = parse_messages(fixture("inbox_messages.html"))
    assert [r[1] for r in rows] == ["feedback", "announcement", "urgent", "message", "message"]
    feedback, announcement, urgent = rows[0], rows[1], rows[2]
    assert feedback[2:6] == ("Computer Algorithm", "Anon Teacher 1", "2026-10-14 10:30", "Assignment#2")
    assert feedback[6] == "https://eclass.inha.ac.kr/mod/assign/view.php?id=71855" and feedback[7] == ""
    assert announcement[2] == "Engineering Communications" and announcement[5] == "Attendance Policy"
    assert announcement[6].endswith("discuss.php?d=2700#p6231") and announcement[7].startswith("UPDATED: October 3, 2026 Dear students")
    assert len(announcement[7]) <= 220 and "---" not in announcement[7]
    assert "campus is closed" in urgent[5] and urgent[2] == "Computer Architecture"  # '(Course[code]) text' form
    assert len({r[0] for r in rows}) == 5  # every message has its own id, even from the same sender


def test_parse_messages_not_the_inbox():
    assert parse_messages("<html><body>Please log in</body></html>") is None


def test_parse_notices_real_page():
    rows = parse_notices(fixture("inbox_notices.html"))
    assert [r[0] for r in rows] == ["notice:7622", "notice:7617", "notice:7612"]
    assert rows[0][1:6] == ("announcement", "Electronic Circuit[202602-ICE2080-001]", "Anon Professor", "2026-09-12", "Telegram Channel")
    assert rows[0][6].endswith("bwid=7622")
    assert parse_notices("<p>nope</p>") is None


@pytest.mark.parametrize(
    "text, kind",
    [
        ("Please come to my office on Wed", "message"),
        ("Class is cancelled tomorrow", "urgent"),
        ("The exam is postponed to Friday", "urgent"),
        ("Don't come to campus", "urgent"),
        ("내일 수업은 휴강입니다", "urgent"),
        ("X -> Forums -> Announcements -> Hello", "announcement"),
        ("X -> Forums -> General discussion -> Hi", "message"),
        ("A has posted some feedback on your\nassignment submission for 'HW'", "feedback"),
    ],
)
def test_classify_message(text, kind):
    assert __import__("bot").classify_message(text) == kind


def test_fixtures_hold_no_teacher_names():
    for f in ("inbox_messages.html", "inbox_notices.html"):
        t = fixture(f)
        assert "profile.php" not in t and "sesskey" not in t.replace("sesskey=X", "")
        assert not any(n in t for n in ("Munisa", "Khankeldiyev", "Chongkoo", "June Sup"))


def _scrape_inbox(monkeypatch, b, state):
    monkeypatch.setattr(b, "ECLASS_USERNAME", "u")
    monkeypatch.setattr(b, "ECLASS_PASSWORD", "p")
    monkeypatch.setattr(b, "scrape_inbox", lambda: state)


def inb(uid, kind, title="T", course="Operating System[001]"):
    return (uid, kind, course, "Someone", "2026-11-10 10:00", title, "https://e/x", "preview text")


@pytest.fixture
def clean_inbox(b):
    b.db.execute("DELETE FROM inbox")
    b.db.execute("DELETE FROM kv")


async def test_inbox_first_read_is_silent_then_new_items_ping_by_kind(b, monkeypatch, clean_inbox):
    _scrape_inbox(monkeypatch, b, {"msg": [inb("m1", "feedback")], "notice": []})
    await b.sync_inbox()
    assert b.sent == []  # what is already there is only recorded

    _scrape_inbox(monkeypatch, b, {"msg": [inb("m1", "feedback"), inb("m2", "feedback", "HW 3"), inb("m3", "message", "hi"),
                                           inb("m4", "urgent", "closed"), inb("m5", "announcement", "News")],
                                   "notice": [inb("notice:1", "announcement", "Board post")]})
    await b.sync_inbox()
    loud = [(t, kw["disable_notification"]) for t, kw in zip(b.sent, b.sent_kw)]
    assert [silent for _, silent in loud] == [False, True]  # one loud message, one silent message
    assert all(w in loud[0][0] for w in ("HW 3", "closed", "News", "Board post")) and "hi" not in loud[0][0]
    assert "💬" in loud[0][0] and "🚨" in loud[0][0] and "📢" in loud[0][0]
    assert "✉️" in loud[1][0] and "hi" in loud[1][0]

    await b.sync_inbox()
    assert len(b.sent) == 2  # nothing repeats


async def test_a_source_that_was_empty_still_announces_its_first_post(b, monkeypatch, clean_inbox):
    _scrape_inbox(monkeypatch, b, {"msg": [], "notice": []})
    await b.sync_inbox()  # both read fine, both empty
    _scrape_inbox(monkeypatch, b, {"msg": [], "notice": [inb("notice:9", "announcement", "First ever notice")]})
    await b.sync_inbox()
    assert len(b.sent) == 1 and "First ever notice" in b.sent[0] and b.sent_kw[0]["disable_notification"] is False


async def test_a_source_that_failed_is_not_marked_seen(b, monkeypatch, clean_inbox):
    _scrape_inbox(monkeypatch, b, {"msg": [inb("m1", "message")]})  # the notice board could not be read
    await b.sync_inbox()
    _scrape_inbox(monkeypatch, b, {"msg": [inb("m1", "message")], "notice": [inb("notice:1", "announcement", "old post")]})
    await b.sync_inbox()
    assert b.sent == []  # the notice board's first successful read is silent too


async def test_inbox_texts_are_escaped_and_batched(b, monkeypatch, clean_inbox):
    _scrape_inbox(monkeypatch, b, {"msg": [], "notice": []})
    await b.sync_inbox()
    many = [inb(f"m{i}", "announcement", f"News <b>{i}") for i in range(7)]
    _scrape_inbox(monkeypatch, b, {"msg": many, "notice": []})
    await b.sync_inbox()
    assert len(b.sent) == 2 and "&lt;b&gt;" in b.sent[0] and "<b>News" not in b.sent[0]  # 6 + 1 per message


def test_render_inbox(b):
    rows = [("announcement", "Operating System[001]", "T", "2026-11-10 10:00", "Exam <room>", "https://e/1"),
            ("message", "Database[001]", "T", "2026-11-09 09:00", "hello", None)]
    out = b.render_inbox(rows).split("\n")
    assert out[0] == '📢 <a href="https://e/1">Exam &lt;room&gt;</a> · Operating System · 2026-11-10'
    assert out[1] == "✉️ hello · Database · 2026-11-09"


# ---------- items without a deadline ----------
NODUE = "<div class='submissionsummarytable'><table><tr><td>Submission status</td><td>No attempt</td></tr></table></div><p>Submit on Gradescope</p>"


def _site(monkeypatch, b, pages):
    monkeypatch.setattr(b, "list_courses", lambda: {"1": "C"})
    monkeypatch.setattr(b, "course_activities", lambda cid, name: ("C", [("assign", mid, t, f"https://e/{mid}") for mid, t in pages], []))
    monkeypatch.setattr(b, "web_get", lambda url, **kw: type("R", (), {"text": NODUE if url.endswith(("/1", "/3")) else ASSIGN})())


def test_items_without_a_deadline_are_returned_with_no_timestamp(b, monkeypatch):
    _site(monkeypatch, b, [("1", "Homework 2"), ("2", "Homework 3")])
    found, _ = b.scrape_courses({})
    assert [(f[0], f[3] is None) for f in found] == [("assign-1", True), ("assign-2", False)]


def test_a_known_item_whose_page_lost_its_date_is_not_called_undated(b, monkeypatch):
    _site(monkeypatch, b, [("3", "Normal homework")])
    found, _ = b.scrape_courses({"assign-3": (time.time() + 86400, "todo")})  # had a deadline before: probably a glitch
    assert found == []


async def test_undated_first_sync_silent_then_new_ones_announced_silently(b, monkeypatch):
    b.db.execute("DELETE FROM kv")
    monkeypatch.setattr(b, "ECLASS_USERNAME", "u")
    monkeypatch.setattr(b, "ECLASS_PASSWORD", "p")
    state = [("assign-1", "Homework 2 <x>", COURSE, None, "https://e/1", "assign", "?")]
    monkeypatch.setattr(b, "scrape_courses", lambda known: (state, []))
    await b.sync_events()
    assert b.sent == []  # first read only records
    state.append(("assign-5", "Quiz 1", COURSE, None, "https://e/5", "assign", "todo"))
    await b.sync_events()
    assert len(b.sent) == 1 and "Quiz 1" in b.sent[0] and "Homework 2" not in b.sent[0]
    assert b.sent_kw[0]["disable_notification"] is True and "📌" in b.sent[0]
    await b.sync_events()
    assert len(b.sent) == 1  # nothing repeats
    assert b.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0  # never in the deadline tables


async def test_undated_item_that_gets_a_deadline_moves_to_events(b, monkeypatch):
    b.db.execute("DELETE FROM kv")
    monkeypatch.setattr(b, "ECLASS_USERNAME", "u")
    monkeypatch.setattr(b, "ECLASS_PASSWORD", "p")
    state = [("assign-1", "HW", COURSE, None, "u", "assign", "?")]
    monkeypatch.setattr(b, "scrape_courses", lambda known: (state, []))
    await b.sync_events()
    assert b.db.execute("SELECT COUNT(*) FROM undated").fetchone()[0] == 1
    state[:] = [("assign-1", "HW", COURSE, int(time.time()) + 86400, "u", "assign", "todo")]
    await b.sync_events()
    assert b.db.execute("SELECT COUNT(*) FROM undated").fetchone()[0] == 0
    assert b.db.execute("SELECT uid FROM events").fetchall() == [("assign-1",)]


def test_undated_line_shows_submitted_state(b):
    assert b.undated_line(("Homework 2 - X", COURSE, "https://e/1", "?")) == '📌 <a href="https://e/1">Homework 2 - X</a> · Electronic Circuit'
    assert b.undated_line(("HW", COURSE, None, "done")).startswith("✅ HW")


# ---------- exam weeks ----------
def test_parse_syllabus_weights_and_exam_weeks():
    assert parse_syllabus(fixture("syllabus_os.html")) == ({"midterm": "25%", "final": "25%"}, {"midterm": 8, "final": 15})
    # a syllabus that gives weights but no exam weeks in its plan (the weeks are simply unknown)
    assert parse_syllabus(fixture("syllabus_no_weeks.html")) == ({"midterm": "35%", "final": "40%"}, {})
    assert parse_syllabus("<p>nothing</p>") == ({}, {})


@pytest.mark.parametrize("label, kind", [("Midterm", "midterm"), ("MidTerm", "midterm"), ("Mid-term", "midterm"), ("Final Exam", "final"), ("Final exam", "final")])
def test_parse_syllabus_exam_week_spellings(label, kind):
    assert parse_syllabus(f"<p>7 Theme {label} Class Details</p>")[1] == {kind: 7}


def test_parse_week_ranges_infers_the_year():
    r = parse_week_ranges(fixture("course_weeks.html"), date(2026, 11, 13))
    assert len(r) == 16
    assert r[1] == (date(2026, 9, 5), date(2026, 9, 11))
    assert r[8] == (date(2026, 10, 24), date(2026, 10, 30))
    assert r[16] == (date(2026, 12, 19), date(2026, 12, 25))
    assert r == parse_week_ranges(fixture("course_weeks.html"), date(2026, 9, 1))  # same answer before the semester starts


def test_parse_week_ranges_over_new_year():
    html = "".join(f'<a href="#section-{i}">{i}Week [{a} - {b}]</a>' for i, (a, b) in
                   enumerate([("22 December", "28 December"), ("29 December", "04 January"), ("05 January", "11 January")], 1))
    r = parse_week_ranges(html, date(2026, 12, 30))
    assert r[2] == (date(2026, 12, 29), date(2027, 1, 4)) and r[3] == (date(2027, 1, 5), date(2027, 1, 11))
    assert parse_week_ranges("<p>no sections</p>", date(2026, 1, 1)) == {}


def test_fmt_range_and_exam_when(b):
    assert b.fmt_range("2026-10-24", "2026-10-30") == "24–30 Oct"
    assert b.fmt_range("2026-10-28", "2026-11-03") == "28 Oct – 3 Nov"
    w = ("2026-12-12", "2026-12-18")
    assert b.exam_when(*w, date(2026, 12, 11)) == "tomorrow"
    assert b.exam_when(*w, date(2026, 11, 13)) == "in 29 days"
    assert b.exam_when(*w, date(2026, 12, 12)) == "🔥 this week" and b.exam_when(*w, date(2026, 12, 18)) == "🔥 this week"
    assert b.exam_when(*w, date(2026, 12, 19)) == "passed"
    assert b.exam_when(None, None, date(2026, 12, 19)) is None


def test_render_exams_orders_soonest_first_unknown_then_passed(b):
    rows = [("Database[001]", "midterm", 7, "2026-10-17", "2026-10-23", "30%"),
            ("Operating System[001]", "final", 15, "2026-12-12", "2026-12-18", "25%"),
            ("Operating System[001]", "midterm", 8, "2026-10-24", "2026-10-30", "25%"),
            ("Engineering Communications[001]", "final", None, None, None, "40%")]
    out = b.render_exams(rows, date(2026, 10, 27)).split("\n")
    assert out[0] == "📝 <b>Operating System</b> · Midterm · week 8 (24–30 Oct) · 25% of the grade · 🔥 this week"
    assert out[1] == "🏁 <b>Operating System</b> · Final · week 15 (12–18 Dec) · 25% of the grade · in 46 days"
    assert out[2] == "🏁 <b>Engineering Communications</b> · Final · week not in the syllabus · 40% of the grade"
    assert out[3].endswith("· passed") and "Database" in out[3]


async def test_sync_exams_replaces_a_courses_rows_and_keeps_failed_ones(b, monkeypatch):
    b.db.execute("DELETE FROM exams")
    monkeypatch.setattr(b, "ECLASS_USERNAME", "u")
    monkeypatch.setattr(b, "ECLASS_PASSWORD", "p")
    state = {"1": ("OS[001]", [("midterm", 8, "2026-10-24", "2026-10-30", "25%"), ("final", 15, "2026-12-12", "2026-12-18", "25%")]),
             "2": ("Online[001]", [])}
    monkeypatch.setattr(b, "scrape_exams", lambda: state)
    await b.sync_exams()
    assert b.db.execute("SELECT COUNT(*) FROM exams").fetchone()[0] == 2
    state["1"] = ("OS[001]", [("final", 16, "2026-12-19", "2026-12-25", "30%")])  # the syllabus changed
    await b.sync_exams()
    assert b.db.execute("SELECT kind,week,weight FROM exams").fetchall() == [("final", 16, "30%")]
    monkeypatch.setattr(b, "scrape_exams", lambda: {})  # everything failed this time
    await b.sync_exams()
    assert b.db.execute("SELECT COUNT(*) FROM exams").fetchone()[0] == 1


def _scrape_exams_pages(monkeypatch, b):
    pages = {"course": fixture("course_weeks.html"), "syl": fixture("syllabus_os.html")}
    monkeypatch.setattr(b, "list_courses", lambda: {"1": "Fallback name"})
    monkeypatch.setattr(b, "web_get", lambda url, **kw: type("R", (), {"text": pages["syl"] if "syllabus" in url else pages["course"]})())


def test_scrape_exams_combines_syllabus_and_week_dates(b, monkeypatch):
    _scrape_exams_pages(monkeypatch, b)
    (cid, (name, rows)), = b.scrape_exams().items()
    assert cid == "1" and name == "Fallback name"  # no <h1> in the fixture
    wk = dict(parse_week_ranges(fixture("course_weeks.html"), datetime.now(TZ).date()))
    assert [(r[0], r[1], r[4]) for r in rows] == [("midterm", 8, "25%"), ("final", 15, "25%")]
    assert rows[0][2:4] == (wk[8][0].isoformat(), wk[8][1].isoformat())


def _exam(b, kind, start, end):
    b.db.execute("INSERT INTO exams VALUES('1','Operating System[001]',?,8,?,?,'25%')", (kind, start, end))


async def test_exam_reminders_at_7_and_1_days_daytime_only_once(b):
    b.db.execute("DELETE FROM exams")
    b.db.execute("DELETE FROM reminded")
    _exam(b, "midterm", "2026-10-24", "2026-10-30")
    _exam(b, "final", "2026-12-12", "2026-12-18")
    at = lambda day, hh: datetime.strptime(f"{day} {hh}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    await b.check_exam_reminders(now=at("2026-10-10", "12:00"))  # two weeks out
    await b.check_exam_reminders(now=at("2026-10-17", "03:00"))  # 7 days, but night
    assert b.sent == []
    await b.check_exam_reminders(now=at("2026-10-17", "09:00"))
    assert len(b.sent) == 1 and "under a week" in b.sent[0] and "Midterm" in b.sent[0] and "Final" not in b.sent[0]
    await b.check_exam_reminders(now=at("2026-10-18", "09:00"))
    assert len(b.sent) == 1  # nothing repeats
    await b.check_exam_reminders(now=at("2026-10-23", "09:00"))
    assert len(b.sent) == 2 and "1 day" in b.sent[1]
    await b.check_exam_reminders(now=at("2026-10-24", "09:00"))  # the week has started
    assert len(b.sent) == 2


async def test_exam_reminder_when_first_seen_late_skips_the_older_one(b):
    b.db.execute("DELETE FROM exams")
    b.db.execute("DELETE FROM reminded")
    _exam(b, "midterm", "2026-10-24", "2026-10-30")
    at = lambda day, hh: datetime.strptime(f"{day} {hh}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    await b.check_exam_reminders(now=at("2026-10-23", "09:00"))  # first look: already 1 day out
    await b.check_exam_reminders(now=at("2026-10-23", "10:00"))
    assert len(b.sent) == 1 and "1 day" in b.sent[0]


# ---------- change watcher ----------
def test_parse_notifications_real_page():
    rows = parse_notifications(fixture("notifications.html"))
    assert len(rows) == 15
    uid, url, course, section = rows[0]
    assert uid.startswith(("ubfile-", "assign-", "vod-", "folder-", "url-")) and url.endswith(uid.split("-")[1])
    assert course == "Database[202602-SOC3020-001]" and section.startswith("10Week")
    assert {r[0].split("-")[0] for r in rows} >= {"ubfile", "assign"}
    assert parse_notifications("<p>Please log in</p>") is None
    assert parse_notifications("<h3>Notification message</h3>") == []  # a real page with nothing in it


async def test_watcher_scans_only_when_something_new_is_listed(b, monkeypatch):
    b.db.execute("DELETE FROM notif_seen")
    b.db.execute("DELETE FROM kv")
    listed = [("ubfile-1", "u", "C", "1Week"), ("assign-2", "u", "C", "1Week")]
    monkeypatch.setattr(b, "parse_notifications", lambda page: listed)
    monkeypatch.setattr(b, "web_get", lambda url, **kw: type("R", (), {"text": ""})())
    scans = []

    async def fake_events():
        scans.append(1)
    fake_events.__name__ = "sync_events"
    monkeypatch.setattr(b, "sync_events", fake_events)

    await b.watch_changes()
    assert scans == []  # the first look only records what is listed
    await b.watch_changes()
    assert scans == []  # nothing new
    listed.insert(0, ("ubfile-3", "u", "C", "2Week"))
    await b.watch_changes()
    assert scans == [1]  # something new: scan now
    await b.watch_changes()
    assert scans == [1]  # and it is not new any more


async def test_watcher_retries_when_the_scan_fails(b, monkeypatch):
    b.db.execute("DELETE FROM notif_seen")
    b.db.execute("DELETE FROM kv")
    listed = [("ubfile-1", "u", "C", "1Week")]
    monkeypatch.setattr(b, "parse_notifications", lambda page: listed)
    monkeypatch.setattr(b, "web_get", lambda url, **kw: type("R", (), {"text": ""})())
    outcomes = [ValueError("eclass hiccup"), None]

    async def flaky_events():
        if (e := outcomes.pop(0)):
            raise e
    flaky_events.__name__ = "sync_events"
    monkeypatch.setattr(b, "sync_events", flaky_events)

    await b.watch_changes()  # first look: records
    listed.append(("assign-9", "u", "C", "1Week"))
    await b.watch_changes()  # the scan fails: the new item stays unseen
    assert b.db.execute("SELECT COUNT(*) FROM notif_seen WHERE uid='assign-9'").fetchone()[0] == 0
    await b.watch_changes()  # the next check tries again and succeeds
    assert b.db.execute("SELECT COUNT(*) FROM notif_seen WHERE uid='assign-9'").fetchone()[0] == 1
    assert outcomes == []


async def test_watch_loop_skips_a_tick_while_a_cycle_is_running(b, monkeypatch):
    cycles = []

    async def fake_cycle(jobs, alert=True):
        cycles.append([j.__name__ for j in jobs])
        return [None] * len(jobs)
    monkeypatch.setattr(b, "run_cycle", fake_cycle)
    sleeps = []

    async def fake_sleep(secs):
        sleeps.append(secs)
        if len(sleeps) == 2:
            b._cycle_lock.release()  # the running cycle ends before the second tick
        if len(sleeps) == 3:
            raise asyncio.CancelledError
    monkeypatch.setattr(b.asyncio, "sleep", fake_sleep)
    await b._cycle_lock.acquire()  # a full cycle is running during the first tick
    with pytest.raises(asyncio.CancelledError):
        await b.watch_loop()
    assert cycles == [["watch_changes"]] and sleeps == [b.WATCH_SECONDS] * 3


async def test_watch_loop_reads_the_inbox_only_on_the_slow_rhythm(b, monkeypatch):
    cycles, clock = [], [1000.0]

    async def fake_cycle(jobs, alert=True):
        cycles.append([j.__name__ for j in jobs])
        return [None] * len(jobs)

    async def fake_sleep(secs):
        clock[0] += secs
        if len(cycles) == 12:
            raise asyncio.CancelledError
    monkeypatch.setattr(b, "run_cycle", fake_cycle)
    monkeypatch.setattr(b.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(b.time, "monotonic", lambda: clock[0])
    with pytest.raises(asyncio.CancelledError):
        await b.watch_loop()
    with_inbox = [i for i, c in enumerate(cycles) if "sync_inbox" in c]
    assert with_inbox and with_inbox[0] >= 3  # not on every fast tick
    assert all(c[-1] == "watch_changes" for c in cycles)
    assert len(with_inbox) < len(cycles) / 2


def test_watch_interval_has_a_floor_of_30_seconds(b):
    assert [b.watch_interval(x) for x in ("60", "10", "1", "0", "90", "30")] == [60, 30, 30, 0, 90, 30]
    assert b.WATCH_SECONDS == 60  # the default


def test_watch_backs_off_when_slow_or_failing_and_recovers(b):
    base = b.WATCH_SECONDS
    assert b.next_watch_delay(base, 0.4, False) == base
    d = b.next_watch_delay(base, b.WATCH_SLOW + 1, False)  # slow response
    assert d == base * 2
    d = b.next_watch_delay(d, 0.4, True)  # still failing
    assert d == base * 4
    for _ in range(10):
        d = b.next_watch_delay(d, 30, False)
    assert d == b.WATCH_MAX_SECONDS  # capped
    assert b.next_watch_delay(d, 0.4, False) == base  # fast again


async def test_status_shows_the_watcher_rate_and_backoff(b):
    now = int(__import__("time").time())
    b.db.execute("INSERT OR REPLACE INTO health(job,last_ok,fails,alerted) VALUES('watch_changes',?,0,0)", (now,))
    b._watch_state["delay"] = b.WATCH_SECONDS
    assert f"change watcher · last ok just now · every {b.WATCH_SECONDS}s" in b.render_status()
    b._watch_state["delay"] = b.WATCH_SECONDS * 4
    assert "backing off" in b.render_status()


class FakeResp:
    def __init__(self, url, body=b"", ctype="application/pdf", headers=None, status=200):
        self.url, self.status_code, self.ok = url, status, status < 400
        self.headers = {"content-type": ctype, **(headers or {})}
        self.content = body
        self.text = body.decode("utf8", "ignore")

    def iter_content(self, n):
        yield self.content

    def close(self):
        pass


def test_download_serves_file_directly(b, monkeypatch):
    r = FakeResp("https://e/pluginfile.php/1/l1.pdf", b"%PDF", headers={"content-disposition": 'attachment; filename="Lecture 1.pdf"'})
    monkeypatch.setattr(b, "_fetch", lambda url: r)
    assert b.download_resource("https://e/mod/ubfile/view.php?id=1") == [("Lecture 1.pdf", b"%PDF")]


def test_download_follows_links_on_a_page_and_skips_other_links(b, monkeypatch):
    page = '<a href="/pluginfile.php/9/a.pdf">a</a><a href="/pluginfile.php/9/b.pptx?forcedownload=1">b</a><a href="/course/view.php?id=2">x</a>'
    pages = {
        "https://e/mod/folder/view.php?id=5": FakeResp("https://e/mod/folder/view.php?id=5", page.encode(), "text/html; charset=utf-8"),
        "https://e/pluginfile.php/9/a.pdf": FakeResp("https://e/pluginfile.php/9/a.pdf", b"A"),
        "https://e/pluginfile.php/9/b.pptx?forcedownload=1": FakeResp("https://e/pluginfile.php/9/b.pptx", b"B"),
    }
    monkeypatch.setattr(b, "_fetch", lambda url: pages[url])
    assert b.download_resource("https://e/mod/folder/view.php?id=5") == [("a.pdf", b"A"), ("b.pptx", b"B")]


def test_download_refuses_oversized_and_empty_pages(b, monkeypatch):
    big = FakeResp("https://e/f.zip", b"x", headers={"content-length": str(b.MAX_UPLOAD + 1)})
    monkeypatch.setattr(b, "_fetch", lambda url: big)
    with pytest.raises(ValueError, match="50 MB"):
        b.download_resource("https://e/m")
    monkeypatch.setattr(b, "_fetch", lambda url: FakeResp(url, b"<p>nothing</p>", "text/html"))
    with pytest.raises(ValueError, match="no file"):
        b.download_resource("https://e/m")


async def test_download_button_sends_document(b, monkeypatch):
    b.db.execute("DELETE FROM resources")
    b.db.execute("INSERT INTO resources VALUES('ubfile-1','Lecture 1',?, 'ubfile','https://e/m',0)", (COURSE,))
    b.db.execute("INSERT INTO resources VALUES('vod-2','Video',?, 'vod','https://e/v',0)", (COURSE,))
    rows = b.db.execute("SELECT uid,title,course,kind,url FROM resources").fetchall()
    text = b.render_resources(rows)
    assert "/dl_ubfile_1" in text and "dl_vod" not in text  # videos can't be downloaded
    docs, notes = [], []

    async def fake_doc(chat, doc, caption=None):
        docs.append((doc.filename, caption))

    async def answer(text):
        notes.append(text)

    monkeypatch.setattr(b.bot, "send_document", fake_doc)
    monkeypatch.setattr(b, "download_resource", lambda url: [("l1.pdf", b"x")])
    await b.send_resource("ubfile-1", answer)
    assert docs == [("l1.pdf", "Lecture 1")]
    await b.send_resource("nope", answer)
    assert notes[-1] == "That item is gone"


async def test_download_failure_tells_you_instead_of_crashing(b, monkeypatch):
    b.db.execute("DELETE FROM resources")
    b.db.execute("INSERT INTO resources VALUES('ubfile-1','Lecture 1',?, 'ubfile','https://e/m',0)", (COURSE,))

    def boom(url):
        raise OSError("net down")

    monkeypatch.setattr(b, "download_resource", boom)

    async def answer(text):
        pass

    await b.send_resource("ubfile-1", answer)
    assert "Could not download" in b.sent[-1]


def test_every_handler_is_in_the_menu_and_on_the_keyboard(b):
    import re

    src = Path(b.__file__).read_text()
    handled = {c for grp in re.findall(r'Command\(([^)]*)\)', src) for c in re.findall(r'"(\w+)"', grp)} - {"start"}
    menu = {c for c, _ in b.MENU}
    assert handled == menu
    assert {k.text for row in b.KEYBOARD.keyboard for k in row} == {f"/{c}" for c in menu}
