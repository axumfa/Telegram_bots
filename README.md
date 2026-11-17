# eclass deadline bot

Telegram bot that tracks deadlines from eclass.inha.ac.kr (Moodle). Single file: `bot.py` (aiogram 3, requests, BeautifulSoup, SQLite).

## What it does

- Logs in to eclass like a browser and reads every course page you're enrolled in every `POLL_MINUTES` (default 20): assignments and quizzes with their **deadline** and whether each is **submitted**. Alerts on **new** and **moved** deadlines.
- Watches eclass's Notifications page every `WATCH_SECONDS` (default 60, minimum 30, 0 = off), so a new upload reaches you within about a minute instead of waiting for the next full refresh. It reads the inbox every 5 minutes and backs off (up to 10 min between checks) while eclass is slow or failing; `/status` shows its last run and current rate.
  ✅ submitted · ❌ not yet · ❔ couldn't tell (e.g. "submit on Gradescope"). Submitted items get no reminders and are hidden from `/today` and `/week`.
- **No deadline on eclass**: assignments and quizzes with no due date (e.g. work submitted on Gradescope, results placeholders) used to be invisible. They are listed by `/nodeadline` with 📌, and a new one is announced silently. They get no reminders; if one later gets a deadline it moves to the normal lists.
- **Exam weeks**: `/exams [course]` lists each course's midterm and final **week** with its date range, weight in the grade and a countdown (passed ones last). It is read from the syllabus (which week is the exam, how much it counts) and the course page (the dates of each week); the syllabi are re-read about daily and on `/sync`. eclass has no exact exam day, so none is shown, and a course whose syllabus doesn't name the week says so. A 🎓 reminder arrives 7 days and 1 day before an exam week starts (daytime only).
- **Missed deadlines**: `/missed` lists items from the last 30 days whose deadline passed while still ❌. An unsubmitted item is re-read for 2 days after its deadline, so a last-minute or late submission is picked up and drops off the list. ❔ items are never called missed.
- **`/done` and mute**: for something you handed in elsewhere (the ❔ items), tap ✅ on a reminder or in `/done [course or title]` and it counts as submitted everywhere: no reminders, hidden from `/today` and `/week`, never listed as missed. 🔕 only stops that item's reminders. Each press turns into an ↩️ undo button. Your choice survives refreshes.
- **Inbox**: your eclass messages (feedback on your submissions, forum announcements, notes from teachers) and the all-courses Notice Board. 💬 feedback, 📢 announcements and 🚨 urgent notices (a message mentioning cancel / postponed / campus closed / 휴강 …) arrive with a notification; any other message arrives silently. `/inbox [word]` lists the latest 10. Only the two list pages are read (never the conversation pages), and each source's first read only records what is already there.
- **Reminders** 7 days, 3 days, 1 day, 6 hours and 1 hour before each deadline, once each. Items due together arrive as one message.
- **Course materials** (files, lecture videos, folders, links, pages — anything without a submission): new ones are announced as 📚 *New material*, grouped by course. `/resources` lists the latest 15; `/resources database` filters by course or title. The first run only records what's already there.
- **Assignment files**: when a teacher attached files to an assignment (the PDF, starter code…), its line shows `📎 /files_<id>`. Tap it and the bot sends those files here. Files you submitted yourself are never included. Existing assignments pick this up on their next scan.
- **Grades**: `/grades` shows one line per course (its total, when eclass has one, and how many items are graded); `/grades database` shows the graded items of matching courses. Every third refresh (about hourly) the grade reports are re-read and a 📊 message arrives when a grade appears or changes (the first run only records them). Courses that hide their grade report from students (eclass answers "Can not view grades") are read from the grade shown on each assignment and quiz instead, so they list graded items but have no course total. A course with no assignments or quizzes (e.g. Engineering Communications) has nothing to show.
- **Offline attendance**: `/attendance` shows one line per course (classes attended out of those already taken, absences, late, and how many are still to come); `/attendance database` lists every class of matching courses. Only courses that actually have offline attendance appear: eclass has the page for every course, so a course counts when its attendance table has at least one class (online courses and courses where the teacher hasn't set it up are skipped). Read on every refresh; a 🗓 message arrives when a class is marked absent, late or other (the first run only records; being present never sends a message).

**Quiet messages**: 📚 new material, 📊 grades, ✅ "works again" and a 🆕 deadline more than 3 days away arrive without a notification sound; deadlines, reminders, absences and "a check is failing" ping as usual.

**Daily digest** (off by default): set `DIGEST_TIME=08:00` in `.env` and every morning you get what is due today and tomorrow (not submitted, not muted). If nothing is due, no message. It is only sent within 3 hours after that time, so starting the bot in the afternoon doesn't send a stale one.

Each refresh reads every eclass page at most once, even when deadlines, grades and attendance all need it; the next refresh and `/sync` always read fresh pages. `/status` shows when each check last worked.

Message format: `❌ Homework 04 · Electronic Circuit · 23:59 · 1d 10h` (the title links to the activity).

## Setup

1. Create a bot with @BotFather.
2. `./setup.sh` (venv, deps, `.env`, systemd user service). Logs: `journalctl --user -u eclass-bot -f`

Manual: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`, copy `.env.example` to `.env`, `.venv/bin/python bot.py`.
Quote the password in `.env` if it contains spaces or `#`. `.env` is mode 600 and credentials are never logged. SSO-only accounts are not supported (the plain username/password form is used).

## Commands

`/upcoming` next 15 · `/today` next 24 h · `/week` until the end of next Sunday (≥ 7 days, so next weekend is always included) · `/missed` passed unsubmitted · `/nodeadline` work with no deadline · `/exams` exam weeks · `/done [course]` mark handed in / mute · `/resources [course]` latest materials · `/grades [course]` your grades · `/attendance [course]` offline attendance · `/inbox [word]` messages and notices · `/sync` refresh now · `/help` explains everything (also shown by `/start`, with tap-able buttons; the same list is in Telegram's command menu)

Only the chat id in `.env` is answered.

## Tests

```
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The grade-parser tests use real eclass pages in `tests/fixtures/` with the name, student id and `sesskey` removed. Raw captures named `grades_sample_*.html` are gitignored; never commit them.
