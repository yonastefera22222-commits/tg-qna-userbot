"""Catalog of fast, free AI providers (Groq, Google Gemini, OpenRouter) and live model discovery."""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class ModelPreset:
    model_id: str
    label: str
    note: str


@dataclass(frozen=True)
class ProviderSpec:
    slug: str
    name: str
    emoji: str
    signup_url: str
    base_url: str
    default_model: str
    presets: tuple[ModelPreset, ...]


PROVIDERS: dict[str, ProviderSpec] = {
    "groq": ProviderSpec(
        slug="groq",
        name="Groq",
        emoji="⚡",
        signup_url="https://console.groq.com/keys",
        base_url="https://api.groq.com/openai/v1",
        # Groq retired every Llama model from the free/developer tiers on
        # 2026-08-16 (llama-3.3-70b-versatile and llama-3.1-8b-instant are now
        # Enterprise-only), and shut down Llama 4 Scout and Qwen3 32B on
        # 2026-07-17. The free plan today is gpt-oss + Qwen3.8.
        default_model="openai/gpt-oss-120b",
        presets=(
            ModelPreset(
                "openai/gpt-oss-120b",
                "GPT-OSS 120B",
                "Groq's flagship free model — best accuracy, ~500 tok/s (Recommended)",
            ),
            ModelPreset(
                "openai/gpt-oss-20b",
                "GPT-OSS 20B",
                "Fastest free model (~1000 tok/s) — ideal for speed giveaways",
            ),
            ModelPreset(
                "qwen/qwen3.8-27b",
                "Qwen 3.8 27B",
                "Strong reasoning + multilingual trivia (preview)",
            ),
            ModelPreset(
                "openai/gpt-oss-safeguard-20b",
                "GPT-OSS Safeguard 20B",
                "Free preview; fallback if the others are rate-limited",
            ),
        ),
    ),
    "gemini": ProviderSpec(
        slug="gemini",
        name="Google Gemini",
        emoji="🧠",
        signup_url="https://aistudio.google.com/apikey",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        # Free-tier Flash / Flash-Lite IDs as of late 2026. 2.0 Flash was shut
        # down on 2026-06-01 and 2.5 access is restricted to older projects.
        default_model="gemini-3.5-flash-lite",
        presets=(
            ModelPreset(
                "gemini-3.5-flash-lite",
                "Gemini 3.5 Flash-Lite",
                "Highest free daily quota + ultra-fast",
            ),
            ModelPreset(
                "gemini-3.8-flash",
                "Gemini 3.8 Flash",
                "Newest Flash model, highest trivia accuracy",
            ),
            ModelPreset(
                "gemini-3.6-flash",
                "Gemini 3.6 Flash",
                "Stable Flash model on the free tier",
            ),
            ModelPreset(
                "gemini-3.1-flash-lite",
                "Gemini 3.1 Flash-Lite",
                "Lightweight & very quick responses",
            ),
        ),
    ),
    "openrouter": ProviderSpec(
        slug="openrouter",
        name="OpenRouter",
        emoji="🌐",
        signup_url="https://openrouter.ai/keys",
        base_url="https://openrouter.ai/api/v1",
        default_model="openrouter/free",
        presets=(
            ModelPreset(
                "openrouter/free",
                "OpenRouter Auto Free",
                "Auto-routes to the best available free model",
            ),
            ModelPreset(
                "google/gemma-4-31b-it:free",
                "Gemma 4 31B (Free)",
                "High-accuracy general & multilingual trivia",
            ),
            ModelPreset(
                "google/gemma-4-26b-a4b-it:free",
                "Gemma 4 26B MoE (Free)",
                "Fast active-4B MoE on free tier",
            ),
            ModelPreset(
                "nvidia/nemotron-3.5-lightning:free",
                "Nemotron 3.5 Lightning (Free)",
                "Low-latency free model",
            ),
            ModelPreset(
                "openai/gpt-oss-20b:free",
                "GPT-OSS 20B (Free)",
                "Compact general Q&A model",
            ),
        ),
    ),
}


#: Models that providers have retired. Used to explain a ``model_not_found``
#: error as "our default moved on" instead of blaming the owner's API key.
RETIRED_MODELS: dict[str, dict[str, str]] = {
    "groq": {
        "llama-3.3-70b-versatile": "openai/gpt-oss-120b",
        "llama-3.1-8b-instant": "openai/gpt-oss-20b",
        "meta-llama/llama-4-scout-17b-16e-instruct": "openai/gpt-oss-120b",
        "qwen/qwen3-32b": "openai/gpt-oss-120b",
        "qwen/qwen3.6-27b": "qwen/qwen3.8-27b",
        "llama3-70b-8192": "openai/gpt-oss-120b",
        "llama3-8b-8192": "openai/gpt-oss-20b",
        "deepseek-r1-distill-llama-70b": "openai/gpt-oss-120b",
    },
    "gemini": {
        "gemini-2.0-flash": "gemini-3.5-flash-lite",
        "gemini-2.0-flash-lite": "gemini-3.1-flash-lite",
        "gemini-2.5-flash": "gemini-3.5-flash-lite",
        "gemini-2.5-pro": "gemini-3.8-flash",
    },
}


def retired_model_replacement(provider: str, model: str) -> str | None:
    """Return the live replacement for a retired model ID, if we know it."""
    return RETIRED_MODELS.get(provider, {}).get(model)


def get_provider_spec(slug: str) -> ProviderSpec:
    if slug not in PROVIDERS:
        raise ValueError(f"Unknown provider: {slug}")
    return PROVIDERS[slug]


async def fetch_live_models(
    provider: str, api_key: str, client: httpx.AsyncClient, limit: int = 12
) -> list[str]:
    """Query the provider's live model catalog so users never get stuck on stale model IDs."""
    spec = get_provider_spec(provider)

    if provider == "groq":
        resp = await client.get(
            f"{spec.base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        skip_terms = ("whisper", "tts", "orpheus", "guard", "safeguard", "embed")
        models = [
            m["id"]
            for m in data
            if isinstance(m.get("id"), str)
            and not any(t in m["id"].lower() for t in skip_terms)
        ]
        models.sort()
        return models[:limit]

    if provider == "gemini":
        resp = await client.get(
            f"{spec.base_url}/models",
            params={"key": api_key, "pageSize": 100},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json().get("models", [])
        models = []
        for m in data:
            methods = m.get("supportedGenerationMethods", [])
            name = str(m.get("name", ""))
            if "generateContent" not in methods:
                continue
            model_id = name.removeprefix("models/")
            lower = model_id.lower()
            if ("flash" in lower or "gemma" in lower) and "tts" not in lower and "image" not in lower:
                models.append(model_id)
        models.sort(reverse=True)
        return models[:limit]

    if provider == "openrouter":
        resp = await client.get(
            f"{spec.base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        models = [
            m["id"]
            for m in data
            if isinstance(m.get("id"), str) and m["id"].endswith(":free")
        ]
        models.sort()
        return ["openrouter/free", *models[: max(0, limit - 1)]]

    return []
