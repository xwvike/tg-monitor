#!/usr/bin/env python3
"""
TG Monitor 动态任务调度引擎 (task_engine.py)
特性：
1. 热加载：自动监听 tasks.yaml 变动并防抖（2秒静默期）。
2. 语法防错：编辑中途、语法错误或字段缺失时忽略变更，维持原内存任务运行。
3. 多类型任务支持：
   - script: 执行本地 Shell 命令/脚本
   - agy_task: 调用 agy CLI 智能体执行任务
4. Telegram 通知：统一捕获执行结果并格式化推送。
"""

import hashlib
import logging
import os
import subprocess
import sys
import time

import telebot
import telebot.apihelper
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.tg_format import code_block, esc, send_html

# 配置路径
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config", "tasks.yaml")
LOG_PATH = os.path.join(BASE_DIR, "logs", "task_engine.log")
AGY_BIN = os.path.expanduser("~/.local/bin/agy")

# 加载项目根目录唯一权威的 .env 凭证文件
load_dotenv(os.path.join(BASE_DIR, ".env"))
PROXY_URL = os.getenv("TG_PROXY", "")


# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("TaskEngine")


def send_tg_notification(title: str, body: str):
    """通过 Telegram 发送通知 (凭证直接从 .env 环境变量读取)"""
    try:
        bot_token = os.getenv("TG_BOT_TOKEN")
        chat_id = os.getenv("TG_CHAT_ID")
        proxy = os.getenv("TG_PROXY")

        if not bot_token or not chat_id:
            logger.warning("Telegram bot_token 或 chat_id 未配置，跳过推送")
            return

        if proxy:
            telebot.apihelper.proxy = {"https": proxy, "http": proxy}

        bot = telebot.TeleBot(bot_token, parse_mode="HTML")

        # 任务输出可能是 RSS 正文、XML、报错堆栈 —— 必须转义，
        # 否则一个 `<` 就让整条通知被 Telegram 拒收并被 except 吞掉
        msg = (
            f"🔔 <b>[{esc(title)}] 任务执行完成简报</b>\n"
            f"──────────────────────\n"
            f"{code_block(body)}"
        )
        send_html(bot, chat_id, msg)
        logger.info(f"Telegram 通知已成功发送: {title}")
    except Exception as e:
        logger.error(f"发送 Telegram 通知失败: {e}")


def run_script_task(task: dict):
    """执行 Shell/脚本 类型的任务"""
    task_id = task.get("id")
    name = task.get("name", task_id)
    cmd = task.get("command")
    if not cmd:
        logger.error(f"脚本任务 [{name}] (ID: {task_id}) 缺少有效的 command 字段")
        return
    logger.info(f"开始执行脚本任务 [{name}] (ID: {task_id}): {cmd}")

    try:
        env = os.environ.copy()
        if PROXY_URL:
            env["HTTP_PROXY"] = PROXY_URL
            env["HTTPS_PROXY"] = PROXY_URL
        res = subprocess.run(
            str(cmd),
            shell=True,
            capture_output=True,
            text=True,
            timeout=1800,  # 最长超时 30 分钟
            env=env,
            cwd=BASE_DIR,  # 确保相对路径脚本从项目根目录执行
        )
        output = res.stdout.strip()
        if res.stderr:
            output += f"\n[Stderr]\n{res.stderr.strip()}"

        status_text = (
            "成功" if res.returncode == 0 else f"失败 (exit code {res.returncode})"
        )
        logger.info(f"脚本任务 [{name}] 执行{status_text}")

        if task.get("notify", True):
            send_tg_notification(f"{name} ({status_text})", output or "无输出")

    except subprocess.TimeoutExpired:
        logger.error(f"脚本任务 [{name}] 执行超时！")
        if task.get("notify", True):
            send_tg_notification(f"{name} (超时)", "任务运行超过 30 分钟被强制终止")
    except Exception as e:
        logger.error(f"脚本任务 [{name}] 运行时异常: {e}")
        if task.get("notify", True):
            send_tg_notification(f"{name} (异常)", f"发生未捕获异常: {e}")


def run_agy_task(task: dict):
    """执行 AGY AI 智能体任务"""
    task_id = task.get("id")
    name = task.get("name", task_id)
    prompt = task.get("prompt")
    logger.info(f"开始执行 AGY 任务 [{name}] (ID: {task_id})")

    if not prompt:
        logger.error(f"AGY 任务 [{name}] 未指定 prompt，跳过执行")
        return

    try:
        # 使用 agy CLI 非交互模式运行 prompt
        env = os.environ.copy()
        if PROXY_URL:
            env["HTTP_PROXY"] = PROXY_URL
            env["HTTPS_PROXY"] = PROXY_URL
        local_bin = os.path.expanduser("~/.local/bin")
        env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"
        cmd = [AGY_BIN, "--prompt", prompt, "--dangerously-skip-permissions"]
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 最长超时 10 分钟
            env=env,
            cwd=BASE_DIR,
        )
        output = res.stdout.strip()
        if res.stderr:
            output += f"\n[Logs]\n{res.stderr.strip()}"

        status_text = "完成" if res.returncode == 0 else f"异常 Exit({res.returncode})"
        logger.info(f"AGY 任务 [{name}] 执行完毕 ({status_text})")

        if task.get("notify", True):
            send_tg_notification(
                f"🤖 AGY: {name} ({status_text})",
                output or "AI 未返回文本结果",
            )

    except subprocess.TimeoutExpired:
        logger.error(f"AGY 任务 [{name}] 执行超时")
        if task.get("notify", True):
            send_tg_notification(f"🤖 AGY: {name} (超时)", "AGY 任务运行超时已中断")
    except Exception as e:
        logger.error(f"AGY 任务 [{name}] 发生异常: {e}")
        if task.get("notify", True):
            send_tg_notification(f"🤖 AGY: {name} (错误)", f"异常: {e}")


def dispatch_task(task: dict):
    """任务分发入口"""
    task_type = task.get("type")
    if task_type == "script":
        run_script_task(task)
    elif task_type == "agy_task":
        run_agy_task(task)
    else:
        logger.warning(f"未知任务类型 [{task_type}] for task ID: {task.get('id')}")


class ConfigManager:
    """配置文件解析、校验与动态监听管理器"""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.last_mtime = 0
        self.last_hash = ""
        self.active_config = None

    def validate_config(self, raw_data: dict) -> bool:
        """校验配置文件的结构与语法"""
        if not isinstance(raw_data, dict):
            logger.warning("配置校验失败：根元素必须为字典/对象")
            return False

        if "tasks" not in raw_data:
            logger.warning("配置校验失败：缺失 'tasks' 节点")
            return False

        tasks = raw_data.get("tasks", [])
        if not isinstance(tasks, list):
            logger.warning("配置校验失败：'tasks' 必须为列表")
            return False

        for idx, task in enumerate(tasks):
            if not isinstance(task, dict):
                logger.warning(f"配置校验失败：第 {idx + 1} 个任务格式非法")
                return False
            if "id" not in task:
                logger.warning(f"配置校验失败：第 {idx + 1} 个任务缺失 'id' 字段")
                return False
            if "type" not in task or task["type"] not in ["script", "agy_task"]:
                logger.warning(
                    f"配置校验失败：任务 [{task.get('id')}] 包含不支持的 type: {task.get('type')}"
                )
                return False
            if task["type"] == "script" and "command" not in task:
                logger.warning(
                    f"配置校验失败：script 任务 [{task.get('id')}] 缺失 'command' 字段"
                )
                return False
            if task["type"] == "agy_task" and "prompt" not in task:
                logger.warning(
                    f"配置校验失败：agy_task 任务 [{task.get('id')}] 缺失 'prompt' 字段"
                )
                return False

        return True

    def poll_for_changes(self) -> tuple[bool, dict | None]:
        """
        检查文件变动。
        防抖机制：变动后需要静默 2 秒且完整校验通过才返回 True 和新配置。
        """
        if not os.path.exists(self.filepath):
            return False, None

        try:
            mtime = os.path.getmtime(self.filepath)
            # 如果 mtime 变化，检查是否防抖完成（即距离上次修改超过 2 秒）
            if mtime != self.last_mtime:
                now = time.time()
                if now - mtime < 2.0:
                    # 还在频繁写入中（如防抖窗口内），暂不处理
                    return False, None

                # 读取文件并计算哈希
                with open(self.filepath, "r", encoding="utf-8") as f:
                    content = f.read()

                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                self.last_mtime = mtime

                if content_hash == self.last_hash:
                    # 文件 mtime 变了但内容没变，忽略
                    return False, None

                # 尝试 YAML 解析
                try:
                    data = yaml.safe_load(content)
                except Exception as parse_err:
                    logger.warning(
                        f"⚠️ [YAML 语法错误/编辑未完成] 已忽略本次修改，保留当前有效任务。错误: {parse_err}"
                    )
                    return False, None

                # 进行规则与字段合法性校验
                if not self.validate_config(data):
                    logger.warning(
                        "⚠️ [配置校验不通过] 已忽略本次修改，保留当前有效任务。"
                    )
                    return False, None

                # 校验完全通过！更新 Hash 和配置
                self.last_hash = content_hash
                self.active_config = data
                logger.info("✅ 成功检测并验证通过了新的 tasks.yaml 配置配置表！")
                return True, data

        except Exception as e:
            logger.error(f"检查配置文件时发生异常: {e}")

        return False, None


class TaskEngine:
    """定时任务引擎"""

    def __init__(self, config_path: str):
        self.config_mgr = ConfigManager(config_path)
        self.scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        self.running_jobs = {}  # task_id -> job_id

    def build_trigger(self, task: dict):
        """根据配置生成 APScheduler Trigger"""
        trigger_type = task.get("trigger", "cron")
        if trigger_type == "cron":
            cron_spec = task.get("cron", {})
            return CronTrigger(**cron_spec)
        elif trigger_type == "interval":
            interval_spec = task.get("interval", {})
            return IntervalTrigger(**interval_spec)
        else:
            logger.warning(f"未知 trigger 类型: {trigger_type}，默认退回 cron")
            return CronTrigger(**task.get("cron", {}))

    def sync_tasks(self, config: dict):
        """将最新的配置表同步刷新至 APScheduler 中"""
        tasks = config.get("tasks", [])
        configured_ids = set()

        for task in tasks:
            task_id = task.get("id")
            name = task.get("name", task_id)
            enabled = task.get("enabled", True)

            if not enabled:
                # 如果任务被禁用，且当前存在于调度器中，则移除
                if task_id in self.running_jobs:
                    self.scheduler.remove_job(task_id)
                    del self.running_jobs[task_id]
                    logger.info(f"➖ 已禁用并从调度器中移除任务: [{name}] ({task_id})")
                continue

            configured_ids.add(task_id)
            trigger = self.build_trigger(task)

            if task_id in self.running_jobs:
                # 已存在，先移除再重新添加，确保 command/prompt 等参数变更生效
                self.scheduler.remove_job(task_id)
                del self.running_jobs[task_id]
                logger.info(f"🔄 正在更新定时任务: [{name}] ({task_id})")

            if task_id not in self.running_jobs:
                # 新增或重建任务
                self.scheduler.add_job(
                    func=dispatch_task,
                    trigger=trigger,
                    args=[task],
                    id=task_id,
                    name=name,
                    replace_existing=True,
                )
                self.running_jobs[task_id] = task_id
                logger.info(f"➕ 已添加新的定时任务: [{name}] ({task_id})")

        # 移除已经在配置表中被彻底删掉的任务
        current_job_ids = list(self.running_jobs.keys())
        for job_id in current_job_ids:
            if job_id not in configured_ids:
                self.scheduler.remove_job(job_id)
                del self.running_jobs[job_id]
                logger.info(f"🗑️ 已删除废弃的任务: ({job_id})")

    def start(self):
        """启动定时引擎"""
        logger.info("🚀 启动 TG Monitor 动态调度引擎...")
        self.scheduler.start()

        # 首次强制同步配置
        changed, initial_cfg = self.config_mgr.poll_for_changes()
        if initial_cfg:
            self.sync_tasks(initial_cfg)
        else:
            logger.warning("首次读取配置文件失败或空文件，等待变更...")

        try:
            while True:
                time.sleep(3)
                changed, new_cfg = self.config_mgr.poll_for_changes()
                if changed and new_cfg:
                    logger.info("检测到配置更变，正在动态更新定时任务...")
                    self.sync_tasks(new_cfg)
        except (KeyboardInterrupt, SystemExit):
            logger.info("收到退出信号，正在关闭调度引擎...")
            self.scheduler.shutdown()


if __name__ == "__main__":
    engine = TaskEngine(CONFIG_PATH)
    engine.start()
