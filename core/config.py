"""Load project configuration without third-party packages or shell evaluation."""

import os
from pathlib import Path
import re
import shlex


from core.paths import PROJECT_ROOT


def load_env(path: str | Path | None = None) -> Path:
    """Load KEY=value lines; existing environment variables take precedence.

    Supports comments, quoted values and an optional ``export`` prefix.
    Does not execute shell commands or expand variable references.
    """
    env_path = Path(path).expanduser() if path is not None else PROJECT_ROOT / ".env"
    if not env_path.exists():
        if path is not None:
            raise FileNotFoundError(f"环境文件不存在: {env_path}")
        return env_path
    for number, line in enumerate(
        env_path.read_text(encoding="utf-8-sig").splitlines(), 1
    ):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"环境文件第 {number} 行需要 KEY=value: {env_path}")
        try:
            parsed = " ".join(shlex.split(value, comments=True, posix=True))
        except ValueError:
            raise ValueError(f"环境文件第 {number} 行引号不完整: {env_path}") from None
        os.environ.setdefault(key, parsed)
    return env_path


def local_setting_path(name):
    value = os.environ.get(name)
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def project_prompt(name, default):
    path = local_setting_path(name)
    return path.read_text(encoding="utf-8") if path is not None else default
