#!/usr/bin/env python3
"""
用户状态持久化测试 (tests/test_user_state.py)

守护一类静默数据丢失：`open(path,"w")` 会先截断文件再写，崩在中途就留下
半截 JSON，下次启动解析失败被 except 吞掉、状态静默重置为出厂值。
"""

import json
import os
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.user_state import DEFAULT_STATE, UserStateStore
from tests.harness import main

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_basic_roundtrip(s):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "user_states.json")

        s.section("初始化与默认值")
        store = UserStateStore(path)
        store.load()
        st = store.get(42)
        s.check("补齐全部默认字段", sorted(st), sorted(DEFAULT_STATE))
        s.check("首次 get 即落盘", os.path.exists(path), True)

        s.section("往返")
        st["conv_id"] = "abc"
        st["in_chat"] = True
        store.save()
        reloaded = UserStateStore(path)
        reloaded.load()
        s.check("conv_id 持久化", reloaded.get(42)["conv_id"], "abc")
        s.check("in_chat 持久化", reloaded.get(42)["in_chat"], True)

        s.section("历史版本缺字段自动补齐")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"42": {"in_chat": True}}, f)
        legacy = UserStateStore(path)
        legacy.load()
        st = legacy.get(42)
        s.check("保留原有值", st["in_chat"], True)
        s.check("补上 model", st["model"], DEFAULT_STATE["model"])
        s.check("补上 effort", st["effort"], DEFAULT_STATE["effort"])


def test_atomic_write(s):
    """在写入过程中强杀进程，磁盘上必须仍是旧的完整内容。"""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "user_states.json")
        store = UserStateStore(path)
        store.load()
        store.get(1)["conv_id"] = "GOOD"
        store.save()

        s.section("写入中途被 SIGKILL")
        # 子进程：把 os.replace 替换成"先自杀"，模拟落盘前崩溃
        script = f'''
import os, signal, sys
sys.path.insert(0, {PROJECT_DIR!r})
from core.user_state import UserStateStore
store = UserStateStore({path!r})
store.load()
store.get(1)["conv_id"] = "HALF_WRITTEN"
real_replace = os.replace
def suicide(src, dst):
    os.kill(os.getpid(), signal.SIGKILL)
os.replace = suicide
store.save()
'''
        res = subprocess.run([sys.executable, "-c", script],
                             capture_output=True, timeout=30)
        s.check("子进程确实被杀死", res.returncode, -9)

        survivor = UserStateStore(path)
        survivor.load()
        s.check("旧内容完好无损", survivor.get(1)["conv_id"], "GOOD")
        with open(path, encoding="utf-8") as f:
            s.truthy("文件仍是合法 JSON", isinstance(json.load(f), dict))

        leftovers = [f for f in os.listdir(d) if f.startswith(".user_states.")]
        s.check("崩溃残留的临时文件不干扰主文件", os.path.exists(path), True)
        s.truthy("临时文件用点前缀隔离", all(f.startswith(".") for f in leftovers))


def test_corrupt_file_is_preserved(s):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "user_states.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"42": {"conv_id": "half')  # 半截 JSON

        s.section("损坏文件不得被静默丢弃")
        store = UserStateStore(path)
        store.load()
        s.check("以空状态启动", store.all, {})
        backups = [f for f in os.listdir(d) if ".corrupt." in f]
        s.check("损坏内容已留档", len(backups), 1)
        with open(os.path.join(d, backups[0]), encoding="utf-8") as f:
            s.truthy("留档内容即原始损坏数据", "half" in f.read())

        s.section("根元素类型错误同样按损坏处理")
        path2 = os.path.join(d, "b.json")
        with open(path2, "w", encoding="utf-8") as f:
            json.dump(["not", "a", "dict"], f)
        store2 = UserStateStore(path2)
        store2.load()
        s.check("以空状态启动", store2.all, {})


def test_thread_safety(s):
    """telebot 多线程派发更新，并发读写不得破坏文件或丢字段。"""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "user_states.json")
        store = UserStateStore(path)
        store.load()

        s.section("并发读写")
        errors = []

        def worker(uid):
            try:
                for i in range(30):
                    st = store.get(uid)
                    st["conv_id"] = f"u{uid}-{i}"
                    store.save()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(u,)) for u in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        s.check("无异常", errors, [])
        final = UserStateStore(path)
        final.load()
        s.check("8 个用户全部存在", len(final.all), 8)
        with open(path, encoding="utf-8") as f:
            s.truthy("文件未被并发写坏", isinstance(json.load(f), dict))


SUITES = [
    ("基础往返", test_basic_roundtrip),
    ("原子写", test_atomic_write),
    ("损坏留档", test_corrupt_file_is_preserved),
    ("线程安全", test_thread_safety),
]

if __name__ == "__main__":
    main(SUITES)
