# Personal Google Health AI Coach — Design V3

Builds on [`DESIGN.md`](DESIGN.md) (core system) and [`DESIGN-V2.md`](DESIGN-V2.md)
(multi-user). Both are implemented — see `DESIGN.md` §7. This document covers what's
next: the items from `DESIGN-V2.md` §12–13 that are **still open** as of 2026-08-28,
re-verified against the current codebase (not assumed from the old doc, which turned out
to be mostly stale — most of §12 and Tier 1–2 of §13 already shipped), plus fresh research
on how to build each one against the current state of the relevant APIs.

Everything below was checked against the live code before being written up, and every
external claim was checked against a current source (dated 2026-08) rather than recalled
— see §5 Sources.

---

## 0. Already done (for the record, so this doesn't get re-proposed)

Verified in code, not just claimed: delete confirmation (`chat.py`), photo dedup
(`db.claim_message`, `main.py:764`), HRV/SpO₂/respiratory readiness score
(`stats.py:_readiness`), exercise-session sync feeding the coach (`ai.py`, `db.py`),
scheduler misfire recovery (`main.py` `misfire_grace_time=`), failure notifications for
auth/sync/summary/weekly (`notify.py`), image downscaling before upload (`images.py`),
chat/insights/log-message pruning (`db.py:742-765`), baseline-relative nudges
(`nudges.py:_avg_steps`), nutrition targets + tracking (`chat.py` `SET_NUTRITION_TARGETS`).

## 1. Scope for V3

| # | Feature | Why it's still open |
|---|---|---|
| 1 | Verify/enable Gemini prompt caching | Never explicitly checked whether it's firing |
| 2 | Voice messages (LINE audio → coach) | No audio path in `main.py` webhook at all |
| 3 | Trend charts (image, not just text) | `ai.py` cites `trends.*` in prose only |
| 4 | Structured coach memory (profile) | `coach_memory` is still flat name/content pairs |
| 5 | Tiered sync frequency | All data types sync hourly, uniformly |
| 6 | OAuth verification (scale past 100 users) | Still an open decision, not a code gap |

---

## 2. Feature specs

### 2.1 Gemini prompt caching — measure first, maybe do nothing

**Research (2026-08):** Gemini 2.5+ and 3.x models get **implicit caching automatically**,
no code changes — Google discounts any request whose prefix matches a prior request within
the model's TTL. The catch is a **minimum prefix size**: 2,048 tokens for 2.5 models,
**4,096 tokens for 3.x models**. Explicit caching (manual, with a storage fee) exists but
only pays off for a cache you deliberately hold across many calls with a guaranteed hit
rate — not worth the added state for a personal-scale bot.

**Current state, checked against the running prompts:**
- `chat.py:SYSTEM_PROMPT` alone is ~2,460 tokens — under the 4,096-token 3.x threshold by
  itself.
- `GEMINI_FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-3.5-flash-lite"]` and
  `GEMINI_MODEL`/`GEMINI_ACCURACY_MODEL = "gemini-pro-latest"` (`config.py:42,66,70`) are
  all 2.5+/3.x — implicit caching *can* apply, but only once the full repeated prefix
  (system prompt + whatever fixed preamble precedes the variable health snapshot) clears
  4,096 tokens. Whether it does depends on `contents` ordering, which wasn't designed with
  caching in mind.

**Proposed work (small, verification-first):**
1. ✅ **Done (2026-08-28).** `gemini.py:_log_cache_usage` logs
   `response.usage_metadata.cached_content_token_count` vs. `prompt_token_count` as a
   debug-level HIT/MISS after every `generate_content` call (`_try_model`, alongside the
   existing `_warn_if_truncated`). `tests/test_gemini.py` covers HIT/MISS/zero-cached
   logging, that a malformed or missing `usage_metadata` never raises, and that the
   logged response text is unaffected end-to-end through `generate()` (145/145 pass).
   Confirmed no impact on generation behavior or test isolation (full suite run clean,
   twice, and with `test_gemini` run standalone).
2. **Next:** run for a few days of real traffic with debug logging on; check whether cache
   hits are happening at all (`grep "cache HIT\|cache MISS"` in logs).
3. If they aren't (likely, given the prompt is borderline-small and health-data snapshots
   are user-specific so `contents` never truly repeats), reorder `_build_config` /
   `contents` construction so the **large, byte-identical part** (system prompt) is the
   fixed prefix and *only* the trailing turn is fresh — implicit caching indexes strictly
   from the front.
4. Do not build explicit caching. At this traffic volume (single-digit users, a few Gemini
   calls/user/day) the write cost plus storage meter is very unlikely to beat the free
   automatic path, and it adds a cache-invalidation surface for no measured benefit.

This is a half-day task, not a feature — the deliverable is a measurement, with a
one-line reorder as the only likely code change.

### 2.2 Voice messages — ✅ done (2026-08-28)

**Research (2026-08):** LINE's webhook already delivers audio messages the same way it
delivers images — a `message.type == "audio"` event with a `message.id`, downloadable via
the SDK's `blob_api.get_message_content(message_id)` (the exact call `line.py:121`
already uses for photos; LINE's underlying content host is `api-data.line.me`, which the
SDK handles internally). Gemini's audio understanding (current model note: transcription
is now handled by a dedicated `gemini-3.5-transcribe` model, but the general-purpose 3.x
models the coach already calls also accept audio parts directly for understanding, not
just dedicated transcription) means the coach doesn't need a separate transcription step —
it can take the audio bytes as a content part alongside the existing chat pipeline, the
same way `food.py:536` already passes image parts inline.

**Proposed design (mirrors the existing image path almost exactly):**
1. `main.py:784` — add `elif msg_type == "audio": background_tasks.add_task(_process_audio_message, user_id, msg.get("id", ""), reply_token)`, next to the existing `image` branch.
2. `line.py` — generalize `get_image_content` into `get_message_content(message_id)` (it's already type-agnostic; only the name implies images). Existing photo callers keep working unchanged.
3. New `_process_audio_message` in `main.py` (mirrors `_process_image_message`): download bytes, pass as an inline audio part to the *chat* pipeline (`chat.handle_message`), not a new bespoke path — the transcribed intent should flow through the same directive parser (`LOG_FOOD`, `SET_NUTRITION_TARGETS`, etc.) that text chat already uses, so "log a banana" said out loud behaves identically to typing it.
4. Reply with the transcript quoted back (`"🎤 heard: ..."`) before the coach's answer, so misheard audio is visible and correctable — LINE has no native indication the bot understood correctly otherwise.
5. No new dependency: `google-genai` already accepts inline bytes; no separate speech-to-text call or library needed.

**Shipped as designed**, with one simplification found while implementing: rather than a
bespoke voice-specific reply path, `_process_audio_message` echoes the transcript
("🎤 Heard: ...") on the (single-use) reply token, then calls `_process_text_message`
with the transcript as a push — so voice gets help/login/set-key commands and directive
parsing for free, with zero duplicated logic. `line.py:get_image_content` was generalized
to `get_message_content` (the LINE endpoint was already type-agnostic; only the name
implied otherwise) — its one caller (`_process_image_message`) was updated, no
back-compat alias kept. No hard duration cap was enforced — Gemini accepts audio up to 9.5
hours and LINE's actual cap wasn't confirmed during research, so a long clip is left to
fail through the existing generic-error path rather than encode an unverified limit.

Tests: `tests/test_voice.py` (`transcribe_voice_message` — hit/no-speech/blank/failure/
propagation/mime-type) and `tests/test_main.py` (`_process_audio_message` — gating,
download failure, quota/unavailable messaging, and that a successful transcript is echoed
once then handed to the text pipeline with the reply token cleared).

### 2.3 Trend charts — ✅ done (2026-08-28), and smaller than planned

**Correction to the original research above:** before building the Pillow/tunnel-URL
pipeline this section originally proposed, a closer read of `flex.py` turned up
`_mini_bar_chart` — the weekly report **already** renders a 7-day **steps** bar chart
natively in Flex (box components as bars, no image at all; `flex.py:326-357`,
`weekly.py:_weekly_view_model`'s `steps_series`). The "no chart exists" premise this
section started from was wrong — it only held for `ai.py`'s daily-brief narrative, which
was as far as the original research looked. A raster/PNG pipeline would have duplicated
working infrastructure for a strictly worse result (needs the Cloudflare tunnel, a cleanup
job, and a new Pillow drawing routine, for a less theme-consistent result than a native
Flex box already gives for free).

**What shipped instead:** extended the existing mechanism to a second metric —
**sleep hours per night** — the same way `steps_series` already works:
1. `weekly.py:_weekly_view_model` — added `sleep_series`, built from
   `snapshot["sleep_sessions"]` (summed per calendar date, same `tick()` helper the steps
   series uses; factored out of what was steps-only inline code).
2. `flex.py:build_weekly_report_bubble` — takes an optional `sleep_series` param, renders
   a second `_mini_bar_chart` block (`🛌 SLEEP · 7-DAY`) under the steps chart. Omitted
   entirely when there's no data to chart, same as the steps chart already did.
3. Added `sleep_7day` labels (en/th) next to the existing `steps_7day` ones.
4. `weekly.py:WEEKLY_SYSTEM_PROMPT` updated to mention both charts are already shown, so
   the narrative doesn't restate sleep hours the card already displays.

Resting-HR and active-zone-minutes stay as the existing averages-with-trend-chip rows,
not charts — daily resting-HR is noisier and a single well-understood number-plus-arrow
already answers "is this trending the right way" better than 7 tiny bars would for that
metric. Revisit only if a user asks for it.

Tests: `tests/test_weekly.py:SleepSeries` (series shape/values) and `tests/test_flex.py`
(chart appears/disappears correctly in the rendered bubble, steps chart unaffected,
bubble stays JSON-serializable).

### 2.4 Structured coach memory — ✅ done (2026-08-28)

**Was:** `coach_memory` (`db.py:87`) was `(user_id, name, content)` — flat, free-text
key-value, read back by `chat.py` via `[MEMORY: ...]` directives and only special-cased
for `name = 'language'` (`db.py:513-523`).

**Shipped, as planned — no schema rewrite:**
1. Added a nullable `category` column (`injury`/`diet`/`goal`/`preference`/`general` —
   `chat.MEMORY_CATEGORIES`) via the existing `_ADD_COLUMNS` migration mechanism (same one
   that's added every other column since v2). The `[MEMORY: category:key = value]` syntax
   is optional — a plain `[MEMORY: key = value]` behaves exactly as before, and only a
   *recognized* category prefix is split off (so a key that legitimately contains `:` for
   another reason isn't mangled).
2. New shared `db.get_coach_memory(user_id, category=None, limit=20)` — one query instead
   of the three near-identical ones that used to live separately in `chat.py`, `ai.py`,
   and (newly) `food.py`. `chat._get_coach_memory` (flat dict) is now a thin wrapper over
   it, kept for its one real caller (the `language` lookup path/test) — unchanged
   behavior.
3. `chat._build_context_message` and `ai.py`'s daily-brief snapshot now group memory by
   category in the prompt (`{"diet": {...}, "goal": {...}, "general": {...}}`) instead of
   one flat list, with an explicit instruction to weigh `injury`/`diet` entries into
   food/exercise advice.
4. **The concrete deterministic case:** `food.analyze_food_images` now pulls
   `category="diet"` memories directly and injects them into the vision prompt
   unconditionally (not left to the model noticing them in a long list) — an allergy is a
   worse thing to miss than an ordinary preference. Scoped to the main photo-vision path
   only, not the restaurant-lookup refinement call or the text-only log fallback
   (`chat._extract_log_fallback`) — lower-traffic paths, left for later if it turns out to
   matter.

No new profile API, no admin UI, no PK change — exactly the scope planned.

Tests: `tests/test_db.py:CoachMemory` (the shared query), `tests/test_chat_directives.py`
(category-prefix parsing, unrecognized-prefix safety, grouped context message),
`tests/test_food_prompt.py` (diet facts reach the vision prompt, other categories don't).

### 2.5 Tiered sync frequency

**Current:** every data type syncs hourly (`main.py:317`, `_safe_sync_all`), uniformly.

**Assessment: low priority, likely not worth building.** The stated motivation in
`DESIGN-V2.md` §13.10 was Health API load/quota — but nothing in `DESIGN.md` §10's
"operational facts learned the hard way" (which documents every real API constraint hit in
production) mentions a rate-limit or quota problem from hourly syncing all types. Slow-
moving types (HRV baseline, respiratory rate) don't cost meaningfully more to fetch hourly
than daily at this user scale (single-digit users, personal Mac mini). **Recommend
deferring** — revisit only if `sync.py` failures start correlating with quota errors in
`notify.py`'s `sync` failure tracking, at which point tiering fast vs. slow types would be
the fix, not before.

### 2.6 OAuth verification (scale past 100 users)

**Research (2026-08):** Restricted health scopes require two things to leave Testing mode:
Google's app-identity/Trust-and-Safety review, and an **annual third-party CASA security
assessment** (OWASP-based) — **$500–$4,500 USD** and **2–6 weeks**, depending on tier, paid
to the assessor, not Google. Testing mode caps at 100 manually-added test users.

**Recommendation: no code change, stay on the plan `DESIGN-V2.md` §14 already
recommended** (Option A — Testing mode, manually add each person's email). The
$500–$4,500/year plus review lead time only makes sense past a genuinely open-signup
scale; for a small trusted group on a personal Mac mini it's a pure cost with no
corresponding benefit. Document the two-step signup (operator adds the email as a test
user, then the person authorizes) if it isn't already — worth checking `README.md`'s
onboarding section covers this explicitly.

---

## 3. Suggested order

1. **2.1 caching measurement** — half a day, tells you if there's a real cost problem
   worth solving before building anything else.
2. **2.3 trend charts** — reuses existing plumbing almost entirely, clearest
   user-visible improvement.
3. **2.2 voice messages** — mirrors the existing image path closely; medium effort,
   clear value.
4. **2.4 structured memory** — small, do opportunistically alongside other `chat.py` work.
5. **2.5 tiered sync** — deferred, revisit only if evidence appears.
6. **2.6 OAuth verification** — a decision to leave as-is, not a task.

---

## 4. Sources

- [Context caching (Gemini API docs)](https://ai.google.dev/gemini-api/docs/caching) — implicit caching defaults, per-model token thresholds
- [Gemini API Pricing 2026 — context caching rates](https://geotoolbox.ai/blog/gemini-api-pricing)
- [Gemini API audio transcription docs](https://ai.google.dev/gemini-api/docs/transcribe) — `gemini-3.5-transcribe`, multimodal audio understanding
- [LINE Messaging API reference — Get content](https://developers.line.biz/en/reference/messaging-api/) — audio/image content download via `api-data.line.me`
- [LINE Messaging API — receiving messages](https://developers.line.biz/en/docs/messaging-api/receiving-messages/)
- [Google Health API — App verification](https://developers.google.com/health/app-verification) — CASA assessment, cost, timeline
- [Restricted scope verification (Google Identity docs)](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification) — 100-user Testing-mode cap
