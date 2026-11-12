# eclass deadline bot

Telegram bot that tracks deadlines from eclass.inha.ac.kr (Moodle). Single file: `bot.py` (aiogram 3, requests, BeautifulSoup, SQLite).

## What it does

- Logs in to eclass like a browser and reads every course page you're enrolled in every `POLL_MINUTES` (default 20): assignments and quizzes with their **deadline** and whether each is **submitted**. Alerts on **new** and **moved** deadlines.
  ✅ submitted · ❌ not yet · ❔ couldn't tell (e.g. "submit on Gradescope"). Submitted items get no reminders and are hidden from `/today` and `/week`.
- **Missed deadlines**: `/missed` lists items from the last 30 days whose deadline passed while still ❌. An unsubmitted item is re-read for 2 days after its deadline, so a last-minute or late submission is picked up and drops off the list. ❔ items are never called missed.
- **`/done` and mute**: for something you handed in elsewhere (the ❔ items), tap ✅ on a reminder or in `/done [course or title]` and it counts as submitted everywhere: no reminders, hidden from `/today` and `/week`, never listed as missed. 🔕 only stops that item's reminders. Each press turns into an ↩️ undo button. Your choice survives refreshes.
- **Reminders** 7 days, 3 days, 1 day, 6 hours and 1 hour before each deadline, once each. Items due together arrive as one message.
- **Course materials** (files, lecture videos, folders, links, pages — anything without a submission): new ones are announced as 📚 *New material*, grouped by course. `/resources` lists the latest 15; `/resources database` filters by course or title. The first run only records what's already there.
- **Grades**: `/grades` shows one line per course (its total, when eclass has one, and how many items are graded); `/grades database` shows the graded items of matching courses. Every third refresh (about hourly) the grade reports are re-read and a 📊 message arrives when a grade appears or changes (the first run only records them). Courses that hide their grade report from students (eclass answers "Can not view grades") are read from the grade shown on each assignment and quiz instead, so they list graded items but have no course total. A course with no assignments or quizzes (e.g. Engineering Communications) has nothing to show.
- **Offline attendance**: `/attendance` shows one line per course (classes attended out of those already taken, absences, late, and how many are still to come); `/attendance database` lists every class of matching courses. Only courses that actually have offline attendance appear: eclass has the page for every course, so a course counts when its attendance table has at least one class (online courses and courses where the teacher hasn't set it up are skipped). Read on every refresh; a 🗓 message arrives when a class is marked absent, late or other (the first run only records; being present never sends a message).

**Quiet messages**: 📚 new material, 📊 grades, ✅ "works again" and a 🆕 deadline more than 3 days away arrive without a notification sound; deadlines, reminders, absences and "a check is failing" ping as usual.

Each refresh reads every eclass page at most once, even when deadlines, grades and attendance all need it; the next refresh and `/sync` always read fresh pages. `/status` shows when each check last worked.

Message format: `❌ Homework 04 · Electronic Circuit · 23:59 · 1d 10h` (the title links to the activity).

## Setup

1. Create a bot with @BotFather.
2. `./setup.sh` (venv, deps, `.env`, systemd user service). Logs: `journalctl --user -u eclass-bot -f`

Manual: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`, copy `.env.example` to `.env`, `.venv/bin/python bot.py`.
Quote the password in `.env` if it contains spaces or `#`. `.env` is mode 600 and credentials are never logged. SSO-only accounts are not supported (the plain username/password form is used).

## Commands

`/upcoming` next 15 · `/today` next 24 h · `/week` until the end of next Sunday (≥ 7 days, so next weekend is always included) · `/missed` passed unsubmitted · `/done [course]` mark handed in / mute · `/resources [course]` latest materials · `/grades [course]` your grades · `/attendance [course]` offline attendance · `/sync` refresh now · `/help` explains everything (also shown by `/start`, with tap-able buttons; the same list is in Telegram's command menu)

Only the chat id in `.env` is answered.

## Tests

```
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The grade-parser tests use real eclass pages in `tests/fixtures/` with the name, student id and `sesskey` removed. Raw captures named `grades_sample_*.html` are gitignored; never commit them.
