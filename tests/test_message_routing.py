#!/usr/bin/env python3
"""
消息路由与会话隔离测试 (tests/test_message_routing.py)

覆盖"一次操作产生两条互不相干的回复"这一类竞态：文件与文字必须合并成
单次请求，无论两者以什么顺序、间隔多久到达。用 FakeBot 完全离线驱动。
"""

import atexit
import os
import shutil
import sys
import tempfile
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import file_pipeline as fp
from core.handlers import agy as ah
from tests.harness import main

# Rig 会把 ah.run_file_task 换成桩，想真跑它必须在那之前留一份引用
_REAL_RUN_FILE_TASK = ah.run_file_task

# ⚠️ 本模块会 monkeypatch agy_handler 的模块级函数，且会调用 sweep_workspaces()。
# 绝不能被导入进真正在服务的 bot 进程 —— 仅供 --test-sandbox 与手动执行。

# 把工作区指向临时目录：真实的 /tmp/tg_files 里可能有正在处理的任务，
# 测试里的启动清扫会把它们连同产物一起删掉。
_TEST_WORKSPACE = tempfile.mkdtemp(prefix="tg_test_ws_")
import core.handlers.agy.constants as agy_constants
import core.handlers.agy.media as agy_media
import core.handlers.agy.tasks as agy_tasks
import core.handlers.agy.utils as agy_utils

ah.WORKSPACE_ROOT = _TEST_WORKSPACE
agy_constants.WORKSPACE_ROOT = _TEST_WORKSPACE
agy_media.WORKSPACE_ROOT = _TEST_WORKSPACE
agy_tasks.WORKSPACE_ROOT = _TEST_WORKSPACE
agy_utils.WORKSPACE_ROOT = _TEST_WORKSPACE
atexit.register(shutil.rmtree, _TEST_WORKSPACE, True)

# 压缩各窗口，让整套测试在数秒内跑完（--test-sandbox 会在还原流程中调用）
ah.MEDIA_GROUP_WINDOW = 0.25
agy_constants.MEDIA_GROUP_WINDOW = 0.25
agy_media.MEDIA_GROUP_WINDOW = 0.25

ah.FILE_CAPTION_WINDOW = 0.6
agy_constants.FILE_CAPTION_WINDOW = 0.6
agy_media.FILE_CAPTION_WINDOW = 0.6

ah.TEXT_ABSORB_MAX_AGE = 3.0
agy_constants.TEXT_ABSORB_MAX_AGE = 3.0
agy_media.TEXT_ABSORB_MAX_AGE = 3.0
SETTLE = 0.9  # 足够覆盖最长窗口 + 线程调度


class FakeBot:
    """只实现被测路径用到的 telebot 接口。"""

    def __init__(self):
        self.handlers = {}
        self.download_delay = 0.0

    def message_handler(self, *a, **k):
        def deco(fn):
            self.handlers[fn.__name__] = fn
            return fn
        return deco

    def callback_query_handler(self, *a, **k):
        return lambda fn: fn

    def send_chat_action(self, *a, **k):
        pass

    def send_message(self, chat_id, text, **k):
        return types.SimpleNamespace(message_id=999)

    def get_file(self, file_id):
        return types.SimpleNamespace(file_path="x.jpg", file_size=1000)

    def download_file(self, path):
        if self.download_delay:
            time.sleep(self.download_delay)
        return b"\xff\xd8\xff" + b"0" * 500


def photo(mid, caption=None, group=None, forwarded=False):
    return types.SimpleNamespace(
        message_id=mid,
        from_user=types.SimpleNamespace(id=42),
        chat=types.SimpleNamespace(id=42),
        media_group_id=group,
        caption=caption,
        text=None,
        content_type="photo",
        photo=[types.SimpleNamespace(file_id=f"f{mid}", file_size=1000)],
        # 真实 telebot Message 上这些属性恒存在（未命中时为 None），
        # handle_any_file 会一次性取全部四个，缺一个就抛 AttributeError
        document=None,
        video=None,
        audio=None,
        video_note=None,
        voice=None,
        sticker=None,
        forward_origin=(
            types.SimpleNamespace(type="user",
                                  sender_user=types.SimpleNamespace(first_name="Eric"))
            if forwarded else None
        ),
    )


def voice(mid):
    m = photo(mid)
    m.content_type = "voice"
    m.voice = types.SimpleNamespace(file_id=f"v{mid}", file_size=1000)
    return m


def document(mid, name="a.pdf"):
    m = photo(mid)
    m.content_type = "document"
    m.document = types.SimpleNamespace(file_id=f"d{mid}", file_size=1000, file_name=name)
    return m


def sticker(mid):
    m = photo(mid)
    m.content_type = "sticker"
    m.sticker = types.SimpleNamespace(emoji="🙂")
    return m


def text(mid, body):
    m = photo(mid)
    m.text = body
    m.content_type = "text"
    return m


class Rig:
    """装配一套隔离的 handler，并拦截最终出口只记录"发起了几次请求"。"""

    def __init__(self):
        self.bot = FakeBot()
        self.launched = []
        self.state = {"in_chat": True, "conv_id": "c", "model": "m"}
        self.dispatch_text, _, _btn = ah.register_agy_handlers(
            self.bot, 42, lambda uid: self.state, lambda: None, lambda uid: None
        )
        self.send_photo = self.bot.handlers["handle_photo"]

        # 分流已经删掉：文件任务只有 run_file_task 一个出口
        def mock_run_file_task(b, m, files, wi, wo, cap, mo, tg_photo=False):
            self.launched.append(("TASK", cap, len(files), "", tg_photo))
        
        import core.handlers.agy.media as agy_media
        agy_media.run_file_task = mock_run_file_task
        ah.run_file_task = mock_run_file_task

    def reset(self):
        for store, lock in ((ah.file_batches, ah.file_batches_lock),
                            (ah.user_buffers, ah.user_buffers_lock)):
            with lock:
                for entry in store.values():
                    if entry.get("timer"):
                        entry["timer"].cancel()
                store.clear()
        self.launched.clear()

    def first(self, idx=1):
        return self.launched[0][idx] if self.launched else None


def test_file_then_text(s):
    r = Rig()

    s.section("先发文件、后打字（手动追问）")
    r.reset()
    r.send_photo(photo(1))
    time.sleep(0.15)
    s.check("文件未独立发起请求", len(r.launched), 0)
    r.dispatch_text(text(2, "这张图什么意思？"))
    time.sleep(0.3)
    s.check("合并成单次请求", len(r.launched), 1)
    s.check("指令被认领", r.first(1), "这张图什么意思？")
    s.check("走文件任务", r.first(0), "TASK")

    s.section("后打的是处理指令")
    r.reset()
    r.send_photo(photo(3))
    time.sleep(0.15)
    r.dispatch_text(text(4, "帮我压缩一下"))
    time.sleep(0.3)
    s.check("单次请求", len(r.launched), 1)
    s.check("走文件任务", r.first(0), "TASK")

    s.section("一直不说话 → 窗口到点用默认问句放行")
    r.reset()
    r.send_photo(photo(5))
    time.sleep(0.15)
    s.check("窗口内未发出", len(r.launched), 0)
    time.sleep(SETTLE)
    s.check("窗口后自动放行", len(r.launched), 1)
    s.check("走文件任务", r.first(0), "TASK")


def test_text_then_file(s):
    r = Rig()

    s.section("转发+评论：评论先到（TG 的实际顺序）")
    r.reset()
    r.dispatch_text(text(20, "上下拼接，第一张在上面"))
    time.sleep(0.03)
    r.send_photo(photo(21, group="g2", forwarded=True))
    r.send_photo(photo(22, group="g2", forwarded=True))
    time.sleep(SETTLE)
    s.check("合并成单次请求", len(r.launched), 1)
    s.check("评论被吸收为指令", r.first(1), "上下拼接，第一张在上面")
    s.check("两张图都在同一任务", r.first(2), 2)

    s.section("转发+评论：单文件")
    r.reset()
    r.dispatch_text(text(23, "何意为？"))
    time.sleep(0.03)
    r.send_photo(photo(24, forwarded=True))
    time.sleep(SETTLE)
    s.check("合并成单次请求", len(r.launched), 1)
    s.check("评论被吸收", r.first(1), "何意为？")

    s.section("陈旧闲聊不得被文件误吸收")
    r.reset()
    saved = ah.TEXT_ABSORB_MAX_AGE
    ah.TEXT_ABSORB_MAX_AGE = 0.2
    agy_media.TEXT_ABSORB_MAX_AGE = 0.2
    try:
        r.dispatch_text(text(25, "你好啊"))
        time.sleep(0.4)
        r.send_photo(photo(26))
        time.sleep(0.15)
        s.check("文件未吸收陈旧文本", len(r.launched), 0)
    finally:
        ah.TEXT_ABSORB_MAX_AGE = saved
        agy_media.TEXT_ABSORB_MAX_AGE = saved


def test_text_during_download(s):
    r = Rig()

    s.section("文字在文件【下载期间】到达")
    r.reset()
    r.bot.download_delay = 0.5
    threading.Thread(target=lambda: r.send_photo(photo(40, forwarded=True))).start()
    time.sleep(0.15)  # 此刻仍在下载
    r.dispatch_text(text(41, "解释一下这张图"))
    time.sleep(SETTLE + 0.5)
    r.bot.download_delay = 0.0
    s.check("只发起一次请求", len(r.launched), 1)
    s.check("下载期间的文字被认领", r.first(1), "解释一下这张图")


def test_caption_ownership(s):
    r = Rig()

    s.section("转发件的原作者 caption 不是用户指令")
    r.reset()
    r.dispatch_text(text(30, "上下拼接，第一张在上面"))
    time.sleep(0.03)
    r.send_photo(photo(31, caption="via Eric", group="g3", forwarded=True))
    r.send_photo(photo(32, caption="via Eric", group="g3", forwarded=True))
    time.sleep(SETTLE)
    s.check("只发起一次请求", len(r.launched), 1)
    s.truthy("指令以用户评论开头", (r.first(1) or "").startswith("上下拼接，第一张在上面"))
    s.truthy("原 caption 降级为括注而非指令", "原始附带说明：via Eric" in (r.first(1) or ""))
    s.check("两张图都在", r.first(2), 2)

    s.section("转发件带 caption 但用户没写评论 → 降级为上下文")
    r.reset()
    r.send_photo(photo(33, caption="via Eric", forwarded=True))
    time.sleep(0.15)
    s.check("原 caption 未被当成指令立即开工", len(r.launched), 0)
    time.sleep(SETTLE)
    s.check("窗口后按默认问句放行", len(r.launched), 1)
    s.check("没写评论时原 caption 即指令", r.first(1), "via Eric")

    s.section("非转发件的 caption 仍然是用户指令")
    r.reset()
    r.send_photo(photo(34, caption="压缩一下"))
    time.sleep(0.3)
    s.check("立即开工", len(r.launched), 1)
    s.check("指令即 caption", r.first(1), "压缩一下")

    # 用户转发自己那条带「转gif」的视频、且没另写评论时，旧实现判路由时把
    # 原 caption 当作不存在（→ 问答），却又把它塞进问答 prompt。AGY 于是照着
    # 它真跑了 ffmpeg，可问答链路没有回传通道，用户只收到一个够不着的服务器
    # 路径，产物随工作区被清掉 —— 表现为"它说做完了，我什么也没收到"。
    s.section("转发件原 caption 是处理指令 → 必须真去处理，不能在问答里空转")
    r.reset()
    r.send_photo(photo(35, caption="转成gif", forwarded=True))
    time.sleep(SETTLE)
    s.check("发起了一次", len(r.launched), 1)
    s.check("走文件任务", r.first(0), "TASK")
    s.check("指令取自转发原文", r.first(1), "转成gif")

    s.section("自己写了评论时，转发原文仍然只当上下文")
    r.reset()
    r.dispatch_text(text(36, "压缩一下"))
    time.sleep(0.03)
    r.send_photo(photo(37, caption="转成gif", forwarded=True))
    time.sleep(SETTLE)
    s.check("发起了一次", len(r.launched), 1)
    s.truthy("指令以自己的评论开头", (r.first(1) or "").startswith("压缩一下"))


def test_handlers_respect_chat_mode(s):
    """所有内容类 handler 必须一致地只在对话模式下响应。

    语音曾是唯一漏掉 in_chat 守卫的入口 —— 面板模式下发语音仍会唤起 AGY，
    而图片/文档/贴纸都会安静忽略。
    """
    r = Rig()
    import core.handlers.agy.voice as agy_voice
    original_stt = agy_voice.transcribe_voice_file
    try:
        r.reset()
        r.state["in_chat"] = False

        s.section("非对话模式：一律不响应")
        stt_calls = []
        agy_voice.transcribe_voice_file = lambda p: (stt_calls.append(p), (True, "x"))[1]

        r.bot.handlers["handle_voice"](voice(50))
        s.check("voice 未触发 STT", stt_calls, [])

        r.bot.handlers["handle_photo"](photo(51))
        time.sleep(0.15)
        s.check("photo 未建立文件批次", len(ah.file_batches), 0)

        r.bot.handlers["handle_any_file"](document(52))
        time.sleep(0.15)
        s.check("document 未建立文件批次", len(ah.file_batches), 0)

        r.bot.handlers["handle_sticker"](sticker(53))
        s.check("sticker 未发起请求", len(r.launched), 0)

        s.check("文本分发也返回未处理", r.dispatch_text(text(54, "hi")), False)

        s.section("对话模式：正常响应")
        r.reset()
        r.state["in_chat"] = True
        stt_calls.clear()
        r.bot.handlers["handle_voice"](voice(55))
        s.check("voice 触发 STT", len(stt_calls), 1)

        r.bot.handlers["handle_photo"](photo(56))
        time.sleep(0.15)
        s.check("photo 建立了文件批次", len(ah.file_batches), 1)
    finally:
        agy_voice.transcribe_voice_file = original_stt
        r.state["in_chat"] = True
        r.reset()


def test_ingest_sanitizes_filename(s):
    """落盘环节必须真的调用收敛函数 —— 只测工具函数挡不住这个回归。"""
    r = Rig()
    r.reset()
    s.section("恶意文件名落盘时被中和")
    evil = "report;touch PWNED_MARKER;.pdf"
    r.bot.handlers["handle_any_file"](document(60, name=evil))
    time.sleep(0.2)

    with ah.file_batches_lock:
        batch = ah.file_batches.get(42)
        workspace = batch["in"] if batch else None
    s.truthy("批次已建立", workspace is not None)

    on_disk = sorted(os.listdir(workspace)) if workspace else []
    s.check("落盘 1 个文件", len(on_disk), 1)
    name = on_disk[0] if on_disk else ""
    s.check("磁盘上的文件名不含 shell 元字符",
            sorted(set(name) & set(";|&$`()<>*?'\" ")), [])
    s.truthy("扩展名保留", name.endswith(".pdf"))
    s.check("原始危险名未落盘", name == evil, False)

    s.section("正常文件名不受影响")
    r.reset()
    r.bot.handlers["handle_any_file"](document(61, name="季度报告.pdf"))
    time.sleep(0.2)
    with ah.file_batches_lock:
        batch = ah.file_batches.get(42)
        workspace = batch["in"] if batch else None
    on_disk = sorted(os.listdir(workspace)) if workspace else []
    s.check("中文文件名原样保留", on_disk, ["季度报告.pdf"])
    r.reset()


def test_conversation_isolation(s):
    s.section("内部会话不得污染 /history 与 conv_id")
    s.truthy("Planner prompt 带内部标记",
             fp.INTERNAL_MARKER in fp.build_task_prompt(["/a.jpg"], "/out", "压缩"))
    import core.handlers.agy.utils as agy_utils
    s.check("预览剥掉 XML 包装",
            agy_utils._clean_preview("<USER_REQUEST>\n何意为？\n</USER_REQUEST>"
                              "\n<ADDITIONAL_METADATA>\nt\n</ADDITIONAL_METADATA>"),
            "何意为？")
    s.check("新标记会被识别为内部会话",
            agy_utils._is_internal_conversation(f"x {fp.INTERNAL_MARKER} y"), True)
    import core.handlers.agy.constants as agy_constants
    for sig in agy_constants.LEGACY_INTERNAL_SIGNATURES:
        s.check(f"历史特征被识别: {sig[:16]}...",
                agy_utils._is_internal_conversation(f"<USER_REQUEST>{sig}..."), True)
    s.check("用户真实会话不被误杀", agy_utils._is_internal_conversation("帮我看看这段代码"), False)

    if os.path.isdir(ah.BRAIN_DIR):
        convs = agy_utils.get_brain_conversations()
        polluted = [c for c in convs if "Planner" in c[1] or fp.INTERNAL_MARKER in c[1]]
        s.check("实际会话列表中无 Planner 残留", polluted, [])


def test_workspace_not_on_tmpfs(s):
    """工作区必须落在真实磁盘上。

    放 /tmp 会占内存：本机 /tmp 是 tmpfs（3.8G 上限，可用内存仅 2.6G），
    而视频转码的中间产物不受 MAX_TG_FILE_SIZE 约束，峰值轻易上 GB。
    这个回归不会报错，只会表现为"任务莫名其妙失败"，所以必须由测试守住。
    """
    import importlib
    import subprocess

    s.section("默认位置")
    # 注意：模块级 WORKSPACE_ROOT 已被本测试文件改写成临时目录，
    # 因此重新读一份未被污染的默认值
    src = importlib.import_module("core.handlers.agy.constants")
    assert src.__file__ is not None
    default = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(src.__file__))),
        "..", "workspace",
    )
    s.check("默认值不在 /tmp 下",
            os.path.normpath(default).startswith("/tmp"), False)

    s.section("实际挂载点不是 tmpfs")
    probe = os.path.normpath(default)
    while not os.path.exists(probe) and probe != "/":
        probe = os.path.dirname(probe)
    fstype = subprocess.run(
        ["stat", "-f", "-c", "%T", probe],
        capture_output=True, text=True,
    ).stdout.strip()
    # 只断言"不是内存盘"—— 具体是 ext4 / btrfs / overlayfs 都无所谓，
    # 写死某个文件系统会让干净容器里的部署验证误报
    s.check(f"{probe} 的文件系统 ({fstype or '?'}) 不是内存盘",
            fstype in ("tmpfs", "ramfs"), False)

    s.section("可被环境变量覆盖")
    s.truthy("读取 TG_WORKSPACE_ROOT",
             "TG_WORKSPACE_ROOT" in _read_source(src.__file__))


def _read_source(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_tg_photo_flag(s):
    """以「图片」方式上传的文件已被 Telegram 转码缩图，必须传到下游并如实告知。

    症状是"我发的 png 压完变成又小又糊的 jpg" —— 转码发生在客户端，
    流水线没做错，但沉默会让用户以为是我们弄坏的。
    """
    r = Rig()

    s.section("photo 上传打标")
    r.reset()
    r.send_photo(photo(80, caption="压缩一下"))
    time.sleep(SETTLE)
    s.check("发起了处理任务", len(r.launched), 1)
    s.check("标记为 Telegram 图片", r.launched[0][4], True)

    s.section("document 上传不打标")
    r.reset()
    doc = document(81, name="raw.png")
    doc.caption = "压缩一下"
    r.bot.handlers["handle_any_file"](doc)
    time.sleep(SETTLE)
    s.check("发起了处理任务", len(r.launched), 1)
    s.check("未被标记", r.launched[0][4], False)

    s.section("提示真的发出去了（run_file_task 端到端）")
    for flag, expect in ((True, 1), (False, 0)):
        work = tempfile.mkdtemp()
        sent = []
        try:
            wout = os.path.join(work, "out")
            os.makedirs(wout)
            product = os.path.join(wout, "a_compressed.png")
            with open(product, "wb") as fh:
                fh.write(b"\x89PNG" + b"0" * 100)

            class _Bot:
                def __init__(self, target_list):
                    self.sent = target_list

                def send_message(self, cid, txt, **k):
                    self.sent.append(txt)
                    return types.SimpleNamespace(message_id=1)

                def edit_message_text(self, *a, **k):
                    pass

                def delete_message(self, *a, **k):
                    pass

                def send_chat_action(self, *a, **k):
                    pass

                def send_document(self, *a, **k):
                    pass

            import core.handlers.agy.tasks as agy_tasks
            orig_run, orig_html = agy_tasks.run_task, agy_tasks.send_html
            agy_tasks.run_task = lambda *a, _p=product, **k: (True, [_p], "", None, None)
            agy_tasks.send_html = lambda b, cid, txt, _s=sent, **k: _s.append(txt)
            try:
                _REAL_RUN_FILE_TASK(_Bot(sent), photo(90), [product], work, wout,
                                    "压缩一下", "m", flag)
            finally:
                agy_tasks.run_task, agy_tasks.send_html = orig_run, orig_html
            s.check(f"tg_photo={flag} 时发出提示的条数",
                    sum(1 for t in sent if "「<b>图片</b>」" in t), expect)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    s.section("提示文案")
    import core.handlers.agy.constants as agy_constants
    s.truthy("点明是「图片」方式发送的", "「<b>图片</b>」" in agy_constants.TG_PHOTO_NOTICE)
    s.truthy("说明已转成 JPEG", "JPEG" in agy_constants.TG_PHOTO_NOTICE)
    s.truthy("说明尺寸被缩小", "缩小" in agy_constants.TG_PHOTO_NOTICE)
    # 两条出路都要给：桌面端「压缩」勾选框默认勾上，取消它比改发文件更顺手
    s.truthy("给出取消勾选压缩的做法", "取消勾选「压缩」" in agy_constants.TG_PHOTO_NOTICE)
    s.truthy("给出改发文件的做法", "「<b>文件</b>」" in agy_constants.TG_PHOTO_NOTICE)


def test_workspace_sweep(s):
    s.section("启动清扫遗留工作区")
    probe_in = os.path.join(ah.WORKSPACE_ROOT, "in", "__sweep_probe__")
    probe_out = os.path.join(ah.WORKSPACE_ROOT, "out", "__sweep_probe__")
    os.makedirs(probe_in, exist_ok=True)
    os.makedirs(probe_out, exist_ok=True)
    open(os.path.join(probe_in, "leftover.bin"), "w").close()
    ah.sweep_workspaces()
    s.check("遗留输入目录已回收", os.path.exists(probe_in), False)
    s.check("遗留输出目录已回收", os.path.exists(probe_out), False)


SUITES = [
    ("文件先到", test_file_then_text),
    ("文本先到", test_text_then_file),
    ("下载期间到达", test_text_during_download),
    ("caption 归属", test_caption_ownership),
    ("对话模式守卫", test_handlers_respect_chat_mode),
    ("落盘文件名收敛", test_ingest_sanitizes_filename),
    ("会话隔离", test_conversation_isolation),
    ("工作区不在内存盘", test_workspace_not_on_tmpfs),
    ("Telegram 图片打标", test_tg_photo_flag),
    ("工作区清扫", test_workspace_sweep),
]

if __name__ == "__main__":
    main(SUITES)
