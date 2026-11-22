"""AI provider clients, giveaway answer engine, and instant local math solver."""

from .engine import AIEngine, AnswerResult
from .math_solver import try_solve_math
from .providers import PROVIDERS, ProviderSpec, fetch_live_models, get_provider_spec

__all__ = [
    "AIEngine",
    "AnswerResult",
    "PROVIDERS",
    "ProviderSpec",
    "fetch_live_models",
    "get_provider_spec",
    "try_solve_math",
]
