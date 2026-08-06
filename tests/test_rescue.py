#!/usr/bin/env python3
"""
自救与快照选取测试 (tests/test_rescue.py)

守护一个曾经致命的缺陷：rescue 先给"故障现场"打快照、再按"最新快照"还原，
于是把刚坏掉的状态原样装了回去，自救系统整体空转。
"""

import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.harness import main

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANAGE = os.path.join(PROJECT_DIR, "bin", "manage.sh")


def _call(snapshot_dir, func):
    """在隔离的快照目录下调用 manage.sh 中的某个函数。"""
    script = (
        f'source "{MANAGE}" >/dev/null 2>&1\n'
        f'SNAPSHOT_DIR="{snapshot_dir}"\n'
        f"{func}\n"
    )
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    return res.stdout.strip()


def test_snapshot_selection(s):
    with tempfile.TemporaryDirectory() as d:
        s.section("还原目标必须跳过故障现场取证包")
        # 时间线：稳态快照 → 代码改坏 → rescue 拍下故障现场
        for name, stamp in (
            ("snap_20260804_1000_good.tar.gz", "202608041000"),
            ("snap_20260804_1100_stable.tar.gz", "202608041100"),
            ("snap_20260804_1436_before_rescue.tar.gz", "202608041436"),
        ):
            path = os.path.join(d, name)
            open(path, "w").close()
            subprocess.run(["touch", "-t", stamp, path], check=True)

        picked = os.path.basename(_call(d, "latest_restorable_snapshot"))
        s.check("选中故障前的稳态快照", picked, "snap_20260804_1100_stable.tar.gz")
        s.check("未选中故障现场取证包", picked.endswith("before_rescue.tar.gz"), False)

        s.section("无稳态快照时必须返回空（rescue 应拒绝执行而非误还原）")
        for f in os.listdir(d):
            if "before_rescue" not in f:
                os.remove(os.path.join(d, f))
        s.check("返回空", _call(d, "latest_restorable_snapshot"), "")

    with tempfile.TemporaryDirectory() as d:
        s.section("keep 快照同样可作为还原目标")
        path = os.path.join(d, "snap_20260801_0900_final_keep.tar.gz")
        open(path, "w").close()
        s.check("选中 keep 快照",
                os.path.basename(_call(d, "latest_restorable_snapshot")),
                "snap_20260801_0900_final_keep.tar.gz")


def test_rescue_ordering(s):
    """静态守护：还原目标必须在打取证快照之前就锁定。"""
    with open(MANAGE, encoding="utf-8") as fh:
        body = fh.read()
    start = body.index("rescue_system()")
    block = body[start:body.index("\n}", start)]

    s.section("rescue_system 的关键顺序")
    pos_target = block.find("recovery_target=$(latest_restorable_snapshot)")
    pos_snap = block.find('create_snapshot "before_rescue"')
    s.check("锁定还原目标的语句存在", pos_target >= 0, True)
    s.check("打取证快照的语句存在", pos_snap >= 0, True)
    s.check("锁定目标发生在打取证快照之前", 0 <= pos_target < pos_snap, True)
    s.truthy("还原时显式传入目标（而非依赖'最新'）",
             'restore_snapshot "$(basename "$recovery_target")"' in block)


def test_project_dir_resolves_through_symlink(s):
    """经软链调用时项目根必须仍解析到真实项目目录。

    manage.sh 的正式入口是软链 /usr/local/bin/tg-bot。若用 $0 而非
    readlink -f，PROJECT_DIR 会变成 /usr/local，backup / restore / rescue /
    test 全部失效；而 status 不碰 PROJECT_DIR 仍正常，故障因此极难察觉。
    """
    s.section("软链入口下的项目根解析")
    with tempfile.TemporaryDirectory() as d:
        link = os.path.join(d, "tg-bot")
        os.symlink(MANAGE, link)
        # 必须真正**执行**软链：source 不会把 $0 设成被 source 的文件，
        # 那样测的是别的东西。-x 让赋值结果直接可观测，backups 只读不写。
        res = subprocess.run(
            ["bash", "-x", link, "backups"],
            capture_output=True, text=True, timeout=30, cwd="/",
        )
        m = re.search(r"^\+ PROJECT_DIR=(.*)$", res.stderr, re.MULTILINE)
        s.truthy("捕获到 PROJECT_DIR 赋值", m is not None)
        resolved = m.group(1).strip("'\"") if m else ""
        s.check("经软链解析出的项目根", os.path.realpath(resolved),
                os.path.realpath(PROJECT_DIR))
        for name in ("venv", "core", "releases"):
            s.check(f"{name}/ 在解析出的根下存在",
                    os.path.isdir(os.path.join(resolved, name)) if resolved else False,
                    True)


def test_path_traversal_guard(s):
    s.section("快照名与 tag 的路径穿越防护")
    with open(MANAGE, encoding="utf-8") as fh:
        body = fh.read()
    s.truthy("restore 对入参做 basename 收敛",
             'target_name=$(basename "$target_name")' in body)
    s.truthy("backup tag 过滤路径分隔符",
             "tr -cd '[:alnum:]_.-'" in body)

    with tempfile.TemporaryDirectory() as d:
        res = subprocess.run(
            ["bash", "-c", (
                f'source "{MANAGE}" >/dev/null 2>&1\n'
                f'SNAPSHOT_DIR="{d}"\n'
                f'restore_snapshot "../../../etc/passwd"'
            )],
            capture_output=True, text=True, timeout=30,
        )
        s.truthy("穿越路径被拒绝", "找不到指定的快照文件" in res.stdout)


def test_privilege_adaptation(s):
    """运行时不得依赖免密 sudo，且拿不到权限时必须如实汇报。"""
    with open(MANAGE, encoding="utf-8") as fh:
        body = fh.read()

    s.section("不得假设存在免密 sudo")
    # 除适配层自身外，不应再有裸的 sudo systemctl 调用
    for lineno, line in enumerate(body.splitlines(), 1):
        stripped = line.strip()
        if not stripped.startswith("sudo systemctl"):
            continue
        # try_systemctl 内部的交互式分支是允许的
        s.check(f"第 {lineno} 行的 sudo 调用位于适配层内", "try_systemctl" in body[:body.index(line)][-400:], True)

    s.section("三档降级链完整")
    s.truthy("有免密探测", "can_sudo_noninteractive()" in body)
    s.truthy("有终端探测", "has_tty()" in body)
    s.truthy("有无特权降级方案", "restart_via_self_kill()" in body)
    s.truthy("降级方案依赖 Restart=always 而非提权",
             "Restart=always" in body or "systemd 自动拉起" in body)

    s.section("绝不谎报成功")
    start = body.index("restart_services()")
    block = body[start:body.index("\n}", start)]
    s.truthy("失败路径有明确的失败输出", "❌ 重启失败" in block)
    s.truthy("失败时返回非 0", "return 1" in block)
    # 旧实现的病根：`|| true` 之后无条件 echo 成功
    s.check("重启逻辑中已无 '|| true' 吞错", "|| true" in block, False)

    s.section("stop/start 无法降级时必须说明而不是假装成功")
    case_stop = body[body.index("    stop)"):body.index("    start)")]
    s.truthy("stop 失败有明确提示", "❌ 停止失败" in case_stop)
    s.truthy("stop 说明了为何无法降级", "Restart=always" in case_stop)


def test_installer_does_not_touch_sudoers(s):
    """安装脚本不得擅自修改用户的 sudoers。"""
    installer = os.path.join(PROJECT_DIR, "install.sh")
    with open(installer, encoding="utf-8") as fh:
        body = fh.read()

    s.section("install.sh 不写入 /etc/sudoers.d")
    s.check("无 visudo 安装动作", "visudo -cf" in body, False)
    s.check("无 install -m 0440 写入", "install -m 0440" in body, False)
    s.check("无 tee 写入 sudoers", "tee $SUDOERS_FILE" in body, False)
    s.truthy("仅做探测与告知", "runtime_privilege()" in body)
    s.truthy("把可选方案交给用户自行决定", "print_optional_sudoers()" in body)


def test_unit_stays_minimal(s):
    """systemd unit 只负责拉起进程，配置一律留在 .env。"""
    installer = os.path.join(PROJECT_DIR, "install.sh")
    res = subprocess.run(
        ["bash", "-c", (
            f'source <(sed -n "/^render_unit()/,/^}}/p" "{installer}")\n'
            'USER=u VENV_DIR=/v PROJECT_ROOT=/p render_unit "D" "core/bot.py"'
        )],
        capture_output=True, text=True, timeout=30,
    )
    unit = res.stdout

    s.section("unit 不承载配置")
    s.check("无 Environment= 参数堆积", "Environment=" in unit, False)
    s.check("无写死的代理地址", "PROXY" in unit, False)
    s.check("无写死的 PATH", "PATH=" in unit, False)

    s.section("unit 保留必要的拉起要素")
    for key in ("ExecStart=", "WorkingDirectory=", "Restart=always", "User="):
        s.truthy(f"含 {key}", key in unit)
    # 无特权重启方案完全依赖 Restart=always，这一行不能丢
    s.truthy("Restart=always 是无特权降级的前提", "Restart=always" in unit)

    s.section("配置的唯一真相源是 .env")
    with open(os.path.join(PROJECT_DIR, "core", "file_pipeline.py"), encoding="utf-8") as fh:
        fp_body = fh.read()
    s.truthy("代理由应用从 TG_PROXY 读取", 'os.getenv("TG_PROXY"' in fp_body)
    s.check("代码中无硬编码代理端口", "10809" in fp_body, False)


def test_maintenance_never_blocks_on_sudo(s):
    """无人值守的维保任务不得卡在 sudo 密码提示上。"""
    script = os.path.join(PROJECT_DIR, "jobs", "auto-maintenance.sh")
    with open(script, encoding="utf-8") as fh:
        body = fh.read()

    s.section("journalctl 清理的降级")
    s.check("无裸 sudo journalctl", "$(sudo journalctl" in body, False)
    s.truthy("使用非交互探测", "sudo -n true" in body)
    s.truthy("有用户级日志降级", "journalctl --user" in body)


SUITES = [
    ("快照选取", test_snapshot_selection),
    ("自救顺序", test_rescue_ordering),
    ("软链下的项目根解析", test_project_dir_resolves_through_symlink),
    ("路径穿越防护", test_path_traversal_guard),
    ("特权适配", test_privilege_adaptation),
    ("安装脚本边界", test_installer_does_not_touch_sudoers),
    ("unit 最小化", test_unit_stays_minimal),
    ("维保无人值守", test_maintenance_never_blocks_on_sudo),
]

if __name__ == "__main__":
    main(SUITES)
