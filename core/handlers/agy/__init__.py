from core.handlers.agy.chat import register_chat_handlers
from core.handlers.agy.constants import (
    BRAIN_DIR,
    FILE_CAPTION_WINDOW,
    MEDIA_GROUP_WINDOW,
    TEXT_ABSORB_MAX_AGE,
    WORKSPACE_ROOT,
    file_batches,
    file_batches_lock,
    user_buffers,
    user_buffers_lock,
)
from core.handlers.agy.media import register_media_handlers
from core.handlers.agy.tasks import run_file_task
from core.handlers.agy.utils import sweep_workspaces
from core.handlers.agy.voice import register_voice_handlers

__all__ = [
    "BRAIN_DIR",
    "FILE_CAPTION_WINDOW",
    "MEDIA_GROUP_WINDOW",
    "TEXT_ABSORB_MAX_AGE",
    "WORKSPACE_ROOT",
    "file_batches",
    "file_batches_lock",
    "register_agy_handlers",
    "run_file_task",
    "user_buffers",
    "user_buffers_lock",
]


def register_agy_handlers(
    bot,
    allowed_user_id: int,
    get_user_state_fn,
    save_user_states_fn,
    get_main_keyboard_fn,
):
    sweep_workspaces()

    claim_batch_fn = register_media_handlers(bot, allowed_user_id, get_user_state_fn, save_user_states_fn)
    register_voice_handlers(bot, allowed_user_id, get_user_state_fn, save_user_states_fn)
    dispatch_text_message, render_history_page, button_handlers = register_chat_handlers(
        bot, allowed_user_id, get_user_state_fn, save_user_states_fn, get_main_keyboard_fn, claim_batch_fn
    )

    return dispatch_text_message, render_history_page, button_handlers
