#!/usr/bin/env python3
"""
测试总入口 (tests/run_all.py)

用法:
  python3 tests/run_all.py            # 完整输出
  python3 tests/run_all.py --quiet    # 只输出失败项与汇总（--test-sandbox 使用）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import (
    test_file_pipeline,
    test_message_routing,
    test_rescue,
    test_run_archive,
    test_tg_format,
    test_toolchain_doc,
    test_user_state,
)
from tests.harness import run_suites

ALL_SUITES = (
    test_rescue.SUITES
    + test_user_state.SUITES
    + test_tg_format.SUITES
    + test_toolchain_doc.SUITES
    + test_file_pipeline.SUITES
    + test_message_routing.SUITES
    + test_run_archive.SUITES
)


def run(verbose=True):
    """返回 (全部通过?, 通过数, 失败数)。供 bot.py --test-sandbox 直接调用。"""
    return run_suites(ALL_SUITES, verbose=verbose)


if __name__ == "__main__":
    ok, passed, failed = run(verbose="--quiet" not in sys.argv)
    print(f"\n{'✅' if ok else '❌'} 合计: {passed} 通过, {failed} 失败")
    sys.exit(0 if ok else 1)
