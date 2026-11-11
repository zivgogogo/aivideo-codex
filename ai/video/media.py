"""Convert local images to data URIs; leave remote assets on their public URLs."""

import base64
import binascii
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


IMAGE_MAX_BYTES = 20 * 1024 * 1024
HAPPYHORSE_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}


def http_url(value: str, label: str = "URL") -> str:
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ValueError(f"{label} 必须是 HTTP(S) URL")
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
        valid = valid and parsed.username is None and parsed.password is None
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            f"{label} 必须是公网 HTTP(S) URL；本地视频请先上传至可访问的存储"
        )
    return value


def reference_video_url(value: str) -> str:
    """Wan accepts public URLs or model-bound DashScope temporary objects."""
    if isinstance(value,str) and re.fullmatch(r'oss://dashscope-instant/[\w./-]+',value) and '..' not in value.split('/'):
        return value
    return http_url(value,'参考视频 URL')


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    raise ValueError("无法识别图片格式；需要 JPEG、PNG、WEBP（Wan 另支持 BMP）")


def image_url(source: str | os.PathLike, *, allow_bmp: bool = False) -> str:
    """Accept a local path, public HTTP(S) URL, or base64 data URI.

    Validate local/data-URI size and MIME. Pixel dimensions and remote media
    requirements are validated by the provider; this function makes no HTTP calls.
    """
    value = os.fspath(source)
    if not isinstance(value, str) or not value:
        raise ValueError("图片不能为空")
    if value.startswith(("http://", "https://")):
        return http_url(value, "图片 URL")
    if value.startswith("data:"):
        match = re.fullmatch(
            r"data:(image/[\w.+-]+);base64,([A-Za-z0-9+/=\r\n]+)", value
        )
        if not match:
            raise ValueError("图片 data URI 格式应为 data:image/格式;base64,内容")
        encoded = match[2].replace("\r", "").replace("\n", "")
        if len(encoded) > 4 * ((IMAGE_MAX_BYTES + 2) // 3):
            raise ValueError("图片大小不能超过 20MB（编码前）")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("图片 Base64 编码无效") from None
        supplied_mime = match[1]
    else:
        if "://" in value:
            raise ValueError("图片需使用本地路径、公网 HTTP(S) URL 或 Base64 data URI")
        path = Path(value).expanduser()
        with path.open("rb") as handle:
            data = handle.read(IMAGE_MAX_BYTES + 1)
        supplied_mime = None
    if not data or len(data) > IMAGE_MAX_BYTES:
        raise ValueError("图片大小应在 0 到 20MB 之间（编码前）")
    mime = _image_mime(data)
    allowed = HAPPYHORSE_IMAGE_MIMES | ({"image/bmp"} if allow_bmp else set())
    if mime not in allowed:
        raise ValueError("该模型图片仅支持 JPEG、PNG、WEBP")
    if supplied_mime is not None and supplied_mime != mime:
        raise ValueError("图片 data URI 的 MIME 与文件内容不一致")
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
