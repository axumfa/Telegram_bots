from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def by_uid(events):
    return {e[0].split("@")[0]: e for e in events}


def test_parses_all_events(b, ics):
    assert len(b.parse_ics(ics)) == 5


def test_utc_time(b, ics):
    ts = by_uid(b.parse_ics(ics))["1001"][3]
    assert ts == int(datetime(2026, 10, 20, 14, 30, tzinfo=timezone.utc).timestamp())


def test_floating_time_uses_site_tz(b, ics):
    ts = by_uid(b.parse_ics(ics))["1002"][3]
    assert ts == int(datetime(2026, 10, 21, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")).timestamp())


def test_all_day_is_end_of_day(b, ics):
    ts = by_uid(b.parse_ics(ics))["1003"][3]
    assert ts == int(datetime(2026, 10, 25, 23, 59, tzinfo=ZoneInfo("Asia/Seoul")).timestamp())


def test_tzid_respected(b, ics):
    ts = by_uid(b.parse_ics(ics))["1004"][3]
    assert ts == int(datetime(2026, 10, 22, 23, 59, tzinfo=ZoneInfo("Asia/Seoul")).timestamp())


def test_categories_single_multiple_missing(b, ics):
    ev = by_uid(b.parse_ics(ics))
    assert ev["1001"][2] == "CSE3101-001"
    assert ev["1004"][2] == "PHY1001, LAB"  # two CATEGORIES lines
    assert ev["1005"][2] == ""


def test_title(b, ics):
    assert by_uid(b.parse_ics(ics))["1001"][1] == "Assignment 1 is due"
