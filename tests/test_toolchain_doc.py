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

from tests.harness import main

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLCHAIN_MD = os.path.join(PROJECT_DIR, "config", "TOOLCHAIN.md")
INSTALLER = os.path.join(PROJECT_DIR, "install.sh")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _declared_entrypoints(body):
    """解析形如：
        - `core/tts.py` → `generate_telegram_voice(text, voice=...)` 说明
    返回 [(模块相对路径, 函数名), ...]
    """
    return re.findall(
        r"`([A-Za-z0-9_/]+\.py)`\s*→\s*`([A-Za-z_][A-Za-z0-9_]*)\s*\(", body
    )


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
    # 「命令行工具」表格里每行的 | `cmd` | 用途 |，含 `convert` / `magick` 这种并列写法
    declared = []
    for row in re.findall(r"^\|\s*(`[^|]+`)\s*\|", body, re.MULTILINE):
        declared += re.findall(r"`([a-z0-9]+)`", row)
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


def test_docs_reference_real_files(s):
    """文档里以反引号引用的项目文件必须真实存在。"""
    s.section("文档引用的路径")
    pattern = re.compile(
        r"`((?:core|tests|config|jobs|bin)/[A-Za-z0-9_./-]+\.(?:py|md|sh|yaml|json))`"
    )
    for doc in ("README.md", "GEMINI.md"):
        doc_path = os.path.join(PROJECT_DIR, doc)
        if not os.path.exists(doc_path):
            continue
        for rel in sorted(set(pattern.findall(_read(doc_path)))):
            s.check(f"{doc} → {rel}", os.path.exists(os.path.join(PROJECT_DIR, rel)), True)


def test_docs_avoid_rotting_counts(s):
    """文档不得写死断言条数 —— 它每加一个测试就过期一次，且无人会去改。

    README 里那句"257 项断言"就是这么一路停在 257 的，直到实际有 600 多项。
    """
    s.section("无写死的断言条数")
    for doc in ("README.md", "GEMINI.md"):
        body = _read(os.path.join(PROJECT_DIR, doc))
        s.check(f"{doc} 未写死断言条数",
                re.findall(r"\d+\s*项断言", body), [])


def test_snapshot_manifest_consistency(s):
    """打包清单与还原时清空的受控目录必须一致，且被文档如实描述。

    两者不一致时，还原后会出现"旧代码 + 新测试"这类自相矛盾的组合。
    """
    manage = _read(os.path.join(PROJECT_DIR, "bin", "manage.sh"))
    m = re.search(r"for item in ([^;]+); do", manage)
    assert m is not None
    packed = set(m.group(1).split())
    packed_dirs = {x for x in packed if "." not in x}
    wiped = set(re.findall(r'"\$PROJECT_DIR/(\w+)"', manage))

    s.section("打包 / 清空 / 文档 三方一致")
    s.check("受控清空目录 == 打包目录", packed_dirs == wiped, True)
    for doc in ("README.md", "GEMINI.md"):
        body = _read(os.path.join(PROJECT_DIR, doc))
        for d in sorted(packed_dirs):
            s.check(f"{doc} 描述了快照含 {d}/", f"`{d}/`" in body, True)


def test_sandbox_level_count_matches_docs(s):
    """预检流水线的级数在代码与文档中必须一致。"""
    preflight_src = _read(os.path.join(PROJECT_DIR, "core", "preflight.py"))
    totals = {m[1] for m in re.findall(r"\[(\d)/(\d)\]", preflight_src)}

    s.section("级数一致")
    s.check("preflight.py 中级数唯一", len(totals), 1)
    total = totals.pop() if totals else "?"
    for doc in ("README.md", "GEMINI.md"):
        body = _read(os.path.join(PROJECT_DIR, doc))
        s.check(f"{doc} 无过期的'三级'表述", "三级" in body, False)
        s.check(f"{doc} 提及 {total} 级校验", f"{total} 级" in body or f"[2/{total}]" in body, True)


def test_docread_actually_reads(s):
    """清单声称能把文档读成 Markdown —— 这条要真的跑一遍。

    只做 AST 检查是不够的：core/docread.py 是懒导入 anydoc 的，依赖没装上
    时函数照样存在、照样解析得过，而 agy 拿到的会是一句"未安装"。
    """
    import subprocess
    import sys as _sys
    import tempfile

    sys.path.insert(0, PROJECT_DIR)
    from core.docread import DocReadError, to_markdown

    work = tempfile.mkdtemp()
    try:
        s.section("底层依赖真的装上了")
        try:
            import anydoc  # noqa: F401
            s.check("firecrawl-anydoc 可导入", True, True)
        except ImportError as e:
            s.check(f"firecrawl-anydoc 可导入 ({e})", False, True)
            return

        s.section("docx 能读出结构")
        src = os.path.join(work, "src.md")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("# 季度报告\n\n## 概述\n\n| 地区 | Q1 |\n|---|---|\n| 华北 | 120 |\n")
        docx = os.path.join(work, "a.docx")
        made = subprocess.run(["pandoc", src, "-o", docx],
                              capture_output=True, timeout=60).returncode == 0
        if not made:
            s.check("造得出测试 docx", False, True)
        else:
            md = to_markdown(docx)
            s.truthy("标题层级还在", "# 季度报告" in md and "## 概述" in md)
            s.truthy("表格还是表格", "| 地区 |" in md and "| 华北 |" in md)

        s.section("读不了时给的是人话和替代路线，不是异常类名")
        bad_file = os.path.join(work, "x.bin")
        with open(bad_file, "wb") as fh:
            fh.write(b"\x00\x01 not a document")
        try:
            to_markdown(bad_file)
            s.check("应当抛 DocReadError", False, True)
        except DocReadError as e:
            s.truthy("带上了文件名", "x.bin" in str(e))
            s.truthy("给了替代路线", "OCR" in str(e) or "cat" in str(e))

        s.check("文件不存在也走同一条错误路径",
                _raises_docread(to_markdown, os.path.join(work, "没有.docx")), True)

        s.section("命令行入口可用")
        res = subprocess.run(
            [_sys.executable, os.path.join(PROJECT_DIR, "core", "docread.py"), docx],
            capture_output=True, text=True, timeout=60)
        s.check("退出码 0", res.returncode, 0)
        s.truthy("stdout 是 Markdown", "# 季度报告" in res.stdout)
        res = subprocess.run(
            [_sys.executable, os.path.join(PROJECT_DIR, "core", "docread.py"), bad_file],
            capture_output=True, text=True, timeout=60)
        s.check("读不了时退出码非 0", res.returncode, 1)
        s.truthy("原因写在 stderr", len(res.stderr.strip()) > 0)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _raises_docread(fn, *args):
    from core.docread import DocReadError
    try:
        fn(*args)
    except DocReadError:
        return True
    except Exception:
        return False
    return False


SUITES = [
    ("文档引用完整性", test_docs_reference_real_files),
    ("文档无过期计数", test_docs_avoid_rotting_counts),
    ("快照清单一致性", test_snapshot_manifest_consistency),
    ("沙箱级数一致性", test_sandbox_level_count_matches_docs),
    ("入口函数一致性", test_python_entrypoints_exist),
    ("文档读取真的能用", test_docread_actually_reads),
    ("二进制覆盖", test_declared_binaries_are_installable),
]

if __name__ == "__main__":
    main(SUITES)
