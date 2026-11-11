"""Command-line entry for the same atomic Python client."""

import argparse
import json
from pathlib import Path
import sys

from .client import DashScopeLLMClient, LLMError, build_messages, extract_text


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="百炼 LLM 原子工具（.env 配置，Python 标准库）")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--prompt", help="用户提示词")
    source.add_argument("--prompt-file", type=Path, help="UTF-8 提示词文件；- 表示标准输入")
    source.add_argument("--messages-file", type=Path, help="完整 messages JSON 数组，支持多轮对话")
    parser.add_argument("--system-prompt", help="系统提示词；空字符串禁用 .env 中的默认提示词")
    parser.add_argument("--env-file", type=Path, help="指定环境文件，默认项目根目录 .env")
    parser.add_argument("--base-url", help="LLM OpenAI 兼容接口根地址")
    parser.add_argument("--model")
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--timeout", type=float, help="连接/读取超时秒数")
    parser.add_argument("--thinking", choices=["true", "false"], help="覆盖思考开关")
    parser.add_argument("--extra-body", type=Path, help="额外请求参数 JSON 文件，如 response_format/tools")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stream", action="store_true", help="逐段输出正文")
    mode.add_argument("--json", action="store_true", help="输出完整响应 JSON，包含 usage")
    parser.add_argument("--output", type=Path, help="将与标准输出相同的内容保存到文件")
    parser.add_argument("--dry-run", action="store_true", help="只预览 URL 和请求体，不需要 API Key")
    args = parser.parse_args(argv)
    try:
        if args.messages_file is not None and args.system_prompt is not None:
            raise ValueError("--messages-file 已包含完整消息，请直接在文件中设置 system 消息")
        client = DashScopeLLMClient(
            env_file=args.env_file, base_url=args.base_url, model=args.model,
            timeout=args.timeout, temperature=args.temperature, max_tokens=args.max_tokens,
            enable_thinking=None if args.thinking is None else args.thinking == "true",
        )
        if args.messages_file is not None:
            messages = json.loads(args.messages_file.read_text(encoding="utf-8-sig"))
        else:
            prompt = args.prompt
            if args.prompt_file is not None:
                prompt = sys.stdin.read() if str(args.prompt_file) == "-" else args.prompt_file.read_text(encoding="utf-8-sig")
            system = client.config.system_prompt if args.system_prompt is None else args.system_prompt
            messages = build_messages(prompt, system)
        options = {}
        if args.extra_body is not None:
            options["extra_body"] = json.loads(args.extra_body.read_text(encoding="utf-8-sig"))
        payload = client.build_payload(messages, stream=args.stream, **options)
        if args.dry_run:
            output = json.dumps({"url": client.endpoint, "body": payload}, ensure_ascii=False, indent=2)
        elif args.stream:
            parts = []
            for chunk in client.stream_chat(messages, **options):
                if chunk["choices"]:
                    text = chunk["choices"][0]["delta"].get("content")
                    if text:
                        if not isinstance(text, str):
                            raise ValueError("流式正文必须为字符串")
                        print(text, end="", flush=True)
                        parts.append(text)
            output = "".join(parts)
            print()
        else:
            result = client.chat(messages, **options)
            output = json.dumps(result, ensure_ascii=False, indent=2) if args.json else extract_text(result)
        if args.output is not None:
            path = args.output.expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(output + "\n", encoding="utf-8")
        if not args.stream or args.dry_run:
            print(output)
        return 0
    except (LLMError, ValueError, OSError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
