from core.handlers.agy.chat import register_chat_handlers
from core.handlers.agy.media import register_media_handlers
from core.handlers.agy.voice import register_voice_handlers

__all__ = ["register_agy_handlers"]


def register_agy_handlers(
    bot,
    allowed_user_id: int,
    get_user_state_fn,
    save_user_states_fn,
    get_main_keyboard_fn,
):
    dispatch_text_message, render_history_page, button_handlers, begin_attachment, end_attachment = (
        register_chat_handlers(
            bot, allowed_user_id, get_user_state_fn, save_user_states_fn, get_main_keyboard_fn
        )
    )
    register_media_handlers(
        bot, allowed_user_id, get_user_state_fn, save_user_states_fn, begin_attachment, end_attachment
    )
    register_voice_handlers(bot, allowed_user_id, get_user_state_fn, save_user_states_fn)

    return dispatch_text_message, render_history_page, button_handlers
