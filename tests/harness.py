"""
极简断言与统计工具 (tests/harness.py)

刻意不引入 pytest：项目本身零测试依赖，为几十个断言拉一个框架不划算，
而且 --test-sandbox 需要在快照还原后的裸环境里也能跑起来。
"""

import logging
import sys
import traceback


def silence_app_logging():
    """静音被测模块的 INFO 日志，避免淹没 --test-sandbox 的判定输出。"""
    for name in ("AGYHandler", "FilePipeline", "TTSEngine", "STTEngine", "TGFormat", "TaskEngine", "UserState"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


class Suite:
    def __init__(self, name, verbose=True):
        self.name = name
        self.verbose = verbose
        self.passed = 0
        self.failures = []
        self._section = ""

    def section(self, title):
        self._section = title
        if self.verbose:
            print(f"\n--- {title} ---")

    def check(self, label, got, want):
        if got == want:
            self.passed += 1
            if self.verbose:
                print(f"  ✅ {label}: {got!r}")
        else:
            self.failures.append(f"[{self._section}] {label}: 实际 {got!r}，期望 {want!r}")
            print(f"  ❌ {label}: 实际 {got!r}，期望 {want!r}")

    def truthy(self, label, got):
        self.check(label, bool(got), True)

    def report(self):
        total = self.passed + len(self.failures)
        if self.failures:
            print(f"\n❌ {self.name}: {len(self.failures)}/{total} 项失败")
            for f in self.failures:
                print(f"   - {f}")
        elif self.verbose:
            print(f"\n✅ {self.name}: {total}/{total} 项通过")
        return not self.failures


def run_suites(suites, verbose=True):
    """依次运行 [(名称, 函数), ...]，返回 (全部通过?, 通过数, 失败数)"""
    if not verbose:
        silence_app_logging()
    total_passed = 0
    total_failed = 0
    for name, fn in suites:
        suite = Suite(name, verbose=verbose)
        try:
            fn(suite)
        except Exception:
            suite.failures.append(f"套件抛出未捕获异常:\n{traceback.format_exc()}")
            print(f"  ❌ {name} 抛出异常:\n{traceback.format_exc()}")
        suite.report()
        total_passed += suite.passed
        total_failed += len(suite.failures)
    return total_failed == 0, total_passed, total_failed


def main(suites):
    verbose = "--quiet" not in sys.argv
    ok, passed, failed = run_suites(suites, verbose=verbose)
    print(f"\n{'✅' if ok else '❌'} 合计: {passed} 通过, {failed} 失败")
    sys.exit(0 if ok else 1)
