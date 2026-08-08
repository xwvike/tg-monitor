#!/usr/bin/env bash
# ==============================================================================
# tg-monitor 一键安装 / 审计脚本
#
#   ./install.sh          安装或就地修复（幂等，可重复执行）
#   ./install.sh --check   只审计当前部署，不改动任何东西
#
# 设计原则：
#   1. **线上环境必须完全由本脚本产出**。之前的版本只装 Python 依赖，
#      systemd unit 缺代理、不装系统工具链、不建软链 —— 结果线上是手工
#      改出来的，换台机器根本起不来。
#   2. **脚本适配环境，不要求用户改造环境**。安装时用交互式 sudo 装服务是
#      合理的（人就在终端前）；但绝不写入 /etc/sudoers.d 去永久放宽用户的
#      安全策略。运行时的重启由 manage.sh 的无特权降级方案完成。
#      只有在确实无法适配时才告知用户，由用户自行决定是否调整环境。
# ==============================================================================
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_ROOT/venv"
ENV_FILE="$PROJECT_ROOT/.env"
AGY_BIN="${AGY_BIN:-$HOME/.local/bin/agy}"
SYMLINK="/usr/local/bin/tg-bot"
SUDOERS_FILE="/etc/sudoers.d/tg-monitor"
UNIT_BOT="/etc/systemd/system/tg-monitor.service"
UNIT_ENGINE="/etc/systemd/system/tg-task-engine.service"

# 文件处理流水线所依赖的系统工具链（对应 config/TOOLCHAIN.md）
# 缺任何一个，Layer 3 的文件处理能力就是残废的
declare -A TOOLCHAIN=(
    [ffmpeg]=ffmpeg
    [ffprobe]=ffmpeg
    [convert]=imagemagick
    [identify]=imagemagick
    [magick]=imagemagick
    [pngquant]=pngquant
    [pandoc]=pandoc
    [pdftotext]=poppler-utils
    [pdfinfo]=poppler-utils
    [pdfimages]=poppler-utils
    [pdftoppm]=poppler-utils
    [gs]=ghostscript
    [soffice]=libreoffice-core-nogui
    [unar]=unar
    [lsar]=unar
    [zip]=zip
    [unzip]=unzip
    [zstd]=zstd
)

# 有些能力**无法靠"命令是否存在"判断**：soffice 二进制由 core 提供，
# 但缺了 calc/writer/impress 组件就读不了对应格式，而 libreoffice
# 报错时仍返回 0 —— 只会静默产不出文件。这类必须直接查包。
declare -a REQUIRED_PKGS=(
    libreoffice-calc      # xls / xlsx / csv
    libreoffice-writer    # doc / docx / odt
    libreoffice-impress   # ppt / pptx
)

CHECK_ONLY=false
[ "${1:-}" = "--check" ] && CHECK_ONLY=true

PASS=0
WARN=0
FAIL=0
ok()   { echo "  ✅ $1"; PASS=$((PASS + 1)); }
warn() { echo "  ⚠️  $1"; WARN=$((WARN + 1)); }
bad()  { echo "  ❌ $1"; FAIL=$((FAIL + 1)); }
step() { echo; echo "▶ $1"; }

# ------------------------------------------------------------------------------
# systemd unit 内容生成（安装与审计共用同一份定义，杜绝二者漂移）
#
# 设计取向：**unit 只负责"把进程拉起来"，不承载任何配置**。
# 全部配置集中在 .env，由应用自己 load_dotenv 读取：
#   - 代理：应用从 TG_PROXY 读取，并自行为 agy 子进程构造 HTTP_PROXY/NO_PROXY
#   - PATH：ExecStart 与 agy 均用绝对路径；ffmpeg/convert 等在系统默认 PATH 内
# 把参数堆进 unit 会造成两个真相源：改配置要同时动 .env 和 unit，且必须
# daemon-reload —— 线上那两个手工改出来的 unit 就是这么漂移的。
# ------------------------------------------------------------------------------
render_unit() {
    local desc="$1" script="$2"
    cat <<EOF
[Unit]
Description=$desc
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$PROJECT_ROOT
ExecStart=$VENV_DIR/bin/python3 $PROJECT_ROOT/$script
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

# 运行时特权探测。
#
# 本项目**不要求**免密 sudo：manage.sh 已内置无特权降级方案
# （结束自身进程，交由 systemd Restart=always 拉起）。
# 这里只做探测与告知，绝不擅自修改用户的 sudoers 配置 ——
# 那是永久改变用户系统的安全策略，应当由用户自己决定。
runtime_privilege() {
    if [ "$(id -u)" -eq 0 ]; then echo root
    elif sudo -n true 2>/dev/null; then echo nopasswd
    else echo none
    fi
}

print_optional_sudoers() {
    cat <<EOF

     若你希望 Telegram 端的自救/重启走标准 systemctl 路径（可选，非必需），
     可自行创建 $SUDOERS_FILE，内容如下（务必用 visudo 校验）：
     ------------------------------------------------------------------
     Cmnd_Alias TGMON_SVC = /usr/bin/systemctl restart tg-monitor.service tg-task-engine.service
     Cmnd_Alias TGMON_LOG = /usr/bin/journalctl --vacuum-time=*
     $USER ALL=(root) NOPASSWD: TGMON_SVC, TGMON_LOG
     ------------------------------------------------------------------
EOF
}

# sudo 默认 env_reset，而 Debian 的 sudoers 里
#   #Defaults:%sudo env_keep += "http_proxy https_proxy ..."
# 是**注释掉的** —— 于是 `sudo apt-get` 会丢掉用户环境里的代理设置，
# 在必须走代理才能出网的机器上静默转为直连并卡死。这里显式透传。
run_apt() {
    sudo env \
        ${http_proxy:+http_proxy="$http_proxy"} \
        ${https_proxy:+https_proxy="$https_proxy"} \
        ${HTTP_PROXY:+HTTP_PROXY="$HTTP_PROXY"} \
        ${HTTPS_PROXY:+HTTPS_PROXY="$HTTPS_PROXY"} \
        ${no_proxy:+no_proxy="$no_proxy"} \
        ${NO_PROXY:+NO_PROXY="$NO_PROXY"} \
        DEBIAN_FRONTEND=noninteractive \
        apt-get "$@"
}

get_proxy() {
    [ -f "$ENV_FILE" ] || return 0
    # 只取 TG_PROXY 一行并剥掉引号，避免 `export $(cat .env)` 那种遇空格就碎的写法
    sed -n 's/^TG_PROXY=//p' "$ENV_FILE" | head -n1 | tr -d '"'"'"
}

# ==============================================================================
# 审计模式
# ==============================================================================
if $CHECK_ONLY; then
    echo "======================================"
    echo "🔍 tg-monitor 部署审计"
    echo "   项目根目录: $PROJECT_ROOT"
    echo "======================================"

    step "系统工具链 (config/TOOLCHAIN.md 声明的能力)"
    for cmd in "${!TOOLCHAIN[@]}"; do
        if command -v "$cmd" >/dev/null 2>&1; then
            ok "$cmd"
        else
            bad "$cmd 缺失 (apt 包: ${TOOLCHAIN[$cmd]}) — 文件处理相关能力将不可用"
        fi
    done

    step "无法由命令名判断的组件包"
    for pkg in "${REQUIRED_PKGS[@]}"; do
        if dpkg -s "$pkg" >/dev/null 2>&1; then
            ok "$pkg"
        else
            bad "$pkg 缺失 — soffice 存在但读不了对应格式，且失败时仍返回 0"
        fi
    done

    # 视频硬编解码（可选加速，非硬性依赖）。
    # 缺了只是转码回落到 CPU 软编 —— 功能不受影响，所以一律 warn 不 bad。
    # 加组会永久改动用户权限，按本脚本原则 2 只告知、不代劳。
    step "视频硬件加速 (核显, 可选)"
    if [ ! -e /dev/dri/renderD128 ]; then
        warn "无 /dev/dri/renderD128 — 本机无可用核显, 转码只能走 CPU 软编"
    else
        if dpkg -s intel-media-va-driver >/dev/null 2>&1 \
           || dpkg -s intel-media-va-driver-non-free >/dev/null 2>&1; then
            ok "VA-API 驱动已装"
        else
            warn "缺 VA-API 驱动 — 装: sudo apt install intel-media-va-driver"
        fi
        # 注意: 本脚本 set -o pipefail, 不能用 `cmd | grep -q`。grep -q 一匹配就
        # 关掉管道读端, 左边进程吃 SIGPIPE 以非零退出, pipefail 会把整条管道判为
        # 失败 —— 明明匹配上了却走 else 分支。所以先取回全部输出再做字符串匹配。
        _groups="$(id -nG "$USER" 2>/dev/null || true)"
        if [[ " $_groups " == *" render "* ]]; then
            ok "$USER 在 render 组"
        else
            warn "$USER 不在 render 组 — 服务访问不到 /dev/dri/renderD128, 硬编不可用"
            echo "     修复: sudo usermod -aG render $USER 然后重启服务"
        fi
        _encoders="$(ffmpeg -hide_banner -encoders 2>/dev/null || true)"
        if [[ "$_encoders" == *h264_vaapi* ]]; then
            ok "ffmpeg 带 h264_vaapi 编码器"
        else
            warn "ffmpeg 未编译 VAAPI 支持 — 硬编不可用"
        fi
    fi

    step "AGY 智能体引擎"
    if [ -x "$AGY_BIN" ]; then
        ok "agy 已安装: $("$AGY_BIN" --version 2>&1 | head -n1)"
    else
        bad "未在 $AGY_BIN 找到 agy — Layer 3 全部功能不可用"
    fi

    step "Python 环境"
    if [ -x "$VENV_DIR/bin/python3" ]; then
        ok "venv 存在: $("$VENV_DIR/bin/python3" --version)"
        # 注意：--dry-run 的退出码只表示"能装得上"，缺包时照样是 0 ——
        # 直接判返回码等于什么都没查。真正的信号是它打不打算装东西。
        missing=$("$VENV_DIR/bin/pip" install -r "$PROJECT_ROOT/requirements.txt" \
                    --dry-run 2>/dev/null | sed -n 's/^Would install //p')
        if [ -z "$missing" ]; then
            ok "requirements.txt 依赖齐全"
        else
            bad "依赖缺失: $missing —— 请重跑 ./install.sh"
        fi
    else
        bad "venv 缺失"
    fi

    step "配置文件"
    if [ -f "$ENV_FILE" ]; then
        ok ".env 存在"
        for key in TG_BOT_TOKEN TG_CHAT_ID; do
            if grep -q "^$key=..*" "$ENV_FILE"; then ok "$key 已配置"; else bad "$key 未配置"; fi
        done
        proxy=$(get_proxy)
        [ -n "$proxy" ] && ok "TG_PROXY = $proxy" || warn "TG_PROXY 未设置（无代理环境下 agy 可能不可用）"
    else
        bad ".env 缺失"
    fi

    step "Systemd 服务单元（与本脚本应产出的内容比对）"
    for pair in "$UNIT_BOT:Telegram Monitor Bot Service:core/bot.py" \
                "$UNIT_ENGINE:Telegram Monitor Task Engine Service:core/task_engine.py"; do
        unit="${pair%%:*}"; rest="${pair#*:}"; desc="${rest%%:*}"; script="${rest##*:}"
        if [ ! -f "$unit" ]; then
            bad "$(basename "$unit") 不存在"
        elif diff -q <(render_unit "$desc" "$script") "$unit" >/dev/null 2>&1; then
            ok "$(basename "$unit") 与脚本定义一致"
        else
            warn "$(basename "$unit") 与脚本定义已漂移（线上是手工改过的）"
            diff <(render_unit "$desc" "$script") "$unit" | sed 's/^/       /' || true
        fi
    done

    step "运维入口"
    if [ -L "$SYMLINK" ] && [ "$(readlink -f "$SYMLINK")" = "$PROJECT_ROOT/bin/manage.sh" ]; then
        ok "tg-bot 软链正确"
    else
        bad "$SYMLINK 缺失或指向错误"
    fi

    # 软链指对了不代表能用：manage.sh 曾用 $0 而非 readlink -f 推导项目根，
    # 经软链调用时解析成 /usr/local，backup/restore/rescue/test 全废，而
    # status 照常工作所以毫无征兆。还原旧快照会把这个缺陷装回来，因此这里
    # 必须实际执行一次软链、看它自己解析出的根，而不是只看软链指向。
    # 先整段捕获再解析：本脚本是 set -euo pipefail，若直接把 bash -x 管进
    # grep -m1，grep 命中即退出会给上游发 SIGPIPE，整个 --check 会从这里
    # 静默截断（退出码 141），后面的沙箱校验压根不跑却显得一切正常。
    if [ -L "$SYMLINK" ]; then
        trace=$(cd / && bash -x "$SYMLINK" backups 2>&1 || true)
        resolved=$(grep -m1 '^+ PROJECT_DIR=' <<<"$trace" | cut -d= -f2- || true)
        if [ "$(readlink -f "${resolved:-/nonexistent}")" = "$(readlink -f "$PROJECT_ROOT")" ]; then
            ok "tg-bot 经软链调用时项目根解析正确"
        else
            bad "tg-bot 经软链调用时项目根解析为 '${resolved:-空}'，自救命令将全部失效"
        fi
    fi

    case "$(runtime_privilege)" in
        root)     ok "以 root 运行，服务控制无需额外配置" ;;
        nopasswd) ok "检测到免密 sudo，服务控制走标准 systemctl 路径" ;;
        none)
            ok "无免密 sudo — 已启用无特权降级方案（结束自身进程 + Restart=always）"
            echo "       tg-bot restart / 自救可正常工作；tg-bot start|stop 需在终端手动提权。"
            ;;
    esac

    step "服务运行状态"
    for svc in tg-monitor.service tg-task-engine.service; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then ok "$svc 运行中"; else bad "$svc 未运行"; fi
    done

    step "沙箱四级校验"
    if [ -x "$VENV_DIR/bin/python3" ] && "$VENV_DIR/bin/python3" \
         "$PROJECT_ROOT/core/bot.py" --test-sandbox >/dev/null 2>&1; then
        ok "语法 / 单元测试 / Telegram API / AGY 探针 全部通过"
    else
        bad "沙箱校验未通过，执行 'tg-bot test' 查看详情"
    fi

    echo
    echo "======================================"
    echo "审计结果: ✅ $PASS 通过   ⚠️ $WARN 警告   ❌ $FAIL 失败"
    echo "======================================"
    [ "$FAIL" -eq 0 ] || exit 1
    exit 0
fi

# ==============================================================================
# 安装模式
# ==============================================================================
echo "======================================"
echo "🚀 安装 tg-monitor"
echo "   项目根目录: $PROJECT_ROOT"
echo "======================================"

step "[1/7] 前置检查"
command -v apt-get >/dev/null 2>&1 || {
    echo "❌ 本脚本目前只支持 Debian/Ubuntu 系 (需要 apt-get)。"
    echo "   其他发行版请手动安装: ${TOOLCHAIN[*]} 并跳过本步骤。"
    exit 1
}
command -v sudo >/dev/null 2>&1 || { echo "❌ 缺少 sudo"; exit 1; }
ok "apt-get / sudo 就绪"

step "[2/7] 安装系统工具链"
missing_pkgs=()
for cmd in "${!TOOLCHAIN[@]}"; do
    command -v "$cmd" >/dev/null 2>&1 || missing_pkgs+=("${TOOLCHAIN[$cmd]}")
done
for pkg in "${REQUIRED_PKGS[@]}"; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing_pkgs+=("$pkg")
done
# 去重
if [ ${#missing_pkgs[@]} -gt 0 ]; then
    mapfile -t missing_pkgs < <(printf '%s\n' "${missing_pkgs[@]}" | sort -u)
    echo "  需要安装: ${missing_pkgs[*]}"
    run_apt update -qq
    # --no-install-recommends：imagemagick/ffmpeg 的推荐依赖会拖进 mesa、GTK、
    # X11 等整套图形栈（361 → 255 个包），在无头服务器上纯属浪费且拖慢部署。
    # 真正需要的编解码库都是 Depends，不受影响。
    run_apt install -y --no-install-recommends "${missing_pkgs[@]}"
    ok "系统工具链安装完成"

    # 装完立刻验证二进制真的可用（避免 --no-install-recommends 漏掉关键依赖）
    for cmd in "${!TOOLCHAIN[@]}"; do
        command -v "$cmd" >/dev/null 2>&1 || bad "$cmd 安装后仍不可用，请手动检查 ${TOOLCHAIN[$cmd]}"
    done
else
    ok "系统工具链已齐全"
fi

step "[3/7] 配置 .env"
if [ ! -f "$ENV_FILE" ]; then
    cp "$PROJECT_ROOT/.env.example" "$ENV_FILE"
    read -r -p "  Telegram Bot Token: " bot_token
    read -r -p "  你的 Telegram Chat ID (管理员 UID): " chat_id
    read -r -p "  网络代理 (如 http://127.0.0.1:10809，回车跳过): " proxy
    sed -i "s|^TG_BOT_TOKEN=.*|TG_BOT_TOKEN=\"$bot_token\"|" "$ENV_FILE"
    sed -i "s|^TG_CHAT_ID=.*|TG_CHAT_ID=\"$chat_id\"|" "$ENV_FILE"
    sed -i "s|^TG_PROXY=.*|TG_PROXY=\"${proxy:-}\"|" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    ok ".env 已生成"
else
    chmod 600 "$ENV_FILE"
    ok ".env 已存在，保留原有配置"
fi
PROXY_URL=$(get_proxy)   # 仅用于安装期回显，不写进 unit

step "[4/7] Python 虚拟环境与依赖"
[ -d "$VENV_DIR" ] || python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install -q --upgrade pip
"$VENV_DIR/bin/pip" install -q -r "$PROJECT_ROOT/requirements.txt"
ok "Python 依赖就绪"

step "[5/7] 检查 AGY 智能体引擎"
if [ -x "$AGY_BIN" ]; then
    ok "agy 已安装: $("$AGY_BIN" --version 2>&1 | head -n1)"
else
    warn "未在 $AGY_BIN 找到 agy"
    echo "     Layer 3（对话、多模态、文件处理）将全部不可用。"
    echo "     请先安装 Google Antigravity CLI 并完成 'agy' 登录授权，再重跑本脚本。"
fi

step "[6/7] 安装 systemd 服务与运维入口"
render_unit "Telegram Monitor Bot Service" "core/bot.py" | sudo tee "$UNIT_BOT" >/dev/null
render_unit "Telegram Monitor Task Engine Service" "core/task_engine.py" \
    | sudo tee "$UNIT_ENGINE" >/dev/null
ok "systemd 服务单元已写入（仅负责拉起进程，配置全在 .env）"

sudo ln -sfn "$PROJECT_ROOT/bin/manage.sh" "$SYMLINK"
sudo chmod +x "$PROJECT_ROOT/bin/manage.sh"
ok "运维入口: tg-bot -> bin/manage.sh"

case "$(runtime_privilege)" in
    root|nopasswd)
        ok "运行时特权充足，服务控制走标准 systemctl 路径"
        ;;
    none)
        ok "运行时无免密 sudo — 使用内置的无特权降级方案，无需改动你的系统配置"
        echo "     （tg-bot restart 与 Telegram 端自救会通过"
        echo "       结束自身进程 + systemd Restart=always 完成重启）"
        print_optional_sudoers
        ;;
esac

step "[7/7] 启动前沙箱校验"
if "$VENV_DIR/bin/python3" "$PROJECT_ROOT/core/bot.py" --test-sandbox; then
    ok "沙箱四级校验通过"
else
    echo "❌ 沙箱校验未通过，已中止启动以免部署坏版本。"
    exit 1
fi

sudo systemctl daemon-reload
sudo systemctl enable -q tg-monitor.service tg-task-engine.service
sudo systemctl restart tg-monitor.service tg-task-engine.service
sleep 3

echo
echo "======================================"
for svc in tg-monitor.service tg-task-engine.service; do
    systemctl is-active --quiet "$svc" && ok "$svc 运行中" || bad "$svc 启动失败"
done
echo "======================================"
echo "🎉 安装完成。常用命令："
echo "   tg-bot status          查看服务状态"
echo "   tg-bot logs            实时日志"
echo "   tg-bot test            沙箱四级校验"
echo "   ./install.sh --check   审计当前部署"
echo "======================================"
[ "$FAIL" -eq 0 ]
