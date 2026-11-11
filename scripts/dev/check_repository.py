#!/usr/bin/env python3
"""Check upload contents and Git history without displaying credential values."""
from pathlib import Path
import re
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
MAX_BYTES = 50 * 1024 * 1024  # Project policy, including historical blobs.


def git(root, *args, input=None):
    return subprocess.run(["git", "-C", str(root), *args], input=input,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout


PUBLIC_DIRECTORIES = {"ai", "backend", "core", "frontend", "resources", "scripts", "tests", "docs", "examples", ".github"}
PUBLIC_FILES = {".gitignore", ".gitattributes", ".env.example", "requirements.txt", "README.md",
                "项目目录说明.md", "初始化项目.command", "启动短剧画布.command", "启动短剧工作台.command"}


def excluded(path):
    parts = Path(path).parts
    if not parts:
        return False
    if parts[0] not in PUBLIC_DIRECTORIES and path not in PUBLIC_FILES:
        return True
    if any(p in {".venv", "venv", "node_modules", "__pycache__"} for p in parts):
        return True
    if path.startswith(("resources/stories/", "docs/production/", "docs/research/")):
        return True
    name = parts[-1]
    return (((name == ".env" or name.startswith(".env.")) and name != ".env.example")
            or name.endswith((".pem", ".key", ".log", ".docx", ".mp4", ".mov"))
            or bool(re.fullmatch(r"EP[0-9]{3}\.md", name)) or "story-seed" in name)


def local_secrets(root):
    env = root / ".env"
    if not env.is_file():
        return []
    values = []
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        key, separator, raw = line.partition("=")
        if not separator or key.lstrip().startswith("#"):
            continue
        if not any(part in key.upper() for part in ["KEY", "TOKEN", "SECRET", "PASSWORD"]):
            continue
        value = " ".join(shlex.split(raw, comments=True))
        if len(value) >= 12 and value not in {"your-dashscope-api-key", "YOUR_API_KEY"}:
            values.append(value.encode())
    return values


def check(root):
    root = Path(root).resolve()
    secrets = local_secrets(root)
    errors = set()
    private_key = re.compile(rb"(?m)^-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")

    def inspect(data, label):
        if any(value in data for value in secrets):
            errors.add(f"发现本机已配置的密钥：{label}")
        if private_key.search(data):
            errors.add(f"发现私钥内容：{label}")

    # Include tracked files and additions that git add would pick up.
    names = {p.decode() for p in git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0") if p}
    for name in sorted(names):
        if excluded(name):
            errors.add(f"应排除的文件进入提交范围：{name}")
        path = root / name
        if path.is_symlink():
            errors.add(f"提交范围包含软链接，请检查目标是否可移植：{name}")
        elif path.is_file():
            if path.stat().st_size > MAX_BYTES:
                errors.add(f"文件超过项目的 50 MiB 限制：{name}")
            else:
                inspect(path.read_bytes(), name)

    objects = {}
    for entry in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, oid, stage = metadata.decode().split()
        if mode == "160000":
            errors.add(f"提交范围包含嵌套 Git 仓库：{name.decode()}")
        else:
            objects[oid] = "暂存区：" + name.decode()
    revisions = git(root, "rev-list", "--objects", "--all").decode().splitlines()
    for row in revisions:
        oid, _, name = row.partition(" ")
        objects.setdefault(oid, "历史：" + (name or oid[:12]))
        if name and excluded(name):
            errors.add(f"历史包含本地内容或非源码路径：{name}")
    blobs = 0
    ids = list(objects)
    # Bounded batches avoid buffering the complete history at once.
    for start in range(0, len(ids), 64):
        block = ids[start:start + 64]
        payload = ("\n".join(block) + "\n").encode()
        headers = git(root, "cat-file", "--batch-check", input=payload).decode().splitlines()
        selected = []
        for header in headers:
            oid, kind, size = header.split()
            if kind != "blob":
                continue
            blobs += 1
            if int(size) > MAX_BYTES:
                errors.add(f"历史文件超过项目的 50 MiB 限制：{objects[oid]}")
            else:
                selected.append((oid, int(size)))
        if not selected:
            continue
        groups = []
        group, total = [], 0
        for oid, size in selected:
            if group and total + size > 8 * 1024 * 1024:
                groups.append(group)
                group, total = [], 0
            group.append(oid)
            total += size
        groups.append(group)
        for group in groups:
            data = git(root, "cat-file", "--batch", input=("\n".join(group) + "\n").encode())
            offset = 0
            for oid in group:
                end = data.index(b"\n", offset)
                size = int(data[offset:end].split()[2])
                inspect(data[end + 1:end + 1 + size], objects[oid])
                offset = end + size + 2
    return sorted(errors), len(names), blobs


def main():
    try:
        errors, files, blobs = check(ROOT)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"仓库检查失败：{type(exc).__name__}，请确认 Git 仓库和本地配置有效。", file=sys.stderr)
        return 1
    if errors:
        for message in errors:
            print(message, file=sys.stderr)
        return 1
    print(f"上传前检查通过：{files} 个文件，{blobs} 个 Git 文件对象。")
    print("未发现运行数据、嵌套仓库、超限文件、本机已配置密钥或私钥。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
