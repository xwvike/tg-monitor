#!/usr/bin/env python3
"""
文档一致性测试 (tests/test_docs.py)

README.md / GEMINI.md 描述的文件、快照清单与校验级数必须与代码保持一致。
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.harness import main

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


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


SUITES = [
    ("文档引用完整性", test_docs_reference_real_files),
    ("文档无过期计数", test_docs_avoid_rotting_counts),
    ("快照清单一致性", test_snapshot_manifest_consistency),
    ("沙箱级数一致性", test_sandbox_level_count_matches_docs),
]

if __name__ == "__main__":
    main(SUITES)
