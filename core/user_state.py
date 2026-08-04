"""
用户会话状态持久化 (core/user_state.py)

从 bot.py 抽出，原因有二：
  1. `open(path, "w")` 会先把文件截断为 0 字节再写。崩在中途就留下半截 JSON，
     下次启动解析失败被 except 吞掉、静默重置为 {} —— conv_id / 对话模式 /
     模型选择全部丢失。改为写临时文件 + os.replace 原子替换。
  2. telebot 默认多线程派发更新，读写共享 dict 必须加锁。

顺带：解析失败时把损坏文件另存为 .corrupt 而不是直接丢弃，留一条人工恢复的路。
"""

import json
import logging
import os
import tempfile
import threading
import time

logger = logging.getLogger("UserState")

DEFAULT_STATE = {
    "in_chat": False,
    "conv_id": None,
    "model": "gemini-3.6-flash-high",
    "effort": "high",
}


class UserStateStore:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self._lock = threading.RLock()
        self._states = {}

    # -- 读写 ------------------------------------------------------------
    def load(self):
        with self._lock:
            if not os.path.exists(self.path):
                self._states = {}
                return self._states
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise TypeError("状态文件根元素必须是对象")
                self._states = data
            except Exception as e:
                # 不要静默丢弃：留档以便人工恢复
                backup = f"{self.path}.corrupt.{int(time.time())}"
                try:
                    os.replace(self.path, backup)
                    logger.error(f"状态文件损坏，已另存为 {backup} 并以空状态启动: {e}")
                except Exception:
                    logger.error(f"状态文件损坏且无法留档: {e}")
                self._states = {}
            return self._states

    def save(self):
        """原子落盘：先写同目录临时文件，fsync 后 os.replace 覆盖。

        os.replace 在同一文件系统内是原子的 —— 任何时刻读到的要么是旧的
        完整内容，要么是新的完整内容，不存在半截状态。
        """
        with self._lock:
            try:
                directory = os.path.dirname(self.path)
                os.makedirs(directory, exist_ok=True)
                fd, tmp = tempfile.mkstemp(
                    dir=directory, prefix=".user_states.", suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(self._states, f, ensure_ascii=False, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, self.path)
                except Exception:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            except Exception as e:
                logger.error(f"保存用户状态失败: {e}")

    # -- 访问 ------------------------------------------------------------
    def get(self, user_id):
        """取得（必要时初始化并补齐）某个用户的状态。"""
        uid = str(user_id)
        with self._lock:
            state = self._states.get(uid)
            if state is None:
                self._states[uid] = dict(DEFAULT_STATE)
                self.save()
                return self._states[uid]

            # 补齐历史版本缺失的字段
            missing = {k: v for k, v in DEFAULT_STATE.items() if k not in state}
            if missing:
                state.update(missing)
                self.save()
            return state

    @property
    def all(self):
        with self._lock:
            return self._states
