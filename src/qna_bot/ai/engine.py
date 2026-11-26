"""High-speed multi-provider AI engine with HTTP/2 pooling, single-shot Q&A, and auto-fallback."""

from __future__ import annotations

import copy
import logging
import re
import time
from dataclasses import dataclass

import httpx

from .forex import group_guidance as forex_group_guidance
from .forex import is_refusal
from .forex_math import solve_forex_math
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
from .text_cleanup import (
    collapse_emoji_runs,
    is_photo_caption_stub,
    looks_like_question,
    strip_emoji,
    strip_leading_emoji,
)

logger = logging.getLogger(__name__)

SKIP_TOKEN = "SKIP"

#: Reasoning models bill their "thinking" against the completion budget. A
#: giveaway answer is capped at 48 tokens, which leaves nothing for the model to
#: think with, so answers came back empty and the bot looked like it had stalled.
#: Give the thought process room without letting the visible answer grow.
REASONING_HEADROOM_TOKENS = 256

#: Ethiopic script tokenizes at roughly one token per character, against about
#: four characters per token for Latin. The same 48-token giveaway budget that
#: fits a one-word English answer can therefore cut an Amharic sentence off
#: mid-word. Amharic rewards the extra room; the prompt still asks for a bare
#: answer, so the visible reply does not grow.
AMHARIC_TOKEN_HEADROOM = 64


class _TruncatedAnswerError(RuntimeError):
    """The provider stopped at its token ceiling, so the text is a fragment."""

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
#:
#: The leading ``\\**`` matters: an Amharic answer formatted as ``**መልሱ፦ 85**``
#: used to keep its label, because the plain pattern could not match past the
#: bold markers, and the group was posted "መልሱ፦ 85" instead of "85".
_AMHARIC_ANSWER_PREFIX_RE = re.compile(
    r"^[*_`~\s]*(?:መልሱ|መልስ|መልስህ)\s*[:፦\-–]?\s*[*_`~\s]*"
)
#: "Answer: 85", "The answer is 85", and the decorated forms models produce -
#: "**Answer:** 85", "**Answer**: 85", "Answer — 85". Vision models are the worst
#: offenders because they mirror the bold they see in the picture, and the
#: cleanup used to hand the group a literal "Answer:** 85".
_ANSWER_PREFIX_RE = re.compile(
    r"^[*_`~\s]*"
    # "answer" / "the answer" / "answers", optionally followed by "is". The
    # negative lookahead refuses a sentence that merely *starts* with the word:
    # "Answer is not possible" and "An answer cannot be determined" are answers,
    # not labels, and stripping them would post an empty message.
    r"(?:(?:the\s+)?answers?\b(?:\s+is\b)?"
    r"(?![^:*_~`\-–—]*\b(?:is|was|will|would|cannot|can't|not)\b))"
    r"[*_`~\s]*"
    # The separator may be wrapped in the model's own bold: "**Answer**: 85".
    r"(?:[:\-–—][*_`~\s]*)?",
    re.IGNORECASE,
)


#: A line that is only an answer label, with nothing after it: "**Answer:**",
#: "መልሱ፦". Vision models split the label and the figure across lines often enough
#: that treating the label as the answer is a real failure, not a theoretical one.
_ANSWER_LABEL_ONLY_RE = re.compile(
    r"^[*_`~\s]*(?:(?:the\s+)?answers?|መልሱ|መልስ|መልስህ)\s*[:፦\-–—]?[*_`~\s]*$",
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

    #: Settings the market feed needs to reach Exness. Read per answer, so a
    #: key saved from the dashboard works without a restart. The private key is
    #: the sensitive one; the rest identify the account and its host.
    MARKET_SETTING_KEYS = (
        "exness_api_key",
        "exness_private_key",
        "exness_account_id",
        "exness_gateway_url",
    )

    def __init__(self, market_feed: MarketFeed | None = None) -> None:
        self._client: httpx.AsyncClient | None = None
        # Serves live prices to Forex groups. Injectable so tests never touch
        # the network.
        self.market_feed = market_feed or MarketFeed()

    def attach_market_settings(self, loader) -> None:
        """Point the market feed at the database the owner saves keys into.

        Called once at startup. ``loader`` is an awaitable returning the four
        Exness settings, so the feed always signs with the *current* key rather
        than one captured when the process booted.
        """
        self.market_feed._settings_loader = loader  # noqa: SLF001 - wiring, not poking
        self._settings_loader = loader

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

        # A photo can carry the question in more than one place, and the
        # question the shortcuts below need is always the same one: what words
        # is this answer about? Resolving that once, up front, keeps every
        # shortcut usable instead of gating each on `not image` and quietly
        # discarding the accurate answer whenever a picture was attached.
        #
        #   caption that IS a question -> use it; no extra call
        #   no caption, or a stub     -> transcribe the picture once
        #   long caption, no question -> transcribe, keep it only if it yields
        #                                something the caption did not
        #   nothing readable anywhere -> a vision answer, as before
        attempts = self._provider_attempts(
            primary_provider=primary_provider,
            primary_model=primary_model,
            provider_keys=provider_keys,
            fallback_provider=fallback_provider,
            fallback_model=fallback_model,
        )

        asked = (text or "").strip()
        #: True when `asked` is what the picture itself says, rather than a
        #: caption that the picture might contradict. That distinction decides
        #: which shortcuts are trustworthy - see below.
        asked_from_image = False
        if image and is_photo_caption_stub(asked):
            # Either there is no caption, or the caption is the "answer below" /
            # "🔥🔥" wrapper a Telegram post needs around its media. In both cases
            # the actual question is printed on the picture, so read it rather
            # than answering the wrapper.
            transcribed = await self._transcribe_image(
                attempts=attempts, image=image, image_mime=image_mime
            )
            if transcribed:
                asked = transcribed
                asked_from_image = True
        elif image and not self._has_answerable_text(asked):
            # A long caption is not necessarily a better caption: an announcement
            # wrapping a poster ("follow us for more daily questions, rules
            # below, winner announced tomorrow") can be longer than the stub
            # ceiling and still contain no question at all. Rather than guess
            # from length alone, read the picture too and use it when it yields
            # something the caption did not.
            transcribed = await self._transcribe_image(
                attempts=attempts, image=image, image_mime=image_mime
            )
            if transcribed and self._has_answerable_text(transcribed):
                asked = transcribed
                asked_from_image = True

        # A caption is only *the* question when nothing else could contradict it.
        # With a picture attached and a caption too, the two can disagree: a
        # caption reading "47 * 83" under an image reading "2 + 2 = ?" would be
        # answered 3901, posted instantly and confidently, and be wrong. Local
        # arithmetic is therefore trusted for a caption only when there is no
        # image, or when the text IS the image's own words (transcribed above).
        # Market-history questions are exempt: an OHLC high is a fact about the
        # market, so it cannot be contradicted by what the picture happens to
        # show, and the instrument must be named in the text anyway.
        trust_arithmetic = local_math_enabled and bool(asked) and (
            not image or asked_from_image
        )

        # 1. Sub-millisecond local math fast-path for giveaway arithmetic.
        #    Runs on the caption, or on what the picture says when there is no
        #    caption: "34% of 250" printed on a poster is the same arithmetic as
        #    "34% of 250" typed, and answering it locally beats a model call.
        if trust_arithmetic:
            # numeral_style="auto" answers ፵፫ questions with ፺፭ instead of 95.
            math_answer = try_solve_math(asked)
            if math_answer is not None:
                elapsed_ms = max(1, int((time.perf_counter() - t0) * 1000))
                return AnswerResult(
                    answered=True,
                    answer=math_answer,
                    provider="local_math",
                    model="ast-instant",
                    latency_ms=elapsed_ms,
                    language=detect_language(asked),
                )

        # 1a. Forex arithmetic - pip value, pip distance, position size, risk %.
        #     Runs after the plain math solver so "15% of 240" keeps its existing
        #     path, and before any AI call: these are exact formulas and a
        #     giveaway cannot absorb a 500ms guess.
        if forex_group and trust_arithmetic:
            fx_answer = solve_forex_math(asked)
            if fx_answer is not None:
                elapsed_ms = max(1, int((time.perf_counter() - t0) * 1000))
                return AnswerResult(
                    answered=True,
                    answer=fx_answer,
                    provider="local_math",
                    model="forex-formula",
                    latency_ms=elapsed_ms,
                    language=detect_language(asked),
                )

        # 1b. Period questions ("highest BTC price last week") are arithmetic
        #     over a real OHLC series, so they are computed here and returned
        #     without an AI call at all - the same reasoning as the math solver.
        #     A model asked to eyeball a series will eventually pick the wrong
        #     bar, and this answer has to be exact to be worth posting.
        #
        #     An image cannot make a calendar week less exact, so a photo takes
        #     this path too: on its caption, or on the transcription when the
        #     question was printed on the picture itself.
        if forex_group and live_data and asked:
            period = parse_period_query(asked)
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
                        language=detect_language(asked),
                    )

        # Everything past this point may call out to a model, so nothing that
        # could be computed has been left behind. `text` is what the prompt will
        # describe: the caption, or the transcribed question, or - for a purely
        # decorative image - nothing at all.
        text = asked

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
        if forex_group and live_data:
            # Also for photos. A chart picture cannot supply "the current price"
            # for every question, but the feed can - and the image guidance used
            # to replace the live-data rule entirely, so a photo question about
            # a live level was answered from pixels alone.
            market = await self.market_feed.snapshot(text)

        system_prompt = self._build_system_prompt(
            answer_style,
            text,
            forex_group=forex_group,
            market=market,
            has_image=bool(image),
        )
        user_prompt = self._build_user_prompt(
            text, context_lines, from_image=asked_from_image
        )
        effective_max_tokens = min(max_tokens, 48) if answer_style == "giveaway" else max_tokens
        # Ethiopic script costs far more tokens per character than Latin, so an
        # Amharic answer needs more room to say the same thing. The prompt still
        # asks for a bare answer, so this widens the ceiling, not the reply.
        if detect_language(text) == "am":
            effective_max_tokens += AMHARIC_TOKEN_HEADROOM

        last_error: str | None = None
        for idx, (provider, model, api_key) in enumerate(attempts):
            attempt_t0 = time.perf_counter()
            try:
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
                except _TruncatedAnswerError:
                    # One retry with a ceiling wide enough to finish. Posting the
                    # fragment instead would look like a confident wrong answer.
                    logger.info("Answer truncated at %s tokens; retrying with room", effective_max_tokens)
                    raw_text = await self._call_provider(
                        provider=provider,
                        model=model,
                        api_key=api_key,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        temperature=temperature,
                        max_tokens=effective_max_tokens * 3,
                        image=image,
                        image_mime=image_mime,
                        speed_mode=speed_mode,
                    )
                elapsed_ms = int((time.perf_counter() - attempt_t0) * 1000)
                cleaned = self._clean_output(raw_text, answer_style)

                # A refusal is not an answer when a picture is attached. The
                # reported failure was exactly this: a legible poster plus a
                # "What is the answer" caption came back "No live data" - twice,
                # because two members asked. The model declined to quote a price
                # it was told not to invent while the price sat in the image it
                # was given. Ask once more with the picture framed as the source.
                if image and is_refusal(cleaned):
                    retried = await self._retry_from_image(
                        provider=provider,
                        model=model,
                        api_key=api_key,
                        text=text,
                        context_lines=context_lines,
                        answer_style=answer_style,
                        temperature=temperature,
                        max_tokens=effective_max_tokens,
                        image=image,
                        image_mime=image_mime,
                        speed_mode=speed_mode,
                        from_image=asked_from_image,
                        forex_group=forex_group,
                    )
                    if retried is not None:
                        cleaned = self._clean_output(retried, answer_style)
                    elapsed_ms = int((time.perf_counter() - attempt_t0) * 1000)

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
        has_image: bool = False,
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
            prompt = f"{prompt}\n{forex_group_guidance(has_live_data=bool(block), has_image=has_image)}"
            if block:
                prompt = f"{prompt}\n\n{block}"
        return prompt

    @staticmethod
    def _build_user_prompt(
        text: str,
        context_lines: list[str] | None,
        *,
        from_image: bool = False,
    ) -> str:
        """The question as the model should see it.

        ``from_image`` marks text that was transcribed out of the attached
        picture. Saying so matters: otherwise the model reads the same words
        twice - once in the prompt and once in the image - and concludes it is
        looking at two questions, or paraphrases instead of answering.
        """
        body = text.strip()
        if from_image:
            body = (
                "The question below is the text printed on the attached image, "
                "transcribed verbatim. Answer it; do not transcribe or describe "
                "the image again.\n\n"
                f"{body}"
            )
        if not context_lines:
            return body
        joined = "\n".join(context_lines[-3:])
        return f"Recent group context:\n{joined}\n\nLatest message to evaluate:\n{body}"

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
            # A vision model very often writes the label on one line and the
            # figure on the next:
            #
            #     **Answer:**
            #     **85**
            #
            # Keeping only the first line posted the word "Answer:" as the
            # answer. When the first line is nothing but a label, take the next
            # one instead.
            if _ANSWER_LABEL_ONLY_RE.fullmatch(text) and len(lines) > 1:
                text = lines[1]
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

    @staticmethod
    def _provider_attempts(
        *,
        primary_provider: str,
        primary_model: str,
        provider_keys: dict[str, str],
        fallback_provider: str | None = None,
        fallback_model: str | None = None,
    ) -> list[tuple[str, str, str]]:
        """Primary, then the optional fallback - only where a key exists."""
        attempts: list[tuple[str, str, str]] = []
        primary_key = provider_keys.get(primary_provider)
        if primary_key:
            attempts.append((primary_provider, primary_model, primary_key))
        if fallback_provider and fallback_model and fallback_provider != primary_provider:
            fallback_key = provider_keys.get(fallback_provider)
            if fallback_key:
                attempts.append((fallback_provider, fallback_model, fallback_key))
        return attempts

    #: Prompt for pulling the question out of a picture.
    #:
    #: Deliberately narrow: this call is not asked to answer anything, only to
    #: report what the image says, so a bad transcription costs a wasted string
    #: rather than a confident wrong number in a competition. Ask for the whole
    #: text rather than "the question" because a poster lays the question out
    #: across several lines around its artwork ("EYUEL FX presents", "The 20$
    #: Question", "On the Exness BTC chart,", "what was the highest price",
    #: "recorded this week?") and a model asked for "the question" will drop the
    #: half it decided was branding. Keeping every line and letting the parser
    #: find the question is far more forgiving.
    TRANSCRIBE_PROMPT = (
        "Transcribe all text visible in this image, line by line, exactly as "
        "written, keeping the original line breaks. Include every question, "
        "instrument name (BTC, ETH, gold, EURUSD, XAUUSD), number, time window "
        "(today, this week, last week, this month) and unit. Keep non-English "
        "text in its original script - do not translate it and do not "
        "transliterate it. If there are several numbered or bulleted questions, "
        "keep the numbering. Omit only logos and watermarks. Do not answer, "
        "explain or translate anything. If the image contains no legible text "
        "at all, reply with exactly: NONE"
    )

    #: Appended to the prompt when a vision answer came back as a refusal. The
    #: normal rules are deliberately strict about not inventing a price, and a
    #: model that reads an image only as decoration obeys them by declining. This
    #: says, in one place, that the picture is evidence - which is the part the
    #: general rule cannot know.
    REFUSAL_RETRY_GUIDANCE = (
        "You just replied that you have no live data. Look at the attached image "
        "again before repeating that. If the image shows a chart, a broker panel "
        "or a card with numbers or a question printed on it, those figures ARE "
        "the evidence: answer from them, briefly and directly. Only repeat the "
        "refusal if the image genuinely contains nothing to read."
    )

    async def _retry_from_image(
        self,
        *,
        provider: str,
        model: str,
        api_key: str,
        text: str,
        context_lines: list[str] | None,
        answer_style: str,
        temperature: float,
        max_tokens: int,
        image: bytes,
        image_mime: str,
        speed_mode: str,
        from_image: bool,
        forex_group: bool,
    ) -> str | None:
        """One second look at a picture after the model refused to read it.

        Returns None when the retry itself failed, so the caller keeps the
        original refusal rather than replacing it with an error.
        """
        # Same prompt as the first attempt, plus the one rule the model needs to
        # stop declining: that the picture is evidence. Rebuilt rather than
        # patched so the guidance stays a single, readable block.
        base = self._build_system_prompt(
            answer_style, text, forex_group=forex_group, market=None, has_image=True
        )
        system_prompt = f"{base}\n{self.REFUSAL_RETRY_GUIDANCE}"
        user_prompt = self._build_user_prompt(text, context_lines, from_image=from_image)
        try:
            return await self._call_provider(
                provider=provider,
                model=model,
                api_key=api_key,
                system_prompt=system_prompt,
                user_prompt=user_prompt or "Read the attached image and answer.",
                temperature=temperature,
                max_tokens=max_tokens,
                image=image,
                image_mime=image_mime,
                speed_mode=speed_mode,
            )
        except Exception as exc:  # noqa: BLE001 - keep the original refusal
            logger.warning("Refusal retry failed on %s: %s", provider, exc)
            return None

    @staticmethod
    def _has_answerable_text(text: str) -> bool:
        """True when the text looks like something we can actually answer.

        A caption can be long and question-free, so length says nothing. What
        matters is whether a shortcut could fire on it: a period question, a
        sum, or an explicit question marker.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return False
        if "?" in cleaned or "፧" in cleaned:
            return True
        if parse_period_query(cleaned) is not None:
            return True
        if try_solve_math(cleaned) is not None:
            return True
        if solve_forex_math(cleaned) is not None:
            return True
        return looks_like_question(cleaned)

    #: Words that mean the transcription is probably not a question we can
    #: answer locally, even though it is readable text.
    _NON_QUESTION_TRANSCRIPTIONS = ("NONE", "NO TEXT", "NO LEGIBLE TEXT")

    async def _transcribe_image(
        self,
        *,
        attempts: list[tuple[str, str, str]],
        image: bytes,
        image_mime: str = "image/jpeg",
    ) -> str:
        """Read the printed question out of a photo, or "" when there isn't one.

        Never raises. A failed transcription simply means the caller falls
        through to the ordinary vision answer, which is what happened before
        this existed - so the failure mode is the old behaviour, not an error.
        """
        for provider, model, api_key in attempts:
            vision_model = vision_model_for(provider, model)
            if vision_model is None:
                continue
            try:
                raw = await self._call_provider(
                    provider=provider,
                    model=vision_model,
                    api_key=api_key,
                    system_prompt=self.TRANSCRIBE_PROMPT,
                    user_prompt="Transcribe the text in this image.",
                    temperature=0.0,
                    max_tokens=400,
                    image=image,
                    image_mime=image_mime,
                )
            except Exception as exc:  # noqa: BLE001 - best effort by design
                logger.warning("Image transcription failed on %s: %s", provider, exc)
                continue
            cleaned = (raw or "").strip()
            flattened = " ".join(cleaned.split())
            if not flattened:
                return ""
            if flattened.rstrip(".").upper() in self._NON_QUESTION_TRANSCRIPTIONS:
                return ""
            # Keep the line structure the model reported: the period parser
            # works on prose but a question split across lines needs its spaces
            # preserved, not collapsed into one endless run-on.
            lines = [" ".join(line.split()) for line in cleaned.splitlines()]
            return " ".join(line for line in lines if line)
        return ""

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
            choice = choices[0]
            if choice.get("finish_reason") == "length":
                # The answer was cut off mid-thought. Posting the fragment is
                # worse than posting nothing: it reads as a confident wrong
                # answer. Callers treat it as SKIP and let the fallback try.
                raise _TruncatedAnswerError(
                    f"{provider}/{model} hit the {max_tokens}-token ceiling"
                )
            return str(choice.get("message", {}).get("content") or "")

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
            candidate = candidates[0]
            # MAX_TOKENS = 2 in the Gemini proto enum.
            if candidate.get("finishReason") in ("MAX_TOKENS", 2):
                raise _TruncatedAnswerError(
                    f"{provider}/{model} hit the {max_tokens}-token ceiling"
                )
            parts = candidate.get("content", {}).get("parts") or []
            return "".join(str(p.get("text", "")) for p in parts)

        raise ValueError(f"Unsupported provider: {provider}")
