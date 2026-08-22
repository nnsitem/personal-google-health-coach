"""Centralized Gemini client with quota-aware cross-model retry.

Google's Gemini API fails in distinct ways that deserve distinct handling:

- 503 UNAVAILABLE / 500: the model tier is overloaded. Worth retrying, but
  re-hitting it every few seconds adds to the pressure, spams the logs, and
  burns the key's own request quota. Each failure parks that model on a
  short cooldown instead.
- 429 RESOURCE_EXHAUSTED (per-minute): rate spike. The error usually carries
  an explicit retryDelay, which is honored instead of guessed at.
- 429 RESOURCE_EXHAUSTED (per-DAY): the free-tier daily cap for that model
  is spent — retrying is pointless until the quota resets (midnight US
  Pacific), so the model is parked until then, per API key (each user
  brings their own). When every model is parked, GeminiQuotaExhausted is
  raised immediately so callers can tell the user honestly, and scheduled
  jobs stop burning a full retry window per user per hour rediscovering
  the same dead quota.
- 404 / PERMISSION_DENIED: the model doesn't exist for this key — removed
  from rotation for the process lifetime.
- 400 mentioning thinking/budget: config rejection — handled by the
  per-model thinking ladder below, sent at most once per model.

Replies are delivered via LINE (reply token or push, no hard deadline), so
waits can be generous — but interactive callers (chat, food photos) pass a
shorter max_wait so users aren't left staring at a silent chat.

All modules (chat, ai, nudges, weekly, plans, food) call generate() here so
retry behavior is consistent in one place.
"""

import logging
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google import genai

from coach.config import (GEMINI_MODEL, GEMINI_FALLBACK_MODELS, GEMINI_ACCURACY_MODEL,
                          GEMINI_MAX_WAIT_SECONDS, GEMINI_REQUEST_TIMEOUT_SECONDS,
                          GEMINI_THINKING_LEVEL)

log = logging.getLogger(__name__)

# Errors that are transient — worth retrying after a cooldown.
# "timeout"/"timed out" included so a model that stalls gets a cooldown like any
# other transient fault, instead of being retried straight into another stall.
_TRANSIENT_MARKERS = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500",
                      "INTERNAL", "502", "Bad Gateway", "504", "DEADLINE_EXCEEDED",
                      "Deadline", "timeout", "timed out", "Timeout", "ReadTimeout")
# Errors meaning "this model won't work for this key" — drop from rotation.
_SKIP_MARKERS = ("404", "NOT_FOUND", "PERMISSION_DENIED")

# Default cooldowns (seconds) when the error doesn't say how long to wait.
_OVERLOAD_COOLDOWN = 15.0  # 503/500 — tier overload persists tens of seconds
_RATE_COOLDOWN = 30.0      # 429 without an explicit retryDelay
_PERMANENT = float("inf")

# (api_key, model) -> time.time() before which the pair is not tried.
# Keyed per api_key because quotas are per key; overload cooldowns are
# technically global per model, but rediscovering one per key is harmless.
_cooldown_until: dict[tuple[str, str], float] = {}

# Thinking configs tried per model, cheapest first. thinking_level replaced the
# legacy thinking_budget in the Gemini API; MINIMAL ≈ old budget=0 (no thinking,
# fast). Some models reject levels they don't support (e.g. gemini-pro-latest
# rejects MINIMAL but accepts LOW), so each model climbs this ladder on a
# config-rejection 400 and the winning rung is cached for the process lifetime —
# without the cache the bad config would be re-sent on every retry round.
# Starts at the configured level and degrades only when a model REJECTS it.
# "MINIMAL" used to be first; the API now answers 400 INVALID_ARGUMENT for it, so
# every model burned one wasted call per process just discovering that.
_THINKING_LADDER = (GEMINI_THINKING_LEVEL, None)  # None = model's own default
_model_thinking_rung: dict[str, int] = {}


class GeminiUnavailable(RuntimeError):
    """Raised when all models stay unavailable for the whole time budget."""


class GeminiQuotaExhausted(GeminiUnavailable):
    """Every model's daily free-tier quota is spent for this API key."""


def _parse_retry_delay(msg: str) -> float | None:
    """Extract the server-suggested retry delay from a 429 error body."""
    m = re.search(r"retry_?[Dd]elay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)\s*s", msg)
    if m:
        return float(m.group(1))
    m = re.search(r"retry in (\d+(?:\.\d+)?)", msg, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


def _is_daily_quota(msg: str) -> bool:
    """Whether a 429 is the per-DAY cap (vs a per-minute rate spike).

    Daily-quota violations carry quota ids like
    'GenerateRequestsPerDayPerProjectPerModel-FreeTier'.
    """
    lowered = msg.lower()
    if "resource_exhausted" not in lowered and "429" not in msg:
        return False
    return bool(re.search(r"per\s?day|daily", lowered))


def _seconds_until_quota_reset() -> float:
    """Free-tier daily quotas reset at midnight US Pacific."""
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - now).total_seconds() + 60  # small buffer past the reset


def _http_options():
    """Per-request timeout, and no retrying inside the SDK.

    Two separate problems, one object:

    - No timeout is the SDK default, so a stalled connection blocks forever.
      One call hung for 30m32s on 2026-08-22 and the reply arrived after the
      LINE reply token had died.
    - The SDK retries 5 times by default (408/429/5xx) against the SAME model
      with its own backoff. That competes with the rotation below, which is
      strictly better informed: it moves to a different capacity tier, parks a
      model whose daily quota is spent, and honors a server retryDelay. Letting
      the SDK burn the budget on a model we already know is failing just delays
      the failover — a 503 took ~14s to surface instead of ~2s.

    Returns None when the installed SDK predates these fields, leaving the old
    behavior rather than failing to build a client at all.
    """
    try:
        return genai.types.HttpOptions(
            timeout=GEMINI_REQUEST_TIMEOUT_SECONDS * 1000,   # milliseconds
            retry_options=genai.types.HttpRetryOptions(attempts=1),
        )
    except Exception:
        log.warning("this google-genai build has no HttpOptions timeout/retry_options — "
                    "requests will be unbounded", exc_info=True)
        return None


def _thinking_config(model: str):
    """Build the thinking config for a model's current ladder rung (None = omit)."""
    level = _THINKING_LADDER[_model_thinking_rung.get(model, 0)]
    if level is None:
        return None
    try:
        return genai.types.ThinkingConfig(thinking_level=level)
    except Exception:
        # SDK predates thinking_level — fall back to the legacy budget knob,
        # which only maps cleanly to the "no thinking" rung.
        if level == "MINIMAL":
            return genai.types.ThinkingConfig(thinking_budget=0)
        return None


def _warn_if_truncated(model: str, response, max_output_tokens: int) -> None:
    """Log when a response was cut off by the output cap.

    A truncated reply still arrives as ordinary text, so callers can't tell.
    That matters because coach.chat puts its [LOG_FOOD]/[MEMORY] directives at
    the END of the reply — a response clipped mid-sentence loses them, and the
    log silently never happens. Thinking tokens count against this same cap,
    so the visible text can be short while the budget is spent.
    """
    try:
        candidate = (response.candidates or [None])[0]
        reason = str(getattr(candidate, "finish_reason", "") or "")
        if "MAX_TOKENS" not in reason.upper():
            return
        usage = getattr(response, "usage_metadata", None)
        log.warning(
            "model %s hit the %d-token output cap (finish_reason=%s, thoughts=%s, "
            "output=%s) — the reply is truncated and any trailing directive is lost",
            model, max_output_tokens, reason,
            getattr(usage, "thoughts_token_size", None) or getattr(usage, "thoughts_token_count", None),
            getattr(usage, "candidates_token_count", None),
        )
    except Exception:  # diagnostics must never break generation
        pass


def generate(
    api_key: str,
    contents,
    system_instruction: str | None = None,
    max_output_tokens: int = 2048,
    max_wait: int | None = None,
    min_chars: int = 1,
    prefer_accuracy: bool = False,
) -> str:
    """Generate content, retrying across models until success or timeout.

    contents: str or list (list supports multimodal, e.g. [prompt, image_part])
    Returns the response text. Raises GeminiQuotaExhausted when the key's
    daily quota is spent on every model, GeminiUnavailable otherwise.
    """
    if not api_key:
        raise RuntimeError("No Gemini API key provided")

    if max_wait is None:
        max_wait = GEMINI_MAX_WAIT_SECONDS

    client = genai.Client(api_key=api_key, http_options=_http_options())
    # prefer_accuracy puts the strongest model first for calls where a wrong
    # number is worse than a slow answer (a meal's nutrition, which the user
    # then acts on). Everything else leads with the faster default and keeps it
    # as the first fallback, so an accuracy call still gets an answer if the
    # strong tier is down.
    chain = [GEMINI_ACCURACY_MODEL, GEMINI_MODEL] if prefer_accuracy else [GEMINI_MODEL]
    models = chain + [m for m in GEMINI_FALLBACK_MODELS if m not in chain]

    def _build_config(model: str):
        cfg_kwargs = {"max_output_tokens": max_output_tokens}
        thinking = _thinking_config(model)
        if thinking is not None:
            cfg_kwargs["thinking_config"] = thinking
        if system_instruction:
            cfg_kwargs["system_instruction"] = system_instruction
        return genai.types.GenerateContentConfig(**cfg_kwargs)

    def _try_model(model: str):
        """Call one model. Returns text on success, None on empty. Raises on error.
        On a thinking-config rejection, advances the model's ladder rung (cached
        process-wide) and retries the same model with the next config."""
        while True:
            try:
                response = client.models.generate_content(
                    model=model, contents=contents, config=_build_config(model)
                )
                text = response.text
                _warn_if_truncated(model, response, max_output_tokens)
                return text if (text and len(text.strip()) >= min_chars) else None
            except Exception as e:
                msg = str(e).lower()
                rung = _model_thinking_rung.get(model, 0)
                if rung < len(_THINKING_LADDER) - 1 and ("thinking" in msg or "budget" in msg):
                    _model_thinking_rung[model] = rung + 1
                    log.info(
                        "model %s rejected thinking config %s — using %s from now on",
                        model, _THINKING_LADDER[rung], _THINKING_LADDER[rung + 1],
                    )
                    continue
                raise

    deadline = time.time() + max_wait
    last_error = None
    round_num = 0

    while True:
        round_num += 1
        now = time.time()

        for model in models:
            if _cooldown_until.get((api_key, model), 0.0) > now:
                continue
            try:
                text = _try_model(model)
                if text:
                    if round_num > 1:
                        log.info("Gemini succeeded on %s (round %d)", model, round_num)
                    return text
                log.warning("model %s returned empty/short response", model)
                last_error = last_error or RuntimeError(f"{model} returned an empty response")
            except Exception as e:
                last_error = e
                msg = str(e)
                if any(mk in msg for mk in _SKIP_MARKERS):
                    _cooldown_until[(api_key, model)] = _PERMANENT
                    log.warning("model %s not available for this key — removed from rotation", model)
                elif _is_daily_quota(msg):
                    park = _seconds_until_quota_reset()
                    _cooldown_until[(api_key, model)] = time.time() + park
                    log.warning("model %s daily quota exhausted for this key — parked ~%.1fh until reset",
                                model, park / 3600)
                elif any(mk in msg for mk in _TRANSIENT_MARKERS):
                    delay = _parse_retry_delay(msg) or (
                        _RATE_COOLDOWN if "429" in msg else _OVERLOAD_COOLDOWN
                    )
                    _cooldown_until[(api_key, model)] = time.time() + delay
                    log.info("model %s transient error — cooling down %.0fs (%s)",
                             model, delay, msg[:60])
                else:
                    log.warning("model %s error (%s) — trying next", model, msg[:80])

        # Decide whether another round can possibly succeed.
        now = time.time()
        cooldowns = {m: _cooldown_until.get((api_key, m), 0.0) for m in models}
        waitable = {m: t for m, t in cooldowns.items() if t != _PERMANENT}

        if not waitable:
            raise GeminiUnavailable(
                f"No Gemini model is available for this API key. Last error: {last_error}"
            )
        # Fail fast when every usable model is parked beyond this call's
        # deadline. Parked for hours == daily quota; the distinct exception
        # lets callers tell the user (and skip work) instead of retrying.
        if min(waitable.values()) >= deadline:
            if all(t >= now + 3600 for t in waitable.values()):
                hours = (min(waitable.values()) - now) / 3600
                raise GeminiQuotaExhausted(
                    f"Gemini daily free-tier quota is exhausted for this API key "
                    f"on all models (~{hours:.1f}h until reset)."
                )
            raise GeminiUnavailable(
                f"All Gemini models cooling down past the {max_wait}s budget. "
                f"Last error: {last_error}"
            )
        if now >= deadline:
            raise GeminiUnavailable(
                f"All Gemini models unavailable after {max_wait}s ({round_num} rounds). "
                f"Last error: {last_error}"
            )

        # Sleep until the earliest cooldown expires; if a model is ready now
        # but keeps failing without a cooldown (e.g. empty responses), use a
        # short growing backoff instead of spinning hot.
        next_ready = min(waitable.values())
        wait = (next_ready - now) if next_ready > now else min(5.0 * round_num, 30.0)
        wait = max(0.5, min(wait, deadline - now))
        log.info("waiting %.0fs before next Gemini round (%d)", wait, round_num + 1)
        time.sleep(wait)
