# 🤖 Telegram 监控与 AGY 自演进机器人项目规范 (GEMINI.md)

## 📌 项目标准分层架构与核心目录
- **项目根目录**: 动态相对路径解析 `$(cd "$(dirname "$0")/.." && pwd)`
  - `install.sh`: 傻瓜式一键依赖安装与 Systemd 服务部署脚本
  - `.env`: (未入库) 存放 Bot Token 等敏感密钥，通过 `python-dotenv` 加载
- **核心逻辑 (`core/`)**:
  - `core/bot.py`: Telegram 机器人 Layer 0 微内核 (动态时间戳版本 vYYYY.MM.DD-HHMM)
  - `core/task_engine.py`: 声明式动态任务调度引擎 (APScheduler + 语法防错 + 热加载)
  - `core/tts.py`: 独立两阶段 TTS 语音合成与 OGG/Opus 转码流水线引擎
  - `core/stt.py`: 独立两阶段 STT 语音识别与 Faster-Whisper 转译流水线引擎
  - `core/handlers/rescue_handler.py`: Layer 1 远程自救与快照管理 Handler
  - `core/handlers/system_handler.py`: Layer 2 硬件诊断、Docker 容器与 Systemctl 健康度 Handler
  - `core/handlers/agy_handler.py`: Layer 3 AGY AI 对话、多模态、模型/思考深度切换与消息防抖 Handler
- **配置与持久化 (`config/`)**:
  - `config/tasks.yaml`: 声明式定时任务配置表
  - `config/user_states.json`: 机器人会话模式、选定 AI 模型与思考深度持久化文件
- **定时任务与脚本库 (`jobs/`)**:
  - `jobs/auto-maintenance.sh`: 周自动维保脚本 (AGY 更新、Docker prune、日志清理)
  - `jobs/check_claude_rss.py`: Claude Code RSS 监控与 AI 中文翻译脚本
- **运维与自救工具 (`bin/`)**:
  - `bin/manage.sh`: `tg-bot` 控制与自救工具箱 (映射至 `/usr/local/bin/tg-bot`)
- **系统快照库 (`releases/snapshots/`)**:
  - 存放项目全量平滑备份快照包 (`snap_YYYYMMDD_HHMM[_tag].tar.gz`)，上限保留 20 个
- **日志目录 (`logs/`)**:
  - `logs/task_engine.log`: 调度引擎日志
- **Systemd 服务**:
  - `tg-monitor.service`: 运行 `core/bot.py`
  - `tg-task-engine.service`: 运行 `core/task_engine.py`

---

## 🛡️ 自救与快照备份四大铁律 (SELF-RESCUE & BACKUP RULES)

### 1. 修改代码前的快照备份机制
任何大型改动、重构或部署前，必须运行：
`tg-bot backup [tag_name]`
系统会自动生成包含 `core/`、`config/`、`jobs/`、`bin/`、`requirements.txt` 及文档的轻量快照包，保存于 `releases/snapshots/snap_YYYYMMDD_HHMM[_tag].tar.gz`。

**永久快照保护**: 滚动清理默认上限 20 个。若生成或重命名的快照文件名中包含 `keep` 字样（如 `*_final_keep.tar.gz`），该快照将被视作免死金牌，**永久保存**，不计入滚动清除指标。

### 2. 幽灵文件消除与一键自救恢复机制
若系统遇到故障、服务崩溃或代码修改坏掉，可以执行以下命令救援：
- `tg-bot rescue`: 自动诊断服务健康度，若服务异常，自动提取最新的稳态快照覆盖恢复并重启。
- `tg-bot restore [snapshot_name]`: 手动还原指定快照包。执行**压缩包完整性预检 (tar -tzf)** ➔ **净空受控子目录消除幽灵文件** ➔ **干净解压** ➔ **沙箱探针校验** ➔ **重启服务**。
- `tg-bot backups`: 查看所有可用的历史快照列表。

### 3. 修改与发布标准流程
任何对 `core/bot.py` 的代码修改，必须严格执行以下三步：
1. **动态版本构建**: `VERSION` 由 `core/bot.py` 根据文件最后修改时间戳自动生成，无需手写硬编码。
2. **沙箱预检测试**: 执行 `tg-bot test` 确保语法预检、Telegram API 联调与 AGY 探针三级校验全部 PASS！
3. **安全升级部署**: 执行 `tg-bot upgrade <new_file>`（自动先抓快照 ➔ 校验代码 ➔ 部署重启 ➔ 崩溃自动触发自救回滚）。

### 4. 声明式定时任务配置规范 (`config/tasks.yaml` & `core/task_engine.py`)
- **禁用硬编码 timer**: 严禁直接新建或硬编码系统级的 `systemd timer`，统一在 `config/tasks.yaml` 中配置。
- **任务放置规范**: 所有新增的定时任务代码或即用即弃脚本，统一存放在 `jobs/` 目录下，严禁随手丢在项目根目录。

### 5. 菜单与按键四位一体同步
凡是新增 Telegram 功能或指令（例如 `/model`, `/effort`），必须同时更新以下 4 个位置：
1. `init_commands()`: 注册到 Telegram API 的斜杠弹窗菜单中。
2. `get_main_keyboard(user_id)`: 注册到 Reply 底部面板按钮中。
3. `global_text_router(message)`: 在普通模式分发中挂载文本匹配。
4. `send_welcome(message)`: 在 `/help` 欢迎词中加入功能说明。

### 6. AGY AI 模型与思考深度交互规范
- `/model`: 弹窗 1-Click 选择 AI 模型 (Flash / Pro / Claude / GPT)。
- `/effort`: 弹窗选择思考推理深度 (Low / Medium / High)。
- `/settings`: 查阅当前会话选定的模型、思考深度及 Conversation ID。
- **自动退避重试 (Auto-Fallback Retry)**: 若所选模型（如 Claude Opus/Sonnet）不支持 `--effort`，后台捕获异常后自动剥离 `--effort` 重试，保障 Telegram 前端 100% 顺畅应答。
