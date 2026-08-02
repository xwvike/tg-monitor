#!/usr/bin/env python3
"""
Claude Code RSS 自动监控与中文翻译脚本 (check_claude_rss.py)
具备：超时防护(15s)、网络异常报错、历史记录去重、AGY 智能翻译
"""

import logging
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

import requests

from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env_path = os.path.join(PROJECT_ROOT, ".env")
if os.path.exists(env_path):
    load_dotenv(env_path)

RSS_URL = "https://code.claude.com/docs/en/whats-new/rss.xml"
STATE_FILE = os.path.join(PROJECT_ROOT, "config", "last_claude_rss.guid")
AGY_BIN = os.path.expanduser("~/.local/bin/agy")
PROXY = os.getenv("TG_PROXY")

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")


def get_last_guid():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def save_last_guid(guid):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(guid)


def fetch_rss():
    proxies = {"http": PROXY, "https": PROXY} if PROXY else None
    try:
        resp = requests.get(RSS_URL, proxies=proxies, timeout=15)
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.RequestException as e:
        print(f"⚠️ [Claude Code RSS] 抓取失败/网络异常: {e}")
        sys.exit(1)


def parse_latest_item(xml_text):
    try:
        root = ET.fromstring(xml_text)
        channel = root.find("channel")
        if channel is None:
            return None
        items = channel.findall("item")
        if not items:
            return None

        latest = items[0]
        title = latest.findtext("title", "")
        link = latest.findtext("link", "")
        guid = latest.findtext("guid", title)
        pub_date = latest.findtext("pubDate", "")

        # 兼容 content:encoded 或 description
        content = ""
        for child in latest:
            if child.tag.endswith("encoded") or child.tag == "description":
                content = child.text or ""
                break

        return {
            "guid": guid,
            "title": title,
            "link": link,
            "pubDate": pub_date,
            "content": content,
        }
    except Exception as e:
        print(f"⚠️ [Claude Code RSS] XML 解析失败: {e}")
        sys.exit(1)


def translate_with_agy(item_data):
    raw_text = f"Title: {item_data['title']}\nLink: {item_data['link']}\nDate: {item_data['pubDate']}\nContent:\n{item_data['content']}"
    prompt = (
        "请将以下 Claude Code 最新周报更新日志翻译并提炼成精美、专业、简洁的中文 Markdown 简报。\n"
        "保留版本号、核心功能亮点和改进细节。不需要废话，直接输出 Markdown 内容。\n\n"
        f"{raw_text}"
    )

    try:
        res = subprocess.run(
            [AGY_BIN, "--prompt", prompt, "--dangerously-skip-permissions"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
        else:
            return f"<b>{item_data['title']}</b>\n\n{item_data['content']}"
    except Exception as e:
        print(f"AGY 翻译发生异常，回退原始文本: {e}")
        return f"<b>{item_data['title']}</b>\n\n{item_data['content']}"


def main():
    xml_text = fetch_rss()
    item = parse_latest_item(xml_text)

    if not item:
        print("未在 RSS 中解析到任何有效文章条目。")
        return

    last_guid = get_last_guid()
    if item["guid"] == last_guid:
        print(f"未检测到新更新 (当前最新 GUID: {last_guid})")
        return

    print(f"🎉 发现 Claude Code 新版本更新: {item['title']}")

    # 自动进行翻译
    translated_content = translate_with_agy(item)

    # 输出给调度引擎以便发送 Telegram 通知
    print(
        f"🚀 [Claude Code 最新更新周报]\n\n{translated_content}\n\n🔗 链接: {item['link']}"
    )

    # 记入状态防止重复推送
    save_last_guid(item["guid"])


if __name__ == "__main__":
    main()
