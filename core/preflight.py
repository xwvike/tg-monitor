"""
部署预检流水线与四级校验 (core/preflight.py)

从 bot.py 的 __main__ 中抽出。独立模块化的好处：
  1. 可被 manage.sh / bot.py / 未来的 CI 独立调用
  2. 校验逻辑本身可以被测试
  3. bot.py 保持纯粹的编排器角色
"""

import glob
import os
import py_compile
import subprocess
import sys


def run(bot=None, agy_bin=None):
    """执行预检四级校验，返回 True 表示全部通过。

    Parameters
    ----------
    bot : telebot.TeleBot, optional
        传入已初始化的 bot 实例用于 [3/4] Telegram API 探针。
        不传则跳过该级。
    agy_bin : str, optional
        agy 可执行文件路径，用于 [4/4] AGY 引擎探针。
        不传则跳过该级。
    """
    core_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(core_dir)

    # [1/4] 静态分析预检：除了 py_compile 扫描语法外，调用 ruff 进行深度静态分析
    py_files = (
        glob.glob(os.path.join(core_dir, "*.py"))
        + glob.glob(os.path.join(core_dir, "handlers", "*.py"))
        + glob.glob(os.path.join(project_dir, "jobs", "*.py"))
    )

    scan_failed = []
    for pf in py_files:
        try:
            py_compile.compile(pf, doraise=True)
        except py_compile.PyCompileError as compile_err:
            scan_failed.append((os.path.relpath(pf, project_dir), str(compile_err)))

    if scan_failed:
        print(f"[1/4] ❌ 语法预检失败 ({len(scan_failed)} 个文件):")
        for fname, err_detail in scan_failed:
            print(f"       {fname}: {err_detail}")
        return False
        
    try:
        venv_ruff = os.path.join(project_dir, "venv", "bin", "ruff")
        if os.path.exists(venv_ruff):
            res = subprocess.run([venv_ruff, "check", core_dir, os.path.join(project_dir, "jobs")], capture_output=True, text=True)
            if res.returncode != 0:
                print("[1/4] ❌ Ruff 静态分析拦截 (检测到未定义变量/错误导入等):")
                print(res.stdout.strip())
                return False
    except Exception as e:
        print(f"[1/4] ⚠️ Ruff 检查未执行: {e}")

    print(
        f"[1/4] 依赖包语法预检与静态分析: OK ({len(py_files)} 个 Python 文件已扫描)"
    )

    # [2/4] 业务逻辑单元测试
    try:
        if project_dir not in sys.path:
            sys.path.insert(0, project_dir)
        from tests import run_all

        tests_ok, n_passed, n_failed = run_all.run(verbose=False)
        if not tests_ok:
            print(f"[2/4] ❌ 单元测试失败: {n_failed} 项未通过（共 {n_passed + n_failed} 项）")
            return False
        print(f"[2/4] 业务逻辑单元测试: OK ({n_passed} 项断言全部通过)")
    except Exception as e:
        print(f"[2/4] ❌ 单元测试无法执行: {e}")
        return False

    # [3/4] Telegram API 联调探针
    if bot is not None:
        try:
            me = bot.get_me()
            print(f"[3/4] Telegram API 联调校验成功: @{me.username} ({me.first_name})")
        except Exception as e:
            print(f"[3/4] ❌ Telegram API 联调失败: {e}")
            return False
    else:
        print("[3/4] Telegram API 联调: 跳过（未传入 bot 实例）")

    # [4/4] AGY 引擎底层探针
    if agy_bin is not None:
        try:
            out = subprocess.check_output(
                [agy_bin, "--version"], stderr=subprocess.STDOUT
            ).decode("utf-8")
            print(f"[4/4] AGY 引擎底层探针连通成功: {out.strip()}")
        except Exception as e:
            print(f"[4/4] ❌ AGY 引擎探针异常: {e}")
            return False
    else:
        print("[4/4] AGY 引擎探针: 跳过（未传入 agy_bin 路径）")

    print("✅ 沙箱四级分层校验全部 PASS！准备发布升级！")
    return True
