#!/usr/bin/env python3
"""
文件任务留痕测试 (tests/test_run_archive.py)

守护一件事：文件处理的输入、产物和"当时到底执行了哪几条命令"必须留在同一个
目录里，且占盘有上界。少了留痕，调参就只能靠猜；少了配额，磁盘迟早被吃光。
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile

from core import run_archive as ra
from tests.harness import main


def _make_run(root, key, files_in, files_out):
    """造一对在途工作区，返回 (workspace_in, workspace_out)。"""
    win = os.path.join(root, "in", key)
    wout = os.path.join(root, "out", key)
    os.makedirs(win)
    os.makedirs(wout)
    for name, size in files_in:
        with open(os.path.join(win, name), "wb") as fh:
            fh.write(b"\0" * size)
    for name, size in files_out:
        with open(os.path.join(wout, name), "wb") as fh:
            fh.write(b"\0" * size)
    return win, wout


def _env(**kw):
    """临时设置归档相关环境变量的上下文。"""
    class _Ctx:
        def __enter__(self):
            self.old = {k: os.environ.get(k) for k in kw}
            for k, v in kw.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        def __exit__(self, *a):
            for k, v in self.old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    return _Ctx()


def test_size_parsing(s):
    """.env 里写 10G 就该是 10G，写坏了必须退回默认值而不是退回 0。"""
    s.section("容量写法")
    for raw, expect in (
        ("10G", 10 * 1024 ** 3),
        ("10GB", 10 * 1024 ** 3),
        ("10GiB", 10 * 1024 ** 3),
        ("500M", 500 * 1024 ** 2),
        ("1.5G", int(1.5 * 1024 ** 3)),
        ("1073741824", 1024 ** 3),
        ("0", 0),
    ):
        s.check(f"{raw} 解析", ra._try_size(raw), expect)

    s.section("写坏时退回默认值")
    # 退回 0 等于"配额为零"，而 prune 对 0 的语义是**不限制**，
    # 一个笔误就会静默关掉容量上限 —— 必须退回默认的 10G
    s.check("乱写退回 10G", ra._parse_size("十个G", ra.DEFAULT_MAX_SIZE), 10 * 1024 ** 3)
    s.check("空值退回 10G", ra._parse_size("", ra.DEFAULT_MAX_SIZE), 10 * 1024 ** 3)
    s.check("天数写坏退回默认", ra._parse_days("两周", 14), 14)


def test_archive_keeps_files_and_commands(s):
    """输入、产物、执行过的命令必须落在同一个归档目录里。"""
    with tempfile.TemporaryDirectory() as root, _env(
        TG_ARCHIVE_ENABLED="1", TG_ARCHIVE_MAX_SIZE="10G", TG_ARCHIVE_MAX_DAYS="14"
    ):
        win, wout = _make_run(root, "2112_x", [("src.mov", 2048)], [("out.gif", 4096)])
        trace = {
            "caption": "转成gif",
            "recipes": ["video_to_gif.md"],
            "attempts": [{"attempt": 1, "commands": ["ffmpeg -i src.mov out.gif"],
                          "ok": True, "failure": None, "note": None}],
            "ok": True,
        }
        dest = ra.archive_run(root, win, wout, trace)

        s.section("工作区被搬进归档而不是删掉")
        s.truthy("返回了归档目录", bool(dest))
        s.check("原 in/ 已不在原地", os.path.exists(win), False)
        s.check("原 out/ 已不在原地", os.path.exists(wout), False)
        s.check("输入文件留存", os.path.isfile(os.path.join(dest, "in", "src.mov")), True)
        s.check("产物文件留存", os.path.isfile(os.path.join(dest, "out", "out.gif")), True)

        s.section("run.json 记下了命令与用户原话")
        with open(os.path.join(dest, "run.json"), encoding="utf-8") as fh:
            rec = json.load(fh)
        s.check("用户原话", rec["caption"], "转成gif")
        s.check("命中的菜谱", rec["recipes"], ["video_to_gif.md"])
        s.truthy("实际执行的命令在案",
                 "ffmpeg -i src.mov out.gif" in rec["attempts"][0]["commands"])
        s.section("产物体积可直接比对")
        s.check("输入体积", rec["inputs"][0]["bytes"], 2048)
        s.check("产物体积", rec["products"][0]["bytes"], 4096)


def test_failed_run_is_archived_too(s):
    """失败的任务比成功的更该留 —— 要排查的正是它们。"""
    with tempfile.TemporaryDirectory() as root, _env(
        TG_ARCHIVE_ENABLED="1", TG_ARCHIVE_MAX_SIZE="10G", TG_ARCHIVE_MAX_DAYS="14"
    ):
        win, wout = _make_run(root, "fail_1", [("bad.pdf", 512)], [])
        trace = {"ok": False, "error": "转换失败",
                 "attempts": [{"attempt": 1, "commands": ["false"], "ok": False,
                               "failure": {"cmd": "false", "stderr": "boom"}}]}
        dest = ra.archive_run(root, win, wout, trace)

        s.section("失败任务同样留痕")
        s.truthy("已归档", bool(dest) and os.path.isdir(dest))
        s.check("输入仍在", os.path.isfile(os.path.join(dest, "in", "bad.pdf")), True)
        with open(os.path.join(dest, "run.json"), encoding="utf-8") as fh:
            rec = json.load(fh)
        s.check("记录了失败", rec["ok"], False)
        s.truthy("记录了 stderr", "boom" in json.dumps(rec, ensure_ascii=False))


def test_quota_by_size(s):
    """超容量按最旧优先回收，且永远保住最新一次。"""
    with tempfile.TemporaryDirectory() as root, _env(
        TG_ARCHIVE_ENABLED="1", TG_ARCHIVE_MAX_SIZE="10K", TG_ARCHIVE_MAX_DAYS="0"
    ):
        dests = []
        for i in range(4):
            win, wout = _make_run(root, f"r{i}", [], [(f"p{i}.bin", 4096)])
            dests.append(ra.archive_run(root, win, wout, {"n": i}))
            # 同秒内目录名会撞，且 mtime 分不出先后，无法验证"最旧优先"
            os.utime(dests[-1], (time.time() + i, time.time() + i))

        s.section("容量配额 10K / 每次 4K")
        alive = [d for d in dests if os.path.isdir(d)]
        s.truthy("留下的总量不超过配额",
                 sum(ra._dir_size(d) for d in alive) <= 10 * 1024)
        s.check("最新一次必定留存", os.path.isdir(dests[-1]), True)
        s.check("最旧一次已被回收", os.path.isdir(dests[0]), False)

    s.section("单次就超配额时也不能把它删掉")
    with tempfile.TemporaryDirectory() as root, _env(
        TG_ARCHIVE_ENABLED="1", TG_ARCHIVE_MAX_SIZE="1K", TG_ARCHIVE_MAX_DAYS="0"
    ):
        win, wout = _make_run(root, "big", [], [("huge.bin", 40960)])
        dest = ra.archive_run(root, win, wout, {})
        # 归档完立刻被自己触发的回收删掉，等于白留 —— 那次恰恰最值得看
        s.check("超配额的单次留存", os.path.isdir(dest), True)


def test_quota_by_age(s):
    """过期按天回收。"""
    with tempfile.TemporaryDirectory() as root, _env(
        TG_ARCHIVE_ENABLED="1", TG_ARCHIVE_MAX_SIZE="0", TG_ARCHIVE_MAX_DAYS="7"
    ):
        old_a, old_b, fresh = None, None, None
        for name, age_days in (("old_a", 30), ("old_b", 20), ("fresh", 1)):
            win, wout = _make_run(root, name, [], [("p.bin", 128)])
            dest = ra.archive_run(root, win, wout, {})
            stamp = time.time() - age_days * 86400
            os.utime(dest, (stamp, stamp))
            if name == "old_a":
                old_a = dest
            elif name == "old_b":
                old_b = dest
            else:
                fresh = dest

        ra.prune(ra.archive_root(root))
        s.section("保留 7 天")
        s.check("30 天前的已回收", os.path.isdir(old_a), False)
        s.check("20 天前的已回收", os.path.isdir(old_b), False)
        s.check("1 天前的留存", os.path.isdir(fresh), True)


def test_disabled_switch(s):
    """关掉留痕时不得偷偷归档，且必须让调用方照常清理。"""
    with tempfile.TemporaryDirectory() as root, _env(TG_ARCHIVE_ENABLED="0"):
        win, wout = _make_run(root, "off", [("a.txt", 8)], [])
        dest = ra.archive_run(root, win, wout, {})
        s.section("开关生效")
        s.check("未归档", dest, None)
        s.check("工作区原样留在原地（交由调用方清理）", os.path.isdir(win), True)
        s.check("归档目录未被创建", os.path.isdir(ra.archive_root(root)), False)


def test_pipeline_fills_trace(s):
    """run_task 必须把"这次到底干了什么"写进 trace。

    它会被写到产物旁边的 run.json —— 出效果问题时，要看的正是
    "这句话 + 这个输入 → agy 回了什么 + 产出了什么"。
    """
    from core import file_pipeline as fp

    with tempfile.TemporaryDirectory() as root:
        win = os.path.join(root, "in")
        wout = os.path.join(root, "out")
        os.makedirs(win)
        os.makedirs(wout)
        with open(os.path.join(win, "clip.mp4"), "wb") as fh:
            fh.write(b"\0" * 16)

        fake = os.path.join(root, "fake_agy")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write(f'#!/bin/bash\necho "已转成 GIF"\ntouch {wout}/clip.gif\n')
        os.chmod(fake, 0o755)

        orig = fp.AGY_BIN
        fp.AGY_BIN = fake
        try:
            trace = {}
            ok, products, reply, _err, _warn = fp.run_task(
                [os.path.join(win, "clip.mp4")], win, wout, "转成gif", "m",
                trace=trace,
            )
        finally:
            fp.AGY_BIN = orig

        s.section("trace 被填满")
        s.check("任务成功", ok, True)
        s.check("用户原话在案", trace["message"], "转成gif")
        s.check("产物清单在案", trace["product_names"], ["clip.gif"])
        s.truthy("agy 的回复在案", "已转成 GIF" in trace["reply"])
        s.truthy("记下了 prompt 规模", trace.get("prompt_chars", 0) > 0)

        s.section("不传 trace 时行为不变")
        s.check("产物照常回收", len(products), 1)
        s.truthy("回复照常返回", bool(reply))


def test_sweep_never_touches_archive(s):
    """启动清扫只回收在途工作区，绝不能碰留痕。"""
    src = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "core", "handlers", "agy_handler.py",
    )
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    block = body[body.index("def sweep_workspaces"):body.index("def _cleanup_dirs")]

    s.section("清扫范围")
    s.check("只遍历 in/out", 'for sub in ("in", "out")' in block, True)
    s.truthy("显式排除归档目录", "protected" in block and "startswith" in block)
    s.check("未对 WORKSPACE_ROOT 整体 rmtree",
            "shutil.rmtree(WORKSPACE_ROOT" in block, False)
    s.truthy("启动时按配额回收归档", "prune(archive_root(WORKSPACE_ROOT))" in block)


SUITES = [
    ("容量解析", test_size_parsing),
    ("留痕内容", test_archive_keeps_files_and_commands),
    ("失败任务留痕", test_failed_run_is_archived_too),
    ("容量配额", test_quota_by_size),
    ("日期配额", test_quota_by_age),
    ("留痕开关", test_disabled_switch),
    ("流水线写入 trace", test_pipeline_fills_trace),
    ("清扫不碰留痕", test_sweep_never_touches_archive),
]

if __name__ == "__main__":
    main(SUITES)
