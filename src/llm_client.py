"""
src/llm_client.py — Provider-agnostic LLM client.

Hides the difference between Gemini and Anthropic so that scorer.py,
tailor.py, quiz.py, gmail_classifier.py all call the same `complete_json()`
method without caring which provider is configured.

USAGE
-----
    client = LLMClient(phase="scorer", model=config.MODEL_SCORER)
    result, usage = client.complete_json(
        system="You are a strict job-fit evaluator...",
        user="Resume:\n... Job:\n...",
        schema=JobScore,    # a pydantic BaseModel subclass
    )

The returned `result` is a parsed Pydantic instance (not raw dict).

WHY THIS ABSTRACTION
--------------------
1. Phase 1 wrap-up: Sourabh picked Gemini Free but wants the option to
   switch to Anthropic later via a single .env flip. This module is what
   makes that switch a one-liner.
2. Both providers have first-class JSON-schema support (Gemini via
   response_schema, Anthropic via tool_use). We thread the same Pydantic
   model through to both.
3. Token usage logging, rate limiting, retries — all happen here, not
   spread across every caller.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Type, TypeVar

from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import (
    ANTHROPIC_RPM,
    GEMINI_RPM,
    LLM_PROVIDER,
    SCORER_MAX_OUTPUT_TOKENS,
    SCORER_TEMPERATURE,
)
from src import token_log

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


# =============================================================================
# Rate limiter — one shared instance per provider, so all callers (scorer,
# tailor, etc.) self-throttle against the same per-minute budget.
# =============================================================================
class _RateLimiter:
    """Token-bucket-ish: ensures at least min_interval seconds between calls."""

    def __init__(self, requests_per_minute: int):
        self.min_interval = 60.0 / max(requests_per_minute, 1)
        self._last_call_at = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call_at
            if elapsed < self.min_interval:
                sleep_for = self.min_interval - elapsed
                log.debug("rate limiter: sleeping %.2fs", sleep_for)
                time.sleep(sleep_for)
            self._last_call_at = time.monotonic()


_RATE_LIMITERS: dict[str, _RateLimiter] = {
    "gemini":    _RateLimiter(GEMINI_RPM),
    "anthropic": _RateLimiter(ANTHROPIC_RPM),
}


# =============================================================================
# LLM call exceptions we want tenacity to retry on
# =============================================================================
class TransientLLMError(Exception):
    """Network blips, 429s, 5xx — worth retrying."""


class DailyQuotaExhausted(RuntimeError):
    """
    The provider's PER-DAY free-tier quota is gone. Distinct from
    TransientLLMError because there's no point retrying — the quota
    only resets at midnight Pacific. Callers (scorer/tailor/quiz) should
    catch this and break out of their per-item loop instead of burning
    ~15 minutes on doomed retries.
    """


# =============================================================================
# The unified client
# =============================================================================
class LLMClient:
    """
    One client per (phase, model) pair. Phase is just a label written to
    the token usage log so we can attribute spend to scorer vs tailor etc.
    """

    def __init__(self, *, phase: str, model: str, provider: str | None = None):
        self.phase = phase
        self.model = model
        self.provider = (provider or LLM_PROVIDER).lower()
        self._rate_limiter = _RATE_LIMITERS[self.provider]

        # Lazy provider-client init — keeps import cost low when only
        # one provider is configured.
        self._gemini_client = None
        self._anthropic_client = None

    # ----------------------------------------------------------------------
    # Public API: structured JSON completion with a Pydantic schema.
    # ----------------------------------------------------------------------
    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: Type[T],
        job_id: str | None = None,
        max_output_tokens: int = SCORER_MAX_OUTPUT_TOKENS,
        temperature: float = SCORER_TEMPERATURE,
    ) -> tuple[T, dict]:
        """
        Send one prompt, expect a JSON response matching `schema`. Returns
        (parsed_pydantic_instance, usage_dict).

        usage_dict has keys: input_tokens, output_tokens, cost_usd, duration_s.
        """
        # Safety brake — refuse if today's usage would cross the cap.
        token_log.enforce_daily_cap(projected_extra_tokens=max_output_tokens)

        started = time.monotonic()

        # The actual API call, wrapped so tenacity can retry it. Retry
        # waits up to ~30s for transient 429s; the rate limiter inside
        # each attempt prevents us from immediately retrying through the
        # next throttle window.
        def _attempt() -> tuple[T, dict]:
            self._rate_limiter.wait()
            try:
                if self.provider == "gemini":
                    return self._call_gemini(
                        system=system, user=user, schema=schema,
                        max_output_tokens=max_output_tokens, temperature=temperature,
                    )
                if self.provider == "anthropic":
                    return self._call_anthropic(
                        system=system, user=user, schema=schema,
                        max_output_tokens=max_output_tokens, temperature=temperature,
                    )
                raise ValueError(f"Unknown LLM_PROVIDER={self.provider!r}")
            except Exception as exc:
                msg = str(exc).lower()
                # CHECK DAILY-QUOTA EXHAUSTION FIRST — it looks like a
                # 429 too, but retrying it is pointless (quota resets at
                # midnight Pacific). We want callers to abort their loop.
                # Signature varies a bit across providers; cover common phrasings:
                #   - "GenerateRequestsPerDayPerProjectPerModel" (Gemini)
                #   - "requests per day" / "per-day" / "daily quota"
                if (
                    "generaterequestsperday" in msg.replace(" ", "")
                    or "requests per day" in msg
                    or "perdayperproject" in msg.replace(" ", "")
                    or "daily quota" in msg
                ):
                    raise DailyQuotaExhausted(str(exc)) from exc
                # Per-minute throttles, network blips, 5xx — retry these.
                if any(s in msg for s in (
                    "429", "rate limit", "resource_exhausted", "timeout",
                    "500", "502", "503", "504", "deadline",
                )):
                    raise TransientLLMError(str(exc)) from exc
                raise

        parsed, usage = self._retry_call(_attempt)

        duration = time.monotonic() - started
        usage["duration_s"] = round(duration, 2)
        usage["cost_usd"]   = round(
            token_log.cost_usd(self.model, usage["input_tokens"], usage["output_tokens"]),
            6,
        )

        token_log.record(
            phase=self.phase, provider=self.provider, model=self.model,
            input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
            duration_s=duration, job_id=job_id,
        )
        return parsed, usage

    # ----------------------------------------------------------------------
    # Retry wrapper applied to each call. tenacity decorator on inner method.
    # ----------------------------------------------------------------------
    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type(TransientLLMError),
    )
    def _retry_call(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    # ----------------------------------------------------------------------
    # Gemini implementation
    # ----------------------------------------------------------------------
    def _call_gemini(
        self, *, system: str, user: str, schema: Type[T],
        max_output_tokens: int, temperature: float,
    ) -> tuple[T, dict]:
        import os
        from google import genai
        from google.genai import types

        # Read the key from the live env each call (vs caching at import)
        # so updating .env mid-session takes effect.
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set in .env")

        if self._gemini_client is None:
            self._gemini_client = genai.Client(api_key=api_key)

        # IMPORTANT: Gemini 2.5 series spends internal "thinking" tokens
        # by default, and they count against max_output_tokens. For a
        # structured-output task like job scoring we don't need a thinking
        # trace — we just want the JSON. thinking_budget=0 disables it,
        # making responses faster, deterministic, and fitting comfortably
        # in our 1024-token output budget.
        response = self._gemini_client.models.generate_content(
            model=self.model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=schema,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )

        # response.parsed is the auto-parsed Pydantic instance when
        # response_schema is a BaseModel. If parsing failed (rare), fall
        # back to manual json.loads.
        parsed = response.parsed
        if parsed is None:
            text = (response.text or "").strip()
            if not text:
                raise RuntimeError("Gemini returned empty response")
            parsed = schema.model_validate_json(text)

        usage_meta = response.usage_metadata
        usage = {
            "input_tokens":  int(getattr(usage_meta, "prompt_token_count", 0) or 0),
            "output_tokens": int(getattr(usage_meta, "candidates_token_count", 0) or 0),
        }
        return parsed, usage

    # ----------------------------------------------------------------------
    # Anthropic implementation (uses tool_use for structured output)
    # ----------------------------------------------------------------------
    def _call_anthropic(
        self, *, system: str, user: str, schema: Type[T],
        max_output_tokens: int, temperature: float,
    ) -> tuple[T, dict]:
        import os
        import anthropic

        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key or api_key == "sk-ant-REPLACE_ME":
            raise RuntimeError("ANTHROPIC_API_KEY not set in .env")

        if self._anthropic_client is None:
            self._anthropic_client = anthropic.Anthropic(api_key=api_key)

        # Convert pydantic schema → JSON schema for tool_use.
        json_schema = schema.model_json_schema()
        # Pydantic emits 'title' and '$defs' that some Anthropic checks
        # don't love; drop them defensively.
        json_schema.pop("title", None)

        response = self._anthropic_client.messages.create(
            model=self.model,
            max_tokens=max_output_tokens,
            temperature=temperature,
            system=system,
            tools=[{
                "name": "submit_result",
                "description": "Return your answer as structured JSON.",
                "input_schema": json_schema,
            }],
            tool_choice={"type": "tool", "name": "submit_result"},
            messages=[{"role": "user", "content": user}],
        )

        # Find the tool_use block.
        tool_block = next(
            (b for b in response.content if getattr(b, "type", "") == "tool_use"),
            None,
        )
        if not tool_block:
            raise RuntimeError("Anthropic response had no tool_use block")
        parsed = schema.model_validate(tool_block.input)

        usage = {
            "input_tokens":  int(response.usage.input_tokens),
            "output_tokens": int(response.usage.output_tokens),
        }
        return parsed, usage
