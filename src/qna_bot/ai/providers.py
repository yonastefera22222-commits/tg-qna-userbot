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
        default_model="llama-3.3-70b-versatile",
        presets=(
            ModelPreset(
                "llama-3.3-70b-versatile",
                "Llama 3.3 70B Versatile",
                "Best accuracy + ~300 tok/s (Recommended)",
            ),
            ModelPreset(
                "llama-3.1-8b-instant",
                "Llama 3.1 8B Instant",
                "Ultra-fast ~800+ tok/s for speed giveaways",
            ),
            ModelPreset(
                "meta-llama/llama-4-scout-17b-16e-instruct",
                "Llama 4 Scout 17B",
                "Very fast MoE model with strong trivia accuracy",
            ),
            ModelPreset(
                "qwen/qwen3-32b",
                "Qwen 3 32B",
                "Strong reasoning & multilingual Q&A",
            ),
            ModelPreset(
                "openai/gpt-oss-20b",
                "GPT-OSS 20B",
                "Blazing speed on Groq LPUs",
            ),
        ),
    ),
    "gemini": ProviderSpec(
        slug="gemini",
        name="Google Gemini",
        emoji="🧠",
        signup_url="https://aistudio.google.com/apikey",
        base_url="https://generativelanguage.googleapis.com/v1beta",
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
                "Stable Flash model on free tier",
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
