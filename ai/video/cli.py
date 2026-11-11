"""Command line for generation primitives and task lifecycle."""

import argparse
import json
from pathlib import Path
import sys

from .client import DashScopeError, DashScopeVideoClient, _video_url, download_video
from .payloads import (
    T2V_MODEL,
    WAN_MODEL,
    WAN_MODELS,
    build_frame_video_payload,
    build_image_video_payload,
    build_reference_video_payload,
    build_text_video_payload,
    build_video_edit_payload,
    build_multimodal_reference_payload,
)


def _connection(parser):
    parser.add_argument("--env-file", type=Path, help="环境文件，默认项目根目录 .env")
    parser.add_argument("--base-url", help="同地域 DashScope 主机或 /api/v1 地址")
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=60,
        help="单次 HTTP 请求超时秒数（默认 60）",
    )


def _wait_options(parser):
    parser.add_argument(
        "--timeout", type=float, default=600, help="等待预算秒数（默认 600）"
    )
    parser.add_argument(
        "--poll-interval", type=float, default=15, help="查询间隔秒数（默认 15）"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DashScope 视频原子能力：HappyHorse、Wan首尾帧与多模态参考"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    descriptions = {
        "t2v": "happyhorse-1.1-t2v 文生视频",
        "i2v": "happyhorse-1.1-i2v 文本 + 首帧图片",
        "r2v": "happyhorse-1.1-r2v 文本 + 1~9 张参考图片",
        "edit": "happyhorse-1.0-video-edit 指令 + 视频 + 可选参考图",
        "frames": "wan3.0-video 文本 + 首帧/首尾帧",
        "wan-ref": "Wan 2.7 多图、多视频与文本参考生视频",
    }
    for name, description in descriptions.items():
        command = commands.add_parser(name, help=description, description=description)
        _connection(command)
        _wait_options(command)
        prompt = command.add_mutually_exclusive_group(
            required=name in {"t2v", "r2v", "edit", "wan-ref"}
        )
        prompt.add_argument("--prompt", help="文本提示词")
        prompt.add_argument(
            "--prompt-file", type=Path, help="UTF-8 文本/Markdown 提示词文件"
        )
        command.add_argument(
            "--resolution",
            default="1080P",
            choices=["720P", "1080P"] if name in {"edit","wan-ref"} else ["480P", "720P", "1080P"],
        )
        command.add_argument("--seed", type=int)
        command.add_argument(
            "--watermark",
            action=argparse.BooleanOptionalAction,
            default=False,
            help="添加水印（默认关闭）",
        )
        command.add_argument("--output", type=Path, help="可选，本地 MP4 保存路径")
        command.add_argument(
            "--no-wait", action="store_true", help="只创建任务，立即返回 task_id"
        )
        command.add_argument(
            "--dry-run",
            action="store_true",
            help="只输出请求 JSON，无需密钥，不调用 API",
        )
        command.add_argument(
            "--save-task", type=Path, help="提交后立即把 task_id 和创建响应保存到 JSON"
        )
        if name != "edit":
            command.add_argument("--duration", type=int, default=5)
        if name in {"t2v", "r2v", "frames", "wan-ref"}:
            command.add_argument(
                "--ratio", default="adaptive" if name == "frames" else "16:9"
            )
        if name in {"i2v", "frames"}:
            command.add_argument(
                "--first-frame",
                required=True,
                help="首帧本地图片、HTTP(S) URL 或 data URI",
            )
        if name == "frames":
            command.add_argument("--last-frame", help="可选尾帧；未传则为 Wan 首帧模式")
        if name in {"r2v", "edit", "wan-ref"}:
            command.add_argument(
                "--reference-image",
                action="append",
                default=[],
                required=name == "r2v",
                help="可重复传入，保留输入顺序",
            )
        if name == 'wan-ref':
            command.add_argument('--reference-video',action='append',default=[],help='可重复传入公网视频 URL；图片与视频合计1~5个')
        if name == "edit":
            command.add_argument(
                "--video-url",
                required=True,
                help="待编辑视频公网 HTTP(S) URL，不支持本地文件",
            )
            command.add_argument(
                "--audio-setting", default="auto", choices=["auto", "origin"]
            )
        if name in {"t2v", "frames"}:
            command.add_argument(
                "--model",
                default=T2V_MODEL if name == "t2v" else WAN_MODEL,
                choices=sorted(WAN_MODELS | ({T2V_MODEL} if name == "t2v" else set())),
            )
            command.add_argument(
                "--audio",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="仅 Wan 支持",
            )
            command.add_argument(
                "--prompt-extend",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="仅 Wan 支持提示词改写开关",
            )
    query = commands.add_parser("query", help="查询一次任务，返回原始状态 JSON")
    _connection(query)
    query.add_argument("task_id")
    wait = commands.add_parser("wait", help="继续等待已有任务，可在成功后下载")
    _connection(wait)
    _wait_options(wait)
    wait.add_argument("task_id")
    wait.add_argument("--output", type=Path)
    download = commands.add_parser("download", help="只下载已有结果，无需 API Key")
    download.add_argument("video_url")
    download.add_argument("--output", required=True, type=Path)
    download.add_argument("--request-timeout", type=float, default=60)
    return parser


def _payload(args) -> dict:
    prompt = (
        args.prompt_file.read_text(encoding="utf-8-sig").strip()
        if args.prompt_file
        else (args.prompt or "")
    )
    common = {
        "resolution": args.resolution,
        "watermark": args.watermark,
        "seed": args.seed,
    }
    if args.command != "edit":
        common["duration"] = args.duration
    if args.command in {"t2v", "r2v", "frames", "wan-ref"}:
        common["ratio"] = args.ratio
    if args.command == "t2v":
        return build_text_video_payload(
            prompt,
            model=args.model,
            audio=args.audio,
            prompt_extend=args.prompt_extend,
            **common,
        )
    if args.command == "i2v":
        return build_image_video_payload(prompt, first_frame=args.first_frame, **common)
    if args.command == "r2v":
        return build_reference_video_payload(prompt, args.reference_image, **common)
    if args.command == 'wan-ref':
        return build_multimodal_reference_payload(prompt,args.reference_image,args.reference_video,**common)
    if args.command == "edit":
        return build_video_edit_payload(
            prompt,
            args.video_url,
            reference_images=args.reference_image,
            audio_setting=args.audio_setting,
            **common,
        )
    return build_frame_video_payload(
        prompt,
        args.first_frame,
        args.last_frame,
        model=args.model,
        audio=args.audio,
        prompt_extend=args.prompt_extend,
        **common,
    )


def _print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def _progress(result):
    output = result["output"]
    print(
        f"[任务] {output.get('task_id', '')} {output['task_status']}",
        file=sys.stderr,
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "download":
            # Download is independent of credentials/configuration.
            path = download_video(
                args.video_url, args.output, request_timeout=args.request_timeout
            )
            _print_json({"local_path": path})
            return 0
        if args.command in {"t2v", "i2v", "r2v", "edit", "frames", "wan-ref"}:
            payload = _payload(args)
            if args.no_wait and args.output:
                raise ValueError(
                    "--no-wait 不下载视频，请使用 --save-task 保存任务，之后用 wait --output 下载"
                )
            if args.dry_run:
                _print_json(payload)
                return 0
        client = DashScopeVideoClient(
            base_url=args.base_url,
            env_file=args.env_file,
            request_timeout=args.request_timeout,
        )
        if args.command == "query":
            result = client.query_task_result(args.task_id)
        elif args.command == "wait":
            result = client.wait_for_task(
                args.task_id,
                timeout=args.timeout,
                poll_interval=args.poll_interval,
                on_progress=_progress,
            )
            if args.output:
                result["local_path"] = client.download_video(
                    _video_url(result), args.output
                )
        else:

            def submitted(created):
                print(
                    f"[已提交] model={payload['model']} task_id={created['output']['task_id']}",
                    file=sys.stderr,
                    flush=True,
                )
                if args.save_task:
                    args.save_task.parent.mkdir(parents=True, exist_ok=True)
                    record = {
                        **created,
                        "model": payload["model"],
                        "base_url": client.base_url,
                    }
                    args.save_task.write_text(
                        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )

            if args.no_wait:
                result = client.submit(payload)
                submitted(result)
            else:
                result = client.run(
                    payload,
                    output_path=args.output,
                    timeout=args.timeout,
                    poll_interval=args.poll_interval,
                    on_submitted=submitted,
                    on_progress=_progress,
                )
        _print_json(result)
        return 0
    except (DashScopeError, ValueError, OSError) as exc:
        print(f"[错误] {exc}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print(
            "[停止等待] 本地进程已中断；已提交的远端任务会继续，可用 task_id 查询。",
            file=sys.stderr,
            flush=True,
        )
        return 130
