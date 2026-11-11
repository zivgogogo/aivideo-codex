#!/usr/bin/env python3
"""Qwen Image 3.0: text-to-image or text + 1–3 reference images.

Python 3.10+, standard library only. Run with --help for CLI examples/options.
Official API: https://help.aliyun.com/zh/model-studio/qwen-image-generation-and-editing-api-reference
"""

import argparse
import base64
import binascii
from datetime import datetime
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from core.config import load_env
from core.paths import PROJECT_ROOT

MODEL = "qwen-image-3.0-pro"
API_PATH = "/services/aigc/multimodal-generation/generation"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
MAX_IMAGE_BYTES = 10 * 1024 * 1024


class ImageGenerationError(RuntimeError):
    """API, transport, response, or download failure."""


def _http_url(value):
    if isinstance(value, str) and value and not any(c.isspace() for c in value):
        try:
            parsed = urlsplit(value)
            if (parsed.scheme in {"http", "https"} and parsed.hostname
                    and parsed.username is None and parsed.password is None):
                return value
        except ValueError:
            pass
    raise ValueError("需要有效的 HTTP(S) URL")


def _positive_timeout(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError("timeout 必须是大于 0 的有限秒数")


def _image_mime(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    if data.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        return "image/tiff"
    raise ValueError("图片格式不支持，需要 JPEG、PNG、BMP、TIFF、WEBP 或 GIF")


def image_input(source):
    """Convert a local file to a data URI, or validate a URL/data URI.

    Checks local/embedded size and file signature. Pixel dimensions/aspect ratio
    and remote image contents are checked by the provider; no input URL is fetched.
    """
    value = os.fspath(source)
    if not isinstance(value, str) or not value:
        raise ValueError("图片输入不能为空")
    if value.startswith(("http://", "https://")):
        return _http_url(value)
    declared_mime = None
    if value.startswith("data:"):
        match = re.fullmatch(r"data:(image/[\w.+-]+);base64,([A-Za-z0-9+/=\r\n]+)", value)
        if not match:
            raise ValueError("需要 data:image/格式;base64,内容 格式的图片")
        encoded = match[2].replace("\r", "").replace("\n", "")
        if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise ValueError("单张输入图片不能超过 10MB（Base64 编码前）")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("图片 Base64 编码无效") from None
        declared_mime = {"image/jpg": "image/jpeg"}.get(match[1], match[1])
    else:
        if "://" in value:
            raise ValueError("图片需使用本地路径、HTTP(S) URL 或 Base64 data URI")
        with Path(value).expanduser().open("rb") as handle:
            data = handle.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("单张输入图片需非空，且不能超过 10MB（Base64 编码前）")
    mime = _image_mime(data)
    if declared_mime is not None and declared_mime != mime:
        raise ValueError("data URI 的 MIME 与图片内容不一致")
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def build_payload(prompt, images=None, *, size=None, n=1, seed=None,
                  negative_prompt=None, prompt_extend=True,
                  prompt_extend_mode="direct", enable_thinking=True,
                  watermark=False, model=MODEL):
    """Build the same request for both modes; images=None means text-to-image."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt 不能为空")
    if model not in {MODEL, "qwen-image-3.0"}:
        raise ValueError("此脚本支持 qwen-image-3.0-pro 和 qwen-image-3.0")
    if isinstance(images, (str, os.PathLike)):
        images = [images]
    elif images is None:
        images = []
    elif not isinstance(images, (list, tuple)):
        raise ValueError("images 需为单张图片输入或图片列表")
    if len(images) > 3:
        raise ValueError("Qwen Image 3.0 最多支持 3 张参考图")
    if type(n) is not int or not 1 <= n <= 6:
        raise ValueError("n 必须为 1 到 6 的整数")
    if seed is not None and (type(seed) is not int or not 0 <= seed <= 2147483647):
        raise ValueError("seed 必须为 0 到 2147483647 的整数")
    for name, value in (("prompt_extend", prompt_extend),
                        ("enable_thinking", enable_thinking), ("watermark", watermark)):
        if type(value) is not bool:
            raise ValueError(f"{name} 必须为布尔值")
    if prompt_extend_mode not in {"direct", "agent"}:
        raise ValueError("prompt_extend_mode 只能为 direct 或 agent")
    if images and prompt_extend_mode == "agent":
        raise ValueError("agent 提示词改写仅支持文生图；图生图请使用 direct")
    parameters = {
        "n": n, "prompt_extend": prompt_extend,
        "prompt_extend_mode": prompt_extend_mode,
        "enable_thinking": enable_thinking, "watermark": watermark,
    }
    if size is not None and size != "auto":
        match = re.fullmatch(r"([1-9][0-9]{0,5})[xX*]([1-9][0-9]{0,5})", str(size).strip())
        if not match:
            raise ValueError('size 需为 "宽x高"、"宽*高" 或 "auto"')
        width, height = map(int, match.groups())
        if not (512 * 512 <= width * height <= 2048 * 2048
                and 1 / 8 <= width / height <= 8):
            raise ValueError("输出总像素需在 512*512 至 2048*2048 之间，宽高比在 1:8 至 8:1 之间")
        parameters["size"] = f"{width}*{height}"
    if seed is not None:
        parameters["seed"] = seed
    if negative_prompt is not None:
        if not isinstance(negative_prompt, str):
            raise ValueError("negative_prompt 必须为字符串")
        parameters["negative_prompt"] = negative_prompt
    content = [{"image": image_input(source)} for source in images]
    content.append({"text": prompt.strip()})
    return {
        "model": model,
        "input": {"messages": [{"role": "user", "content": content}]},
        "parameters": parameters,
    }


def generate_image(prompt, images=None, *, api_key=None, base_url=None,
                   env_file=None, timeout=300, **options):
    """Generate once and return the complete API response. Does not download.

    Uses synchronous DashScope HTTP. Never retries a paid POST automatically.
    """
    payload = build_payload(prompt, images, **options)
    _positive_timeout(timeout)
    load_env(env_file)
    key = (api_key if api_key is not None else os.getenv("DASHSCOPE_API_KEY", "")).strip()
    if not key:
        raise ValueError("请在 .env 或系统环境变量中设置 DASHSCOPE_API_KEY")
    root = (base_url or os.getenv("DASHSCOPE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    _http_url(root)
    parsed = urlsplit(root)
    if parsed.path not in {"", "/api/v1"} or parsed.query or parsed.fragment:
        raise ValueError("base_url 需为 DashScope 主机地址或以 /api/v1 结尾的地址")
    if not root.endswith("/api/v1"):
        root += "/api/v1"
    request = Request(root + API_PATH,
                      data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                      headers={"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json", "Accept": "application/json"},
                      method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except HTTPError as exc:
        try:
            status, raw = exc.code, exc.read()
        finally:
            exc.close()
    except (URLError, OSError, HTTPException) as exc:
        raise ImageGenerationError(
            f"生成请求网络失败或超时（{type(exc).__name__}）；未自动重试。"
            "服务端可能已受理，请先在百炼控制台核实调用记录。"
        ) from exc
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ImageGenerationError(f"HTTP {status}: 接口返回非 JSON 响应；未自动重试") from None
    if not isinstance(result, dict):
        raise ImageGenerationError("接口响应应为 JSON 对象；未自动重试")
    if not 200 <= status < 300 or result.get("code"):
        raise ImageGenerationError(
            f"HTTP {status} [{result.get('code', 'APIError')}] "
            f"{result.get('message', '请求失败')} (request_id={result.get('request_id', '')})"
        )
    return result


def extract_image_urls(result):
    """Collect every image, across both multiple choices and multiple contents."""
    try:
        urls = [
            _http_url(item["image"])
            for choice in result["output"]["choices"]
            for item in choice["message"]["content"]
            if "image" in item
        ]
    except (KeyError, TypeError, ValueError):
        raise ImageGenerationError("生成响应中的图片结构或 URL 无效，请检查保存的原始响应") from None
    if not urls:
        raise ImageGenerationError("接口未返回生成图片，请检查保存的原始响应及内容审核结果")
    return urls


def _image_paths(output, count):
    if type(count) is not int or not 1 <= count <= 6:
        raise ValueError("图片数量必须为 1 到 6 的整数")
    return [output] if count == 1 else [
        output.with_name(f"{output.stem}_{index}.png") for index in range(1, count + 1)
    ]


def download_image(url, output, *, timeout=60):
    """Download a PNG atomically, without sending the API key to its host."""
    _http_url(url)
    _positive_timeout(timeout)
    path = Path(output).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with urlopen(Request(url, headers={"Accept": "image/png"}), timeout=timeout) as response:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".qwen-", suffix=".part",
                                             delete=False) as handle:
                temporary = Path(handle.name)
                first = response.read(8)
                if first != b"\x89PNG\r\n\x1a\n":
                    raise ImageGenerationError("下载结果不是 PNG 图片，未覆盖目标文件")
                handle.write(first)
                count = len(first)
                while chunk := response.read(1024 * 1024):
                    handle.write(chunk)
                    count += len(chunk)
            expected = response.headers.get("Content-Length")
            if expected is not None and count != int(expected):
                raise ImageGenerationError("图片下载不完整，请使用已有 URL 重试下载")
        temporary.replace(path)
        return str(path.resolve())
    except (URLError, OSError, HTTPException) as exc:
        raise ImageGenerationError("图片下载失败，请使用已保存的图片 URL 重试下载，无需重新生成") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build_parser():
    parser = argparse.ArgumentParser(description="Qwen Image 3.0 文生图 / 文本+参考图生图（同步调用）")
    prompt = parser.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt", help="生成描述或图片编辑指令")
    prompt.add_argument("--prompt-file", type=Path, help="UTF-8 文本/Markdown 提示词文件")
    parser.add_argument("--image", action="append", help="可选：本地图片、HTTP(S) URL 或 data URI；可重复，最多 3 张")
    parser.add_argument("--model", default=MODEL, choices=[MODEL, "qwen-image-3.0"])
    parser.add_argument("--size", help='输出分辨率，如 1024x1024、"1536*1024"；默认自动推荐')
    parser.add_argument("--n", type=int, default=1, help="输出数量，1~6，默认 1")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--negative-prompt", help="反向提示词")
    parser.add_argument("--prompt-extend", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--prompt-extend-mode", choices=["direct", "agent"], default="direct")
    parser.add_argument("--enable-thinking", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--watermark", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--output", type=Path, help="输出 PNG 路径；多图自动添加 _1、_2 后缀；默认 outputs/时间戳.png")
    parser.add_argument("--save-response", type=Path, help="原始响应 JSON 路径，默认输出文件同名 .json")
    parser.add_argument("--no-download", action="store_true", help="只保存响应并输出 URL，不下载图片")
    parser.add_argument("--env-file", type=Path, help="默认读取项目根目录 .env")
    parser.add_argument("--base-url", help="同地域 DashScope 主机地址或 /api/v1 地址")
    parser.add_argument("--timeout", type=float, default=300, help="生成请求超时秒数，默认 300")
    parser.add_argument("--dry-run", action="store_true", help="只打印请求 JSON，不读取密钥、不调用 API")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        prompt = args.prompt_file.read_text(encoding="utf-8-sig") if args.prompt_file else args.prompt
        options = {name: getattr(args, name) for name in (
            "model", "size", "n", "seed", "negative_prompt", "prompt_extend",
            "prompt_extend_mode", "enable_thinking", "watermark")}
        if args.dry_run:
            print(json.dumps(build_payload(prompt, args.image, **options), ensure_ascii=False, indent=2))
            return 0
        _positive_timeout(args.timeout)
        output = (args.output or Path("outputs") / f"qwen_{datetime.now():%Y%m%d_%H%M%S_%f}.png").expanduser()
        if output.suffix.lower() != ".png":
            raise ValueError("--output 需以 .png 结尾（模型输出 PNG）")
        response_path = (args.save_response or output.with_suffix(".json")).expanduser()
        # A partial result may contain fewer images than requested.
        possible_paths = {output, *_image_paths(output, args.n)}
        if response_path.resolve() in {path.resolve() for path in possible_paths}:
            raise ValueError("图片和原始响应不能保存到同一路径")
        if any(path.is_dir() for path in possible_paths):
            raise ValueError("图片输出路径不能是已有目录")
        # Establish writable destinations before making the paid generation call.
        output.parent.mkdir(parents=True, exist_ok=True)
        response_path.parent.mkdir(parents=True, exist_ok=True)
        with response_path.open("x", encoding="utf-8") as handle:
            try:
                print("正在生成图片…", file=sys.stderr, flush=True)
                result = generate_image(prompt, args.image, env_file=args.env_file,
                                        base_url=args.base_url, timeout=args.timeout, **options)
            except BaseException:
                response_path.unlink(missing_ok=True)
                raise
            # Persist URLs immediately, before attempting any downloads.
            json.dump(result, handle, ensure_ascii=False, indent=2)
        print(f"原始响应已保存：{response_path.resolve()}", file=sys.stderr, flush=True)
        urls = extract_image_urls(result)
        paths = []
        if not args.no_download:
            for url, path in zip(urls, _image_paths(output, len(urls))):
                paths.append(download_image(url, path))
        print(json.dumps({"request_id": result.get("request_id"), "image_urls": urls,
                          "local_paths": paths, "response_file": str(response_path.resolve()),
                          "usage": result.get("usage")}, ensure_ascii=False, indent=2))
        return 0
    except (ImageGenerationError, ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断；若正在生成，请先核实百炼调用记录；若正在下载，可从响应 JSON 获取 URL。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
