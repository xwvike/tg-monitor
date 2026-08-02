#!/bin/bash
# ==============================================================================
# TG Monitor 运维管理与自救快照工具 (bin/manage.sh)
# ==============================================================================

# 动态获取项目根目录（脱离路径与用户名硬编码）
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="$PROJECT_DIR/venv/bin/python3"
BOT_SCRIPT="$PROJECT_DIR/core/bot.py"
STATE_FILE="$PROJECT_DIR/config/user_states.json"
MAINTAIN_SCRIPT="$PROJECT_DIR/jobs/auto-maintenance.sh"
SNAPSHOT_DIR="$PROJECT_DIR/releases/snapshots"
SERVICE_BOT="tg-monitor.service"
SERVICE_ENGINE="tg-task-engine.service"
MAX_SNAPSHOTS=20

mkdir -p "$SNAPSHOT_DIR"

# 创建完整快照并自动维持最多 MAX_SNAPSHOTS 个
create_snapshot() {
    local tag="${1:-}"
    local timestamp=$(date +%Y%m%d_%H%M)
    local snapshot_name=""
    if [ -n "$tag" ] && [ "$tag" != "manual" ]; then
        snapshot_name="snap_${timestamp}_${tag}.tar.gz"
    else
        snapshot_name="snap_${timestamp}.tar.gz"
    fi
    local snapshot_file="$SNAPSHOT_DIR/$snapshot_name"

    echo "📸 正在创建系统快照 [$snapshot_name]..."
    
    # 动态构建包含的项目，保证 requirements.txt 等新文件自动被打包
    local include_items=()
    for item in core config jobs bin requirements.txt README.md GEMINI.md AGENTS.md; do
        if [ -e "$PROJECT_DIR/$item" ]; then
            include_items+=("$item")
        fi
    done

    tar -czf "$snapshot_file" -C "$PROJECT_DIR" "${include_items[@]}" 2>/dev/null

    if [ $? -eq 0 ]; then
        local size=$(du -sh "$snapshot_file" | cut -f1)
        echo "✅ 快照创建完成！文件: $snapshot_name (大小: $size)"
        
        # 保留最近最多 MAX_SNAPSHOTS 个普通快照，排除文件名中带有 'keep' 的永久保留快照
        local normal_snapshots=($(ls -1t $SNAPSHOT_DIR/*.tar.gz 2>/dev/null | grep -v 'keep' || true))
        local count=${#normal_snapshots[@]}
        if [ "$count" -gt "$MAX_SNAPSHOTS" ]; then
            local remove_count=$((count - MAX_SNAPSHOTS))
            echo "🧹 普通快照总数 ($count) 超过上限 ($MAX_SNAPSHOTS)，自动清理最早的 $remove_count 个普通快照 (永久保留的快照将被忽略)..."
            printf "%s\n" "${normal_snapshots[@]}" | tail -n "$remove_count" | xargs rm -f
        fi
        echo "$snapshot_file"
    else
        echo "❌ 快照创建失败！"
        return 1
    fi
}

# 列出所有历史快照
list_snapshots() {
    echo "=== 📜 历史版本系统快照列表 (上限: ${MAX_SNAPSHOTS} 个) ==="
    if [ ! -d "$SNAPSHOT_DIR" ] || [ -z "$(ls -A $SNAPSHOT_DIR/*.tar.gz 2>/dev/null)" ]; then
        echo "暂无保存的系统快照。"
        return
    fi

    printf "%-35s %-12s %-20s\n" "快照名称" "大小" "创建时间"
    echo "------------------------------------------------------------------"
    for file in $(ls -t $SNAPSHOT_DIR/*.tar.gz 2>/dev/null); do
        local fname=$(basename "$file")
        local fsize=$(du -sh "$file" | cut -f1)
        local ftime=$(date -r "$file" "+%Y-%m-%d %H:%M:%S")
        printf "%-35s %-12s %-20s\n" "$fname" "$fsize" "$ftime"
    done
}

# 还原指定快照
restore_snapshot() {
    local target_name="$1"
    local target_file=""

    if [ -z "$target_name" ]; then
        # 默认选择最新的快照
        target_file=$(ls -t $SNAPSHOT_DIR/*.tar.gz 2>/dev/null | head -n 1)
        if [ -z "$target_file" ]; then
            echo "❌ 备份库中未找到可用于还原的快照！"
            return 1
        fi
        target_name=$(basename "$target_file")
        echo "ℹ️ 未指定快照名称，默认自动选择最新的快照: [$target_name]"
    else
        target_file="$SNAPSHOT_DIR/$target_name"
        if [ ! -f "$target_file" ]; then
            echo "❌ 找不到指定的快照文件: [$target_name]"
            return 1
        fi
    fi

    echo "🔄 正在从快照 [$target_name] 恢复系统..."

    # 1. 完整性校验：测试快照压缩包是否完好（防御解压失败破坏现存代码）
    if ! tar -tzf "$target_file" >/dev/null 2>&1; then
        echo "❌ 错误: 快照文件 [$target_name] 损坏或格式无效，恢复终止！"
        return 1
    fi

    # 2. 彻底消除“幽灵文件”：清空受快照管控的核心子目录
    echo "🧹 正在清理受控子目录，消除潜在的幽灵文件..."
    rm -rf "$PROJECT_DIR/core" \
           "$PROJECT_DIR/config" \
           "$PROJECT_DIR/jobs" \
           "$PROJECT_DIR/bin"

    # 3. 干净解压快照
    tar -xzf "$target_file" -C "$PROJECT_DIR"
    if [ $? -eq 0 ]; then
        echo "✅ 快照文件已完全解压并彻底还原。"
        
        # 4. 沙箱探针校验 (若 Python 虚拟环境存在)
        if [ -x "$PYTHON_BIN" ] && [ -f "$BOT_SCRIPT" ]; then
            echo "🧪 运行沙箱三级测试验证还原后的代码..."
            if "$PYTHON_BIN" "$BOT_SCRIPT" --test-sandbox; then
                echo "✅ 还原代码测试通过。"
            else
                echo "⚠️ 警告: 还原代码沙箱探针未完全通过，请检查代码或配置。"
            fi
        else
            echo "ℹ️ 未检测到可用的 Python 虚拟环境，跳过沙箱代码探针检测。"
        fi

        # 5. 重启服务 (若系统存在 systemctl 支持)
        if command -v systemctl >/dev/null 2>&1; then
            echo "🔄 正在刷新并重启关联 systemd 服务..."
            sudo systemctl daemon-reload 2>/dev/null || true
            sudo systemctl restart $SERVICE_BOT $SERVICE_ENGINE 2>/dev/null || true
            echo "🎉 系统服务重置重启指示完成！"
        else
            echo "ℹ️ 环境无 systemctl 支持，文件还原成功，请根据需要开启服务。"
        fi
    else
        echo "❌ 解压快照失败！"
        return 1
    fi
}

# Telegram API 应用层健康探针（检测进程假活/Polling 卡死）
check_telegram_api_health() {
"$PYTHON_BIN" -c "
import os, sys
from dotenv import load_dotenv
try:
    load_dotenv('$PROJECT_DIR/.env')
    token = os.getenv('TG_BOT_TOKEN')
    proxy = os.getenv('TG_PROXY', '')
    if not token:
        raise ValueError('TG_BOT_TOKEN not found in .env')
    import telebot
    from telebot import apihelper
    if proxy:
        apihelper.proxy = {'http': proxy, 'https': proxy}
    apihelper.CONNECT_TIMEOUT = 5
    apihelper.READ_TIMEOUT = 5
    bot = telebot.TeleBot(token)
    me = bot.get_me()
    print(f'API_OK:@{me.username}')
except Exception as e:
    print(f'API_FAIL:{e}', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null
    return $?
}

# 一键自救诊断与修复
rescue_system() {
    echo "=== 🚨 启动系统自救与自愈诊断 ==="
    local bot_active=true
    local engine_active=true
    local api_healthy=true

    if command -v systemctl >/dev/null 2>&1; then
        systemctl is-active --quiet $SERVICE_BOT || bot_active=false
        systemctl is-active --quiet $SERVICE_ENGINE || engine_active=false
    fi

    # 应用层探针：即使 systemd 报告 active，也要验证 Telegram API 是否真正可达
    if [ "$bot_active" = true ]; then
        echo "🔍 正在探测 Telegram API 应用层响应..."
        if check_telegram_api_health; then
            echo "  ✅ Telegram API 探针正常"
        else
            api_healthy=false
            echo "  ⚠️ Telegram API 探针超时/失败（服务假活）"
        fi
    fi

    if [ "$bot_active" = true ] && [ "$engine_active" = true ] && [ "$api_healthy" = true ]; then
        echo "✅ 诊断结果：所有服务与 API 探针全部正常！无须自救。"
        echo "（如需强制覆盖恢复，请使用 'manage.sh restore'）"
        return 0
    fi

    echo "⚠️ 诊断发现异常:"
    [ "$bot_active" = false ] && echo "  - ❌ $SERVICE_BOT 服务异常/崩溃"
    [ "$engine_active" = false ] && echo "  - ❌ $SERVICE_ENGINE 服务异常/崩溃"
    [ "$api_healthy" = false ] && echo "  - ❌ Telegram API 应用层无响应（进程假活）"

    echo "🚑 开始触发自动自救与稳态覆盖还原..."
    create_snapshot "before_rescue" >/dev/null 2>&1
    restore_snapshot
}

case "$1" in
    version)
        echo "=== ℹ️ Telegram 监控机器人当前运行版本 ==="
        "$PYTHON_BIN" -c "import sys; sys.path.append('$PROJECT_DIR/core'); from bot import VERSION; print(f'v{VERSION}')" 2>/dev/null || echo "未知版本"
        ;;
    backup|snapshot)
        create_snapshot "${2:-manual}"
        ;;
    backups|snapshots|list-snapshots)
        list_snapshots
        ;;
    restore)
        restore_snapshot "$2"
        ;;
    rescue|fix)
        rescue_system
        ;;
    test)
        CANDIDATE="${2:-$BOT_SCRIPT}"
        echo "=== 🧪 运行沙箱三级探针校验 ($CANDIDATE) ==="
        "$PYTHON_BIN" "$CANDIDATE" --test-sandbox
        ;;
    upgrade)
        NEW_SRC="$2"
        if [ -z "$NEW_SRC" ] || [ ! -f "$NEW_SRC" ]; then
            echo "❌ 错误: 请指定升级的目标 Python 源码文件路径。"
            echo "用法: $0 upgrade <path_to_new_bot.py>"
            exit 1
        fi

        echo "🚀 启动自动化发布与升级流程..."
        echo "1️⃣ 正在自动创建发布前完整快照..."
        create_snapshot "auto_before_upgrade"

        echo "2️⃣ 正在沙箱环境中校验候选代码..."
        if ! "$PYTHON_BIN" "$NEW_SRC" --test-sandbox; then
            echo "❌ 沙箱校验未通过！升级取消，保持线上生产版本不变。"
            exit 1
        fi

        echo "3️⃣ 正在覆盖部署新版本代码..."
        cp "$NEW_SRC" "$BOT_SCRIPT"

        echo "4️⃣ 正在重启机器人服务..."
        if command -v systemctl >/dev/null 2>&1; then
            sudo systemctl restart $SERVICE_BOT $SERVICE_ENGINE 2>/dev/null || true
        fi
        sleep 3

        echo "5️⃣ 正在检测重启后的在线健康状态..."
        if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet $SERVICE_BOT; then
            echo "✅ 升级成功！线上服务运行正常。"
            $0 version
        else
            echo "🚨 警告: 服务未激活或重启崩溃！启动一键自救恢复..."
            restore_snapshot
            exit 1
        fi
        ;;
    status)
        echo "=== 🤖 Telegram 监控与动态引擎服务状态 ==="
        if command -v systemctl >/dev/null 2>&1; then
            systemctl status $SERVICE_BOT $SERVICE_ENGINE
        else
            echo "当前系统未安装 systemctl。"
        fi
        ;;
    logs|log)
        echo "=== 📜 实时查看机器人运行日志 (Ctrl+C 退出) ==="
        if command -v journalctl >/dev/null 2>&1; then
            journalctl -u $SERVICE_BOT -f -n 30
        else
            echo "当前系统未安装 journalctl。"
        fi
        ;;
    restart)
        echo "🔄 正在重启服务..."
        if command -v systemctl >/dev/null 2>&1; then
            sudo systemctl restart $SERVICE_BOT $SERVICE_ENGINE 2>/dev/null || true
            echo "✅ 重启完成！"
        else
            echo "当前系统未安装 systemctl。"
        fi
        ;;
    stop)
        echo "🛑 正在停止服务..."
        if command -v systemctl >/dev/null 2>&1; then
            sudo systemctl stop $SERVICE_BOT $SERVICE_ENGINE 2>/dev/null || true
            echo "✅ 已停止！"
        else
            echo "当前系统未安装 systemctl。"
        fi
        ;;
    start)
        echo "▶️ 正在启动服务..."
        if command -v systemctl >/dev/null 2>&1; then
            sudo systemctl start $SERVICE_BOT $SERVICE_ENGINE 2>/dev/null || true
            echo "✅ 已启动！"
        else
            echo "当前系统未安装 systemctl。"
        fi
        ;;
    tts|speak)
        TEXT="${2:-你好，这是一条独立 TTS 流水线测试}"
        "$PYTHON_BIN" "$PROJECT_DIR/core/tts.py" "$TEXT"
        ;;
    stt|transcribe)
        FILE="${2:-/tmp/test_voice.ogg}"
        "$PYTHON_BIN" "$PROJECT_DIR/core/stt.py" "$FILE"
        ;;
    state|chat)
        echo "=== 💬 当前 Telegram 对话模式与持久化状态 ==="
        if [ -f "$STATE_FILE" ]; then
            cat "$STATE_FILE"
            echo ""
        else
            echo "暂无状态持久化记录。"
        fi
        ;;
    maintain|maintenance)
        echo "🧹 正在手动触发运行每周自动维保任务..."
        bash "$MAINTAIN_SCRIPT"
        ;;
    edit)
        nano $BOT_SCRIPT
        ;;
    *)
        echo "🤖 Telegram 监控与 AGY 机器人管理及自救系统"
        echo "用法: $0 {backup|backups|restore|rescue|test|upgrade|status|logs|restart|start|stop|state|maintain|edit}"
        echo ""
        echo "自救与快照指令:"
        echo "  $0 backup [tag]     - 📸 手动创建当前系统的完整打包快照 (上限保留 20 个)"
        echo "  $0 backups          - 📜 查看所有保存的历史版本快照"
        echo "  $0 restore [name]   - 🔄 手动还原指定快照 (不填参数默认还原最新健康快照)"
        echo "  $0 rescue           - 🚨 一键自救！服务崩溃时自动恢复稳态快照并自愈重启"
        echo ""
        echo "基础运维指令:"
        echo "  $0 version          - 查看机器人当前运行版本"
        echo "  $0 test [file]      - 运行沙箱 3 级探针校验 (依赖、Telegram API、AGY 引擎)"
        echo "  $0 upgrade file     - 自动快照 ➔ 校验新代码 ➔ 部署重启 ➔ 失败自动恢复"
        echo "  $0 status           - 查看机器人与动态任务引擎服务状态"
        echo "  $0 logs             - 查看机器人实时运行日志"
        echo "  $0 restart          - 重启所有关联服务"
        echo "  $0 state            - 查看 Telegram 用户的当前 Chat 模式与持久化状态"
        echo "  $0 maintain         - 手动立即触发自动维保"
        echo "  $0 edit             - 使用 nano 编辑机器人源码 (core/bot.py)"
        ;;
esac
