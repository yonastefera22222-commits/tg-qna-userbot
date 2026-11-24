"""Dashboard Bot router handlers."""

from .ai_settings import router as ai_router
from .dashboard import router as dashboard_router
from .exness import router as exness_router
from .groups import router as groups_router
from .wizard import router as wizard_router

__all__ = [
    "ai_router",
    "dashboard_router",
    "exness_router",
    "groups_router",
    "wizard_router",
]
