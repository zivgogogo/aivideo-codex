"""Environment-backed defaults for the Bailian Chat Completions API."""

from dataclasses import dataclass, field, fields
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

from core.config import load_env


DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _number(name, value, *, minimum, maximum=None, integer=False):
    valid = type(value) is int if integer else (
        not isinstance(value, bool) and isinstance(value, (int, float))
        and math.isfinite(value)
    )
    if not valid or value < minimum or (maximum is not None and value > maximum):
        limit = f"{minimum}~{maximum}" if maximum is not None else f">= {minimum}"
        raise ValueError(f"{name} 必须为{limit}的{'整数' if integer else '有限数值'}")


def normalize_base_url(value: str) -> str:
    """Accept a host or compatible API root, never the video /api/v1 root."""
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ValueError("DASHSCOPE_LLM_BASE_URL 必须为有效的 HTTP(S) URL")
    root = value.rstrip("/")
    try:
        parsed = urlsplit(root)
        valid = (
            parsed.scheme in {"http", "https"} and parsed.hostname
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment
            and parsed.path in {"", "/compatible-mode/v1"}
        )
        parsed.port  # Validate the port as well.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("LLM base_url 需为主机地址或以 /compatible-mode/v1 结尾；不能使用视频 /api/v1 地址")
    return root + "/compatible-mode/v1" if not parsed.path else root


@dataclass(frozen=True)
class LLMConfig:
    api_key: str = field(default="", repr=False)
    base_url: str = DEFAULT_BASE_URL
    model: str = "qwen-plus"
    timeout: float = 120
    temperature: float | None = 0.7
    max_tokens: int | None = 2048
    enable_thinking: bool | None = False
    system_prompt: str = ""

    def __post_init__(self):
        if not isinstance(self.api_key, str) or any(c in self.api_key for c in "\r\n"):
            raise ValueError("DASHSCOPE_API_KEY 必须为单行字符串")
        object.__setattr__(self, "api_key", self.api_key.strip())
        object.__setattr__(self, "base_url", normalize_base_url(self.base_url))
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("DASHSCOPE_LLM_MODEL 不能为空")
        object.__setattr__(self, "model", self.model.strip())
        _number("timeout", self.timeout, minimum=0)
        if self.timeout == 0:
            raise ValueError("timeout 必须大于 0")
        if self.temperature is not None:
            _number("temperature", self.temperature, minimum=0, maximum=2)
        if self.max_tokens is not None:
            _number("max_tokens", self.max_tokens, minimum=1, integer=True)
        if self.enable_thinking is not None and type(self.enable_thinking) is not bool:
            raise ValueError("enable_thinking 必须为布尔值或 None")
        if not isinstance(self.system_prompt, str):
            raise ValueError("system_prompt 必须为字符串")

    @classmethod
    def from_env(cls, env_file: str | Path | None = None, **overrides):
        """Explicit non-None options > system environment > .env > defaults.

        Reuses the project's safe .env loader. Blank numeric settings and
        enable_thinking=auto omit these parameters from the API request.
        Credentials are required only when a network request is made.
        """
        load_env(env_file)
        unknown = overrides.keys() - {item.name for item in fields(cls)}
        if unknown:
            raise TypeError(f"未知 LLM 配置: {', '.join(sorted(unknown))}")
        values = {}
        for item in fields(cls):
            name = item.name
            if overrides.get(name) is not None:
                values[name] = overrides[name]
                continue
            key = "DASHSCOPE_API_KEY" if name == "api_key" else f"DASHSCOPE_LLM_{name.upper()}"
            raw = os.getenv(key)
            if raw is None:
                continue
            if name in {"timeout", "temperature", "max_tokens"}:
                try:
                    values[name] = None if not raw.strip() else (
                        int(raw) if name == "max_tokens" else float(raw)
                    )
                except ValueError:
                    raise ValueError(f"{key} 需要{'整数' if name == 'max_tokens' else '数字'}") from None
            elif name == "enable_thinking":
                booleans = {"true": True, "1": True, "false": False, "0": False,
                            "auto": None, "": None}
                if raw.strip().lower() not in booleans:
                    raise ValueError(f"{key} 需要 true、false 或 auto")
                values[name] = booleans[raw.strip().lower()]
            else:
                values[name] = raw
        return cls(**values)
