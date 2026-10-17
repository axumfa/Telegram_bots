NOW = 1_800_000_000
D = 86400


def feed(*items):
    return [(u, t, c, ts) for u, t, c, ts in items]


async def test_first_run_is_silent(b):
    await b.sync_events(now=NOW, events=feed(("1@x", "HW1", "CSE", NOW + 5 * D)))
    assert b.sent == []
    assert b.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


async def test_new_event_announced_once(b):
    await b.sync_events(now=NOW, events=feed(("1@x", "HW1", "CSE", NOW + 5 * D)))
    two = feed(("1@x", "HW1", "CSE", NOW + 5 * D), ("2@x", "HW2", "CSE", NOW + 6 * D))
    await b.sync_events(now=NOW, events=two)
    await b.sync_events(now=NOW, events=two)
    assert len(b.sent) == 1 and "New deadline" in b.sent[0] and "HW2" in b.sent[0]


async def test_changed_deadline_announced_and_reminders_reset(b):
    await b.sync_events(now=NOW, events=feed(("1@x", "HW1", "CSE", NOW + 2 * D)))
    b.db.execute("INSERT INTO reminded VALUES('1@x','3d')")
    await b.sync_events(now=NOW, events=feed(("1@x", "HW1", "CSE", NOW + 5 * D)))
    assert len(b.sent) == 1 and "Deadline changed" in b.sent[0] and "was:" in b.sent[0]
    assert b.db.execute("SELECT COUNT(*) FROM reminded").fetchone()[0] == 0


async def test_unchanged_is_silent(b):
    e = feed(("1@x", "HW1", "CSE", NOW + 2 * D))
    await b.sync_events(now=NOW, events=e)
    await b.sync_events(now=NOW, events=e)
    assert b.sent == []


async def test_old_events_never_announced(b):
    await b.sync_events(now=NOW, events=feed(("1@x", "HW1", "CSE", NOW + D)))
    await b.sync_events(now=NOW, events=feed(("1@x", "HW1", "CSE", NOW + D), ("2@x", "old", "CSE", NOW - D)))
    assert b.sent == []


async def test_muted_course_not_announced(b):
    await b.sync_events(now=NOW, events=feed(("1@x", "HW1", "CSE", NOW + D)))
    b.db.execute("INSERT INTO muted VALUES('lab')")
    await b.sync_events(now=NOW, events=feed(("1@x", "HW1", "CSE", NOW + D), ("2@x", "L", "LAB", NOW + D)))
    assert b.sent == []


async def test_deleted_near_event_pruned_far_kept(b):
    await b.sync_events(
        now=NOW,
        events=feed(("1@x", "a", "C", NOW + D), ("2@x", "far", "C", NOW + 40 * D), ("3@x", "keep", "C", NOW + 2 * D)),
    )
    await b.sync_events(now=NOW, events=feed(("3@x", "keep", "C", NOW + 2 * D)))
    uids = {r["uid"] for r in b.db.execute("SELECT uid FROM events")}
    assert uids == {"2@x", "3@x"}


async def test_empty_feed_does_not_wipe(b):
    await b.sync_events(now=NOW, events=feed(("1@x", "a", "C", NOW + D)))
    await b.sync_events(now=NOW, events=[])
    assert b.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


async def test_submission_state_survives_resync(b):
    await b.sync_events(now=NOW, events=feed(("1@x", "a", "C", NOW + D)))
    b.db.execute("UPDATE events SET submitted=1, modname='assign', instance=7")
    await b.sync_events(now=NOW, events=feed(("1@x", "a", "C", NOW + D)))
    r = b.db.execute("SELECT * FROM events").fetchone()
    assert (r["submitted"], r["modname"], r["instance"]) == (1, "assign", 7)
