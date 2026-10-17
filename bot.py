import asyncio
import html
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from dotenv import load_dotenv
from icalendar import Calendar

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
ICAL_URL = os.environ["ICAL_URL"].replace("webcal://", "https://", 1)
MOODLE_URL = os.getenv("MOODLE_URL", "https://eclass.inha.ac.kr").rstrip("/")
MOODLE_TOKEN = os.getenv("MOODLE_TOKEN", "")  # optional: submission status + forum announcements
ECLASS_USERNAME = os.getenv("ECLASS_USERNAME", "")  # optional: Ubion board fallback (web login)
ECLASS_PASSWORD = os.getenv("ECLASS_PASSWORD", "")
COURSE_IDS = [c.strip() for c in os.getenv("COURSE_IDS", "").split(",") if c.strip()]
BOARD_FILTER = re.compile(os.getenv("BOARD_FILTER", r"공지|notice|announce"), re.I)
POLL_MINUTES = int(os.getenv("POLL_MINUTES", "20"))
TZ = ZoneInfo("Asia/Tashkent")  # where you are: display + digest
SITE_TZ = ZoneInfo(os.getenv("SITE_TZ", "Asia/Seoul"))  # floating / all-day times in the feed
DIGEST_HOUR = 8
DIGEST_DAYS = 3
PRUNE_DAYS = 14  # events missing from the feed are dropped only if due within this window
FAIL_THRESHOLD = 2  # consecutive transient failures before alerting
# smallest first
REMINDERS = [("3h", timedelta(hours=3)), ("1d", timedelta(days=1)), ("3d", timedelta(days=3))]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("eclass-bot")


def open_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS events(
            uid TEXT PRIMARY KEY, title TEXT, course TEXT, start_ts INTEGER,
            modname TEXT, instance INTEGER, submitted INTEGER);
        CREATE TABLE IF NOT EXISTS reminded(uid TEXT, label TEXT, PRIMARY KEY(uid, label));
        CREATE TABLE IF NOT EXISTS news(id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS boards(key TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS courses(name TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS muted(name TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
        """
    )
    return conn


db = open_db(os.getenv("DB_PATH", "eclass.db"))

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
dp.message.filter(F.chat.id == CHAT_ID)  # only you


# ---------- helpers ----------
def esc(s):
    return html.escape(str(s or ""))


def utcnow():
    return datetime.now(timezone.utc).timestamp()


def fmt_ts(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%a %d %b, %H:%M")


def left(ts, now=None):
    secs = int(ts - (utcnow() if now is None else now))
    if secs < 0:
        return "overdue"
    d, r = divmod(secs, 86400)
    h, m = divmod(r // 60, 60)
    return f"{d}d {h}h" if d else f"{h}h {m}m"


def mark(row):
    if row["submitted"] is None:
        return ""
    return "✅ " if row["submitted"] else "❌ "


def line(row, now=None):
    c = f" <i>({esc(row['course'])})</i>" if row["course"] else ""
    return f"• {mark(row)}<b>{esc(row['title'])}</b>{c}\n   {fmt_ts(row['start_ts'])} — {left(row['start_ts'], now)} left"


async def send(text):
    await bot.send_message(CHAT_ID, text)


def state_get(key):
    r = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def state_set(key, value):
    db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, value))
    db.commit()


def course_tokens(course):
    return [t.strip() for t in (course or "").split(",") if t.strip()]


def is_muted(course):
    muted = [r["name"] for r in db.execute("SELECT name FROM muted")]
    for t in course_tokens(course):
        tl = t.lower()
        if any(m == tl or m in tl or tl in m for m in muted):
            return True
    return False


def remember_courses(course):
    for t in course_tokens(course):
        db.execute("INSERT OR IGNORE INTO courses VALUES(?)", (t,))


# ---------- iCal ----------
def parse_ics(data):
    """Return [(uid, title, course, start_ts)] from raw .ics bytes."""
    out = []
    for ev in Calendar.from_ical(data).walk("VEVENT"):
        if ev.get("DTSTART") is None:
            continue
        start = ev.decoded("DTSTART")
        if not isinstance(start, datetime):  # all-day -> end of that day
            start = datetime(start.year, start.month, start.day, 23, 59, tzinfo=SITE_TZ)
        elif start.tzinfo is None:  # floating time -> site's local time
            start = start.replace(tzinfo=SITE_TZ)
        cats = ev.get("CATEGORIES")
        if cats is None:
            cats = []
        elif not isinstance(cats, list):  # several CATEGORIES lines give a list
            cats = [cats]
        names = []
        for c in cats:
            names.extend(str(x) for x in getattr(c, "cats", [c]))
        out.append((str(ev.get("UID")), str(ev.get("SUMMARY", "")), ", ".join(names), int(start.timestamp())))
    return out


class FeedError(Exception):
    """A failure that won't fix itself (bad URL, revoked token, wrong password)."""


def definitive(e):
    return isinstance(e, FeedError)


async def fetch_events():
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(ICAL_URL, timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status in (401, 403, 404, 410):
                    raise FeedError(f"calendar URL returned HTTP {r.status}")
                r.raise_for_status()
                data = await r.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise ConnectionError(f"calendar fetch failed: {e.__class__.__name__}") from e
    if b"BEGIN:VCALENDAR" not in data[:500]:
        raise FeedError("calendar URL no longer returns an iCal feed (token revoked?)")
    return parse_ics(data)


# ---------- Moodle web service ----------
async def ws(fn, **params):
    params = {k: str(v) for k, v in params.items()}
    params.update(wstoken=MOODLE_TOKEN, wsfunction=fn, moodlewsrestformat="json")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{MOODLE_URL}/webservice/rest/server.php", data=params, timeout=aiohttp.ClientTimeout(total=60)
            ) as r:
                data = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise ConnectionError(f"moodle request failed: {e.__class__.__name__}") from e
    if isinstance(data, dict) and data.get("exception"):
        if data.get("errorcode") in ("invalidtoken", "accessexception", "invalidlogin"):
            raise FeedError(f"Moodle token rejected ({data.get('errorcode')}): {data.get('message')}")
        raise RuntimeError(f"{fn}: {data.get('message')}")
    return data


def submitted_from_status(res):
    """True/False from a mod_assign_get_submission_status response."""
    last = res.get("lastattempt") or {}
    for key in ("submission", "teamsubmission"):
        if (last.get(key) or {}).get("status") == "submitted":
            return True
    return False


async def refresh_submission(row):
    """Update modname/instance/submitted for one event. Needs MOODLE_TOKEN."""
    uid, modname, instance = row["uid"], row["modname"], row["instance"]
    if modname is None:  # resolve which module this calendar event belongs to
        m = re.match(r"(\d+)@", uid)
        if not m:
            return
        ev = (await ws("core_calendar_get_calendar_event_by_id", eventid=m.group(1))).get("event") or {}
        modname, instance = ev.get("modulename") or "", ev.get("instance")
        db.execute("UPDATE events SET modname=?, instance=? WHERE uid=?", (modname, instance, uid))
    if modname == "assign" and instance:
        res = await ws("mod_assign_get_submission_status", assignid=instance)
        db.execute("UPDATE events SET submitted=? WHERE uid=?", (int(submitted_from_status(res)), uid))
    db.commit()


# ---------- failure alerts (one message per outage) ----------
_fails = {}


async def job_ok(key):
    _fails[key] = 0
    if state_get(f"alert:{key}"):
        state_set(f"alert:{key}", "")
        await safe_send(f"✅ {esc(key)} is working again.")


async def job_failed(key, err):
    log.error("%s failed: %s", key, err, exc_info=not isinstance(err, (FeedError, ConnectionError)))
    _fails[key] = _fails.get(key, 0) + 1
    if state_get(f"alert:{key}"):
        return
    if definitive(err) or _fails[key] >= FAIL_THRESHOLD:
        state_set(f"alert:{key}", "1")
        hint = " Update it in .env and restart the bot." if definitive(err) else ""
        await safe_send(f"🚨 <b>{esc(key)}</b> is failing: {esc(err)}.{hint}\nI'll tell you once it recovers.")


async def safe_send(text):
    try:
        await send(text)
    except Exception:
        log.exception("telegram send failed")


async def run_job(key, fn):
    """Run fn; report outage state. Returns the error (or None)."""
    try:
        await fn()
    except Exception as e:
        await job_failed(key, e)
        return e
    await job_ok(key)
    return None


# ---------- sync jobs ----------
async def sync_events(now=None, events=None):
    now = utcnow() if now is None else now
    first_run = db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    feed = await fetch_events() if events is None else events
    seen = set()
    for uid, title, course, ts in feed:
        seen.add(uid)
        remember_courses(course)
        old = db.execute("SELECT * FROM events WHERE uid=?", (uid,)).fetchone()
        db.execute(
            """INSERT INTO events(uid,title,course,start_ts) VALUES(?,?,?,?)
               ON CONFLICT(uid) DO UPDATE SET title=excluded.title, course=excluded.course,
               start_ts=excluded.start_ts""",
            (uid, title, course, ts),
        )
        if old is not None and old["start_ts"] != ts:
            db.execute("DELETE FROM reminded WHERE uid=?", (uid,))
        if first_run or max(ts, old["start_ts"] if old else 0) < now or is_muted(course):
            continue
        row = db.execute("SELECT * FROM events WHERE uid=?", (uid,)).fetchone()
        if old is None:
            await safe_send("🆕 <b>New deadline</b>\n" + line(row, now))
        elif old["start_ts"] != ts:
            await safe_send(f"⚠️ <b>Deadline changed</b>\n{line(row, now)}\nwas: {fmt_ts(old['start_ts'])}")
    if feed:  # an empty feed is more likely an error than "everything was deleted"
        for r in db.execute(
            "SELECT uid FROM events WHERE start_ts BETWEEN ? AND ?", (now, now + PRUNE_DAYS * 86400)
        ).fetchall():
            if r["uid"] not in seen:
                db.execute("DELETE FROM events WHERE uid=?", (r["uid"],))
                db.execute("DELETE FROM reminded WHERE uid=?", (r["uid"],))
    db.commit()


async def sync_submissions(now=None):
    if not MOODLE_TOKEN:
        return
    now = utcnow() if now is None else now
    for row in db.execute("SELECT * FROM events WHERE start_ts > ?", (now,)).fetchall():
        try:
            await refresh_submission(row)
        except FeedError:
            raise
        except Exception:
            log.exception("submission status for %s failed", row["uid"])


def due_index(remaining):
    """Index in REMINDERS of the tightest threshold that `remaining` has crossed."""
    return next((i for i, (_, d) in enumerate(REMINDERS) if remaining <= d), None)


async def check_reminders(now=None):
    now = utcnow() if now is None else now
    for row in db.execute("SELECT * FROM events WHERE start_ts > ? ORDER BY start_ts", (now,)).fetchall():
        due = due_index(timedelta(seconds=row["start_ts"] - now))
        if due is None:
            continue
        label = REMINDERS[due][0]
        uid = row["uid"]
        if db.execute("SELECT 1 FROM reminded WHERE uid=? AND label=?", (uid, label)).fetchone():
            continue

        def mark_done():  # also mark the larger thresholds so they don't fire later
            for lbl, _ in REMINDERS[due:]:
                db.execute("INSERT OR IGNORE INTO reminded VALUES(?,?)", (uid, lbl))
            db.commit()

        if is_muted(row["course"]):
            mark_done()
            continue
        if MOODLE_TOKEN:  # fresh status right before nagging
            try:
                await refresh_submission(row)
                row = db.execute("SELECT * FROM events WHERE uid=?", (uid,)).fetchone()
            except Exception:
                log.warning("could not refresh submission for %s; using cached", uid)
        if row["submitted"]:
            mark_done()
            continue
        await send(f"⏰ <b>Due in {label}</b>\n" + line(row, now))
        mark_done()  # after a successful send, so a Telegram outage retries next loop


# ---------- announcements ----------
async def handle_posts(board_key, course, posts):
    """posts: [(post_key, title, url)]. First sight of a board only records a baseline."""
    remember_courses(course)
    baseline = db.execute("SELECT 1 FROM boards WHERE key=?", (board_key,)).fetchone() is None
    for key, title, url in posts:
        if db.execute("SELECT 1 FROM news WHERE id=?", (key,)).fetchone():
            continue
        if not baseline and not is_muted(course):
            await send(f"📢 <b>{esc(course)}</b>\n{esc(title)}\n{esc(url)}")
        db.execute("INSERT INTO news VALUES(?)", (key,))
        db.commit()
    db.execute("INSERT OR IGNORE INTO boards VALUES(?)", (board_key,))
    db.commit()


async def sync_forum_news():
    """Standard Moodle news forums via web service (needs MOODLE_TOKEN)."""
    if not MOODLE_TOKEN:
        return
    info = await ws("core_webservice_get_site_info")
    courses = await ws("core_enrol_get_users_courses", userid=info["userid"])
    names = {c["id"]: c["shortname"] for c in courses}
    if not names:
        return
    forums = await ws("mod_forum_get_forums_by_courses", **{f"courseids[{i}]": c for i, c in enumerate(names)})
    news = [f for f in forums if f.get("type") == "news"]
    if not news:
        log.info("no standard news forums found (site may use ubboard)")
    for f in news:
        res = await ws("mod_forum_get_forum_discussions", forumid=f["id"], perpage=10)
        posts = [
            (f"d{d['discussion']}", d.get("name"), f"{MOODLE_URL}/mod/forum/discuss.php?d={d['discussion']}")
            for d in res.get("discussions", [])
        ]
        await handle_posts(f"forum{f['id']}", names.get(f["course"], str(f["course"])), posts)


# Ubion board fallback: log in like a browser and read the course notice boards.
def _soup(html_text):
    from bs4 import BeautifulSoup

    return BeautifulSoup(html_text, "html.parser")


def web_login(session):
    r = session.get(f"{MOODLE_URL}/login/index.php", timeout=30)
    form = {"username": ECLASS_USERNAME, "password": ECLASS_PASSWORD}
    tok = _soup(r.text).find("input", {"name": "logintoken"})
    if tok:
        form["logintoken"] = tok.get("value", "")
    r = session.post(f"{MOODLE_URL}/login/index.php", data=form, timeout=30)
    if "login/index.php" in urlparse(r.url).path or "logout.php" not in r.text:
        raise FeedError("eclass web login failed (wrong credentials, or the site uses SSO/captcha)")
    return r


def parse_course_links(page_html):
    """{course_id: name} from links to course/view.php?id=N."""
    found = {}
    for a in _soup(page_html).select('a[href*="course/view.php?id="]'):
        m = re.search(r"course/view\.php\?id=(\d+)", a["href"])
        name = " ".join(a.get_text().split())
        if m and (name or m.group(1) not in found):
            found[m.group(1)] = name or found.get(m.group(1), "")
    return found


def parse_board_links(page_html, base):
    """[(name, url)] for ubboard boards whose name looks like a notice board."""
    out = {}
    for a in _soup(page_html).select('a[href*="/mod/ubboard/view.php"]'):
        name = " ".join(a.get_text().split())
        if name and BOARD_FILTER.search(name):
            out[urljoin(base, a["href"]).split("#")[0]] = name
    return [(n, u) for u, n in out.items()]


def parse_articles(page_html, base):
    """[(key, title, url)] for article links on a board listing."""
    out = {}
    for a in _soup(page_html).select('a[href*="/mod/ubboard/article.php"]'):
        url = urljoin(base, a["href"]).split("#")[0]
        title = " ".join(a.get_text().split())
        if title and url not in out:
            out[url] = title
    return [(f"ub:{u}", t, u) for u, t in out.items()]


def scrape_boards():
    """Blocking; returns [(board_key, course_name, posts)]."""
    import requests

    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (eclass-deadline-bot)"
    web_login(s)
    if COURSE_IDS:
        courses = {c: f"course {c}" for c in COURSE_IDS}
    else:
        courses = parse_course_links(s.get(f"{MOODLE_URL}/my/", timeout=30).text)
    result = []
    for cid, name in courses.items():
        page = s.get(f"{MOODLE_URL}/course/view.php?id={cid}", timeout=30).text
        if not name or name.startswith("course "):
            t = _soup(page).find("title")
            name = " ".join(t.get_text().split()).split(":")[0] if t else name
        for bname, burl in parse_board_links(page, MOODLE_URL):
            posts = parse_articles(s.get(burl, timeout=30).text, MOODLE_URL)
            result.append((f"ub:{burl}", name, posts))
    return result


async def sync_web_news():
    if not (ECLASS_USERNAME and ECLASS_PASSWORD):
        return
    for board_key, course, posts in await asyncio.to_thread(scrape_boards):
        await handle_posts(board_key, course, posts)


# ---------- digest ----------
def upcoming(days=None, limit=15, now=None):
    now = utcnow() if now is None else now
    q, args = "SELECT * FROM events WHERE start_ts > ?", [now]
    if days is not None:
        q += " AND start_ts < ?"
        args.append(now + days * 86400)
    return db.execute(q + " ORDER BY start_ts LIMIT ?", args + [limit]).fetchall()


def digest_text(now=None):
    rows = [r for r in upcoming(DIGEST_DAYS, 50, now) if not r["submitted"] and not is_muted(r["course"])]
    if not rows:
        return f"☀️ Good morning! Nothing due in the next {DIGEST_DAYS} days 🎉"
    return f"☀️ <b>Due in the next {DIGEST_DAYS} days</b>\n" + "\n".join(line(r, now) for r in rows)


def digest_due(now_dt):
    """Today's date string if a digest should go out now (08:00-12:00 local, once a day)."""
    local = now_dt.astimezone(TZ)
    today = local.strftime("%Y-%m-%d")
    if DIGEST_HOUR <= local.hour < 12 and state_get("last_digest") != today:
        return today
    return None


async def digest_loop():
    while True:
        try:
            today = digest_due(datetime.now(timezone.utc))
            if today:
                await send(digest_text())
                state_set("last_digest", today)
        except Exception:
            log.exception("digest failed")
        await asyncio.sleep(60)


# ---------- loops ----------
async def sync_moodle():
    await sync_submissions()
    await sync_forum_news()


JOBS = [("calendar", sync_events), ("moodle", sync_moodle), ("eclass-web", sync_web_news)]


async def sync_all():
    errors = []
    for key, fn in JOBS:
        e = await run_job(key, fn)
        if e:
            errors.append(f"{key}: {e}")
    return errors


async def poll_loop():
    while True:
        await sync_all()
        await asyncio.sleep(POLL_MINUTES * 60)


async def reminder_loop():
    while True:
        try:
            await check_reminders()
        except Exception:
            log.exception("reminders failed")
        await asyncio.sleep(300)


# ---------- commands ----------
async def reply_list(m: Message, rows, empty):
    await m.answer("\n".join(line(r) for r in rows) if rows else empty)


def resolve_course(arg):
    names = [r["name"] for r in db.execute("SELECT name FROM courses ORDER BY name")]
    exact = [n for n in names if n.lower() == arg.lower()]
    hits = exact or [n for n in names if arg.lower() in n.lower()]
    return hits


@dp.message(Command("start", "help"))
async def cmd_start(m: Message):
    await m.answer(
        "/upcoming — next deadlines (✅ submitted / ❌ not yet, with MOODLE_TOKEN)\n"
        "/today — due in 24h\n/week — due in 7 days\n"
        "/courses — known courses\n/mute &lt;course&gt; — silence a course\n/unmute &lt;course&gt;\n"
        "/sync — refresh now\n\nDaily digest at 08:00 Tashkent."
    )


@dp.message(Command("upcoming"))
async def cmd_upcoming(m: Message):
    await reply_list(m, upcoming(), "Nothing upcoming 🎉")


@dp.message(Command("today"))
async def cmd_today(m: Message):
    await reply_list(m, upcoming(1), "Nothing due in the next 24h 🎉")


@dp.message(Command("week"))
async def cmd_week(m: Message):
    await reply_list(m, upcoming(7), "Nothing due this week 🎉")


@dp.message(Command("courses"))
async def cmd_courses(m: Message):
    names = [r["name"] for r in db.execute("SELECT name FROM courses ORDER BY name")]
    if not names:
        return await m.answer("No courses yet — try /sync.")
    muted = {r["name"] for r in db.execute("SELECT name FROM muted")}
    await m.answer("\n".join(f"{'🔕' if n.lower() in muted else '🔔'} {esc(n)}" for n in names))


async def _set_mute(m: Message, command: CommandObject, on):
    arg = (command.args or "").strip()
    if not arg:
        return await m.answer(f"Usage: /{'mute' if on else 'unmute'} &lt;course&gt; (see /courses)")
    hits = resolve_course(arg)
    if len(hits) != 1:
        msg = "No course matches that." if not hits else "Be more specific:\n" + "\n".join(esc(h) for h in hits)
        return await m.answer(msg)
    if on:
        db.execute("INSERT OR IGNORE INTO muted VALUES(?)", (hits[0].lower(),))
    else:
        db.execute("DELETE FROM muted WHERE name=?", (hits[0].lower(),))
    db.commit()
    await m.answer(f"{'🔕 Muted' if on else '🔔 Unmuted'} {esc(hits[0])}")


@dp.message(Command("mute"))
async def cmd_mute(m: Message, command: CommandObject):
    await _set_mute(m, command, True)


@dp.message(Command("unmute"))
async def cmd_unmute(m: Message, command: CommandObject):
    await _set_mute(m, command, False)


@dp.message(Command("sync"))
async def cmd_sync(m: Message):
    errors = await sync_all()
    await m.answer("Synced ✅" if not errors else "Sync problems:\n" + "\n".join(esc(e) for e in errors))


async def main():
    tasks = [asyncio.create_task(c()) for c in (poll_loop, reminder_loop, digest_loop)]  # keep refs
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
