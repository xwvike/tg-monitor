#!/usr/bin/env python3
"""
Telegram 消息格式化测试 (tests/test_tg_format.py)

守护一类静默故障：Bot 全局 parse_mode="HTML"，动态内容里一个 `<` 就让
整条消息被 Telegram 以 400 拒收，而拒收往往被 except 吞掉只留一行日志 ——
定时任务的 RSS 通知就是这样消失的。
"""

import os
import re
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tg_format import code_block, esc, send_html, strip_html
from tests.harness import main

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeTelegramError(Exception):
    """模拟 telebot 抛出的 ApiTelegramException（不引入真实依赖）。"""


class _RejectingBot:
    """模拟 Telegram：HTML 解析失败就返回 400。"""

    def __init__(self, trigger="<script>"):
        self.trigger = trigger
        self.sent = []

    def send_message(self, chat_id, text, parse_mode=None, **kwargs):
        if parse_mode == "HTML" and self.trigger in text:
            raise FakeTelegramError(
                "A request to the Telegram API was unsuccessful. Error code: 400. "
                "Description: Bad Request: can't parse entities: Unsupported start tag"
            )
        self.sent.append((parse_mode, text))
        return types.SimpleNamespace(message_id=1)


def test_escaping(s):
    s.section("基础转义")
    s.check("尖括号与 &", esc('<b>x</b> & "q"'),
            "&lt;b&gt;x&lt;/b&gt; &amp; &quot;q&quot;")
    s.check("非字符串也能处理", esc(ValueError("bad <tag>")), "bad &lt;tag&gt;")

    s.section("code_block")
    s.truthy("包成 <pre>", code_block("x").startswith("<pre>"))
    s.check("内容已转义", "<p>" in code_block("<p>hi</p>"), False)
    s.truthy("空输入有占位", "无输出" in code_block("   "))
    long_block = code_block("A" * 6000)
    s.truthy("超长被截断", len(long_block) < 4096)
    s.truthy("截断有提示", "已截断" in long_block)


def test_real_world_payloads(s):
    s.section("真实会翻车的载荷")
    cases = {
        "RSS 正文": "<p>Claude Code v2.1 & <code>--flag</code></p>",
        "XML 输出": '<?xml version="1.0"?><root/>',
        "ffmpeg 报错": "Option -vf <filter> requires an argument",
        "进程名": "python3 <defunct>",
        "shell 重定向": "cmd 2>&1 | grep x",
    }
    for name, payload in cases.items():
        block = code_block(payload)
        # 转义后不得残留任何会被 Telegram 当作标签起始的裸 '<'
        inner = block[len("<pre>"):-len("</pre>")]
        s.check(f"{name} 无裸 <", "<" in inner, False)
        s.check(f"{name} 无裸 &（已实体化）",
                bool(re.search(r"&(?!(amp|lt|gt|quot|#\d+);)", inner)), False)


def test_send_fallback(s):
    s.section("兜底：漏转义也不得静默丢失")
    bot = _RejectingBot()
    send_html(bot, 1, "标题 <script>漏转义的内容</script>")
    s.check("发出了 1 条", len(bot.sent), 1)
    mode, text = bot.sent[0]
    s.check("降级为纯文本", mode, None)
    s.truthy("正文仍然送达", "漏转义的内容" in text)

    s.section("正常情况仍走 HTML")
    bot2 = _RejectingBot()
    send_html(bot2, 1, "<b>正常消息</b>")
    s.check("parse_mode 保持 HTML", bot2.sent[0][0], "HTML")

    s.section("非解析类错误必须原样上抛，不得伪装成格式问题")

    class _NetworkDown:
        def send_message(self, *a, **k):
            raise FakeTelegramError("Connection timed out")

    try:
        send_html(_NetworkDown(), 1, "x")
        s.check("网络错误被吞掉", True, False)
    except Exception as e:
        s.truthy("网络错误原样上抛", "timed out" in str(e))

    s.section("strip_html 还原实体")
    s.check("标签剥离 + 实体还原", strip_html("<b>a</b> &lt;t&gt; &amp; b"), "a <t> & b")


def test_no_unescaped_interpolation(s):
    """静态守护：源码里不得再出现未转义的 HTML 插值。"""
    s.section("源码扫描")
    offenders = []
    for root, _dirs, files in os.walk(os.path.join(PROJECT_DIR, "core")):
        if "__pycache__" in root:
            continue
        for fname in files:
            if not fname.endswith(".py") or fname == "tg_format.py":
                continue
            path = os.path.join(root, fname)
            with open(path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    # <pre>{x} / <code>{x} 里的 x 必须经过转义函数
                    for m in re.finditer(r"<(?:pre|code)>\{([^}]+)\}", line):
                        expr = m.group(1)
                        if not re.search(r"esc\(|escape_html|_escape\(|escaped_", expr):
                            offenders.append(
                                f"{os.path.relpath(path, PROJECT_DIR)}:{lineno} → {expr}"
                            )
    s.check("无未转义的 <pre>/<code> 插值", offenders, [])


SUITES = [
    ("转义基础", test_escaping),
    ("真实载荷", test_real_world_payloads),
    ("发送兜底", test_send_fallback),
    ("源码静态扫描", test_no_unescaped_interpolation),
]

if __name__ == "__main__":
    main(SUITES)
