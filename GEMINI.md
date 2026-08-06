# 🤖 Telegram 监控与 AGY 自演进机器人项目规范 (GEMINI.md)

## 📌 项目标准分层架构与核心目录
- **项目根目录**: 动态相对路径解析 `$(cd "$(dirname "$0")/.." && pwd)`
  - `install.sh`: 傻瓜式一键依赖安装与 Systemd 服务部署脚本
  - `.env`: (未入库) 存放 Bot Token 等敏感密钥，通过 `python-dotenv` 加载
- **核心逻辑 (`core/`)**:
  - `core/bot.py`: Telegram 机器人 Layer 0 微内核 (动态时间戳版本 vYYYY.MM.DD-HHMM)
  - `core/task_engine.py`: 声明式动态任务调度引擎 (APScheduler + 语法防错 + 热加载)
  - `core/file_pipeline.py`: **文件处理流水线** —— 意图判定、菜谱检索、元数据探针、
    Planner 规划、命令执行与错误回喂、产物回收
  - `core/tg_format.py`: Telegram HTML 转义与安全发送（解析失败自动降级重发）
  - `core/user_state.py`: 会话状态持久化（原子写 + 线程锁 + 损坏留档）
  - `core/tts.py`: 独立两阶段 TTS 语音合成与 OGG/Opus 转码流水线引擎
  - `core/stt.py`: 独立两阶段 STT 语音识别与 Faster-Whisper 转译流水线引擎
  - `core/handlers/rescue_handler.py`: Layer 1 远程自救与快照管理 Handler
  - `core/handlers/system_handler.py`: Layer 2 硬件诊断、Docker 容器与 Systemctl 健康度 Handler
  - `core/handlers/agy_handler.py`: Layer 3 AGY AI 对话、多模态、文件批次聚合与消息防抖 Handler
- **测试 (`tests/`)**: 零外部依赖的断言套件，由 `tg-bot test` 的 `[2/4]` 执行
  - `test_rescue.py`: 自救顺序、快照选取、特权适配、安装脚本边界
  - `test_user_state.py`: 原子写（含 SIGKILL 实测）、损坏留档、线程安全
  - `test_tg_format.py`: HTML 转义、发送兜底、源码静态扫描
  - `test_toolchain_doc.py`: TOOLCHAIN.md 与代码 / install.sh 的一致性
  - `test_file_pipeline.py`: 意图判定、菜谱、执行层、编排回喂、文件名注入防护
  - `test_message_routing.py`: 文件/文本三种到达顺序、caption 归属、会话隔离
- **配置与持久化 (`config/`)**:
  - `config/tasks.yaml`: 纯声明式定时任务配置表 (不含凭证，凭证由 `.env` 直接提供)
  - `config/user_states.json`: 机器人会话模式、选定 AI 模型与思考深度持久化文件
  - `config/TOOLCHAIN.md`: 工具链能力地图（**会被内联进 Planner 的 prompt**）
  - `config/file_recipes/`: 文件处理标准作业手册（由 `RECIPE_INDEX` 在 Python 侧命中）
- **定时任务与脚本库 (`jobs/`)**:
  - `jobs/auto-maintenance.sh`: 周自动维保脚本 (AGY 更新、Docker prune、日志清理)
  - `jobs/check_claude_rss.py`: Claude Code RSS 监控与 AI 中文翻译脚本
- **运维与自救工具 (`bin/`)**:
  - `bin/manage.sh`: `tg-bot` 控制与自救工具箱 (映射至 `/usr/local/bin/tg-bot`)
- **文档**:
  - `README.md`: 系统架构、功能特性、责任边界速查与运维命令 —— 唯一的总览入口
  - `config/TOOLCHAIN.md`: 服务器现有能力清单（供流水线内联给 Planner）
  - `config/file_recipes/README.md`: 菜谱清单与编写规范
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
系统会自动生成包含 `core/`、`config/`、`jobs/`、`bin/`、`tests/`、`install.sh`、
`requirements.txt` 及文档的轻量快照包，保存于 `releases/snapshots/snap_YYYYMMDD_HHMM[_tag].tar.gz`。

> ⚠️ `create_snapshot` 的打包清单与 `restore_snapshot` 的"受控子目录清空"清单
> **必须保持一致**，否则还原后会出现"旧代码 + 新测试"这类自相矛盾的组合。

**永久快照保护**: 滚动清理默认上限 20 个。若生成或重命名的快照文件名中包含 `keep` 字样（如 `*_final_keep.tar.gz`），该快照将被视作免死金牌，**永久保存**，不计入滚动清除指标。

### 2. 幽灵文件消除与一键自救恢复机制
若系统遇到故障、服务崩溃或代码修改坏掉，可以执行以下命令救援：
- `tg-bot rescue`: 自动诊断服务健康度，若服务异常，自动提取最新的稳态快照覆盖恢复并重启。

> 🔒 **顺序铁律**：`rescue` 必须**先锁定还原目标、再拍故障现场取证快照**。
> 反过来做的话，取证快照会成为"最新快照"，自救就退化成把刚坏掉的状态原样装回去
> —— 这个缺陷曾让整套自救系统长期空转。`latest_restorable_snapshot()` 会排除
> `*_before_rescue*`，`tests/test_rescue.py` 对该顺序做静态守护。
- `tg-bot restore [snapshot_name]`: 手动还原指定快照包。执行**压缩包完整性预检 (tar -tzf)** ➔ **净空受控子目录消除幽灵文件** ➔ **干净解压** ➔ **沙箱探针校验** ➔ **重启服务**。
- `tg-bot backups`: 查看所有可用的历史快照列表。

### 3. 修改与发布标准流程
任何对 `core/` 的代码修改，必须严格执行以下三步：
1. **动态版本构建**: `VERSION` 由 `core/bot.py` 根据文件最后修改时间戳自动生成，无需手写硬编码。
2. **沙箱四级校验**: 执行 `tg-bot test`，四级全部 PASS 才允许发布：
   - `[1/4]` 语法预检（扫描 `core/` 与 `jobs/` 全部 Python 文件）
   - `[2/4]` **业务逻辑单元测试**（`tests/run_all.py`，语法通过不代表行为正确）
   - `[3/4]` Telegram API 联调探针
   - `[4/4]` AGY 引擎底层探针
3. **安全升级部署**: 执行 `tg-bot upgrade <new_file>`（自动先抓快照 ➔ 校验代码 ➔ 部署重启 ➔ 崩溃自动触发自救回滚）。

> ⚠️ **改了行为就必须补测试**。`tests/` 是唯一能挡住"改坏了但还能跑"的闸门，
> 且它随快照一起打包——不进 `tests/` 的验证等于没验证。
> 但"什么算行为"有严格边界，见 3.01。

### 3.01 测什么，不测什么
**只测确定性管道，不测判断力，不测措辞。**

可以测（不依赖任何人怎么说话，坏了就是真坏了）：
- 代码行为：产物投递保真、归档与配额、路径穿越与文件名注入、软链下的项目根、
  消息合并与 caption 归属、工作区生命周期、失衡判据的算术、命令提取的容错。
- 结构性一致：文档引用的路径是否真实存在、声明的二进制是否会被安装、
  快照打包清单与清空清单是否一致。

**不许测**：
- **预测用户会怎么说话** —— 「"帮我压压" 是否命中压缩菜谱」这类断言，措辞是我
  自己编的，而真实用户永远不会照着写。曾经有 170 项这样的断言，今天真正出的
  路由故障（转发件 caption 被吞）一条都没抓到。
- **断言文档/菜谱/prompt 的措辞** —— 菜谱要越改越发散，断言散文只会把它锁死在
  某一版写法上。曾经有 106 项，结果是每次改菜谱都得先去改测试伺候它。
- **守护一个猜测** —— 参数分档、阈值这类没有实测锚点的判断写进代码已经错了，
  再配测试守护它，就是把猜测固化成"事实"。

判据一句话：**这条断言会因为"人换了个说法"或"文字改得更好了"而变红吗？
会，就不该存在。**

出效果问题时的正确做法不是补测试，而是走 3.03：从 `workspace/archive/` 取出
那次的原文件与产物，实测，然后改菜谱。

> 2026-08-06 据此一次性砍掉了当时约四成的断言（预测措辞的、断言散文的、
> 守护猜测的）。删掉的不是安全网，是自我安慰。

### 3.0 Telegram 消息格式化铁律
Bot 全局 `parse_mode="HTML"`，任何插入消息体的**动态内容**（命令输出、异常文本、
进程名、容器名、RSS 正文）只要含 `<` 或 `&`，整条消息就会被 Telegram 以 400 拒收，
且拒收往往被 `except` 吞掉只留一行日志 —— 消息**静默消失**。

因此一律使用 `core/tg_format.py`：
- `esc(value)` — 插值前转义
- `code_block(text)` — 包 `<pre>` + 转义 + 截断
- `send_html(bot, chat_id, text)` — 发送；解析失败自动降级为纯文本重发

> ⚠️ 严禁写 `f"<pre>{output}</pre>"` 这类裸插值。
> `tests/test_tg_format.py` 会静态扫描 `core/` 拦截此类写法。

### 3.02 产物回传的保真铁律
Telegram 会**按文件内容嗅探并在服务端重编码**，与你调用哪个发送方法无关。
文件处理类任务的全部价值就在产物本身，被重编码一次等于白干。

已实测的行为（同一份 720x405 / 723 KB 的 GIF）：

| 发送方式 | 用户实际收到 |
|---|---|
| `send_animation` | `x.gif.mp4`, video/mp4, 320x180, 21 KB |
| `send_document` | `x.gif.mp4`, video/mp4, 320x180, 21 KB |
| `send_document(disable_content_type_detection=True)` | 原文件，sha256 一致 |

- **GIF**: 必须 `send_document` + `disable_content_type_detection=True`。
- **图片**: 必须 `send_document`，`send_photo` 会二次压缩。
- **mp4 / mp3**: 经 `send_video` / `send_audio` 实测逐字节保真，无需特殊处理。

> ⚠️ 新增产物类型时，先实测"发出去再下载回来"的 sha256 是否一致，
> 再决定走哪个方法 —— 返回的 `file_size` 是重编码**之后**的值，看它发现不了问题。
> `tests/test_file_pipeline.py::test_gif_product_delivery` 守护该行为。

### 3.03 菜谱调参的铁律：先看产物，再动参数
转换质量的问题（糊、尺寸不对、体积失控）**不许凭直觉改菜谱**。ffmpeg 命令的
参数看起来是否合理与产物实际好不好是两回事，只读日志永远得不出结论。

流程是固定的：

1. 去 `workspace/archive/` 找那次任务的目录（`run.json` 里有用户原话与命令原文）。
2. 拿 `in/` 的原文件和 `out/` 的产物**实测**：`ffprobe` 比分辨率/帧率，
   `ffmpeg -lavfi ssim` 比画质，`ls -l` 比体积。
3. 只在有测量结果支撑时才改 `config/file_recipes/*.md`。**数字写进提交记录和本节，
   菜谱里只留结论与权衡** —— 菜谱会被整段内联进 prompt，数据表既烧 token 又稀释指令。

留痕不在时（`TG_ARCHIVE_ENABLED=0` 或那次早于配额期限）就用同类素材复现一遍，
**不要跳过测量直接改参数** —— 那正是把 `bayer_scale` 从 5 改成 3 这类"改了个寂寞
甚至改坏"的来源。

已实测的 GIF 结论（源均 2940x1912，fps=12，max_colors=128）：

| 素材 | 源 bpp | 输出 | 体积 | 文字区 SSIM |
|---|---|---|---|---|
| 屏幕录制 | 0.0046 | 720 宽 / 6s | 598 KB | 0.916 |
| 屏幕录制 | 0.0046 | 1470 宽 / 6s | 1.8 MB | **0.970** |
| 屏幕录制 | 0.0046 | 2940 宽 / 6s | 6.1 MB | 0.990 |
| 全屏渐变 | 0.0201 | 720 宽 / 10s | 12 MB | — |
| 全屏渐变 | 0.0201 | 1440 宽 / 10s | **39 MB** | — |
| 纯噪声 | 0.0683 | 720 宽 / 10s | 17 MB | — |

- **宽度必须按内容定**，不能是常数：同为 1440 宽，屏幕录制与渐变素材差 20 倍以上。
  源 h264 的每像素字节数（bpp）是免费的复杂度信号，可作为**证据**交给模型；
  但**不要在 Python 里按它划档** —— 那只是把一个常数换成三个常数，
  中间档还没有实测锚点。判断交给能判断的一方（见 3.04）。
- **抖动分内容**：屏幕录制 `dither=none`（1470 宽下体积相同而文字区 SSIM
  0.9723 > bayer 的 0.9702）；渐变/实拍仍用 `bayer:bayer_scale=5`（5 > 3 > 1）。
  任何内容都别用 `sierra2_4a`（体积翻三倍，画质差异可忽略）。
- `max_colors` 128 → 256 只换来 +0.0007 SSIM，体积却多 20%，不值。
- `palettegen` 的 `stats_mode` 默认就是 `full`，显式写它是空操作。

> `core/run_archive.py` 负责留痕与配额，`tests/test_run_archive.py` 守护它。
> 归档目录不受启动清扫影响；回收只按 `.env` 的容量/天数配额进行。

### 3.04 判断力归模型，确定性归代码
"确定的事交给代码，不确定的事交给模型"这条线，一直在被**往代码那边偷偷挪**：
写死 `scale=720`，是把"多宽合适"这个判断固化成常数；改成按 bpp 分三档，
是把它固化成三个常数。两者是同一个错误，后者只是穿了件带数据的外衣。

正确的划分：

| 归属 | 内容 |
|---|---|
| **代码** | 摆证据：抽帧、探元数据、算 bpp、执行命令、捕获 stderr、回收产物 |
| **模型** | 下判断：这是什么素材、什么优先、因此该用什么参数 |

因此文件处理是**两次调用**，不是一次：

1. **判定**：Python 抽 2–3 帧 + 元数据 → 模型只回答"这是什么素材、什么优先"
   （屏幕录制/文字密集/实拍/动画/渐变；可读性优先还是流畅度优先）。
2. **规划**：判定结论 + 菜谱 + 工具链 → 模型输出命令数组。

拆成两次是有原因的：**多模态模型一旦在规划调用里拿到图片，就会开始描述画面
而不是动手**（实测结论，不要合并这两次调用）。一个前置不够就再加一个，
直到能给出合适的处理方式为止。

菜谱因此也要换形态：**不写填空式命令模板，写权衡** —— 每个旋钮花什么代价、
什么素材在意什么、失败长什么样，外加几个成品例子。让模型依据判定结论自己组命令。

### 3.05 外部输入进入 shell 的铁律
`execute_commands` 以 `shell=True` 执行 Planner 生成的命令，输入文件的**绝对路径
会原样出现在命令中**。因此凡是来自外部的名称（Telegram 提供的 `file_name`、
解压出来的条目名等）必须先经 `safe_filename()` 收敛。

`os.path.basename()` 只挡路径穿越，**不挡 `;` `$()` 反引号 `|` `&` 这些 shell 元字符** ——
用户仅仅转发一个来自频道的恶意命名文件即可触发任意命令执行，无需任何主动的危险操作。
该漏洞曾真实存在并被实测复现。

> 相关但仍存在的风险面：`shell=True` + LLM 生成命令这一组合本身。
> 已知输入路径（文件名）已收敛；caption 来自用户本人，属可接受风险。
> 若需更保守，可给执行层加命令白名单（首词限定为已声明工具）。

### 3.1 部署与可迁移性
- **线上环境必须完全由 `install.sh` 产出**，禁止手工改 systemd unit。
  手工改动会让新机器上的部署与线上不一致（这个坑已经踩过一次）。
- `./install.sh` 幂等，可反复执行以就地修复。
- `./install.sh --check` 审计当前部署：工具链、agy、venv、.env、
  systemd unit 是否与脚本定义漂移、软链、免密 sudo、服务状态、沙箱四级校验。
- 新增系统级依赖时，必须同步更新 `install.sh` 的 `TOOLCHAIN` 映射与 `config/TOOLCHAIN.md`。

### 4. 声明式定时任务配置规范 (`config/tasks.yaml` & `core/task_engine.py`)
- **纯任务声明表**: `tasks.yaml` 只定义“做什么、什么时候做”，严禁在其中放置任何凭证、Token 或连接信息。通知发送模块直接从 `.env` 环境变量读取。
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
