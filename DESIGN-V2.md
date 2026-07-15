# V2 Design — Multi-User Support

Self-hosted health coach that supports multiple LINE users, each with their own
Google Health account and Gemini API key. Open signup (no allowlist), designed
for a small group.

---

## 1. Architecture changes

### Current (v1): single-user
```
LINE webhook → one user (LINE_USER_ID)
              → one Google token (data/google_token.json)
              → one Gemini key (.env)
              → global SQLite tables
```

### Target (v2): multi-user
```
LINE webhook → identify user by LINE userId
             → look up their config in users table
             → use their Google token + their Gemini key
             → all data scoped by user_id
```

---

## 2. Database schema changes

### New table: `users`
```sql
CREATE TABLE IF NOT EXISTS users (
    line_user_id     TEXT PRIMARY KEY,           -- LINE userId (U...)
    display_name     TEXT,                       -- LINE display name (fetched on first message)
    google_token_json TEXT,                      -- encrypted Google OAuth token (JSON blob)
    gemini_api_key   TEXT,                       -- encrypted Gemini API key
    timezone         TEXT NOT NULL DEFAULT 'Asia/Bangkok',
    language         TEXT NOT NULL DEFAULT 'English',
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    active           INTEGER NOT NULL DEFAULT 1  -- soft disable
);
```

### Existing tables: add `user_id` column
Every existing table gets a `user_id TEXT NOT NULL` column added to its schema
and primary key / indexes:

- `metrics` → PK becomes `(user_id, day, hour, data_type, source)`
- `sleep_sessions` → PK becomes `(user_id, start, end)`
- `exercise_sessions` → PK becomes `(user_id, start, end)`
- `insights` → add `user_id`, index on `(user_id, kind, ts)`
- `goals` → PK becomes `(user_id, key)`
- `chat_messages` → add `user_id`, index on `(user_id, ts)`
- `coach_memory` → PK becomes `(user_id, name)`
- `sync_log` → add `user_id`

---

## 3. Onboarding flow (LINE Rich Menu)

### Rich Menu buttons:
1. **🔗 Login Google Health** — connect their Fitbit/Pixel Watch
2. **🔑 Set Gemini Key** — provide their own AI key
3. **💬 Chat** — talk to the coach (default action)
4. **📊 My Summary** — trigger a daily summary on demand

### 3.1 Google Health OAuth flow

```
User taps "Login Google Health"
  → Bot generates a unique login URL:
    https://<host>/auth/google?state=<encrypted LINE userId>
  → Bot sends: "Open this link to connect your Google Health account: <url>"
  → User opens in browser → standard OAuth consent screen
  → Callback: GET /auth/google/callback?code=...&state=<encrypted LINE userId>
  → Server exchanges code for tokens, stores in users.google_token_json
  → Bot sends: "✅ Google Health connected!"
```

**Security:**
- The `state` parameter is a signed/encrypted token containing the LINE userId
  so the callback can associate the grant with the correct user.
- Tokens stored encrypted at rest (Fernet with a server-side key from .env).

### 3.2 Gemini Key setup

```
User taps "Set Gemini Key"
  → Bot sends: "Please paste your Gemini API key (get one from https://aistudio.google.com/apikey)"
  → User sends their key as a text message
  → Bot validates it with a test call (models.list or a trivial generate)
  → If valid: store encrypted in users.gemini_api_key, reply "✅ Key saved!"
  → If invalid: reply "❌ That key didn't work. Please check and try again."
```

**Security:**
- Key stored encrypted (same Fernet key).
- Once set, the key is never shown back to the user (only "✅ configured" / "not set").

---

## 4. Per-user service logic

### Sync (hourly)
- Loop through all `users` where `google_token_json IS NOT NULL AND active = 1`
- For each user: load their token, run sync, store metrics with their `user_id`
- Skip users whose token refresh fails (log a warning, mark for re-auth)

### Daily summary / Nudges / Weekly report
- Query users who have both Google + Gemini configured
- For each: build their snapshot, call Gemini with their key, send to their LINE

### Chat / Food photo
- Already scoped to the sending user via LINE userId
- Look up their Gemini key + Google token at call time

---

## 5. Config (.env changes)

```env
# --- Server-level (shared) ---
LINE_CHANNEL_SECRET=...
LINE_CHANNEL_ACCESS_TOKEN=...
ENCRYPTION_KEY=...                 # Fernet key for encrypting user tokens/keys
GOOGLE_CLIENT_SECRET_FILE=data/google_client_secret.json  # shared OAuth client

# --- Removed (no longer per-env) ---
# LINE_USER_ID        → now per-user in DB
# GEMINI_API_KEY      → now per-user in DB
# google_token.json   → now per-user in DB

# --- General ---
TZ=Asia/Bangkok                    # default timezone for new users
```

---

## 6. New endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/auth/google` | Start OAuth flow (redirects to Google) |
| GET | `/auth/google/callback` | OAuth callback (exchanges code, stores token) |
| POST | `/webhook` | LINE webhook (existing, but removes USER_ID check) |
| GET | `/healthz` | Liveness (existing) |

---

## 7. Migration plan (v1 → v2)

1. Add `users` table; insert current user from `.env` LINE_USER_ID + existing token + key
2. Add `user_id` column to all existing tables (default to current user's ID)
3. Update all queries to filter by `user_id`
4. Remove `LINE_USER_ID` check from webhook (any user accepted)
5. Add onboarding flow + rich menu endpoints
6. Add per-user sync loop
7. Add encryption for stored credentials

---

## 8. LINE Rich Menu setup

Create via LINE Messaging API (or the LINE Official Account Manager):

```
┌─────────────────────────────────────────┐
│  🔗 Login         │  🔑 Set Key         │
│  Google Health    │  Gemini AI          │
├─────────────────────────────────────────┤
│  💬 Chat with     │  📊 My Summary      │
│  Coach            │  (on demand)        │
└─────────────────────────────────────────┘
```

Each button sends a postback action text that the webhook recognizes:
- `action=login_google`
- `action=set_gemini_key`
- `action=chat` (default, just opens keyboard)
- `action=my_summary`

---

## 9. Security considerations

- **Token encryption:** all Google tokens and Gemini keys encrypted with Fernet
  before storing in SQLite. Server-side `ENCRYPTION_KEY` in `.env`.
- **No cross-user access:** every DB query filters by `user_id`; no endpoint
  exposes another user's data.
- **OAuth state validation:** signed state parameter prevents CSRF on the
  Google callback (attacker can't link their Google account to someone else's LINE).
- **Key validation:** Gemini keys are tested before saving (prevents storing junk).
- **Soft delete:** `active = 0` disables a user without deleting their data.

---

## 10. Cost model (per user)

| Item | Cost |
|------|------|
| Google Health API | Free |
| Gemini (user's own key) | On them (~$1-5/month) |
| LINE push messages | Free (500/month free tier per bot) |
| Hosting | Shared (your Mac mini) |

---

## 11. Implementation order

1. DB schema migration (add `users` table + `user_id` columns)
2. User lookup/creation on first message
3. Per-user credential storage with encryption
4. Google OAuth web flow (/auth/google, /auth/google/callback)
5. Gemini key setup via chat
6. Refactor all modules to accept `user_id` parameter
7. Per-user sync loop
8. Rich menu creation
9. Remove single-user `.env` config (LINE_USER_ID, GEMINI_API_KEY)
10. Testing with 2+ accounts

---

## 12. Reliability & quality improvements (from v1 review)

These issues were identified during v1 testing and should be addressed in v2:

1. **Token expiry notification** — If Google token refresh fails 3x in a row,
   send the user a LINE message: "Google Health disconnected — please re-authorize."
2. **Chat history cleanup** — Periodic trim: keep last 500 messages per user,
   archive or delete older ones. Run monthly via scheduler.
3. **Insights table cleanup** — Same as above; VACUUM annually.
4. **Error notification** — If daily summary or sync fails repeatedly, notify
   the user via LINE rather than silently failing.
5. **Duplicate food log prevention** — Store a dedup key (image message ID) to
   prevent re-logging the same photo on LINE webhook retry.
6. **Google token refresh race** — Use file locking or DB-stored tokens (v2
   already plans DB-stored tokens, which solves this).
7. **Delete confirmation** — Before executing `[DELETE_LAST]`, the coach should
   ask "Delete X — are you sure?" and only act on confirmation.
8. **Workout plan auto-progression** — Track current week number; auto-advance
   weekly and adjust the daily summary to reference the correct week's schedule.
9. **Food photo context in chat** — Store the food analysis result as a chat
   message so the coach can reference "what you just ate" in follow-up questions.
