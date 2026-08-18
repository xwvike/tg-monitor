"""
Telegram 消息格式化与安全发送 (core/tg_format.py)

Bot 全局启用 parse_mode="HTML"，因此**任何**插入消息体的动态内容
（命令输出、异常文本、进程名、容器名）只要含 `<`、`&` 就会让整条消息
被 Telegram 以 400 拒收。而这些拒收往往被 except 吞掉，只留一行日志 ——
定时任务的通知就是这样静默消失的。

两层防护：
  1. `esc()` / `code_block()`：在插值处转义，从源头保证正确
  2. `send_html()`：兜底。解析失败时自动降级为纯文本重发，
     绝不让消息**悄无声息地丢掉**
"""

import html
import logging
import re

from telebot import formatting

logger = logging.getLogger("TGFormat")

TG_MAX_LEN = 4096
SAFE_LEN = 3500


def esc(value) -> str:
    """转义任意值，使其可安全嵌入 HTML parse_mode 的消息体。"""
    return formatting.escape_html(str(value))


def code_block(text, limit: int = SAFE_LEN) -> str:
    """把任意输出包成 <pre>，自动转义并截断到 Telegram 可接受的长度。"""
    body = str(text).strip() or "(无输出)"
    if len(body) > limit:
        body = body[:limit] + "\n...(内容过长，已截断)"
    return f"<pre>{esc(body)}</pre>"


def strip_html(text: str) -> str:
    """退化为纯文本：去掉标签并还原实体，供解析失败时重发。"""
    return html.unescape(re.sub(r"<[^>]+>", "", str(text)))


def send_html(bot, chat_id, text, **kwargs):
    """以 HTML 发送；若 Telegram 因标记解析失败拒收，自动降级为纯文本重发。"""
    try:
        return bot.send_message(chat_id, text, parse_mode="HTML", **kwargs)
    except Exception as e:
        if "parse" not in str(e).lower() and "entit" not in str(e).lower():
            raise  # 网络等其他错误交给调用方处理，不要伪装成格式问题
        logger.warning(f"HTML 解析失败，降级为纯文本重发: {e}")
        return bot.send_message(chat_id, strip_html(text), parse_mode=None, **kwargs)
