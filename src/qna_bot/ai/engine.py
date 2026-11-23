"""High-speed multi-provider AI engine with HTTP/2 pooling, single-shot Q&A, and auto-fallback."""

from __future__ import annotations

import copy
import logging
import re
import time
from dataclasses import dataclass

import httpx

from .forex import group_guidance as forex_group_guidance
from .language import ANSWER_IN_LANGUAGE_INSTRUCTION, detect_language, prompt_guidance
from .market import MarketFeed, MarketSnapshot, parse_period_query
from .math_solver import try_solve_math
from .providers import (
    DEFAULT_SPEED_MODE,
    PROVIDERS,
    get_provider_spec,
    reasoning_effort_for,
    retired_model_replacement,
    vision_model_for,
)
from .text_cleanup import collapse_emoji_runs, strip_emoji, strip_leading_emoji

logger = logging.getLogger(__name__)

SKIP_TOKEN = "SKIP"

#: Reasoning models bill their "thinking" against the completion budget. A
#: giveaway answer is capped at 48 tokens, which leaves nothing for the model to
#: think with, so answers came back empty and the bot looked like it had stalled.
#: Give the thought process room without letting the visible answer grow.
REASONING_HEADROOM_TOKENS = 256

#: How long a pooled TLS connection survives an idle spell. httpx defaults to
#: 5s, which is shorter than the gap between giveaway questions, so the next
#: message after every pause paid a fresh handshake.
KEEPALIVE_EXPIRY_SECONDS = 120.0
_GRADED_EFFORTS = ("low", "medium", "high")

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


#: Error bodies that mean our *speed* parameters were rejected, not the request.
_SPEED_REJECTION_MARKERS = (
    "reasoning_effort",
    "thinking_level",
    "thinkingconfig",
    "thinkingconfig.thinkinglevel",
)


def _is_speed_rejection(detail: str) -> bool:
    """True when the API refused because of the reasoning/thinking parameter.

    Model families disagree on the vocabulary ("minimal" vs "low", "none" vs
    "low"), and an unsupported value is a hard 400. That must cost one retry,
    never the answer.
    """
    lowered = (detail or "").lower()
    return any(marker in lowered for marker in _SPEED_REJECTION_MARKERS)


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

#: Amharic answer labels: "መልስ፦" / "መልሱ፦" / "መልስ -" (answer:) at the start.
_AMHARIC_ANSWER_PREFIX_RE = re.compile(r"^(?:መልሱ|መልስ|መልስህ)\s*[:፦\-–]\s*")
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
    #: "am" when the question was Amharic, "en" otherwise. Drives /diag output.
    language: str = "en"


class AIEngine:
    """Manages persistent HTTP/2 clients and fast single-shot Q&A across Groq, Gemini, OpenRouter."""

    def __init__(self, market_feed: MarketFeed | None = None) -> None:
        self._client: httpx.AsyncClient | None = None
        # Serves live prices to Forex groups. Injectable so tests never touch
        # the network.
        self.market_feed = market_feed or MarketFeed()

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                http2=True,
                timeout=httpx.Timeout(connect=5.0, read=12.0, write=5.0, pool=5.0),
                limits=httpx.Limits(
                    max_connections=30,
                    max_keepalive_connections=15,
                    # Giveaway questions arrive in bursts separated by quiet
                    # spells. The default 5s expiry dropped the pooled TLS
                    # connection after every pause, so the next question paid a
                    # fresh handshake - the "sometimes it lags" report.
                    keepalive_expiry=KEEPALIVE_EXPIRY_SECONDS,
                ),
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        await self.market_feed.close()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("AIEngine is not started. Call await engine.start() first.")
        return self._client

    async def answer_question(
        self,
        *,
        text: str,
        forex_group: bool = False,
        live_data: bool = True,
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
        speed_mode: str = DEFAULT_SPEED_MODE,
    ) -> AnswerResult:
        t0 = time.perf_counter()

        # 1. Sub-millisecond local math fast-path for giveaway arithmetic.
        #    Skipped for photos: the arithmetic is inside the image, not the text.
        if local_math_enabled and not image:
            # numeral_style="auto" answers ፵፫ questions with ፺፭ instead of 95.
            math_answer = try_solve_math(text)
            if math_answer is not None:
                elapsed_ms = max(1, int((time.perf_counter() - t0) * 1000))
                return AnswerResult(
                    answered=True,
                    answer=math_answer,
                    provider="local_math",
                    model="ast-instant",
                    latency_ms=elapsed_ms,
                    language=detect_language(text),
                )

        # 1b. Period questions ("highest BTC price last week") are arithmetic
        #     over a real OHLC series, so they are computed here and returned
        #     without an AI call at all - the same reasoning as the math solver.
        #     A model asked to eyeball a series will eventually pick the wrong
        #     bar, and this answer has to be exact to be worth posting.
        if forex_group and live_data and not image:
            period = parse_period_query(text)
            if period is not None and period.instrument:
                computed = await self.market_feed.period_answer(period)
                if computed is not None:
                    elapsed_ms = max(1, int((time.perf_counter() - t0) * 1000))
                    return AnswerResult(
                        answered=True,
                        answer=computed.formatted(),
                        provider="local_market",
                        model="ohlc-history",
                        latency_ms=elapsed_ms,
                        language=detect_language(text),
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

        # Fetch market numbers before building the prompt: the guidance itself
        # changes depending on whether there is real data to hand over.
        market: MarketSnapshot | None = None
        if forex_group and live_data and not image:
            market = await self.market_feed.snapshot(text)

        system_prompt = self._build_system_prompt(
            answer_style,
            text,
            forex_group=forex_group,
            market=market,
        )
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
                    speed_mode=speed_mode,
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
                    language=detect_language(text),
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
                            speed_mode=speed_mode,
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
                                language=detect_language(text),
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
    def _build_system_prompt(
        answer_style: str,
        text: str,
        *,
        forex_group: bool = False,
        market: MarketSnapshot | None = None,
    ) -> str:
        """Base prompt + language rules.

        The always-on rule is a constant so the provider's prompt cache keeps
        hitting; only genuinely Amharic questions add a second block.
        """
        base = _SYSTEM_PROMPTS.get(answer_style, _SYSTEM_PROMPTS["giveaway"])
        prompt = f"{base}\n{ANSWER_IN_LANGUAGE_INSTRUCTION}"
        guidance = prompt_guidance(text)
        if guidance:
            prompt = f"{prompt}\n{guidance}"
        # Forex groups get trading conventions and a strict no-invented-prices
        # rule. Applied for the whole group, not just recognised phrasing: a
        # host may ask anything, and the honesty rule must always be present.
        if forex_group:
            block = market.as_prompt_block() if market is not None else ""
            prompt = f"{prompt}\n{forex_group_guidance(has_live_data=bool(block))}"
            if block:
                prompt = f"{prompt}\n\n{block}"
        return prompt

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

        # Detect SKIP decision cleanly. Models decorate the token ("SKIP 🔥"),
        # and a decorated SKIP must never be posted into a group as an answer.
        normalized = text.strip(" .!\"'`").upper()
        if normalized == SKIP_TOKEN or normalized.startswith(f"{SKIP_TOKEN}\n"):
            return None
        emoji_free = strip_emoji(text).strip(" .!\"'`*“”‘’").upper()
        if emoji_free == SKIP_TOKEN or emoji_free.startswith(f"{SKIP_TOKEN}\n"):
            return None

        if answer_style == "giveaway":
            # Take first non-empty line, strip common fluff
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            text = lines[0] if lines else text
            text = _ANSWER_PREFIX_RE.sub("", text).strip()
            # Amharic answers sometimes lead with their own label.
            text = _AMHARIC_ANSWER_PREFIX_RE.sub("", text).strip()
            # "🎯 Nairobi" -> "Nairobi"; a whole-emoji answer is left alone.
            text = strip_leading_emoji(text)
            text = text.strip('"`*“”‘’')
            # ። is the Ethiopic full stop, ፡ the Ethiopic word space.
            for stop in (".", "።"):
                if text.endswith(stop) and len(text) < 60 and "..." not in text:
                    text = text[:-1].strip()
                    break
            text = text.rstrip("፡").strip()

        # A host's answer never has "🔥🔥🔥🔥🔥"; models echo the question's
        # emoji anyway, so collapse runs for every answer style.
        return collapse_emoji_runs(text) or None

    async def _post_with_speed_fallback(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict,
        drop_paths: tuple[str, ...],
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """POST, retrying once without the speed knobs if the API rejects them.

        A rejected ``reasoning_effort``/``thinkingConfig`` must not cost the
        user an answer, and must not fall through to a slower fallback provider.
        """
        resp = await self.client.post(url, headers=headers, json=payload, params=params)
        if resp.status_code != 400 or not _is_speed_rejection(resp.text):
            return resp

        retry_payload = copy.deepcopy(payload)
        for path in drop_paths:
            node: dict = retry_payload
            *parents, leaf = path.split(".")
            try:
                for key in parents:
                    node = node[key]
                node.pop(leaf, None)
            except (KeyError, TypeError):
                continue

        logger.info(
            "Provider rejected the speed parameter (%s); retrying at its default. %s",
            ", ".join(drop_paths),
            resp.text[:160],
        )
        return await self.client.post(url, headers=headers, json=retry_payload, params=params)

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
        speed_mode: str = DEFAULT_SPEED_MODE,
    ) -> str:
        spec = get_provider_spec(provider)
        effort = reasoning_effort_for(provider, model, speed_mode)

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
            if effort:
                payload["reasoning_effort"] = effort
                if effort in _GRADED_EFFORTS:
                    # Reasoning is billed as output tokens: without headroom the
                    # answer gets cut off and comes back empty.
                    payload["max_tokens"] = max_tokens + REASONING_HEADROOM_TOKENS

            resp = await self._post_with_speed_fallback(
                f"{spec.base_url}/chat/completions", headers, payload, ("reasoning_effort",)
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
            if effort:
                # Gemini 3.x thinks dynamically at its default level, which is
                # where multi-second giveaways came from. Levels below "medium"
                # are pure latency savings for one-word trivia answers.
                payload["generationConfig"]["thinkingConfig"] = {"thinkingLevel": effort}
            resp = await self._post_with_speed_fallback(
                url,
                {"Content-Type": "application/json"},
                payload,
                ("generationConfig.thinkingConfig",),
                params={"key": api_key},
            )
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates:
                return ""
            parts = candidates[0].get("content", {}).get("parts") or []
            return "".join(str(p.get("text", "")) for p in parts)

        raise ValueError(f"Unsupported provider: {provider}")
