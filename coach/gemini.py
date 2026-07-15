"""Centralized Gemini client with robust cross-model retry.

Google's Gemini API returns transient 503 (overloaded) / 429 (rate) errors
under load. A single model can stay overloaded for tens of seconds, and
similar-tier models often overload together. This helper:

- Tries the primary model, then each fallback (different capacity tiers)
- On transient errors, cycles through all models, then waits and repeats
- Keeps going until success or a total time budget elapses

Because replies are delivered via LINE *push* (no time-limited reply token),
we can afford a long retry window (default 120s) — far more reliable than the
old fixed 3-attempts-per-model approach that gave up in ~30s.

All modules (chat, ai, nudges, weekly, plans, food) call generate() here so
retry behavior is consistent in one place.
"""

import logging
import time

from google import genai

from coach.config import GEMINI_MODEL, GEMINI_FALLBACK_MODELS, GEMINI_MAX_WAIT_SECONDS

log = logging.getLogger(__name__)

# Errors that are transient — worth retrying on a different model / after a wait.
_TRANSIENT_MARKERS = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500", "INTERNAL")
# Errors meaning "this model won't work" — skip to the next model, don't wait.
_SKIP_MARKERS = ("404", "NOT_FOUND", "PERMISSION_DENIED")

# Thinking configs tried per model, cheapest first. thinking_level replaced the
# legacy thinking_budget in the Gemini API; MINIMAL ≈ old budget=0 (no thinking,
# fast). Some models reject levels they don't support (e.g. gemini-pro-latest
# rejects MINIMAL but accepts LOW), so each model climbs this ladder on a
# config-rejection 400 and the winning rung is cached for the process lifetime —
# without the cache the bad config would be re-sent on every retry round.
_THINKING_LADDER = ("MINIMAL", "LOW", None)  # None = model's default (dynamic)
_model_thinking_rung: dict[str, int] = {}


class GeminiUnavailable(RuntimeError):
    """Raised when all models stay unavailable for the whole time budget."""


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


def generate(
    api_key: str,
    contents,
    system_instruction: str | None = None,
    max_output_tokens: int = 2048,
    max_wait: int | None = None,
    min_chars: int = 1,
) -> str:
    """Generate content, retrying across models until success or timeout.

    contents: str or list (list supports multimodal, e.g. [prompt, image_part])
    Returns the response text. Raises GeminiUnavailable if it never succeeds.
    """
    if not api_key:
        raise RuntimeError("No Gemini API key provided")

    if max_wait is None:
        max_wait = GEMINI_MAX_WAIT_SECONDS

    client = genai.Client(api_key=api_key)
    models = [GEMINI_MODEL] + [m for m in GEMINI_FALLBACK_MODELS if m != GEMINI_MODEL]

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

    while time.time() < deadline:
        round_num += 1
        for model in models:
            try:
                text = _try_model(model)
                if text:
                    if round_num > 1:
                        log.info("Gemini succeeded on %s after %d rounds", model, round_num)
                    return text
                log.warning("model %s returned empty/short response", model)
            except Exception as e:
                last_error = e
                msg = str(e)
                if any(m in msg for m in _SKIP_MARKERS):
                    log.warning("model %s unavailable for this account — skipping", model)
                elif any(m in msg for m in _TRANSIENT_MARKERS):
                    log.warning("model %s transient error (%s) — trying next", model, msg[:60])
                else:
                    # Unexpected error (e.g. bad request for this model): skip to
                    # the next model rather than aborting the whole chain.
                    log.warning("model %s error (%s) — trying next", model, msg[:80])
                continue

        # All models failed this round. Wait (growing per round, capped by the
        # remaining budget) then retry — hammering every few seconds during an
        # overload just adds to the 503/429 pressure.
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        wait = min(5.0 * round_num, 30.0, remaining)
        log.info("all models busy (round %d) — waiting %.0fs before retry", round_num, wait)
        time.sleep(wait)

    raise GeminiUnavailable(
        f"All Gemini models unavailable after {max_wait}s ({round_num} rounds). "
        f"Last error: {last_error}"
    )
