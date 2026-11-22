"""High-speed multi-provider AI engine with HTTP/2 pooling, single-shot Q&A, and auto-fallback."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import httpx

from .math_solver import try_solve_math
from .providers import (
    PROVIDERS,
    get_provider_spec,
    retired_model_replacement,
    vision_model_for,
)

logger = logging.getLogger(__name__)

SKIP_TOKEN = "SKIP"

#: Provider error bodies that mean "this model ID is gone", never "bad key".
_MODEL_GONE_MARKERS = (
    "model_not_found",
    "does not exist",
    "no longer supported",
    "decommissioned",
    "model_decommissioned",
    "not found",
    "unsupported model",
)


def _user_content(prompt: str, image: bytes | None, mime: str) -> object:
    """Build an OpenAI-style ``content`` value (string or multimodal parts)."""
    if not image:
        return prompt
    import base64

    encoded = base64.b64encode(image).decode("ascii")
    return [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
    ]


def _gemini_parts(prompt: str, image: bytes | None, mime: str) -> list[dict]:
    """Build Gemini ``parts``, adding inline image data when present."""
    parts: list[dict] = [{"text": prompt}]
    if image:
        import base64

        parts.append(
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(image).decode("ascii")}}
        )
    return parts


def _is_model_not_found(detail: str) -> bool:
    lowered = (detail or "").lower()
    return any(marker in lowered for marker in _MODEL_GONE_MARKERS)

_SYSTEM_PROMPTS: dict[str, str] = {
    "giveaway": (
        "You are an ultra-fast Telegram Q&A giveaway solver. "
        "Decide whether the user's message is a question, trivia prompt, riddle, math problem, "
        "unscramble challenge, fill-in-the-blank, or quiz prompt.\n"
        "- If it is NOT a question/quiz/prompt to answer (e.g., casual chat, greeting, reaction, "
        "announcement, winner shoutout, spam, or statement), reply with ONLY the exact word: SKIP\n"
        "- If it IS a question or quiz prompt, reply with ONLY the exact, direct, factual answer.\n"
        "- NO preamble ('The answer is...'), NO explanation, NO markdown, NO quotes, NO trailing period.\n"
        "- For multiple choice, give the exact answer text (or letter if only letters are asked).\n"
        "- Be 100% accurate and as short as possible."
    ),
    "short": (
        "You are a fast, accurate Telegram group assistant. "
        "Decide whether the message contains a genuine question, trivia prompt, or problem to solve.\n"
        "- If it is NOT a question or problem to answer, reply with ONLY the exact word: SKIP\n"
        "- If it IS a question, reply with a concise, accurate answer in 1 short sentence. "
        "No filler greetings or disclaimers."
    ),
    "detailed": (
        "You are a helpful, accurate Telegram group assistant. "
        "Decide whether the message asks a genuine question or requests an answer.\n"
        "- If it is NOT a question or request for information, reply with ONLY the exact word: SKIP\n"
        "- If it IS a question, provide a clear, accurate, concise answer (2-4 sentences max)."
    ),
}

_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_ANSWER_PREFIX_RE = re.compile(
    r"^(?:answer\s*[:\-]\s*|the\s+answer\s+is\s*[:\-]?\s*)",
    re.IGNORECASE,
)


@dataclass
class AnswerResult:
    answered: bool
    answer: str | None
    provider: str
    model: str
    latency_ms: int
    used_fallback: bool = False
    error: str | None = None


class AIEngine:
    """Manages persistent HTTP/2 clients and fast single-shot Q&A across Groq, Gemini, OpenRouter."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                http2=True,
                timeout=httpx.Timeout(connect=5.0, read=12.0, write=5.0, pool=5.0),
                limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("AIEngine is not started. Call await engine.start() first.")
        return self._client

    async def answer_question(
        self,
        *,
        text: str,
        answer_style: str,
        primary_provider: str,
        primary_model: str,
        provider_keys: dict[str, str],
        fallback_provider: str | None = None,
        fallback_model: str | None = None,
        local_math_enabled: bool = True,
        context_lines: list[str] | None = None,
        temperature: float = 0.1,
        max_tokens: int = 120,
        image: bytes | None = None,
        image_mime: str = "image/jpeg",
    ) -> AnswerResult:
        t0 = time.perf_counter()

        # 1. Sub-millisecond local math fast-path for giveaway arithmetic.
        #    Skipped for photos: the arithmetic is inside the image, not the text.
        if local_math_enabled and not image:
            math_answer = try_solve_math(text)
            if math_answer is not None:
                elapsed_ms = max(1, int((time.perf_counter() - t0) * 1000))
                return AnswerResult(
                    answered=True,
                    answer=math_answer,
                    provider="local_math",
                    model="ast-instant",
                    latency_ms=elapsed_ms,
                )

        # Build provider attempt list (primary + optional fallback)
        attempts: list[tuple[str, str, str]] = []
        primary_key = provider_keys.get(primary_provider)
        if primary_key:
            attempts.append((primary_provider, primary_model, primary_key))

        if fallback_provider and fallback_model and fallback_provider != primary_provider:
            fb_key = provider_keys.get(fallback_provider)
            if fb_key:
                attempts.append((fallback_provider, fallback_model, fb_key))

        if image:
            # A photo question can only be answered by a model that can see it.
            # Switch each provider to its vision model; drop attempts that cannot.
            vision_attempts: list[tuple[str, str, str]] = []
            for prov, mdl, key in attempts:
                chosen = vision_model_for(prov, mdl)
                if chosen is None:
                    logger.warning(
                        "Skipping %s/%s for an image question: no vision model available",
                        prov,
                        mdl,
                    )
                    continue
                if chosen != mdl:
                    logger.info("Image question: using %s/%s instead of %s", prov, chosen, mdl)
                vision_attempts.append((prov, chosen, key))
            attempts = vision_attempts

        if not attempts:
            return AnswerResult(
                answered=False,
                answer=None,
                provider=primary_provider,
                model=primary_model,
                latency_ms=0,
                error="No API key configured for the selected AI provider.",
            )

        system_prompt = _SYSTEM_PROMPTS.get(answer_style, _SYSTEM_PROMPTS["giveaway"])
        user_prompt = self._build_user_prompt(text, context_lines)
        effective_max_tokens = min(max_tokens, 48) if answer_style == "giveaway" else max_tokens

        last_error: str | None = None
        for idx, (provider, model, api_key) in enumerate(attempts):
            attempt_t0 = time.perf_counter()
            try:
                raw_text = await self._call_provider(
                    provider=provider,
                    model=model,
                    api_key=api_key,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=temperature,
                    max_tokens=effective_max_tokens,
                    image=image,
                    image_mime=image_mime,
                )
                elapsed_ms = int((time.perf_counter() - attempt_t0) * 1000)
                cleaned = self._clean_output(raw_text, answer_style)
                if cleaned is None:
                    return AnswerResult(
                        answered=False,
                        answer=None,
                        provider=provider,
                        model=model,
                        latency_ms=elapsed_ms,
                        used_fallback=idx > 0,
                    )
                return AnswerResult(
                    answered=True,
                    answer=cleaned,
                    provider=provider,
                    model=model,
                    latency_ms=elapsed_ms,
                    used_fallback=idx > 0,
                )
            except Exception as exc:
                last_error = f"{provider}/{model}: {exc}"
                logger.warning("AI provider attempt failed (%s): %s", provider, exc)

                # A model saved to the DB months ago may have been retired since.
                # Retry the same provider once with its live default instead of
                # silently failing every giveaway answer.
                replacement = retired_model_replacement(provider, model)
                if replacement and replacement != model:
                    logger.warning(
                        "Model %s/%s is retired; retrying with %s", provider, model, replacement
                    )
                    try:
                        raw_text = await self._call_provider(
                            provider=provider,
                            model=replacement,
                            api_key=api_key,
                            system_prompt=system_prompt,
                            user_prompt=user_prompt,
                            temperature=temperature,
                            max_tokens=effective_max_tokens,
                            image=image,
                            image_mime=image_mime,
                        )
                        elapsed_ms = int((time.perf_counter() - attempt_t0) * 1000)
                        cleaned = self._clean_output(raw_text, answer_style)
                        if cleaned is not None:
                            return AnswerResult(
                                answered=True,
                                answer=cleaned,
                                provider=provider,
                                model=replacement,
                                latency_ms=elapsed_ms,
                                used_fallback=idx > 0,
                            )
                    except Exception as retry_exc:
                        last_error = f"{provider}/{replacement}: {retry_exc}"
                        logger.warning(
                            "Replacement model attempt failed (%s/%s): %s",
                            provider,
                            replacement,
                            retry_exc,
                        )

        total_ms = int((time.perf_counter() - t0) * 1000)
        return AnswerResult(
            answered=False,
            answer=None,
            provider=primary_provider,
            model=primary_model,
            latency_ms=total_ms,
            error=last_error,
        )

    async def test_connection(
        self, provider: str, model: str, api_key: str
    ) -> tuple[bool, int, str]:
        """Verify an API key with a tiny trivia prompt. Returns (ok, latency_ms, detail).

        A retired/renamed model ID must never be reported as a bad API key: that
        sent owners chasing a key that was always fine. ``model_not_found`` is
        treated as our problem -- we look up the live replacement, retry, and
        report what happened instead of "key test failed".
        """
        ok, latency_ms, detail = await self._probe(provider, model, api_key)
        if ok:
            return ok, latency_ms, detail

        if _is_model_not_found(detail):
            replacement = retired_model_replacement(provider, model) or (
                PROVIDERS[provider].default_model if provider in PROVIDERS else None
            )
            if replacement and replacement != model:
                logger.warning(
                    "Model %s/%s is gone; retrying the key test with %s",
                    provider,
                    model,
                    replacement,
                )
                ok2, latency2, detail2 = await self._probe(provider, replacement, api_key)
                if ok2:
                    return (
                        True,
                        latency2,
                        f"{detail2}\n__MODEL_FALLBACK__:{model}->{replacement}",
                    )
        return ok, latency_ms, detail

    async def _probe(self, provider: str, model: str, api_key: str) -> tuple[bool, int, str]:
        t0 = time.perf_counter()
        try:
            raw = await self._call_provider(
                provider=provider,
                model=model,
                api_key=api_key,
                system_prompt=_SYSTEM_PROMPTS["giveaway"],
                user_prompt="What is the capital of France?",
                temperature=0.0,
                max_tokens=20,
            )
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            cleaned = self._clean_output(raw, "giveaway") or raw.strip()
            return True, elapsed_ms, cleaned
        except httpx.HTTPStatusError as exc:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            body = exc.response.text[:200]
            return False, elapsed_ms, f"HTTP {exc.response.status_code}: {body}"
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            return False, elapsed_ms, str(exc)

    @staticmethod
    def _build_user_prompt(text: str, context_lines: list[str] | None) -> str:
        if not context_lines:
            return text.strip()
        joined = "\n".join(context_lines[-3:])
        return f"Recent group context:\n{joined}\n\nLatest message to evaluate:\n{text.strip()}"

    @staticmethod
    def _clean_output(raw: str, answer_style: str) -> str | None:
        text = _THINK_TAG_RE.sub("", raw or "").strip()
        if not text:
            return None

        # Detect SKIP decision cleanly
        normalized = text.strip(" .!\"'`").upper()
        if normalized == SKIP_TOKEN or normalized.startswith(f"{SKIP_TOKEN}\n"):
            return None

        if answer_style == "giveaway":
            # Take first non-empty line, strip common fluff
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            text = lines[0] if lines else text
            text = _ANSWER_PREFIX_RE.sub("", text).strip()
            text = text.strip('"`*')
            if text.endswith(".") and len(text) < 60 and "..." not in text:
                text = text[:-1].strip()

        return text or None

    async def _call_provider(
        self,
        *,
        provider: str,
        model: str,
        api_key: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
        image: bytes | None = None,
        image_mime: str = "image/jpeg",
    ) -> str:
        spec = get_provider_spec(provider)

        if provider in ("groq", "openrouter"):
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            if provider == "openrouter":
                headers["HTTP-Referer"] = "https://github.com/tg-qna-userbot"
                headers["X-Title"] = "Telegram QnA Userbot"

            payload: dict = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": _user_content(user_prompt, image, image_mime)},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            # Disable hidden chain-of-thought reasoning on Qwen3 models on Groq for speed
            if provider == "groq" and "qwen3" in model.lower():
                payload["reasoning_effort"] = "none"

            resp = await self.client.post(
                f"{spec.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                return ""
            return str(choices[0].get("message", {}).get("content") or "")

        if provider == "gemini":
            url = f"{spec.base_url}/models/{model}:generateContent"
            payload = {
                "system_instruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": _gemini_parts(user_prompt, image, image_mime)}],
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": max_tokens,
                },
            }
            resp = await self.client.post(
                url,
                params={"key": api_key},
                headers={"Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates:
                return ""
            parts = candidates[0].get("content", {}).get("parts") or []
            return "".join(str(p.get("text", "")) for p in parts)

        raise ValueError(f"Unsupported provider: {provider}")
