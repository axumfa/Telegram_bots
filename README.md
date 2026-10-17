# eclass deadline bot

Telegram bot that tracks deadlines from eclass.inha.ac.kr (Moodle, Coursemos theme).
Single file: `bot.py` (aiogram 3, aiohttp, icalendar, SQLite).

## What it does

- Polls your eclass **calendar export (iCal)** every `POLL_MINUTES` (default 20).
- Alerts on **new** and **changed** deadlines.
- **Reminders** at 3 days, 1 day and 3 hours before each deadline, once each, no duplicates.
- **Daily digest** at 08:00 Asia/Tashkent: what's due in the next 3 days.
- With `MOODLE_TOKEN`: knows which assignments you've **already submitted** (✅/❌ in `/upcoming`) and skips their reminders and digest entries.
- **Announcements**: standard news forums (needs `MOODLE_TOKEN`) and/or the **Ubion board** (`mod/ubboard`) via a web login fallback. Only new posts alert; the first sight of a board is recorded silently.
- **One** Telegram message when something breaks (calendar URL dead, token expired, login failed), and one when it recovers. Details go to the journal.

## Setup

1. Create a bot with @BotFather and copy its token.
2. In eclass: **Calendar → Import or export calendars → Export → All courses**, choose a range, and copy the URL.
3. Run `./setup.sh`. It creates `.venv`, installs dependencies, asks for the bot token and calendar URL, finds your chat id (send `/start` to the bot when asked), writes `.env` (mode 600) and starts a systemd **user** service.
4. Logs: `journalctl --user -u eclass-bot -f`

Manual alternative: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`, copy `.env.example` to `.env`, fill it in, `.venv/bin/python bot.py`.

### Optional: submission status (`MOODLE_TOKEN`)

Get a mobile-app web-service token for your account (eclass → *Preferences → Security keys → Moodle mobile web service*; if your site hides that page the university must issue one). Put it in `.env` as `MOODLE_TOKEN=` and restart. The token needs these functions allowed: `core_webservice_get_site_info`, `core_calendar_get_calendar_event_by_id`, `mod_assign_get_submission_status`, and for forum announcements `core_enrol_get_users_courses`, `mod_forum_get_forums_by_courses`, `mod_forum_get_forum_discussions`. Without a token the bot behaves as before (no ✅/❌).

### Optional: Ubion board announcements (`ECLASS_USERNAME` / `ECLASS_PASSWORD`)

If your courses post notices to `mod/ubboard` rather than the news forum, set your eclass login in `.env`. The bot logs in with a normal web session, reads your course list from `/my/`, finds boards whose name matches `BOARD_FILTER` (default `공지|notice|announce`) and watches their article lists. If the dashboard is JavaScript-rendered or yields no courses, set `COURSE_IDS=123,456`. SSO or captcha logins are not supported. Credentials stay in `.env` and are never logged.

## Commands

| Command | |
|---|---|
| `/upcoming` | next 15 deadlines (✅ submitted / ❌ not yet, with token) |
| `/today` | due in the next 24 h |
| `/week` | due in the next 7 days |
| `/courses` | known courses (🔔 active / 🔕 muted) |
| `/mute <course>` | silence reminders, new-deadline alerts, digest and announcements for a course (partial name is fine) |
| `/unmute <course>` | undo |
| `/sync` | refresh now; reports problems in chat |
| `/help` | this list |

The bot only answers the chat id in `.env`.

## Configuration (`.env`)

See `.env.example`. Required: `BOT_TOKEN`, `CHAT_ID`, `ICAL_URL`. Floating and all-day times in the feed are read in `SITE_TZ` (default `Asia/Seoul`); everything is displayed in Asia/Tashkent.

## Behaviour notes

- Events due within 14 days that vanish from the feed are dropped (deleted by the teacher). Events further out are kept, since export windows are limited. An empty feed never deletes anything.
- A transient network error must happen twice in a row before you're alerted; a dead URL, rejected token or failed login alerts immediately.
- Muted courses are matched by name against the iCal `CATEGORIES` (usually the course code).

## Tests

```
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Tests use `tests/sample.ics` and stub Telegram and Moodle; no network or real credentials.
