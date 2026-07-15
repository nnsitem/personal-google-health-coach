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


class GeminiUnavailable(RuntimeError):
    """Raised when all models stay unavailable for the whole time budget."""


def generate(
    api_key: str,
    contents,
    system_instruction: str | None = None,
    max_output_tokens: int = 2048,
    thinking_budget: int = 0,
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

    def _build_config(use_thinking: bool):
        cfg_kwargs = {"max_output_tokens": max_output_tokens}
        if use_thinking:
            # thinking_budget=0 disables "thinking" (faster, more output budget) —
            # but some models (e.g. pro) REQUIRE thinking and reject budget=0.
            cfg_kwargs["thinking_config"] = genai.types.ThinkingConfig(thinking_budget=thinking_budget)
        if system_instruction:
            cfg_kwargs["system_instruction"] = system_instruction
        return genai.types.GenerateContentConfig(**cfg_kwargs)

    def _try_model(model: str):
        """Call one model. Returns text on success, None on empty. Raises on error.
        Adapts config if the model rejects the thinking budget."""
        for use_thinking in (True, False):
            try:
                response = client.models.generate_content(
                    model=model, contents=contents, config=_build_config(use_thinking)
                )
                text = response.text
                return text if (text and len(text.strip()) >= min_chars) else None
            except Exception as e:
                msg = str(e).lower()
                # Model requires thinking mode → retry same model without the budget config
                if use_thinking and ("thinking" in msg or "budget" in msg):
                    log.info("model %s requires thinking mode — retrying without budget", model)
                    continue
                raise
        return None

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

        # All models failed this round. Wait (capped by remaining budget) then retry.
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        wait = min(5.0, remaining)
        log.info("all models busy (round %d) — waiting %.0fs before retry", round_num, wait)
        time.sleep(wait)

    raise GeminiUnavailable(
        f"All Gemini models unavailable after {max_wait}s ({round_num} rounds). "
        f"Last error: {last_error}"
    )
