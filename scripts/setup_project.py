#!/usr/bin/env python3
"""Prepare a local checkout without overwriting configuration or production state."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def environment_python(root):
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def initialize(root, *, install_dependencies=True, create_project=True, project=None):
    root = Path(root).resolve()
    if sys.version_info < (3, 10):
        raise RuntimeError("需要 Python 3.10 或更新版本")
    for name in ["requirements.txt", ".env.example", "backend", "core", "resources"]:
        if not (root / name).exists():
            raise RuntimeError(f"项目文件缺失：{name}，请使用完整源码目录")

    python = environment_python(root)
    if not python.is_file():
        if (root / ".venv").exists() or (root / ".venv").is_symlink():
            raise RuntimeError("现有 .venv 不完整，请检查该目录；初始化不会覆盖它")
        print("创建项目虚拟环境…", flush=True)
        venv.EnvBuilder(with_pip=True).create(root / ".venv")
    else:
        print("使用现有 .venv。", flush=True)
    if install_dependencies:
        subprocess.run([str(python), "-m", "pip", "--disable-pip-version-check", "install",
                        "-r", str(root / "requirements.txt")], cwd=root, check=True)
        subprocess.run([str(python), "-m", "pip", "check"], cwd=root, check=True)
    else:
        print("已跳过依赖安装；启动服务前需安装 requirements.txt。", flush=True)

    env = root / ".env"
    if env.exists() or env.is_symlink():
        print("保留现有 .env。", flush=True)
    else:
        # Exclusive creation also avoids overwriting a concurrent setup run.
        try:
            descriptor = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            print(".env 已存在，保留原文件。", flush=True)
        else:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write((root / ".env.example").read_bytes())
            print("已从 .env.example 创建 .env，请填写自己的密钥。", flush=True)

    for relative in ["production/canvas-projects", "production/projects", "outputs"]:
        (root / relative).mkdir(parents=True, exist_ok=True)

    if create_project:
        target = Path(project or os.environ.get("AIVIDEO_PROJECT_DIR") or "production/projects/default").expanduser()
        if not target.is_absolute():
            target = root / target
        state = target / "project.json"
        if target.is_symlink():
            suffix = "已就绪" if state.is_file() else "未就绪，请挂载原外盘后使用"
            print(f"保留现有项目软链接（{suffix}）：{target}", flush=True)
        elif state.is_file():
            print(f"保留已有制作项目：{target}", flush=True)
        elif target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise RuntimeError(f"项目路径已有内容但缺少 project.json，请先检查：{target}")
        else:
            subprocess.run([str(python), "-m", "backend", "init", "--project", str(target)],
                           cwd=root, check=True)
            print("已初始化空白本地项目，未导入剧本或调用 AI 服务。", flush=True)
    print("初始化完成。画布启动：", flush=True)
    print(f'  "{python}" -m backend serve --canvas-only --port 8766', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-install", action="store_true", help="离线准备：跳过依赖安装")
    parser.add_argument("--no-project", action="store_true", help="只准备环境和空画布目录，不创建工作台项目")
    parser.add_argument("--project", help="空白工作台项目目录；相对路径以项目根目录为基准")
    args = parser.parse_args(argv)
    if args.no_project and args.project:
        parser.error("--no-project 和 --project 不能同时使用")
    try:
        sys.path.insert(0, str(ROOT))
        from core.config import load_env
        load_env()
        initialize(ROOT, install_dependencies=not args.skip_install,
                   create_project=not args.no_project, project=args.project)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
