import asyncio
import contextvars
import hashlib
import html
import logging
import os
import re
import sqlite3
from datetime import datetime, time as dtime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
MOODLE_URL = os.getenv("MOODLE_URL", "https://eclass.inha.ac.kr").rstrip("/")
ECLASS_USERNAME = os.getenv("ECLASS_USERNAME", "")
ECLASS_PASSWORD = os.getenv("ECLASS_PASSWORD", "")
POLL_MINUTES = int(os.getenv("POLL_MINUTES", "20"))
GRADES_EVERY = 3  # check grades on every 3rd poll
RECHECK_DAYS = 2  # keep re-reading an unsubmitted item this long after its deadline (last-minute and late submissions)
MISSED_DAYS = 30  # /missed looks back this far
_digest = os.getenv("DIGEST_TIME", "").strip()  # "08:00" sends a morning summary; empty = off
DIGEST_AT = datetime.strptime(_digest, "%H:%M").time() if _digest else None
DIGEST_WINDOW = timedelta(hours=3)  # a digest is only sent this long after DIGEST_TIME, never as a stale afternoon message
SILENT_BEYOND_DAYS = 3  # a new deadline further away than this arrives without a sound
FAIL_ALERT = 3  # consecutive failures of one job before the bot tells you
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
ATT_STATUS = ("present", "absent", "late", "other")  # the mark's column on the attendance page, left to right
ATT_ICON = {"present": "✅", "absent": "❌", "late": "🕒", "other": "📝", None: "⏳"}
INBOX_ICON = {"feedback": "💬", "announcement": "📢", "urgent": "🚨", "message": "✉️"}
INBOX_LABEL = {"feedback": "Feedback", "announcement": "Announcement", "urgent": "Urgent", "message": "Message"}
INBOX_LOUD = ("feedback", "announcement", "urgent")  # these ping; plain messages arrive silently
URGENT_RE = re.compile(r"urgent|\basap\b|cancel|postpone|reschedul|campus is closed|(?:don'?t|do not) come|no class|휴강|보강|취소|긴급|연기", re.I)
RES_ICON = {"ubfile": "📄", "vod": "🎥", "folder": "📁", "url": "🔗", "page": "📝"}  # course items that need no submission

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("eclass-bot")

# your own "done" (for things handed in elsewhere) wins over what eclass shows; "mute" only silences reminders
STATUS = "CASE WHEN override = 'done' THEN 'done' ELSE status END"
COLS = f"uid,title,course,start_ts,url,{STATUS}"

db = sqlite3.connect(os.getenv("DB_PATH", "eclass.db"))
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS events(uid TEXT PRIMARY KEY, title TEXT, course TEXT, start_ts INTEGER);
    CREATE TABLE IF NOT EXISTS reminded(uid TEXT, label TEXT, PRIMARY KEY(uid, label));
    CREATE TABLE IF NOT EXISTS resources(uid TEXT PRIMARY KEY, title TEXT, course TEXT, kind TEXT, url TEXT, first_seen INTEGER);
    CREATE TABLE IF NOT EXISTS grades(uid TEXT PRIMARY KEY, course TEXT, item TEXT, grade TEXT, max TEXT, pct TEXT, kind TEXT, updated INTEGER);
    CREATE TABLE IF NOT EXISTS health(job TEXT PRIMARY KEY, last_ok INTEGER, last_err TEXT, fails INTEGER NOT NULL DEFAULT 0, alerted INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS inbox(uid TEXT PRIMARY KEY, kind TEXT, course TEXT, sender TEXT, ts TEXT, title TEXT, url TEXT, preview TEXT);
    CREATE TABLE IF NOT EXISTS attendance(uid TEXT PRIMARY KEY, course TEXT, day TEXT, cls TEXT, status TEXT);
    """
)
for col in ("url", "kind", "status", "override"):  # added later; migrate old databases
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


def like(word):
    """LIKE pattern for 'contains word', with %, _ and \\ in the word taken literally (use with ESCAPE '\\')."""
    return "%" + re.sub(r"([\\%_])", r"\\\1", word.strip()) + "%"


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


_page_cache = contextvars.ContextVar("page_cache", default=None)  # {url: response} while a cycle runs


def web_get(url, **kw):
    """GET with the logged-in session, logging in again if it expired. Blocking.

    Inside run_cycle() a page is fetched once and shared by every job of that cycle.
    """
    cache = _page_cache.get()
    if cache is not None and not kw and url in cache:
        return cache[url]
    if _web is None:
        web_login()
    r = _web.get(url, timeout=30, **kw)
    if _is_login_page(r):
        web_login()
        r = _web.get(url, timeout=30, **kw)
    if cache is not None and not kw and r.ok:
        cache[url] = r
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
    if not ids:  # logged in but nothing listed: eclass changed its page (or you have no courses)
        raise RuntimeError("no courses found on the eclass home page")
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

    Walks every course page; opens an activity only if it may still change (not done, and not long past).
    One failure doesn't stop the rest. Blocking.
    """
    now = datetime.now(timezone.utc).timestamp()
    out, resources = [], []
    for cid, fallback in list_courses().items():
        try:
            course, acts, res = course_activities(cid, fallback)
        except RuntimeError:
            raise
        except Exception as e:
            log.warning("course %s failed: %s", cid, e)
            continue
        resources += [(f"{kind}-{mid}", title, course, kind, url) for kind, mid, title, url in res]
        for kind, mid, title, url in acts:
            uid = f"{kind}-{mid}"
            ts, status = known.get(uid, (None, None))
            if ts is not None and (status == "done" or ts < now - RECHECK_DAYS * 86400):
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


def parse_activity_grade(kind, page):
    """(grade, max) from an assignment or quiz view page, or None while it isn't graded.

    Courses that hide their gradebook still show the grade on the activity itself.
    """
    soup = BeautifulSoup(page, "html.parser")
    if kind == "assign":
        for tr in soup.select(".feedbacktable tr"):
            cells = tr.find_all(["th", "td"])
            if len(cells) == 2 and re.fullmatch(r"grade", cells[0].get_text(strip=True), re.I):
                m = re.search(r"([\d.,]+)\s*/\s*([\d.,]+)", cells[1].get_text(" ").replace("\xa0", " "))
                return (m.group(1), m.group(2)) if m else None
        return None
    table = soup.select_one("table.quizattemptsummary")
    head = [th.get_text(" ", strip=True) for th in table.select("thead th")] if table else []
    col = next((i for i, h in enumerate(head) if re.search(r"grade", h, re.I)), None)
    mx = col is not None and re.search(r"/\s*([\d.,]+)", head[col])
    if not mx:
        return None
    best = None  # the best finished attempt
    for tr in table.select("tbody tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if col < len(cells) and re.fullmatch(r"[\d.,]+", cells[col]):
            if best is None or float(cells[col].replace(",", ".")) > float(best.replace(",", ".")):
                best = cells[col]
    return (best, mx.group(1)) if best else None


def _pct(grade, mx):
    try:
        return f"{float(grade.replace(',', '.')) / float(mx.replace(',', '.')) * 100:.2f} %"
    except (ValueError, ZeroDivisionError, AttributeError):
        return None


def scrape_grades():
    """{course id: (course name, [(item, grade, max, pct, kind)])}. Blocking.

    Uses each course's grade report where eclass lets students see it. For courses that hide it, reads the
    grade from each assignment/quiz page instead (no course total then); a course with neither only gets its
    total from the overview report, if listed. One failing course doesn't stop the rest.
    """
    try:
        overview = parse_overview(web_get(f"{MOODLE_URL}/grade/report/overview/index.php").text)
    except RuntimeError:
        raise
    except Exception as e:
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
            else:
                name, acts, _ = course_activities(cid, fallback)
                items = []
                for kind, mid, title, url in acts:  # a failure here skips the course, keeping its old rows
                    got = parse_activity_grade(kind, web_get(url).text)
                    items.append((clean_title(title), *(got or (None, None)), got and _pct(*got), "item"))
                if items:
                    out[cid] = (name, items)
                elif cid in overview:
                    name, total = overview[cid]
                    out[cid] = (name, [("Course total", total, None, None, "total")])
        except RuntimeError:
            raise
        except Exception as e:
            log.warning("grades of course %s failed: %s", cid, e)
    return out


def parse_attendance(page):
    """(course name or None, [(date 'YYYY-MM-DD', class label, status)]) from an offline-attendance page.

    status is 'present' | 'absent' | 'late' | 'other', or None while the class hasn't been marked yet.
    The list is empty when the course has no offline attendance ("There is no week found").
    """
    soup = BeautifulSoup(page, "html.parser")
    name = None
    for li in soup.select(".course_info li"):
        if re.match(r"course name", li.get_text(strip=True), re.I):
            name = li.get_text(" ", strip=True).split(":", 1)[-1].strip() or None
    rows = []
    for tr in soup.select("table.attendance_my tbody tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
        if len(cells) != 6 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[0]):
            continue  # the "no week found" row
        marks = [st for st, c in zip(ATT_STATUS, cells[2:]) if c]
        rows.append((cells[0], cells[1], marks[0] if marks else None))
    return name, rows


def scrape_attendance():
    """{course id: (course name, [(date, class, status)])} for every course whose page could be read. Blocking.

    Courses without offline attendance come back with an empty list. One failing course doesn't stop the rest.
    """
    out = {}
    for cid, fallback in list_courses().items():
        try:
            name, rows = parse_attendance(web_get(f"{MOODLE_URL}/local/ubattendance/my_status.php?id={cid}").text)
            out[cid] = (name or fallback, rows)
        except RuntimeError:
            raise
        except Exception as e:
            log.warning("attendance of course %s failed: %s", cid, e)
    return out


def _clean(text, n):
    text = re.sub(r"-{5,}", " ", text)
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def classify_message(text):
    """'feedback' | 'announcement' | 'urgent' | 'message' for an inbox message's text."""
    if re.search(r"has\s+posted\s+some\s+feedback\s+on\s+your\s+assignment", text, re.I):
        return "feedback"
    if re.search(r"->\s*(?:Forums|포럼)\s*->\s*(?:Announcements?|News|Notices?|공지)", text, re.I):
        return "announcement"
    return "urgent" if URGENT_RE.search(text) else "message"


def parse_messages(page):
    """[(uid, kind, course, sender, 'YYYY-MM-DD HH:MM', title, url, preview)] from the messages inbox, newest first.

    None if the page has no message list at all (not the inbox, or eclass changed it).
    """
    box = BeautifulSoup(page, "html.parser").select_one("div.message_list")
    if box is None:
        return None
    out = []
    for li in box.select("li.media"):
        sender, when, msg = li.select_one("h4"), li.select_one(".time"), li.select_one(".msg")
        if not (sender and when and msg):
            continue
        a = li.select_one(".media-body > a[href]")
        who = re.search(r"[?&]id=(\d+)", a["href"]).group(1) if a and re.search(r"[?&]id=(\d+)", a["href"]) else "0"
        text = msg.get_text()
        lines = [ln.strip() for ln in text.strip().splitlines()]
        head = lines[0] if lines else ""
        course = (re.match(r"\(?\s*([^\[(]+?)\s*[\[(]", head) or [None, ""])[1]
        url = (re.search(r"https?://\S+", text) or [None])[0]
        kind = classify_message(text)
        if "->" in head:  # system mail: 'Course -> Forums -> Announcements -> Title', body after the dashed line
            title = head.split("->")[-1].strip()
            body = re.split(r"-{10,}", text, maxsplit=1)[-1] if re.search(r"-{10,}", text) else ""
            preview = "" if kind == "feedback" else _clean(body, 220)
            if kind == "feedback":
                title = (re.search(r"for '([^']+)'", text) or [None, title])[1]
        else:  # a message written by a person
            body = re.sub(r"^\(?\s*[^\[(]+[\[(][^\])]*[\])]\)?\s*", "", " ".join(text.split()))
            title, preview = _clean(body, 70), ""
        uid = f"msg:{who}:{when.get_text(strip=True)}:{hashlib.sha1(text.encode()).hexdigest()[:8]}"
        out.append((uid, kind, course, sender.get_text(strip=True), when.get_text(strip=True), title, url, preview))
    return out


def parse_notices(page):
    """[(uid, 'announcement', course, writer, 'YYYY-MM-DD', title, url, '')] from the all-courses Notice Board. None if absent."""
    table = BeautifulSoup(page, "html.parser").select_one("table.ubboard_table")
    if table is None:
        return None
    out = []
    for tr in table.select("tbody tr"):
        cells = tr.find_all("td")
        a = tr.select_one("a[href*='bwid=']")
        if len(cells) < 4 or not a:
            continue
        strong = a.select_one("strong")
        course = re.sub(r"^\[|\]$", "", strong.get_text(strip=True)) if strong else ""
        if strong:
            strong.decompose()
        bwid = re.search(r"bwid=(\d+)", a["href"]).group(1)
        out.append((f"notice:{bwid}", "announcement", course, cells[2].get_text(strip=True), cells[3].get_text(strip=True),
                    " ".join(a.get_text().split()), a["href"], ""))
    return out


def scrape_inbox():
    """{'msg': [...], 'notice': [...]} for the inbox pages that could be read. Blocking."""
    out = {}
    for src, path, parse in (("msg", "/local/ubmessage/", parse_messages), ("notice", "/mod/ubboard/my.php", parse_notices)):
        try:
            items = parse(web_get(MOODLE_URL + path).text)
        except RuntimeError:
            raise
        except Exception as e:
            log.warning("inbox %s failed: %s", src, e)
            continue
        if items is None:
            log.warning("inbox %s: page not recognised", src)
        else:
            out[src] = items
    return out


# ---------- sync jobs ----------
async def sync_events():
    """Pull deadlines and submission status from the course pages; alert on new and moved ones."""
    if not (ECLASS_USERNAME and ECLASS_PASSWORD):
        raise RuntimeError("ECLASS_USERNAME / ECLASS_PASSWORD not set")
    # drop rows from the old iCal-based versions (their uids don't look like 'assign-123')
    db.execute("DELETE FROM events WHERE uid NOT LIKE 'assign-%' AND uid NOT LIKE 'quiz-%'")
    first_run = db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    first_res = db.execute("SELECT COUNT(*) FROM resources").fetchone()[0] == 0
    known = {r[0]: (r[1], r[2]) for r in db.execute("SELECT uid,start_ts,status FROM events")}
    found, resources = await asyncio.to_thread(scrape_courses, known)
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
            far = ts - now > SILENT_BEYOND_DAYS * 86400
            await bot.send_message(CHAT_ID, "🆕 " + line(row), disable_notification=far)
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
        await bot.send_message(CHAT_ID, "📚 <b>New material</b>\n\n" + render_resources(rows[i : i + 25]), disable_notification=True)


def item_row(uid, title, state=None):
    """Buttons of one item: ✅ done / 🔕 mute, or (after one was pressed) a single undo button."""
    name = clean_title(title)[:22]
    if state:
        return [InlineKeyboardButton(text=f"↩️ undo · {'✅' if state == 'done' else '🔕'} {name}", callback_data=f"undo:{uid}")]
    return [
        InlineKeyboardButton(text=f"✅ {name}", callback_data=f"done:{uid}"),
        InlineKeyboardButton(text="🔕 mute", callback_data=f"mute:{uid}"),
    ]


def action_keyboard(rows):
    """✅ marks an item done (submitted elsewhere), 🔕 stops its reminders."""
    return InlineKeyboardMarkup(inline_keyboard=[item_row(r[0], r[1]) for r in rows])


async def check_reminders():
    now = datetime.now(timezone.utc).timestamp()
    rows = db.execute(
        f"SELECT {COLS} FROM events WHERE start_ts > ? AND COALESCE({STATUS},'') != 'done' "
        "AND COALESCE(override,'') != 'mute' ORDER BY start_ts",
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
        await bot.send_message(
            CHAT_ID, f"⏰ <b>Due in {label}</b>\n" + "\n".join(line(r) for r in items), reply_markup=action_keyboard(items)
        )


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
        await bot.send_message(CHAT_ID, "📊 <b>New grades</b>\n\n" + "\n".join(alerts[i : i + 25]), disable_notification=True)


def fmt_day(day):
    return datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b")


async def check_digest(now=None):
    """Once a day, shortly after DIGEST_TIME: what is due today and tomorrow. Nothing due: nothing is sent."""
    if not DIGEST_AT:
        return
    now = now or datetime.now(TZ)
    start = datetime.combine(now.date(), DIGEST_AT, tzinfo=TZ)
    today = now.date().isoformat()
    sent = db.execute("SELECT value FROM kv WHERE key='digest_sent'").fetchone()
    if not (start <= now < start + DIGEST_WINDOW) or (sent and sent[0] == today):
        return
    d = now.date() + timedelta(days=2)
    rows = upcoming(until=datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp(), limit=30, hide_done=True, hide_muted=True, now=now.timestamp())
    if rows:
        await bot.send_message(CHAT_ID, "☀️ <b>Due today and tomorrow</b>\n\n" + render(rows))
    db.execute("INSERT INTO kv VALUES('digest_sent',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (today,))
    db.commit()  # marked only after a successful send, so a Telegram hiccup is retried on the next tick


def inbox_item(row):
    kind, course, sender, ts, title, url, preview = row
    head = f"{INBOX_ICON[kind]} <b>{INBOX_LABEL[kind]}</b> · {esc(short_course(course))}"
    name = f'<a href="{esc(url)}">{esc(title)}</a>' if url else esc(title)
    out = f"{head}\n{name}" + (f" — {esc(sender)}" if sender else "")
    return out + (f"\n<i>{esc(preview)}</i>" if preview else "")


async def sync_inbox():
    """Messages and Notice Board posts. Feedback, announcements and urgent notices ping; the rest arrives silently.

    Each source's first read only records what is already there (kv 'inbox_seen:<source>').
    """
    if not (ECLASS_USERNAME and ECLASS_PASSWORD):
        raise RuntimeError("ECLASS_USERNAME / ECLASS_PASSWORD not set")
    found = await asyncio.to_thread(scrape_inbox)
    new = []
    for src, items in found.items():
        first = not db.execute("SELECT 1 FROM kv WHERE key=?", (f"inbox_seen:{src}",)).fetchone()
        for it in items:
            if not db.execute("SELECT 1 FROM inbox WHERE uid=?", (it[0],)).fetchone():
                db.execute("INSERT INTO inbox VALUES(?,?,?,?,?,?,?,?)", it)
                if not first:
                    new.append(it)
        db.execute("INSERT OR IGNORE INTO kv VALUES(?,'1')", (f"inbox_seen:{src}",))
    db.commit()
    for loud in (True, False):
        group = [it[1:2] + it[2:8] for it in new if (it[1] in INBOX_LOUD) == loud]
        for i in range(0, len(group), 6):  # stay under Telegram's message size limit
            await bot.send_message(CHAT_ID, "\n\n".join(inbox_item(r) for r in group[i : i + 6]), disable_notification=not loud)


async def sync_attendance():
    """Pull offline attendance from eclass; alert when a class is marked absent, late or other (first run only records)."""
    if not (ECLASS_USERNAME and ECLASS_PASSWORD):
        raise RuntimeError("ECLASS_USERNAME / ECLASS_PASSWORD not set")
    first_run = db.execute("SELECT COUNT(*) FROM attendance").fetchone()[0] == 0
    old = {uid: status for uid, status in db.execute("SELECT uid,status FROM attendance")}
    found = await asyncio.to_thread(scrape_attendance)
    alerts = []
    for cid, (course, rows) in found.items():
        db.execute("DELETE FROM attendance WHERE uid LIKE ?", (f"{cid}:%",))
        seen = {}
        for day, cls, status in rows:
            n = seen[(day, cls)] = seen.get((day, cls), 0) + 1
            uid = f"{cid}:{day}:{cls}" + (f"#{n}" if n > 1 else "")
            db.execute("INSERT INTO attendance VALUES(?,?,?,?,?)", (uid, course, day, cls, status))
            if not first_run and status not in (None, "present") and old.get(uid) != status:
                alerts.append(f"• <b>{esc(short_course(course))}</b> · {fmt_day(day)} · {ATT_ICON[status]} {status}")
    db.commit()
    for i in range(0, len(alerts), 25):  # stay under Telegram's message size limit
        await bot.send_message(CHAT_ID, "🗓 <b>Attendance</b>\n\n" + "\n".join(alerts[i : i + 25]))


JOB_LABEL = {
    "sync_events": "deadlines",
    "sync_attendance": "attendance",
    "sync_inbox": "inbox",
    "sync_grades": "grades",
    "check_reminders": "reminders",
    "check_digest": "daily digest",
}


async def notify(text, silent=False):
    """Send a message to you; a Telegram hiccup is logged, never raised (it must not kill a loop)."""
    try:
        await bot.send_message(CHAT_ID, text, disable_notification=silent)
    except Exception:
        log.exception("could not send message")


async def run_job(job, alert=True):
    """Run one job and record its health. Returns the exception, or None on success.

    Tells you once when a job has failed FAIL_ALERT times in a row (a RuntimeError, i.e. a wrong login or an
    eclass page that changed, right away) and once when it works again. alert=False is for /sync, which
    reports to you itself.
    """
    name, now = job.__name__, int(datetime.now(timezone.utc).timestamp())
    fails, alerted = db.execute("SELECT fails, alerted FROM health WHERE job=?", (name,)).fetchone() or (0, 0)
    label = JOB_LABEL.get(name, name)
    try:
        await job()
    except Exception as e:
        log.exception("%s failed", name)
        fails += 1
        send = alert and not alerted and (fails >= FAIL_ALERT or isinstance(e, RuntimeError))
        db.execute(
            "INSERT INTO health(job,last_err,fails,alerted) VALUES(?,?,?,?) "
            "ON CONFLICT(job) DO UPDATE SET last_err=excluded.last_err, fails=excluded.fails, alerted=excluded.alerted",
            (name, str(e) or type(e).__name__, fails, int(alerted or send)),
        )
        db.commit()
        if send:
            await notify(f"⚠️ <b>{esc(label)}</b> isn't updating: {esc(e) or type(e).__name__}")
        return e
    db.execute(
        "INSERT INTO health(job,last_ok,fails,alerted) VALUES(?,?,0,0) "
        "ON CONFLICT(job) DO UPDATE SET last_ok=excluded.last_ok, last_err=NULL, fails=0, alerted=0",
        (name, now),
    )
    db.commit()
    if alerted and alert:
        await notify(f"✅ <b>{esc(label)}</b> is updating again", silent=True)
    return None


async def run_cycle(jobs, alert=True):
    """run_job for each job in turn, sharing one page cache so the jobs don't fetch the same eclass page again.

    The cache lives only for this call, so every poll and every /sync reads fresh pages.
    """
    token = _page_cache.set({})
    try:
        return [await run_job(job, alert) for job in jobs]
    finally:
        _page_cache.reset(token)


async def poll_loop():
    polls = 0
    while True:
        jobs = [sync_events, sync_attendance, sync_inbox] + ([sync_grades] if polls % GRADES_EVERY == 0 else [])
        polls += 1
        await run_cycle(jobs)
        await asyncio.sleep(POLL_MINUTES * 60)


async def reminder_loop():
    while True:
        await run_job(check_reminders)
        if DIGEST_AT:
            await run_job(check_digest)
        await asyncio.sleep(300)


# ---------- commands ----------
def week_end(now):
    """Start of the Monday after the first Sunday on or after now+7d, so next weekend is always in range."""
    d = datetime.fromtimestamp(now, TZ).date() + timedelta(days=7)
    d += timedelta(days=(6 - d.weekday()) % 7 + 1)
    return datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp()


def upcoming(days=None, limit=15, hide_done=False, until=None, match=None, hide_muted=False, now=None):
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    q, args = f"SELECT {COLS} FROM events WHERE start_ts > ?", [now]
    if days is not None:
        until = now + days * 86400
    if until is not None:
        q += " AND start_ts < ?"
        args.append(until)
    if hide_done:
        q += f" AND COALESCE({STATUS},'') != 'done'"
    if hide_muted:
        q += " AND COALESCE(override,'') != 'mute'"
    if match:
        q += " AND (course LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\')"
        args += [like(match)] * 2
    return db.execute(q + " ORDER BY start_ts LIMIT ?", args + [limit]).fetchall()


def missed(days=MISSED_DAYS, limit=20):
    """Unsubmitted items whose deadline passed in the last `days` days, newest first."""
    now = datetime.now(timezone.utc).timestamp()
    return db.execute(
        f"SELECT {COLS} FROM events WHERE start_ts < ? AND start_ts > ? AND {STATUS} = 'todo' ORDER BY start_ts DESC LIMIT ?",
        (now, now - days * 86400, limit),
    ).fetchall()


async def reply_list(m: Message, rows, empty):
    await m.answer(render(rows) if rows else empty)


def ago(ts):
    secs = max(0, int(datetime.now(timezone.utc).timestamp()) - ts)
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{round(secs / 60)} min ago"
    if secs < 172800:
        return f"{round(secs / 3600)} h ago"
    return f"{secs // 86400} d ago"


def render_status():
    """One line per job (last success, or how it's failing) and how much data is stored."""
    health = {r[0]: r[1:] for r in db.execute("SELECT job,last_ok,last_err,fails FROM health")}
    out = ["🩺 <b>Status</b>"]
    for name, label in JOB_LABEL.items():
        if name not in health:
            if name == "check_digest" and not DIGEST_AT:
                continue  # switched off
            out.append(f"⏳ {label} · not run yet")
            continue
        last_ok, err, fails = health[name]
        when = f"last ok {ago(last_ok)}" if last_ok else "never worked"
        if fails:
            out.append(f"❌ {label} · failing ×{fails} · {when}\n    <i>{esc(err)[:200]}</i>")
        else:
            out.append(f"✅ {label} · {when}")
    counts = [(t, db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in ("events", "resources", "grades", "attendance")]
    out.append("\n" + " · ".join(f"{n} {t}" for t, n in counts))
    return "\n".join(out)


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
        total_txt = f" · total {esc(grade_text(*total[2:5]))}" if total else ""  # hidden gradebooks have no total
        if not detail:
            tail = f" · {len(graded)}/{len(parts)} graded" if parts else ""
            out.append(f"📊 {head}{total_txt}{tail}")
            continue
        out.append(("\n" if out else "") + f"{head}{total_txt}")
        out += [f"✅ {esc(r[1])} · {esc(grade_text(*r[2:5]))}" for r in graded]
        if len(parts) > len(graded):
            out.append(f"⏳ {len(parts) - len(graded)} not graded yet")
        if not parts:
            out.append("<i>this course hides its grade details on eclass</i>")
        elif not total:
            out.append("<i>eclass shows no course total here; grades are read from the assignments and quizzes</i>")
    return "\n".join(out)


def render_attendance(rows, detail):
    """rows: (course, day, cls, status). One line per course, or (detail) every class that has been marked."""
    courses = {}
    for r in rows:
        courses.setdefault(r[0], []).append(r)
    out = []
    for course, items in sorted(courses.items()):
        items.sort(key=lambda r: r[1])
        marked = [r for r in items if r[3] is not None]
        pending = [r for r in items if r[3] is None]
        count = lambda st: sum(1 for r in marked if r[3] == st)
        head = f"<b>{esc(short_course(course))}</b>"
        tail = f"{count('present')}/{len(marked)} attended"
        tail += "".join(f" · {ATT_ICON[st]} {count(st)} {st}" for st in ATT_STATUS[1:] if count(st))
        if pending:
            tail += f" · {len(pending)} to come"
        if not detail:
            out.append(f"🗓 {head} · {tail}")
            continue
        out.append(("\n" if out else "") + f"{head} · {tail}")
        out += [f"{ATT_ICON[r[3]]} {fmt_day(r[1])}" for r in marked]
        if pending:
            out.append(f"⏳ next: {fmt_day(pending[0][1])}")
    return "\n".join(out)


async def answer_chunks(m: Message, text):
    """Send text in pieces under Telegram's limit (4096), splitting on lines so tags stay whole."""
    chunk = ""
    for ln in text.split("\n"):
        if chunk and len(chunk) + len(ln) > 3500:
            await m.answer(chunk)
            chunk = ""
        chunk += ("\n" if chunk else "") + ln
    await m.answer(chunk)


def render_inbox(rows):
    """rows: (kind, course, sender, ts, title, url), newest first."""
    out = []
    for kind, course, sender, ts, title, url in rows:
        name = f'<a href="{esc(url)}">{esc(title)}</a>' if url else esc(title)
        out.append(f"{INBOX_ICON[kind]} {name} · {esc(short_course(course))} · {esc(ts[:10])}")
    return "\n".join(out)


MENU = [
    ("upcoming", "Next 15 deadlines"),
    ("today", "Due in the next 24 hours"),
    ("week", "Due until the end of next Sunday"),
    ("missed", "Deadlines that passed unsubmitted"),
    ("done", "Mark something as handed in / mute it"),
    ("resources", "Latest course materials"),
    ("grades", "Your grades"),
    ("attendance", "Your offline attendance"),
    ("inbox", "Latest messages and notices"),
    ("sync", "Refresh from eclass now"),
    ("status", "Is the bot working?"),
    ("help", "What this bot does"),
]
KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="/upcoming"), KeyboardButton(text="/today"), KeyboardButton(text="/week"), KeyboardButton(text="/missed")],
        [KeyboardButton(text="/resources"), KeyboardButton(text="/grades"), KeyboardButton(text="/attendance")],
        [KeyboardButton(text="/inbox"), KeyboardButton(text="/sync"), KeyboardButton(text="/status"), KeyboardButton(text="/help")],
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
    "/done — mark something you handed in elsewhere as done, or mute its reminders. Add a word to narrow it: <code>/done circuit</code>\n"
    f"/missed — deadlines of the last {MISSED_DAYS} days that passed without a submission\n"
    "/resources — latest course materials (files, videos, links). "
    "Add a word to filter: <code>/resources database</code>\n"
    "/grades — your course totals. Add a word for the details of one course: <code>/grades database</code>\n"
    "/attendance — offline attendance of the courses that have it. Add a word for every class of one course: <code>/attendance operating</code>\n"
    "/inbox — latest messages (feedback, announcements, teachers) and notices. Add a word to filter: <code>/inbox attendance</code>\n"
    "/sync — check eclass right now\n"
    "/status — when each check last worked, and whether anything is failing\n\n"
    "<b>Icons</b>\n"
    "❌ not submitted yet · ✅ submitted · ❔ can't tell (e.g. submitted elsewhere)\n"
    "📄 file · 🎥 video · 📁 folder · 🔗 link · 📝 page\n"
    "Attendance: ✅ present · ❌ absent · 🕒 late · 📝 other · ⏳ not taken yet\n\n"
    "<b>Automatic messages</b>\n"
    "🆕 new deadline · ⚠️ deadline moved\n"
    "⏰ reminder 7 days, 3 days, 1 day, 6 hours and 1 hour before a deadline (not for submitted work). "
    "Its buttons: ✅ done · 🔕 mute · ↩️ undo\n"
    "📚 new course material\n"
    "☀️ a morning summary of what is due today and tomorrow, if you turned it on (nothing due: no message)\n"
    "📊 new or changed grade (checked every third refresh)\n"
    "🗓 a class marked absent, late or other\n"
    "💬 feedback on your work · 📢 an announcement · 🚨 an urgent notice (these ping) · ✉️ any other message (silent)\n"
    "⚠️ a check keeps failing (eclass changed, wrong password…) · ✅ it works again\n"
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


@dp.message(Command("done"))
async def cmd_done(m: Message, command: CommandObject):
    """The next unfinished items with ✅ / 🔕 buttons; '/done word' narrows it to a course or title."""
    rows = upcoming(limit=10, hide_done=True, match=(command.args or "").strip() or None)
    if not rows:
        await m.answer("Nothing open" + (" matches that" if command.args else " 🎉"))
        return
    await m.answer(
        "Tap ✅ for what you already handed in elsewhere, 🔕 to stop an item's reminders.\n\n" + render(rows),
        reply_markup=action_keyboard(rows),
    )


@dp.callback_query(F.message.chat.id == CHAT_ID)
async def on_action(q: CallbackQuery):
    action, _, uid = (q.data or "").partition(":")
    row = db.execute("SELECT title FROM events WHERE uid=?", (uid,)).fetchone()
    if action not in ("done", "mute", "undo") or not row:
        await q.answer("That item is gone")
        return
    state = {"done": "done", "mute": "mute", "undo": None}[action]
    db.execute("UPDATE events SET override=? WHERE uid=?", (state, uid))
    db.commit()
    # change only this item's buttons; the other items in the message keep theirs
    rows = [
        item_row(uid, row[0], state) if r[0].callback_data.partition(":")[2] == uid else r
        for r in (q.message.reply_markup.inline_keyboard if q.message.reply_markup else [])
    ]
    await q.answer({"done": "Marked done ✅", "mute": "Muted 🔕", "undo": "Back to normal"}[action])
    await q.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.message(Command("missed"))
async def cmd_missed(m: Message):
    await reply_list(m, missed(), f"Nothing missed in the last {MISSED_DAYS} days 🎉")


@dp.message(Command("inbox"))
async def cmd_inbox(m: Message, command: CommandObject):
    """Latest messages and notices; '/inbox word' filters by course, sender or title."""
    q, args = "SELECT kind,course,sender,ts,title,url FROM inbox", []
    if command.args:
        q += " WHERE course LIKE ? ESCAPE '\\' OR sender LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\' OR preview LIKE ? ESCAPE '\\'"
        args = [like(command.args)] * 4
    rows = db.execute(q + " ORDER BY ts DESC LIMIT 10", args).fetchall()
    await m.answer(render_inbox(rows) if rows else ("Nothing matches that" if command.args else "No messages yet — try /sync first"))


@dp.message(Command("resources"))
async def cmd_resources(m: Message, command: CommandObject):
    """Latest course materials (no submission needed); '/resources database' filters by course or title."""
    q, args = "SELECT uid,title,course,kind,url FROM resources", []
    if command.args:
        q += " WHERE course LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\'"
        args = [like(command.args)] * 2
    # module ids grow over time, so a bigger id means added later
    rows = db.execute(q + " ORDER BY first_seen DESC, CAST(substr(uid, instr(uid,'-')+1) AS INTEGER) DESC LIMIT 15", args).fetchall()
    rows.sort(key=lambda r: r[2])  # group by course (stable, keeps newest-first inside each)
    await m.answer(render_resources(rows) if rows else "No materials found")


@dp.message(Command("grades"))
async def cmd_grades(m: Message, command: CommandObject):
    """Course totals; '/grades database' shows the graded items of matching courses."""
    q, args = "SELECT course,item,grade,max,pct,kind FROM grades", []
    if command.args:
        q += " WHERE course LIKE ? ESCAPE '\\'"
        args = [like(command.args)]
    rows = db.execute(q, args).fetchall()
    if not rows:
        await m.answer("No grades found — try /sync first" if not command.args else "No course matches that")
        return
    await answer_chunks(m, render_grades(rows, detail=bool(command.args)))


@dp.message(Command("attendance"))
async def cmd_attendance(m: Message, command: CommandObject):
    """Offline attendance, only for courses that have it; '/attendance word' lists every class of matching courses."""
    q, args = "SELECT course,day,cls,status FROM attendance", []
    if command.args:
        q += " WHERE course LIKE ? ESCAPE '\\'"
        args = [like(command.args)]
    rows = db.execute(q, args).fetchall()
    if not rows:
        await m.answer("No course matches that" if command.args else "No course has offline attendance yet — try /sync first")
        return
    await answer_chunks(m, render_attendance(rows, detail=bool(command.args)))


@dp.message(Command("sync"))
async def cmd_sync(m: Message):
    jobs = (sync_events, sync_grades, sync_attendance, sync_inbox)  # one failing job doesn't stop the others
    errors = await run_cycle(jobs, alert=False)
    failed = [f"{JOB_LABEL[j.__name__]}: {esc(e) or type(e).__name__}" for j, e in zip(jobs, errors) if e]
    await m.answer("Synced ✅" if not failed else "Synced, but:\n" + "\n".join(failed))


@dp.message(Command("status"))
async def cmd_status(m: Message):
    await m.answer(render_status())


async def main():
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in MENU])
    asyncio.create_task(poll_loop())
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
