#!/usr/bin/env python3
"""
文档读取 (core/docread.py)

把 Office / PDF / EPUB 等文档转成 Markdown，供 agy 读取内容。

**这是"读"，不是"转换"。** 只有 任意格式 → Markdown 这一个方向：
要产出 docx/html 用 pandoc，要产出 PDF 用 soffice。

底层是 firecrawl-anydoc（Rust 核心 + PyO3 原生 wheel）。选 Python 包而不是
npm 包，是因为 npm 那份是 Node N-API 插件，必须有 node 才能跑，而 `npm i -g`
会落进 nvm 的版本目录 —— 那个路径不在 systemd 下 bot 给 agy 的 PATH 上。
Python 包由 requirements.txt 管，install.sh 和快照还原都已经覆盖它。

命令行：
    ./venv/bin/python core/docread.py 报告.docx
    ./venv/bin/python core/docread.py 课表.xlsx -o 课表.md
"""

import argparse
import os
import sys

try:
    import anydoc
except ImportError:  # pragma: no cover - 只在依赖没装好时走到
    anydoc = None

# 读不了时给出的替代路线。扫描件走 OCR，纯文本层 PDF 的表格 pdftotext 更强。
_FALLBACK_HINT = {
    "UnsupportedError": (
        "这个格式读不了，或者文件里没有可提取的文本层（扫描件、纯图片 PDF）。"
        "扫描件请走 WeChat OCR（POST http://127.0.0.1:5000/ocr），"
        "PDF 可以先 pdftoppm 转成图片再逐页 OCR；"
        "如果它本来就是纯文本/HTML/Markdown，直接 cat 就行，不必经过这里。"
    ),
    "EncryptedError": "文件有密码保护，需要用户提供密码后才能读。",
    "MalformedError": "文件结构损坏，读不出来。可以试试 soffice 打开另存一份再读。",
    "MissingPartError": "文件缺少必要的内部组件，可能在传输中损坏了。",
    "ResourceLimitError": "文件超出解析器的资源上限，太大或嵌套太深。",
}


class DocReadError(Exception):
    """读不了，且已经附上人话解释与可行的替代做法。"""


def to_markdown(path):
    """把文档读成 Markdown 字符串。

    读不了时抛 DocReadError，消息里带上为什么读不了、该走哪条替代路线 ——
    agy 拿到的必须是能据以决定下一步的信息，而不是一个异常类名。
    """
    if anydoc is None:
        raise DocReadError(
            "firecrawl-anydoc 未安装，请在项目根目录执行 "
            "./venv/bin/pip install -r requirements.txt"
        )
    if not os.path.isfile(path):
        raise DocReadError(f"文件不存在: {path}")
    try:
        return anydoc.to_markdown(path)
    except Exception as e:
        name = type(e).__name__
        hint = _FALLBACK_HINT.get(name)
        raise DocReadError(f"{os.path.basename(path)} 读不了（{name}）：{hint or e}") from e


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="把文档读成 Markdown（docx/doc/xlsx/pptx/odt/rtf/epub/csv/pdf）"
    )
    parser.add_argument("file", help="要读的文档路径")
    parser.add_argument("-o", "--output", help="写入此文件，默认打到 stdout")
    args = parser.parse_args(argv)

    try:
        md = to_markdown(args.file)
    except DocReadError as e:
        print(str(e), file=sys.stderr)
        return 1

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(md)
    else:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
