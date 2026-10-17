from datetime import timedelta

NOW = 1_800_000_000
H = 3600


def add(b, uid="1@x", ts=NOW + 10 * H, course="CSE", submitted=None):
    b.db.execute(
        "INSERT INTO events(uid,title,course,start_ts,submitted) VALUES(?,?,?,?,?)", (uid, "HW", course, ts, submitted)
    )
    b.db.commit()


def test_due_index(b):
    assert b.due_index(timedelta(days=4)) is None
    assert b.REMINDERS[b.due_index(timedelta(days=2))][0] == "3d"
    assert b.REMINDERS[b.due_index(timedelta(hours=20))][0] == "1d"
    assert b.REMINDERS[b.due_index(timedelta(hours=2))][0] == "3h"
    assert b.REMINDERS[b.due_index(timedelta(hours=3))][0] == "3h"  # boundary


async def test_each_threshold_fires_once(b):
    add(b, ts=NOW + 4 * 86400)
    labels = []
    # walk time forward in 5-minute steps (the real loop interval), twice per step
    t = NOW
    while t < NOW + 4 * 86400:
        before = len(b.sent)
        await b.check_reminders(now=t)
        await b.check_reminders(now=t)
        labels += [s.split("Due in ")[1].split("</b>")[0] for s in b.sent[before:]]
        t += 300
    assert labels == ["3d", "1d", "3h"]


async def test_nothing_before_3_days(b):
    add(b, ts=NOW + 4 * 86400)
    await b.check_reminders(now=NOW)
    assert b.sent == []


async def test_late_add_fires_only_tightest(b):
    add(b, ts=NOW + 2 * H)  # first seen with 2h left
    await b.check_reminders(now=NOW)
    await b.check_reminders(now=NOW + 3600)
    assert len(b.sent) == 1 and "Due in 3h" in b.sent[0]


async def test_no_older_larger_reminder_after_smaller(b):
    add(b, ts=NOW + 20 * H)
    await b.check_reminders(now=NOW)  # fires 1d, marks 3d too
    assert "Due in 1d" in b.sent[0]
    b.sent.clear()
    await b.check_reminders(now=NOW + 3 * H)  # still > 3h left? 17h -> nothing
    assert b.sent == []


async def test_past_events_ignored(b):
    add(b, ts=NOW - H)
    await b.check_reminders(now=NOW)
    assert b.sent == []


async def test_submitted_skipped(b):
    add(b, ts=NOW + 2 * H, submitted=1)
    await b.check_reminders(now=NOW)
    assert b.sent == []


async def test_unsubmitted_still_reminded(b):
    add(b, ts=NOW + 2 * H, submitted=0)
    await b.check_reminders(now=NOW)
    assert len(b.sent) == 1 and "❌" in b.sent[0]


async def test_muted_course_skipped_and_stays_quiet(b):
    add(b, ts=NOW + 2 * H, course="CSE3101-001, LAB")
    b.db.execute("INSERT INTO muted VALUES('cse3101-001')")
    await b.check_reminders(now=NOW)
    assert b.sent == []


async def test_send_failure_retries(b, monkeypatch):
    add(b, ts=NOW + 2 * H)
    calls = []

    async def boom(text):
        calls.append(text)
        raise RuntimeError("telegram down")

    monkeypatch.setattr(b, "send", boom)
    try:
        await b.check_reminders(now=NOW)
    except RuntimeError:
        pass
    assert b.db.execute("SELECT COUNT(*) FROM reminded").fetchone()[0] == 0  # not marked -> will retry
