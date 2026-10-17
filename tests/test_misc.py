from datetime import datetime, timezone

import pytest

NOW = 1_800_000_000


def test_submitted_from_status(b):
    assert b.submitted_from_status({"lastattempt": {"submission": {"status": "submitted"}}}) is True
    assert b.submitted_from_status({"lastattempt": {"submission": {"status": "draft"}}}) is False
    assert b.submitted_from_status({"lastattempt": {"submission": {"status": "new"}}}) is False
    assert b.submitted_from_status({"lastattempt": {"teamsubmission": {"status": "submitted"}}}) is True
    assert b.submitted_from_status({}) is False


async def test_refresh_submission_via_ws(b, monkeypatch):
    b.db.execute("INSERT INTO events(uid,title,course,start_ts) VALUES('55@x','HW','C',?)", (NOW + 100,))
    calls = []

    async def fake_ws(fn, **p):
        calls.append(fn)
        if fn == "core_calendar_get_calendar_event_by_id":
            return {"event": {"modulename": "assign", "instance": 9}}
        return {"lastattempt": {"submission": {"status": "submitted"}}}

    monkeypatch.setattr(b, "ws", fake_ws)
    await b.refresh_submission(b.db.execute("SELECT * FROM events").fetchone())
    r = b.db.execute("SELECT * FROM events").fetchone()
    assert (r["modname"], r["instance"], r["submitted"]) == ("assign", 9, 1)
    assert "✅" in b.line(r, NOW)


async def test_non_assign_has_no_mark(b, monkeypatch):
    b.db.execute("INSERT INTO events(uid,title,course,start_ts) VALUES('56@x','Quiz','C',?)", (NOW + 100,))

    async def fake_ws(fn, **p):
        return {"event": {"modulename": "quiz", "instance": 3}}

    monkeypatch.setattr(b, "ws", fake_ws)
    await b.refresh_submission(b.db.execute("SELECT * FROM events").fetchone())
    assert b.db.execute("SELECT submitted FROM events").fetchone()[0] is None


async def test_no_token_means_no_ws_calls(b, monkeypatch):
    monkeypatch.setattr(b, "MOODLE_TOKEN", "")

    async def boom(*a, **k):
        raise AssertionError("ws called")

    monkeypatch.setattr(b, "ws", boom)
    await b.sync_submissions(now=NOW)
    await b.sync_forum_news()


# ---- failure alerts ----
async def test_definitive_failure_alerts_once_then_recovers(b):
    for _ in range(5):
        await b.job_failed("calendar", b.FeedError("HTTP 404"))
    assert len(b.sent) == 1 and "calendar" in b.sent[0]
    await b.job_ok("calendar")
    assert len(b.sent) == 2 and "working again" in b.sent[1]
    await b.job_ok("calendar")
    assert len(b.sent) == 2


async def test_transient_failure_needs_two_in_a_row(b):
    await b.job_failed("calendar", ConnectionError("blip"))
    assert b.sent == []
    await b.job_ok("calendar")
    await b.job_failed("calendar", ConnectionError("blip"))
    assert b.sent == []
    await b.job_failed("calendar", ConnectionError("blip"))
    await b.job_failed("calendar", ConnectionError("blip"))
    assert len(b.sent) == 1


async def test_alert_state_survives_restart(b):
    await b.job_failed("moodle", b.FeedError("token expired"))
    b._fails.clear()  # simulate restart: memory gone, DB kept
    await b.job_failed("moodle", b.FeedError("token expired"))
    assert len(b.sent) == 1


async def test_run_job_returns_error(b):
    async def bad():
        raise b.FeedError("x")

    assert isinstance(await b.run_job("k", bad), b.FeedError)

    async def good():
        pass

    assert await b.run_job("k", good) is None


async def test_ws_expired_token_is_definitive(b, monkeypatch):
    class R:
        async def json(self, content_type=None):
            return {"exception": "moodle_exception", "errorcode": "invalidtoken", "message": "Invalid token"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class S:
        def post(self, *a, **k):
            return R()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(b.aiohttp, "ClientSession", lambda: S())
    with pytest.raises(b.FeedError):
        await b.ws("core_webservice_get_site_info")


# ---- announcements ----
async def test_board_baseline_then_only_new(b):
    posts = [("ub:1", "A", "u1"), ("ub:2", "B", "u2")]
    await b.handle_posts("ub:board", "CSE", posts)
    assert b.sent == []
    await b.handle_posts("ub:board", "CSE", posts + [("ub:3", "C", "u3")])
    await b.handle_posts("ub:board", "CSE", posts + [("ub:3", "C", "u3")])
    assert len(b.sent) == 1 and "C" in b.sent[0]


async def test_muted_course_announcements_silent(b):
    await b.handle_posts("ub:board", "CSE3101-001", [("ub:1", "A", "u")])
    b.db.execute("INSERT INTO muted VALUES('cse3101-001')")
    await b.handle_posts("ub:board", "CSE3101-001 Data Structures", [("ub:2", "B", "u")])
    assert b.sent == []


def test_ubboard_parsing(b):
    course = """<a href="/mod/ubboard/view.php?id=10">공지사항</a>
                <a href="/mod/ubboard/view.php?id=11">Q&A</a>
                <a href="/mod/ubboard/view.php?id=12">Notice board</a>"""
    boards = b.parse_board_links(course, "https://eclass.inha.ac.kr")
    assert [n for n, _ in boards] == ["공지사항", "Notice board"]
    listing = """<a href="/mod/ubboard/article.php?id=10&bwid=5"> Midterm  info </a>
                 <a href="/mod/ubboard/article.php?id=10&bwid=5">dup</a>
                 <a href="/mod/ubboard/article.php?id=10&bwid=4">Welcome</a>"""
    arts = b.parse_articles(listing, "https://eclass.inha.ac.kr")
    assert [t for _, t, _ in arts] == ["Midterm info", "Welcome"]
    assert arts[0][2] == "https://eclass.inha.ac.kr/mod/ubboard/article.php?id=10&bwid=5"


def test_course_link_parsing(b):
    h = '<a href="https://eclass.inha.ac.kr/course/view.php?id=77">CSE3101 Algorithms</a><a href="/course/view.php?id=78"></a>'
    assert b.parse_course_links(h) == {"77": "CSE3101 Algorithms", "78": ""}


# ---- digest / mute ----
def test_digest_contents(b):
    for uid, ts, course, sub in [
        ("1@x", NOW + 3600, "A", None),
        ("2@x", NOW + 7200, "B", 1),  # submitted -> hidden
        ("3@x", NOW + 7200, "MUTED", None),  # muted -> hidden
        ("4@x", NOW + 5 * 86400, "A", None),  # beyond 3 days
    ]:
        b.db.execute("INSERT INTO events(uid,title,course,start_ts,submitted) VALUES(?,?,?,?,?)", (uid, uid, course, ts, sub))
    b.db.execute("INSERT INTO muted VALUES('muted')")
    t = b.digest_text(now=NOW)
    assert "1@x" in t and "2@x" not in t and "3@x" not in t and "4@x" not in t


def test_digest_empty(b):
    assert "Nothing due" in b.digest_text(now=NOW)


def test_digest_schedule(b):
    tz = b.TZ
    at = lambda h, m=0: datetime(2026, 10, 20, h, m, tzinfo=tz).astimezone(timezone.utc)
    assert b.digest_due(at(7, 59)) is None
    assert b.digest_due(at(8, 0)) == "2026-10-20"
    b.state_set("last_digest", "2026-10-20")
    assert b.digest_due(at(8, 5)) is None  # already sent today
    assert b.digest_due(at(13, 0)) is None  # too late to be a morning digest


def test_resolve_course(b):
    for n in ("CSE3101-001", "CSE3202-001", "PHY1001"):
        b.db.execute("INSERT INTO courses VALUES(?)", (n,))
    assert b.resolve_course("phy") == ["PHY1001"]
    assert len(b.resolve_course("cse")) == 2
    assert b.resolve_course("zzz") == []
