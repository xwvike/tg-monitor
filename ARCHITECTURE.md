# 🏗️ tg-monitor 智能体架构演进史 (Architecture Evolution)

本文档系统性总结了 tg-monitor 在"多模态文件处理"这一核心痛点上的架构演进，以及在与开发者（USER）高频探讨中诞生的核心设计思想。

---

## 🛑 传统 Bot 的痛点与死局
在传统的 Telegram 机器人开发中，处理文件（图片、视频、PDF等）通常依赖于硬编码（Hardcoding）：
- `if message.text == '压缩图片': run_compress()`
- `elif message.text == '转GIF': run_gif()`

**问题暴露**：
用户的需求是无穷尽且表达随意的（"帮我压一下"、"让这张图小一点"、"拼成长图"），用 `if-else` 永远无法覆盖所有场景，最终会导致代码膨胀为不可维护的"屎山"。

---

## 💡 核心演进：四大 Agentic 架构模式

为了破局，我们抛弃了传统的"代码写死业务"模式，全面转向 **Agentic 智能体驱动架构**。以下是四大核心设计思想：

### 1. I/O 桥接与 Agentic Tooling (智能工具链调度)
- **理念**："把复杂性交给大模型，让 Bot 保持极简中转。"
- **实现**：
  - Python Bot 层完全剥离了具体的业务处理逻辑。它仅作为"网关"，负责把 Telegram 下发的文件保存到标准的 `Workspace IN (/tmp/tg_files/in/...)`。
  - 通过预设 `config/TOOLCHAIN.md`（定义了服务器上现有的能力如 ffmpeg, ImageMagick, Pandoc），让 AGY 智能体决定如何调用终端工具处理文件。
  - 处理完毕后，Bot 回收 `Workspace OUT` 中的产物回传并销毁工作区。

### 2. Recipe Book (声明式标准作业程序)
- **理念**："无论用户是说'压缩'还是'让图小一点'，底层的执行路径必须是确定、唯一的。"
- **实现**：
  - 建立 `config/file_recipes/` 目录，针对高频通用场景抽象为标准的 Markdown 处理手册。
  - 手册的**选取由 Python 完成**（见下方第 5 节），而非交给模型去搜。

### 3. Router Agent 模式 (意图分流)
- **痛点发现**：当用户上传图片并附带文字时，大模型经常产生"模态错位"——到底是要对图片进行物理裁剪，还是要进行视觉问答（QA）？混在一个 Prompt 里模型极易精神分裂。
- **理念**："专业的事交给专业的 Agent 做，先定性，再定量。"
- **实现**：
  - 收到带附言的文件时先做意图判定：`True`（物理转换任务）或 `False`（视觉问答任务），再据此物理隔离分流。

### 4. Planner-Executor 隔离机制
- **痛点发现**：多模态大模型"看到"图片的第一本能永远是去解释和描述。在文件处理任务中给它传入实体图片，它经常会抛弃动作指令，转而强行向用户描述图片内容。
- **理念**："既然你一看图就走神，那干活的时候我就不给你看图。"
- **实现**：
  - **Planner (AGY)**：不接收实体文件，只拿到文件的**元数据**（绝对路径、大小、分辨率/时长/页数）与用户诉求，输出纯粹的 `<json>["cmd1","cmd2"]</json>` 命令数组。
  - **Executor (Python)**：正则提取 JSON 后在宿主机 `subprocess` 执行并回收产物。

---

## 🔄 第二轮修正：把边界重新画一遍

上述四个模式方向是对的，但初版实现把**边界画错了地方**——让 LLM 去做本该确定的事
（找菜谱、填路径），却让 Python 去做本该有判断力的事（错误处理、重试）。
第二轮重构（`core/file_pipeline.py`）做了三个反转：

### 5. 菜谱检索下沉到 Python
- **旧**：prompt 里写"务必先查阅 `config/TOOLCHAIN.md` 与 `config/file_recipes/`"，
  Planner 每次任务先烧 2-4 轮 tool call 去 ls 目录、读 README、读 136 行工具链。
- **新**：`RECIPE_INDEX` 按「扩展名 + 关键词」在 Python 侧命中，**把菜谱正文直接内联进 prompt**。
  工具链清单同时裁掉与文件处理无关的段落（Postgres / Redis / qBittorrent 等纯噪声）。
- **收益**：省掉整轮往返；"用哪本手册"从概率事件变成确定事件。
- **代价**：新增菜谱必须同步注册进 `RECIPE_INDEX`，否则不会被命中。
  菜谱由人写、人审、随代码提交 —— 它决定的是此后**所有**同类任务的走法，
  这条链路上不留无人复核的环节。

### 6. 错误回喂：让 Planner 看见自己的执行结果
- **旧**：`subprocess.run(cmd, shell=True, check=True)`——没有 `capture_output`（ffmpeg
  的报错全进了 journal，用户只拿到"错误代码: 1"）、没有 `timeout`（写坏的命令永久挂死线程）、
  没有 `cwd`（相对路径产物污染仓库根目录）。一步失败整个任务就结束。
- **新**：执行带 `cwd` / `timeout` / `capture_output`；失败时把**真实 stderr 回喂给 Planner
  重新规划**（`MAX_PLAN_ATTEMPTS`）。"命令全绿但输出目录为空"同样视为失败并回喂。
- **兜底**：Planner 若照抄手册写出相对路径，产物会落在输入目录——`collect_outputs()`
  会把输入目录里新增的文件自动搬回输出目录，而不是回一句"未生成任何文件"。

### 7. 意图判定：关键词短路优先
- **旧**：每个带附言的文件都要冷启动一次 193MB 的 agy CLI 只为得到一个布尔值，
  与 Planner 串行叠加延迟；判定结果用 `"true" in (stdout+stderr)` 做子串匹配（任何一行
  带 `true` 的 CLI 日志都会带偏）；超时/异常无条件倒向问答。
- **新**：`PROCESS_PHRASES` / `QA_PHRASES` 关键词短路，单侧命中即直接定性，**零模型调用**；
  只有双侧命中或双侧落空这种真模糊才问模型，且只解析 stdout 末行做整词匹配；
  判定失败时按关键词倾向兜底而非无条件走问答。
- **无附言一律走问答**：非破坏性的默认。旧实现里无附言的文档会被强行"压缩/规范化"，
  用户只想让 AI 读一下 PDF，结果文件被压了。

### 8. 相册聚合 (Media Group)
- **痛点**：Telegram 相册的每张图都是一条**独立 message**。初版为每条各建一个工作区、
  各跑一次 Router + Planner——这意味着写得最详细的 `image_stitching.md`
  **在架构上就不可能成功**：Planner 每次只看得到一张图。
- **实现**：按 `media_group_id` 复用同一个 `workspace_in`，用一个 `MEDIA_GROUP_WINDOW`
  收集窗口把整组攒成**单次任务**，文件名带序号前缀以保证拼接顺序。

---

## 🎯 总结
这套架构将 Telegram 机器人从"僵化的代码机器"升维成了**"能听懂人话、会查阅说明书、
甚至会自动写 Bash 脚本干活的赛博流水线工人"**。

而第二轮重构给出的真正教训是：**Agentic 不等于把所有事都丢给模型。**
确定的部分（选菜谱、填路径、回收产物）交给代码，不确定的部分（把人话翻译成命令、
读懂报错并修正）才交给模型——这条线画对了，稳定性才立得住。

### 责任边界速查
| 环节 | 归属 | 位置 |
|------|------|------|
| 文件落盘、相册聚合 | Python | `core/handlers/agy_handler.py` |
| 意图判定（关键词短路） | Python | `classify_intent()` |
| 意图判定（真模糊时） | LLM | `classify_intent()` 兜底分支 |
| 菜谱与工具链检索 | Python | `select_recipes()` / `load_toolchain()` |
| 元数据探针 | Python | `probe_file()` |
| **需求 → bash 命令** | **LLM** | `build_plan_prompt()` |
| 命令执行与错误捕获 | Python | `execute_commands()` |
| **读懂报错并修正命令** | **LLM** | 回喂重规划 |
| 产物回收与投递 | Python | `collect_outputs()` / `_send_product()` |
