#!/usr/bin/env bash
# 一键安装部署与系统服务配置脚本
set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_ROOT/venv"

echo "======================================"
echo "🚀 开始安装 tg-monitor 监控机器人"
echo "======================================"

# 1. 检查并创建 .env 文件
if [ ! -f "$PROJECT_ROOT/.env" ]; then
    echo "⚠️ 发现未配置 .env 文件。"
    cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
    echo "请根据提示输入你的配置信息："
    
    read -p "请输入 Telegram Bot Token: " bot_token
    read -p "请输入你的 Telegram Chat ID (Admin UID): " chat_id
    read -p "请输入网络代理 (例如 http://127.0.0.1:10809，回车跳过设置): " proxy
    proxy=${proxy:-}

    sed -i "s|TG_BOT_TOKEN=.*|TG_BOT_TOKEN=\"$bot_token\"|" "$PROJECT_ROOT/.env"
    sed -i "s|TG_CHAT_ID=.*|TG_CHAT_ID=\"$chat_id\"|" "$PROJECT_ROOT/.env"
    sed -i "s|TG_PROXY=.*|TG_PROXY=\"$proxy\"|" "$PROJECT_ROOT/.env"
    echo "✅ .env 文件已生成并保存！"
else
    echo "✅ 发现已存在的 .env 文件，跳过配置。"
fi

# 2. 安装 Python 虚拟环境与依赖
echo "📦 正在安装 Python 依赖..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install -r "$PROJECT_ROOT/requirements.txt"

# 3. 创建 Systemd 服务文件
echo "⚙️ 正在配置 Systemd 服务..."
cat <<EOF | sudo tee /etc/systemd/system/tg-monitor.service > /dev/null
[Unit]
Description=Telegram Monitor Bot Service
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$PROJECT_ROOT
Environment="PATH=$VENV_DIR/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ExecStart=$VENV_DIR/bin/python3 $PROJECT_ROOT/core/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat <<EOF | sudo tee /etc/systemd/system/tg-task-engine.service > /dev/null
[Unit]
Description=Telegram Monitor Task Engine Service
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$PROJECT_ROOT
Environment="PATH=$VENV_DIR/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ExecStart=$VENV_DIR/bin/python3 $PROJECT_ROOT/core/task_engine.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 4. 重新加载守护进程并启动服务
echo "🔄 正在重启系统服务..."
sudo systemctl daemon-reload
sudo systemctl enable tg-monitor.service tg-task-engine.service
sudo systemctl restart tg-monitor.service tg-task-engine.service

echo "======================================"
echo "🎉 安装完成！所有服务正在后台运行。"
echo "可以使用以下命令查看日志："
echo "journalctl -u tg-monitor.service -f"
echo "journalctl -u tg-task-engine.service -f"
echo "======================================"
