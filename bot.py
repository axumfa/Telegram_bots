import asyncio
import html
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import aiohttp
import requests
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, KeyboardButton, Message, ReplyKeyboardMarkup
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
MOODLE_URL = os.getenv("MOODLE_URL", "https://eclass.inha.ac.kr").rstrip("/")
MOODLE_TOKEN = os.getenv("MOODLE_TOKEN", "")  # optional: enables announcements
ECLASS_USERNAME = os.getenv("ECLASS_USERNAME", "")
ECLASS_PASSWORD = os.getenv("ECLASS_PASSWORD", "")
POLL_MINUTES = int(os.getenv("POLL_MINUTES", "20"))
GRADES_EVERY = 3  # check grades on every 3rd poll
TZ = ZoneInfo("Asia/Tashkent")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"}
# smallest first
REMINDERS = [
    ("1h", timedelta(hours=1)),
    ("6h", timedelta(hours=6)),
    ("1d", timedelta(days=1)),
    ("3d", timedelta(days=3)),
    ("7d", timedelta(days=7)),
]
DATE_RE = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})"
ICON = {"done": "✅", "todo": "❌", "na": "📌", "?": "❔"}
RES_ICON = {"ubfile": "📄", "vod": "🎥", "folder": "📁", "url": "🔗", "page": "📝"}  # course items that need no submission

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("eclass-bot")

COLS = "uid,title,course,start_ts,url,status"

db = sqlite3.connect(os.getenv("DB_PATH", "eclass.db"))
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS events(uid TEXT PRIMARY KEY, title TEXT, course TEXT, start_ts INTEGER);
    CREATE TABLE IF NOT EXISTS reminded(uid TEXT, label TEXT, PRIMARY KEY(uid, label));
    CREATE TABLE IF NOT EXISTS news(id TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS resources(uid TEXT PRIMARY KEY, title TEXT, course TEXT, kind TEXT, url TEXT, first_seen INTEGER);
    CREATE TABLE IF NOT EXISTS grades(uid TEXT PRIMARY KEY, course TEXT, item TEXT, grade TEXT, max TEXT, pct TEXT, kind TEXT, updated INTEGER);
    """
)
for col in ("url", "kind", "status"):  # added later; migrate old databases
    try:
        db.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT")
    except sqlite3.OperationalError:
        pass

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
)
dp = Dispatcher()
dp.message.filter(F.chat.id == CHAT_ID)  # only you


def esc(s):
    return html.escape(str(s or ""))


def fmt_ts(ts, date=True):
    return datetime.fromtimestamp(ts, TZ).strftime("%a %d %b %H:%M" if date else "%H:%M")


def left(ts):
    secs = int(ts - datetime.now(timezone.utc).timestamp())
    if secs < 0:
        return "overdue"
    d, r = divmod(secs, 86400)
    h, m = divmod(r // 60, 60)
    return f"{d}d {h}h" if d else f"{h}h {m}m"


def short_course(course):
    """'Electronic Circuit[202602-ICE2080-001]' -> 'Electronic Circuit' (code dropped, long names cut)."""
    name = re.split(r"\[", course or "")[0].strip()
    return name if len(name) <= 30 else name[:29].rstrip(" ,") + "…"


def clean_title(title):
    """Drop the noise Moodle puts in event names ('(Quiz closes)', 'Course[001]-' prefix)."""
    t = re.sub(r"\s*\((?:quiz |assignment )?(?:closes|opens|is due)\)\s*$", "", title or "", flags=re.I)
    t = re.sub(r"\s+(?:is due|closes|opens)\s*$", "", t, flags=re.I)
    t = re.sub(r"^.*?\[\d+\]\s*-\s*", "", t)  # 'Electronic Circuit[001]-13교시' -> '13교시'
    return t.strip() or (title or "?").strip()


def line(row, date=True):
    uid, title, course, ts, url, status = row
    name = esc(clean_title(title))
    if url:
        name = f'<a href="{esc(url)}">{name}</a>'
    tag = short_course(course)
    parts = [f"{ICON.get(status, '•')} {name}"]
    if tag:
        parts.append(esc(tag))
    parts += [fmt_ts(ts, date), left(ts)]
    return " · ".join(parts)


def render(rows):
    """Rows grouped under a bold day heading."""
    out, day = [], None
    for r in rows:
        d = datetime.fromtimestamp(r[3], TZ).strftime("%a %d %b")
        if d != day:
            out.append(f"\n<b>{d}</b>" if out else f"<b>{d}</b>")
            day = d
        out.append(line(r, date=False))
    return "\n".join(out)


# ---------- announcements (Moodle web service) ----------
async def ws(fn, **params):
    params.update(wstoken=MOODLE_TOKEN, wsfunction=fn, moodlewsrestformat="json")
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{MOODLE_URL}/webservice/rest/server.php", data=params) as r:
            data = await r.json(content_type=None)
    if isinstance(data, dict) and data.get("exception"):
        raise RuntimeError(data.get("message"))
    return data


# ---------- eclass web session (username/password) ----------
_web = None  # requests.Session once logged in


def _is_login_page(r):
    return "login" in urlparse(r.url).path or 'name="password"' in r.text[:200_000]


def web_login():
    """Log in like a browser; keeps the session in _web. Blocking."""
    global _web
    s = requests.Session()
    s.headers.update(UA)
    page = s.get(f"{MOODLE_URL}/login/index.php", timeout=30)
    form = {"username": ECLASS_USERNAME, "password": ECLASS_PASSWORD}
    tok = BeautifulSoup(page.text, "html.parser").find("input", {"name": "logintoken"})
    if tok:
        form["logintoken"] = tok.get("value", "")
    r = s.post(f"{MOODLE_URL}/login/index.php", data=form, timeout=30)
    r = s.get(f"{MOODLE_URL}/my/", timeout=30)
    if _is_login_page(r):
        raise RuntimeError("eclass login failed (wrong username/password?)")
    _web = s


def web_get(url, **kw):
    """GET with the logged-in session, logging in again if it expired. Blocking."""
    if _web is None:
        web_login()
    r = _web.get(url, timeout=30, **kw)
    if _is_login_page(r):
        web_login()
        r = _web.get(url, timeout=30, **kw)
    return r


def parse_status(kind, page):
    """'done' | 'todo' | '?' from an assignment or quiz view page."""
    soup = BeautifulSoup(page, "html.parser")
    text = " ".join(soup.get_text(" ").split())
    if kind == "assign":
        if re.search(r"draft|not submitted|no submission|no attempt|제출\s*(된|한)?\s*(내용|과제|파일)?\s*(이|가)?\s*없|미제출|제출하지", text, re.I):
            return "todo"
        if re.search(r"submitted for grading|제출\s*완료|제출됨|제출함", text, re.I):
            return "done"
    elif kind == "quiz":
        rows = soup.select("table.quizattemptsummary tbody tr")
        if rows:
            return "done" if any(re.search(r"finished|종료|완료", r.get_text(" "), re.I) for r in rows) else "todo"
        if soup.select_one(".quizinfo, .box.quizattempt, form[action*='startattempt']") or re.search(r"attempts? allowed|시도", text, re.I):
            return "todo"
    return "?"


def parse_due(kind, page):
    """Deadline (unix ts) from an assignment or quiz view page, or None."""
    soup = BeautifulSoup(page, "html.parser")
    m = None
    if kind == "assign":
        for tr in soup.select(".submissionsummarytable tr"):
            cells = tr.find_all("td")
            if len(cells) == 2 and re.search(r"due date|마감", cells[0].get_text(), re.I):
                m = re.search(DATE_RE, cells[1].get_text())
    else:
        info = soup.select_one(".quizinfo")
        m = info and re.search(r"(?:clos|종료|마감)\D*?" + DATE_RE, info.get_text(" "), re.I)
    if not m:
        return None
    return int(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp())


def list_courses():
    """{course id: name} of the courses on the eclass home page. Blocking."""
    soup = BeautifulSoup(web_get(f"{MOODLE_URL}/").text, "html.parser")
    ids = {}
    for a in soup.find_all("a", href=re.compile(r"course/view\.php\?id=\d+")):
        ids.setdefault(re.search(r"id=(\d+)", a["href"]).group(1), a.get_text(" ", strip=True))
    return ids


def course_activities(cid, fallback_name):
    """(course name, [(kind, module id, title, url)] assignments/quizzes, [same] resources). Blocking."""
    soup = BeautifulSoup(web_get(f"{MOODLE_URL}/course/view.php?id={cid}").text, "html.parser")
    h1 = soup.find("h1")
    name = h1.get_text(" ", strip=True) if h1 else fallback_name
    acts, res = [], []
    for li in soup.select("li.activity"):
        kind = next((c.removeprefix("modtype_") for c in li["class"] if c.startswith("modtype_")), "")
        if kind not in ("assign", "quiz") and kind not in RES_ICON:
            continue
        a = li.select_one("a[href*='/mod/']")
        label = li.select_one(".instancename")
        if not (a and label):
            continue
        for hidden in label.select(".accesshide"):
            hidden.decompose()
        item = (kind, li["id"].removeprefix("module-"), label.get_text(" ", strip=True), a["href"])
        (acts if kind in ("assign", "quiz") else res).append(item)
    return name, acts, res


def scrape_courses(known):
    """known: {uid: (start_ts, status)} -> ([(uid, title, course, ts, url, kind, status)], [(uid, title, course, kind, url)]).

    Walks every course page; opens an activity only if it may still change (not done, not past).
    One failure doesn't stop the rest. Blocking.
    """
    now = datetime.now(timezone.utc).timestamp()
    out, resources = [], []
    for cid, fallback in list_courses().items():
        try:
            course, acts, res = course_activities(cid, fallback)
        except Exception as e:
            if isinstance(e, RuntimeError):
                raise
            log.warning("course %s failed: %s", cid, e)
            continue
        resources += [(f"{kind}-{mid}", title, course, kind, url) for kind, mid, title, url in res]
        for kind, mid, title, url in acts:
            uid = f"{kind}-{mid}"
            ts, status = known.get(uid, (None, None))
            if ts is not None and (status == "done" or ts < now):
                continue
            try:
                page = web_get(url).text
                ts = parse_due(kind, page)
                if ts is not None:
                    out.append((uid, title, course, ts, url, kind, parse_status(kind, page)))
            except Exception as e:
                log.warning("%s failed: %s", uid, e)
    return out, resources


def _grade_value(s):
    s = " ".join((s or "").split())
    return None if s in ("", "-") else s


def parse_grade_report(page):
    """[(item, grade, max, pct, kind)] from a Moodle 'User report' page, kind 'item' or 'total' (the course total).

    None if the course hides grades from students. Ungraded items have grade None; category sub-totals are skipped.
    """
    table = BeautifulSoup(page, "html.parser").select_one("table.user-grade")
    if not table:
        return None
    out = []
    for tr in table.select("tbody tr"):
        head, grade = tr.select_one("th.column-itemname"), tr.select_one("td.column-grade")
        if not (head and grade):
            continue  # category heading row
        classes = head.get("class", [])
        if "baggt" in classes:
            if "level1" not in classes:
                continue  # category sub-total
            kind, name = "total", "Course total"
        else:
            kind, name = "item", re.sub(r"\s*Include empty grades\.?\s*$", "", head.get_text(" ", strip=True))
        rng = tr.select_one("td.column-range")
        mx = re.search(r"[–-]\s*([\d.,]+)\s*$", rng.get_text(strip=True)) if rng else None
        pct = tr.select_one("td.column-percentage")
        out.append((name, _grade_value(grade.get_text()), mx and mx.group(1), _grade_value(pct and pct.get_text()), kind))
    return out


def parse_overview(page):
    """{course id: (course name, total or None)} from the cross-course 'Grades overview' report."""
    out = {}
    for tr in BeautifulSoup(page, "html.parser").select("table.generaltable tbody tr"):
        cells = tr.find_all(["th", "td"])
        a = tr.select_one("a[href*='id=']")
        m = a and re.search(r"[?&]id=(\d+)", a["href"])
        if m and len(cells) >= 2:
            out[m.group(1)] = (a.get_text(" ", strip=True), _grade_value(cells[-1].get_text()))
    return out


def scrape_grades():
    """{course id: (course name, [(item, grade, max, pct, kind)])}. Blocking.

    Uses each course's grade report where eclass lets students see it; for courses that hide it,
    falls back to just the course total from the overview report. One failure doesn't stop the rest.
    """
    try:
        overview = parse_overview(web_get(f"{MOODLE_URL}/grade/report/overview/index.php").text)
    except Exception as e:
        if isinstance(e, RuntimeError):
            raise
        log.warning("grades overview failed: %s", e)
        overview = {}
    out = {}
    for cid, fallback in list_courses().items():
        try:
            page = web_get(f"{MOODLE_URL}/grade/report/user/index.php?id={cid}").text
            rows = parse_grade_report(page)
            if rows is not None:
                h1 = BeautifulSoup(page, "html.parser").find("h1")
                out[cid] = (h1.get_text(" ", strip=True) if h1 else fallback, rows)
            elif cid in overview:
                name, total = overview[cid]
                out[cid] = (name, [("Course total", total, None, None, "total")])
        except Exception as e:
            if isinstance(e, RuntimeError):
                raise
            log.warning("grades of course %s failed: %s", cid, e)
    return out


# ---------- sync jobs ----------
_login_alerted = False


async def sync_events():
    """Pull deadlines and submission status from the course pages; alert on new and moved ones."""
    global _login_alerted
    if not (ECLASS_USERNAME and ECLASS_PASSWORD):
        raise RuntimeError("ECLASS_USERNAME / ECLASS_PASSWORD not set")
    # drop rows from the old iCal-based versions (their uids don't look like 'assign-123')
    db.execute("DELETE FROM events WHERE uid NOT LIKE 'assign-%' AND uid NOT LIKE 'quiz-%'")
    first_run = db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    first_res = db.execute("SELECT COUNT(*) FROM resources").fetchone()[0] == 0
    known = {r[0]: (r[1], r[2]) for r in db.execute("SELECT uid,start_ts,status FROM events")}
    try:
        found, resources = await asyncio.to_thread(scrape_courses, known)
    except RuntimeError as e:
        if not _login_alerted:
            _login_alerted = True
            await bot.send_message(CHAT_ID, f"⚠️ {esc(e)}")
        raise
    _login_alerted = False
    now = datetime.now(timezone.utc).timestamp()
    for uid, title, course, ts, url, kind, status in found:
        old = known.get(uid)
        db.execute(
            "INSERT INTO events(uid,title,course,start_ts,url,kind,status) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(uid) DO UPDATE SET title=excluded.title, course=excluded.course, "
            "start_ts=excluded.start_ts, url=excluded.url, kind=excluded.kind, status=excluded.status",
            (uid, title, course, ts, url, kind, status),
        )
        if first_run or ts < now:
            continue
        row = (uid, title, course, ts, url, status)
        if old is None:
            await bot.send_message(CHAT_ID, "🆕 " + line(row))
        elif old[0] != ts:
            db.execute("DELETE FROM reminded WHERE uid=?", (uid,))
            await bot.send_message(CHAT_ID, f"⚠️ moved (was {fmt_ts(old[0])})\n" + line(row))
    new_res = []
    for row in resources:
        if not db.execute("SELECT 1 FROM resources WHERE uid=?", (row[0],)).fetchone():
            db.execute("INSERT INTO resources VALUES(?,?,?,?,?,?)", (*row, int(now)))
            new_res.append(row)
    db.commit()
    if new_res and not first_res:
        await send_new_resources(new_res)


def res_line(row):
    uid, title, course, kind, url = row[:5]
    name = f'<a href="{esc(url)}">{esc(title)}</a>' if url else esc(title)
    return f"{RES_ICON.get(kind, '•')} {name}"


def render_resources(rows):
    """Resources grouped under a bold course heading."""
    out, course = [], None
    for r in rows:
        if r[2] != course:
            course = r[2]
            out.append(("\n" if out else "") + f"<b>{esc(short_course(course))}</b>")
        out.append(res_line(r))
    return "\n".join(out)


async def send_new_resources(rows):
    for i in range(0, len(rows), 25):  # stay under Telegram's message size limit
        await bot.send_message(CHAT_ID, "📚 <b>New material</b>\n\n" + render_resources(rows[i : i + 25]))


async def check_reminders():
    now = datetime.now(timezone.utc).timestamp()
    rows = db.execute(
        f"SELECT {COLS} FROM events WHERE start_ts > ? AND COALESCE(status,'') != 'done' ORDER BY start_ts",
        (now,),
    ).fetchall()
    batches = {}  # label -> rows, so items due together arrive as one message
    for row in rows:
        uid, ts = row[0], row[3]
        remaining = timedelta(seconds=ts - now)
        due = next((i for i, (_, d) in enumerate(REMINDERS) if remaining <= d), None)
        if due is None:
            continue
        label = REMINDERS[due][0]
        if db.execute("SELECT 1 FROM reminded WHERE uid=? AND label=?", (uid, label)).fetchone():
            continue
        for lbl, _ in REMINDERS[due:]:  # don't fire older, larger ones later
            db.execute("INSERT OR IGNORE INTO reminded VALUES(?,?)", (uid, lbl))
        batches.setdefault(label, []).append(row)
    db.commit()
    for label, items in batches.items():
        await bot.send_message(CHAT_ID, f"⏰ <b>Due in {label}</b>\n" + "\n".join(line(r) for r in items))


async def sync_news():
    """Course announcements via Moodle web service (needs MOODLE_TOKEN)."""
    if not MOODLE_TOKEN:
        return
    first_run = db.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 0
    info = await ws("core_webservice_get_site_info")
    courses = await ws("core_enrol_get_users_courses", userid=info["userid"])
    ids = {c["id"]: c["shortname"] for c in courses}
    if not ids:
        return
    params = {f"courseids[{i}]": cid for i, cid in enumerate(ids)}
    forums = await ws("mod_forum_get_forums_by_courses", **params)
    for f in forums:
        if f.get("type") != "news":
            continue
        res = await ws("mod_forum_get_forum_discussions", forumid=f["id"], perpage=10)
        for d in res.get("discussions", []):
            key = f"d{d['id']}"
            if db.execute("SELECT 1 FROM news WHERE id=?", (key,)).fetchone():
                continue
            db.execute("INSERT INTO news VALUES(?)", (key,))
            if not first_run:
                await bot.send_message(
                    CHAT_ID,
                    f"📢 <b>{esc(ids.get(f['course']))}</b>\n{esc(d.get('name'))}\n"
                    f"{MOODLE_URL}/mod/forum/discuss.php?d={d['discussion']}",
                )
    db.commit()


def grade_text(grade, mx, pct):
    """'85/100 (85.00%)', '85', or '—' when not graded."""
    if grade is None:
        return "—"
    out = f"{grade}/{mx}" if mx else grade
    return f"{out} ({pct.replace(' ', '')})" if pct else out


async def sync_grades():
    """Pull grades from eclass; alert on new and changed ones (the first run only records them)."""
    if not (ECLASS_USERNAME and ECLASS_PASSWORD):
        raise RuntimeError("ECLASS_USERNAME / ECLASS_PASSWORD not set")
    first_run = db.execute("SELECT COUNT(*) FROM grades").fetchone()[0] == 0
    old = {uid: grade for uid, grade in db.execute("SELECT uid,grade FROM grades")}
    found = await asyncio.to_thread(scrape_grades)
    now = int(datetime.now(timezone.utc).timestamp())
    alerts = []
    for cid, (course, rows) in found.items():
        db.execute("DELETE FROM grades WHERE uid LIKE ?", (f"{cid}:%",))
        # a total only gets its own alert when the course hides its items
        has_items = any(r[4] == "item" for r in rows)
        seen = {}
        for item, grade, mx, pct, kind in rows:
            n = seen[(kind, item)] = seen.get((kind, item), 0) + 1
            uid = f"{cid}:{kind}:{item}" + (f"#{n}" if n > 1 else "")
            db.execute("INSERT INTO grades VALUES(?,?,?,?,?,?,?,?)", (uid, course, item, grade, mx, pct, kind, now))
            if first_run or grade is None or old.get(uid) == grade or (kind == "total" and has_items):
                continue
            was = f" (was {esc(old[uid])})" if old.get(uid) is not None else ""
            alerts.append(f"• <b>{esc(item)}</b> · {esc(short_course(course))} · {esc(grade_text(grade, mx, pct))}{was}")
    db.commit()
    for i in range(0, len(alerts), 25):  # stay under Telegram's message size limit
        await bot.send_message(CHAT_ID, "📊 <b>New grades</b>\n\n" + "\n".join(alerts[i : i + 25]))


async def poll_loop():
    polls = 0
    while True:
        jobs = [sync_events, sync_news] + ([sync_grades] if polls % GRADES_EVERY == 0 else [])
        polls += 1
        for job in jobs:
            try:
                await job()
            except Exception:
                log.exception("%s failed", job.__name__)
        await asyncio.sleep(POLL_MINUTES * 60)


async def reminder_loop():
    while True:
        try:
            await check_reminders()
        except Exception:
            log.exception("reminders failed")
        await asyncio.sleep(300)


# ---------- commands ----------
def week_end(now):
    """Start of the Monday after the first Sunday on or after now+7d, so next weekend is always in range."""
    d = datetime.fromtimestamp(now, TZ).date() + timedelta(days=7)
    d += timedelta(days=(6 - d.weekday()) % 7 + 1)
    return datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp()


def upcoming(days=None, limit=15, hide_done=False, until=None):
    now = datetime.now(timezone.utc).timestamp()
    q, args = f"SELECT {COLS} FROM events WHERE start_ts > ?", [now]
    if days is not None:
        until = now + days * 86400
    if until is not None:
        q += " AND start_ts < ?"
        args.append(until)
    if hide_done:
        q += " AND COALESCE(status,'') != 'done'"
    return db.execute(q + " ORDER BY start_ts LIMIT ?", args + [limit]).fetchall()


async def reply_list(m: Message, rows, empty):
    await m.answer(render(rows) if rows else empty)


def render_grades(rows, detail):
    """rows: (course, item, grade, max, pct, kind). Course totals one per line, or (detail) each course's graded items."""
    courses = {}
    for r in rows:
        courses.setdefault(r[0], []).append(r)
    out = []
    for course, items in sorted(courses.items()):
        total = next((r for r in items if r[5] == "total"), None)
        parts = [r for r in items if r[5] == "item"]
        graded = [r for r in parts if r[2] is not None]
        head = f"<b>{esc(short_course(course))}</b>"
        total_txt = esc(grade_text(*total[2:5])) if total else "—"
        if not detail:
            tail = f" · {len(graded)}/{len(parts)} graded" if parts else ""
            out.append(f"📊 {head} · total {total_txt}{tail}")
            continue
        out.append(("\n" if out else "") + f"{head} · total {total_txt}")
        out += [f"✅ {esc(r[1])} · {esc(grade_text(*r[2:5]))}" for r in graded]
        if len(parts) > len(graded):
            out.append(f"⏳ {len(parts) - len(graded)} not graded yet")
        if not parts:
            out.append("<i>this course hides its grade details on eclass</i>")
    return "\n".join(out)


MENU = [
    ("upcoming", "Next 15 deadlines"),
    ("today", "Due in the next 24 hours"),
    ("week", "Due until the end of next Sunday"),
    ("resources", "Latest course materials"),
    ("grades", "Your grades"),
    ("sync", "Refresh from eclass now"),
    ("help", "What this bot does"),
]
KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="/upcoming"), KeyboardButton(text="/today"), KeyboardButton(text="/week")],
        [KeyboardButton(text="/resources"), KeyboardButton(text="/grades"), KeyboardButton(text="/sync"), KeyboardButton(text="/help")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)
HELP = (
    "<b>eclass deadline bot</b>\n"
    f"Checks your eclass courses every {POLL_MINUTES} min and tells you when something changes.\n\n"
    "<b>Commands</b>\n"
    "/upcoming — next 15 deadlines\n"
    "/today — due in the next 24 hours\n"
    "/week — due until the end of next Sunday (so next weekend is included)\n"
    "/resources — latest course materials (files, videos, links). "
    "Add a word to filter: <code>/resources database</code>\n"
    "/grades — your course totals. Add a word for the details of one course: <code>/grades database</code>\n"
    "/sync — check eclass right now\n\n"
    "<b>Icons</b>\n"
    "❌ not submitted yet · ✅ submitted · ❔ can't tell (e.g. submitted elsewhere)\n"
    "📄 file · 🎥 video · 📁 folder · 🔗 link · 📝 page\n\n"
    "<b>Automatic messages</b>\n"
    "🆕 new deadline · ⚠️ deadline moved\n"
    "⏰ reminder 7 days, 3 days, 1 day, 6 hours and 1 hour before a deadline (not for submitted work)\n"
    "📚 new course material\n"
    "📊 new or changed grade (checked every third refresh)\n"
    "Tap a title to open it on eclass."
)


@dp.message(Command("start", "help"))
async def cmd_start(m: Message):
    await m.answer(HELP, reply_markup=KEYBOARD)


@dp.message(Command("upcoming"))
async def cmd_upcoming(m: Message):
    await reply_list(m, upcoming(), "Nothing upcoming 🎉")


@dp.message(Command("today"))
async def cmd_today(m: Message):
    await reply_list(m, upcoming(1, hide_done=True), "Nothing due in the next 24h 🎉")


@dp.message(Command("week"))
async def cmd_week(m: Message):
    now = datetime.now(timezone.utc).timestamp()
    await reply_list(m, upcoming(until=week_end(now), limit=30, hide_done=True), "Nothing due this week 🎉")


@dp.message(Command("resources"))
async def cmd_resources(m: Message, command: CommandObject):
    """Latest course materials (no submission needed); '/resources database' filters by course or title."""
    q, args = "SELECT uid,title,course,kind,url FROM resources", []
    if command.args:
        q += " WHERE course LIKE ? OR title LIKE ?"
        args = [f"%{command.args.strip()}%"] * 2
    # module ids grow over time, so a bigger id means added later
    rows = db.execute(q + " ORDER BY first_seen DESC, CAST(substr(uid, instr(uid,'-')+1) AS INTEGER) DESC LIMIT 15", args).fetchall()
    rows.sort(key=lambda r: r[2])  # group by course (stable, keeps newest-first inside each)
    await m.answer(render_resources(rows) if rows else "No materials found")


@dp.message(Command("grades"))
async def cmd_grades(m: Message, command: CommandObject):
    """Course totals; '/grades database' shows the graded items of matching courses."""
    q, args = "SELECT course,item,grade,max,pct,kind FROM grades", []
    if command.args:
        q += " WHERE course LIKE ?"
        args = [f"%{command.args.strip()}%"]
    rows = db.execute(q, args).fetchall()
    if not rows:
        await m.answer("No grades found — try /sync first" if not command.args else "No course matches that")
        return
    chunk = ""
    for ln in render_grades(rows, detail=bool(command.args)).split("\n"):
        if chunk and len(chunk) + len(ln) > 3500:  # Telegram's limit is 4096; split on lines so tags stay whole
            await m.answer(chunk)
            chunk = ""
        chunk += ("\n" if chunk else "") + ln
    await m.answer(chunk)


@dp.message(Command("sync"))
async def cmd_sync(m: Message):
    try:
        await sync_events()
        await sync_news()
        await sync_grades()
        await m.answer("Synced ✅")
    except Exception as e:
        await m.answer(f"Sync failed: {esc(e)}")


async def main():
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in MENU])
    asyncio.create_task(poll_loop())
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
