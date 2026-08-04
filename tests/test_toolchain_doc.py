#!/usr/bin/env python3
"""
工具链清单一致性测试 (tests/test_toolchain_doc.py)

config/TOOLCHAIN.md 不是给人看的文档 —— `load_toolchain()` 会把它**直接内联
进 Planner 的 prompt**。写错一个函数名或声明一个没安装的工具，模型就会照着
错误的"能力地图"去规划命令。这份文档必须与代码、与 install.sh 保持一致。
"""

import ast
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import file_pipeline as fp
from tests.harness import main

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLCHAIN_MD = os.path.join(PROJECT_DIR, "config", "TOOLCHAIN.md")
INSTALLER = os.path.join(PROJECT_DIR, "install.sh")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _declared_entrypoints(body):
    """解析形如：
        - **路径**: `core/stt.py`
        - **入口函数**: `transcribe_voice(ogg_path)`
    返回 [(模块相对路径, 函数名), ...]
    """
    pairs = []
    current_path = None
    for line in body.splitlines():
        m = re.search(r"\*\*路径\*\*.*?`([^`]+\.py)`", line)
        if m:
            current_path = m.group(1)
            continue
        m = re.search(r"\*\*入口函数\*\*.*?`([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
        if m and current_path:
            pairs.append((current_path, m.group(1)))
            current_path = None
    return pairs


def _module_functions(path):
    tree = ast.parse(_read(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_python_entrypoints_exist(s):
    body = _read(TOOLCHAIN_MD)
    pairs = _declared_entrypoints(body)

    s.section("声明的 Python 入口函数必须真实存在")
    s.truthy("确实解析到了入口函数声明", len(pairs) > 0)
    for rel_path, func in pairs:
        abs_path = os.path.join(PROJECT_DIR, rel_path)
        if not os.path.exists(abs_path):
            s.check(f"{rel_path} 存在", False, True)
            continue
        funcs = _module_functions(abs_path)
        s.check(f"{rel_path} 中存在 {func}()", func in funcs, True)


def test_declared_binaries_are_installable(s):
    body = _read(TOOLCHAIN_MD)
    declared = sorted({
        os.path.basename(p)
        for p in re.findall(r"`(/usr/bin/[A-Za-z0-9_.-]+)`", body)
    })
    # 「核心命令」小节里以 `- \`cmd\` — 说明` 形式列出的工具
    declared += re.findall(r"^\s*-\s+`([a-z0-9]+)`\s+—", body, re.MULTILINE)
    declared = sorted(set(declared))

    installer = _read(INSTALLER)
    covered = set(re.findall(r"^\s*\[([A-Za-z0-9_]+)\]=", installer, re.MULTILINE))

    s.section("文档声明的二进制必须被 install.sh 覆盖")
    s.truthy("确实解析到了二进制声明", len(declared) > 0)
    for name in declared:
        s.check(f"install.sh 会安装/校验 {name}", name in covered, True)

    s.section("install.sh 声明的二进制在本机确实可用")
    for name in sorted(covered):
        s.check(f"{name} 可执行", shutil.which(name) is not None, True)


def test_recipes_reference_real_tools(s):
    """菜谱里出现的命令必须是工具链里真实存在的。"""
    installer = _read(INSTALLER)
    known = set(re.findall(r"^\s*\[([A-Za-z0-9_]+)\]=", installer, re.MULTILINE))
    # 菜谱中允许出现的通用 shell 内建/常用命令
    allowed_extra = {"rm", "cp", "mv", "mkdir", "cd", "echo", "unzip", "curl", "wget"}

    s.section("菜谱中的命令均可解析")
    for fname in sorted(os.listdir(fp.RECIPE_DIR)):
        if not fname.endswith(".md") or fname == "README.md":
            continue
        body = _read(os.path.join(fp.RECIPE_DIR, fname))
        # 取出 ```bash 代码块里每行的首个词
        for block in re.findall(r"```bash\n(.*?)```", body, re.DOTALL):
            for line in block.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                cmd = line.split()[0]
                if cmd in allowed_extra or cmd in known:
                    continue
                s.check(f"{fname} 中的 `{cmd}` 属于已声明工具", False, True)
        s.check(f"{fname} 命令全部可解析", True, True)


def test_toolchain_trim_keeps_file_tools(s):
    """load_toolchain() 的裁剪不得误伤文件处理相关段落。"""
    trimmed = fp.load_toolchain()
    s.section("裁剪后仍保留文件处理能力")
    for keyword in ("FFmpeg", "ImageMagick", "pngquant", "Pandoc", "poppler"):
        s.truthy(f"保留 {keyword}", keyword in trimmed)
    s.section("裁剪掉与文件处理无关的服务清单")
    for noise in ("PostgreSQL", "qBittorrent", "MinIO", "Redis"):
        s.check(f"已剔除 {noise}", noise in trimmed, False)


def test_docs_reference_real_files(s):
    """文档里以反引号引用的项目文件必须真实存在。"""
    s.section("文档引用的路径")
    pattern = re.compile(
        r"`((?:core|tests|config|jobs|bin)/[A-Za-z0-9_./-]+\.(?:py|md|sh|yaml|json))`"
    )
    for doc in ("README.md", "GEMINI.md", "ARCHITECTURE.md"):
        doc_path = os.path.join(PROJECT_DIR, doc)
        if not os.path.exists(doc_path):
            continue
        for rel in sorted(set(pattern.findall(_read(doc_path)))):
            s.check(f"{doc} → {rel}", os.path.exists(os.path.join(PROJECT_DIR, rel)), True)


def test_snapshot_manifest_consistency(s):
    """打包清单与还原时清空的受控目录必须一致，且被文档如实描述。

    两者不一致时，还原后会出现"旧代码 + 新测试"这类自相矛盾的组合。
    """
    manage = _read(os.path.join(PROJECT_DIR, "bin", "manage.sh"))
    packed = set(re.search(r"for item in ([^;]+); do", manage).group(1).split())
    packed_dirs = {x for x in packed if "." not in x}
    wiped = set(re.findall(r'"\$PROJECT_DIR/(\w+)"', manage))

    s.section("打包 / 清空 / 文档 三方一致")
    s.check("受控清空目录 == 打包目录", packed_dirs == wiped, True)
    for doc in ("README.md", "GEMINI.md"):
        body = _read(os.path.join(PROJECT_DIR, doc))
        for d in sorted(packed_dirs):
            s.check(f"{doc} 描述了快照含 {d}/", f"`{d}/`" in body, True)


def test_sandbox_level_count_matches_docs(s):
    """沙箱校验的级数在代码与文档中必须一致。"""
    bot_src = _read(os.path.join(PROJECT_DIR, "core", "bot.py"))
    totals = {m[1] for m in re.findall(r"\[(\d)/(\d)\]", bot_src)}

    s.section("级数一致")
    s.check("bot.py 中级数唯一", len(totals), 1)
    total = totals.pop() if totals else "?"
    for doc in ("README.md", "GEMINI.md"):
        body = _read(os.path.join(PROJECT_DIR, doc))
        s.check(f"{doc} 无过期的'三级'表述", "三级" in body, False)
        s.check(f"{doc} 提及 {total} 级校验", f"{total} 级" in body or f"[2/{total}]" in body, True)


SUITES = [
    ("文档引用完整性", test_docs_reference_real_files),
    ("快照清单一致性", test_snapshot_manifest_consistency),
    ("沙箱级数一致性", test_sandbox_level_count_matches_docs),
    ("入口函数一致性", test_python_entrypoints_exist),
    ("二进制覆盖", test_declared_binaries_are_installable),
    ("菜谱命令可解析", test_recipes_reference_real_tools),
    ("工具链裁剪", test_toolchain_trim_keeps_file_tools),
]

if __name__ == "__main__":
    main(SUITES)
