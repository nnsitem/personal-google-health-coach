# Personal Google Health AI Coach

Pulls your Google Health (Fitbit / Pixel Watch) data hourly, summarizes it with Gemini
into Google-Health-Premium-style coaching, and messages you on **LINE**. Runs in Docker
on a Mac mini (portable to Windows/Linux). Full design: [DESIGN.md](DESIGN.md).

**Status: All 5 phases complete** — hourly sync, daily AI summary, rule-based nudges,
two-way chat, and adaptive workout plans + weekly reports.

## Features

- **Hourly sync** of steps, calories, resting heart rate, active-zone minutes, and sleep
- **Daily brief** (7:30am) — AI coaching message with sleep, activity, recovery, and today's focus
- **Nudges** — rule-triggered reminders (low steps, streaks, high resting HR, bedtime), rate-limited with quiet hours
- **Two-way chat** — ask the coach anything on LINE; it answers using your real data, remembers goals and preferences
- **Workout plans** — ask the coach to build a multi-week plan; it's saved and referenced in daily briefs
- **Weekly report** — comprehensive Sunday 9:00am summary

## Setup

### 1. Google Health API (one-time)

1. [Google Cloud console](https://console.cloud.google.com) → new project → enable the **Google Health API**.
2. OAuth consent screen → *Testing* mode → add your Google account (linked to Fitbit/Pixel) as a test user, and add the three `googlehealth.*` readonly scopes.
3. Credentials → create OAuth client → type **Desktop app** → download the JSON → save as `data/google_client_secret.json`.
4. Authorize (prints a URL to open in your browser; token lands in `data/`):

   ```sh
   docker compose run --rm -p 8765:8765 coach python -m coach.auth
   ```

### 2. LINE Messaging API (one-time)

1. [LINE Developers console](https://developers.line.biz/console/) → create a Provider → create a **Messaging API channel**.
2. **Basic settings** tab → copy the **Channel secret**.
3. **Messaging API** tab → issue a **Channel access token** (long-lived).
4. Turn **off** auto-reply messages (Messaging API tab → LINE Official Account features).
5. Add the bot as a friend via the QR code.
6. Fill in `.env` (copy from `.env.example`): `LINE_CHANNEL_SECRET`, `LINE_CHANNEL_ACCESS_TOKEN`.
   Leave `LINE_USER_ID` blank at first — send the bot a message and it will appear in the logs, then paste it in.

### 3. Gemini API (one-time)

Get a key from [Google AI Studio](https://aistudio.google.com/apikey) and set `GEMINI_API_KEY` in `.env`.

### 4. Run

```sh
cp .env.example .env       # then fill it in
docker compose up -d --build
```

The container serves the LINE webhook on `127.0.0.1:8080` and runs all scheduled jobs
in-process. State (SQLite DB + Google token) lives in `./data/`.

### 5. Expose the webhook (for two-way chat)

Set `CLOUDFLARE_TUNNEL_TOKEN` in `.env`, create a tunnel route in the Cloudflare dashboard
pointing your hostname (e.g. `coach.signagegold.co`) at `http://coach:8080`, then:

```sh
docker compose --profile webhook up -d --build
```

Register the webhook URL `https://<your-host>/webhook` in the LINE Developers console
(Messaging API tab → Webhook URL → Verify → enable "Use webhook").

## Schedule (in-process, timezone from `TZ`)

| Job | When |
|-----|------|
| Health sync | hourly at :05 |
| Nudge check | hourly at :35 |
| Daily brief | 7:30am |
| Weekly report | Sunday 9:00am |

## Smoke tests

```sh
# Verify Google Health connection + list available data types
docker compose run --rm coach python -m coach.discover

# Pull recent data now
docker compose run --rm coach python -m coach.sync

# Generate + send today's brief
docker compose run --rm coach python -m coach.daily

# Send a LINE hello
docker compose run --rm coach python -m coach.line "Hello from your health coach 🏃"

# Chat with the coach from the CLI
docker compose run --rm coach python -m coach.chat "How did I sleep last night?"

# Inspect the DB
sqlite3 data/coach.db 'SELECT day, data_type, source FROM metrics ORDER BY day DESC LIMIT 20;'
```

## Mac mini host notes

- Set Docker Desktop (or OrbStack) to start at login; `restart: unless-stopped` handles reboots.
- Prevent sleep: `sudo pmset -a sleep 0` (or Energy settings → prevent automatic sleeping).
- Back up `./data/` — it is the only stateful thing.
