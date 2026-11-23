"""FSM state definitions for the Dashboard Bot."""

from aiogram.fsm.state import State, StatesGroup


class WizardStates(StatesGroup):
    waiting_api_id = State()
    waiting_api_hash = State()
    waiting_phone = State()
    waiting_otp = State()
    waiting_login_email = State()
    waiting_login_email_code = State()
    waiting_qr_2fa = State()
    waiting_2fa = State()
    waiting_string_session = State()
    waiting_ai_key = State()
    waiting_custom_model = State()


class GroupStates(StatesGroup):
    waiting_search_query = State()
    waiting_trigger_keywords = State()


class DashboardStates(StatesGroup):
    waiting_sim_question = State()
    waiting_ai_key_update = State()
    waiting_custom_model_update = State()


def _state_names(group: type[StatesGroup]) -> tuple[str, ...]:
    """Fully-qualified state strings, e.g. ``DashboardStates:waiting_ai_key_update``."""
    return tuple(f"{group.__name__}:{name}" for name in group.__dict__ if not name.startswith("_"))


#: States owned by the AI / model menus (ai_settings router). The wizard's
#: catch-all must not consume input that belongs to them.
AI_MENU_STATES = frozenset(
    {
        f"{DashboardStates.__name__}:waiting_ai_key_update",
        f"{DashboardStates.__name__}:waiting_custom_model_update",
    }
)
