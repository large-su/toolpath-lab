#!/usr/bin/env python3
"""AI coding assistant for ToolpathLab development.

A minimal, dependency-free CLI tool that wraps an OpenAI-compatible
chat-completions API (Doubao / DeepSeek / etc.) so that development
questions can be asked in natural language.

Usage:
    set LLM_API_KEY=your-key [LLM_BASE_URL=https://... LLM_MODEL=doubao-...]
    python examples/ai_coder.py "如何新增一种刀具类型"
    python examples/ai_coder.py --interactive
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = "doubao-1-5-pro-32k-250115"


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {os.environ.get('LLM_API_KEY', '')}",
    }


def ask(question: str, base_url: str, model: str) -> str:
    """Send one question to the LLM and return the assistant reply."""
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是 ToolpathLab 刀路规划项目的 AI 编程助手，"
                           "回答要简洁、可操作，涉及代码时给出可直接运行的代码。",
            },
            {"role": "user", "content": question},
        ],
        "temperature": 0.3,
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


def main() -> int:
    parser = argparse.ArgumentParser(description="ToolpathLab AI 编程助手")
    parser.add_argument("question", nargs="*", help="要问的编程问题（可省略，进入交互模式）")
    parser.add_argument("--interactive", action="store_true", help="进入连续问答模式")
    args = parser.parse_args()

    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        print("未配置 API Key。")
        print("  1) 在豆包开放平台 / DeepSeek 开放平台获取密钥后：")
        print("     set LLM_API_KEY=你的密钥")
        print("  2) 也可以设置 LLM_BASE_URL / LLM_MODEL 指定服务商与模型。")
        print("  3) 没有密钥时，可直接用豆包对话客户端完成编程问答，本工具作为框架演示。")
        return 1

    base_url = os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    model = os.environ.get("LLM_MODEL", DEFAULT_MODEL)

    if args.interactive:
        print(f"AI 编程助手已启动（模型: {model}）。输入 exit 退出。")
        while True:
            question = input("\n你: ").strip()
            if not question:
                continue
            if question.lower() in {"exit", "quit", "q"}:
                break
            print("\n助手:", end=" ")
            try:
                print(ask(question, base_url, model))
            except Exception as error:  # noqa: BLE001 - 统一把网络/接口错误展示给用户
                print(f"请求失败: {error}")
        return 0

    if not args.question:
        parser.print_help()
        return 1

    try:
        print(ask(" ".join(args.question), base_url, model))
    except Exception as error:  # noqa: BLE001
        print(f"请求失败: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
