"""
Layer 3.6: 文件任务留痕 (core/run_archive.py)

文件处理的效果只能靠**看产物**判断。日志里那行 ffmpeg 命令永远看着是对的 ——
糊没糊、尺寸掉没掉，必须把原文件和产物摆在一起才知道。旧实现在任务结束时
rmtree 掉整个工作区，于是每次调参都只能靠猜，改完也无从验证。

这里把收尾从"删除"改成"归档"：workspace/in|out 原样搬进
workspace/archive/<时间戳>_<key>/，外加一份 run.json 记下用户原话、命中的菜谱、
每一轮实际执行的命令与失败原因 —— 命令和它产出的文件从此躺在同一个目录里。

失败的任务同样归档，而且比成功的更值得留：要排查的正是它们。

留痕的代价是磁盘，所以配额是硬性的：超了就按**最旧优先**回收，
容量与保留天数都从 .env 读。用户上传的原始文件会在这里留存至配额期限，
这是留痕功能的固有代价，不是疏漏。
"""

import json
import logging
import os
import shutil
import time

logger = logging.getLogger("RunArchive")

ARCHIVE_DIRNAME = "archive"

# 默认 10G / 14 天。两个维度**同时**生效，先撞上哪个就按哪个回收；
# 任一项设为 0 即关闭该维度的限制。
DEFAULT_MAX_SIZE = "10G"
DEFAULT_MAX_DAYS = 14

_SIZE_UNITS = {"": 1, "B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def _try_size(raw):
    """把 10G / 500M / 1.5G / 1073741824 解析成字节；无法解析返回 None。"""
    text = str(raw or "").strip().upper()
    if text.endswith("IB"):  # 10GiB
        text = text[:-2]
    elif text.endswith("B") and len(text) > 1 and not text[-2].isdigit():
        text = text[:-1]  # 10GB → 10G，但 "1024B" 的 B 留给下面按单位吃掉
    if not text:
        return None
    unit = _SIZE_UNITS.get(text[-1], 1) if not text[-1].isdigit() else 1
    if not text[-1].isdigit():
        if text[-1] not in _SIZE_UNITS:
            return None
        text = text[:-1].strip()
    try:
        return max(0, int(float(text) * unit))
    except ValueError:
        return None


def _parse_size(raw, fallback):
    """写错不该让留痕整个失效，更不该按 0 处理（那等于归档完立刻删光），
    因此一律退回默认值并留一条 warning。"""
    parsed = _try_size(raw)
    if parsed is not None:
        return parsed
    logger.warning(f"TG_ARCHIVE_MAX_SIZE 无法解析: {raw!r}，改用 {fallback}")
    return _try_size(fallback) or 0


def _parse_days(raw, fallback):
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        logger.warning(f"TG_ARCHIVE_MAX_DAYS 无法解析: {raw!r}，改用 {fallback}")
        return fallback


def archive_config():
    """每次调用都重读 .env —— 改配额不必重启进程。"""
    return {
        "enabled": os.getenv("TG_ARCHIVE_ENABLED", "1").strip().lower()
        not in ("0", "false", "no", "off"),
        "max_bytes": _parse_size(
            os.getenv("TG_ARCHIVE_MAX_SIZE", DEFAULT_MAX_SIZE), DEFAULT_MAX_SIZE
        ),
        "max_days": _parse_days(
            os.getenv("TG_ARCHIVE_MAX_DAYS", DEFAULT_MAX_DAYS), DEFAULT_MAX_DAYS
        ),
    }


def archive_root(workspace_root):
    return os.path.join(workspace_root, ARCHIVE_DIRNAME)


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _describe(path):
    """列出目录下的文件名与体积。体积是唯一值得单独记的元数据 ——
    分辨率、码率这些直接 ffprobe 归档里的文件就能拿到，抄一份只会过期。"""
    if not os.path.isdir(path):
        return []
    out = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if os.path.isfile(full):
            try:
                out.append({"name": name, "bytes": os.path.getsize(full)})
            except OSError:
                out.append({"name": name, "bytes": None})
    return out


def list_runs(root):
    """归档目录下的每一次任务，按时间从旧到新。"""
    if not os.path.isdir(root):
        return []
    runs = []
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        try:
            runs.append((os.path.getmtime(path), name, path))
        except OSError:
            continue
    # 同秒归档的 mtime 会并列，只按 mtime 排序则先后不定；目录名以时间戳打头，
    # 拿它当第二关键字，顺序才是确定的
    runs.sort()
    return [p for _mtime, _name, p in runs]


def prune(root, max_bytes=None, max_days=None, protect=None):
    """按配额回收，最旧优先。返回被删掉的目录数。

    protect 是本次刚归档的目录，永不回收 —— 单次任务体积就超过配额时，
    那恰恰是最该看的一次，归档完立刻被自己触发的回收删掉等于白留。
    这里用**路径**而不是"保留最新 N 个"：后者依赖 mtime 排序，同秒归档
    就会并列，刚写完的那次可能排不到最后，于是被当成最旧删掉。
    """
    cfg = archive_config()
    max_bytes = cfg["max_bytes"] if max_bytes is None else max_bytes
    max_days = cfg["max_days"] if max_days is None else max_days

    keep = os.path.abspath(protect) if protect else None
    runs = list_runs(root)
    removed = 0

    def drop(path, why):
        nonlocal removed
        try:
            shutil.rmtree(path)
            removed += 1
            logger.info(f"🧹 归档回收（{why}）: {os.path.basename(path)}")
        except Exception as e:
            logger.warning(f"回收归档 {path} 失败: {e}")

    def protected(path):
        return keep is not None and os.path.abspath(path) == keep

    if max_days > 0:
        cutoff = time.time() - max_days * 86400
        survivors = []
        for path in runs:
            try:
                stale = os.path.getmtime(path) < cutoff
            except OSError:
                stale = False
            if stale and not protected(path):
                drop(path, f"超过 {max_days} 天")
            else:
                survivors.append(path)
        runs = survivors

    if max_bytes > 0:
        sizes = {p: _dir_size(p) for p in runs}
        total = sum(sizes.values())
        for path in runs:
            if total <= max_bytes:
                break
            if protected(path):
                continue
            total -= sizes[path]
            drop(path, f"总量超过 {_human(max_bytes)}")

    return removed


def _human(size):
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0


def archive_run(workspace_root, workspace_in, workspace_out, meta):
    """把本次任务的工作区搬进归档并写下 run.json。返回归档目录，失败返回 None。

    这里用 move 而不是 copy：同一文件系统内是改名，不额外占盘，也不会在
    大文件上多花几秒。调用方**无论如何都应再调一次工作区清理** ——
    搬走后原目录自然不存在，那次清理只在本函数没跑成时兜底，从而不留泄漏路径。
    """
    cfg = archive_config()
    if not cfg["enabled"]:
        return None

    root = archive_root(workspace_root)
    key = os.path.basename(workspace_in.rstrip("/")) or "run"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(root, f"{stamp}_{key}")
    # 同秒 + 同 key 会撞名，撞了就往里搬会把两次任务混在一个目录里
    suffix = 1
    while os.path.exists(dest):
        dest = os.path.join(root, f"{stamp}_{key}_{suffix}")
        suffix += 1

    try:
        os.makedirs(dest, exist_ok=True)
        for src, sub in ((workspace_in, "in"), (workspace_out, "out")):
            if src and os.path.isdir(src):
                shutil.move(src, os.path.join(dest, sub))
    except Exception as e:
        logger.warning(f"归档任务工作区失败: {e}")
        return None

    record = dict(meta or {})
    record["key"] = key
    record["archived_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    record["inputs"] = _describe(os.path.join(dest, "in"))
    record["products"] = _describe(os.path.join(dest, "out"))
    try:
        with open(os.path.join(dest, "run.json"), "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=2)
    except Exception as e:
        # 文件已经搬进来了，run.json 写不出也不该把归档整个作废
        logger.warning(f"写入 run.json 失败: {e}")

    logger.info(f"📁 已留痕: {dest}")
    try:
        prune(root, cfg["max_bytes"], cfg["max_days"], protect=dest)
    except Exception as e:
        logger.warning(f"归档回收失败: {e}")
    return dest
