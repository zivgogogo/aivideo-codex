"""Local, guarded updates from the project's public main branch.

The worker is copied outside tracked source before the server exits, so it can
restore the previous version even if the new application cannot start.
"""
from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from urllib.request import ProxyHandler, build_opener

REPOSITORY = "https://github.com/zivgogogo/aivideo-codex"
REMOTE = REPOSITORY + ".git"
BRANCH = "main"
CANDIDATE = "refs/aivideo/update"
ACTIVE_PHASES = {"preparing", "stopping", "fetching", "backing_up", "updating", "dependencies", "restarting", "rolling_back"}
PROTECTED = {".git", ".venv", "venv", "production", "outputs", "uploads", "local", "node_modules"}


class UpdateError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {} if default is None else default


def alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def redact(value):
    text = str(value)
    for name, secret in os.environ.items():
        if len(secret) >= 8 and any(part in name.upper() for part in ("KEY", "SECRET", "TOKEN", "PASSWORD")):
            text = text.replace(secret, "[REDACTED]")
    return re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)[-1600:]


def command(arguments, root, *, timeout=90, allowed=(0,), strip=True):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", PIP_NO_INPUT="1")
    try:
        result = subprocess.run(arguments, cwd=root, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError as exc:
        raise UpdateError("缺少 Git 或项目 Python 环境，请按初始化文档安装。") from exc
    except subprocess.TimeoutExpired as exc:
        raise UpdateError("更新操作超时，请检查网络后重试。") from exc
    if result.returncode not in allowed:
        raise UpdateError(redact(result.stderr.strip() or result.stdout.strip() or "更新命令未成功"))
    return result.stdout.strip() if strip else result.stdout


def git(root, *arguments, **options):
    # Public source updates never need stored account credentials or prompts.
    return command(["git", "-c", "credential.helper=", *arguments], root, **options)


def validate_tree(root, sha):
    records = command(["git", "ls-tree", "-rz", "--full-tree", sha], root, strip=False).split("\0")
    for row in records:
        if not row:
            continue
        metadata, name = row.split("\t", 1)
        mode, kind, _ = metadata.split()
        parts = tuple(part.casefold() for part in Path(name).parts)
        if mode not in {"100644", "100755"} or kind != "blob":
            raise UpdateError("新版本包含软链接或嵌套仓库，未自动更新。")
        if (not parts or parts[0] in PROTECTED or ".." in parts
                or any(part == ".git" for part in parts)
                or (Path(name).name.startswith(".env") and name != ".env.example")
                or re.fullmatch(r"EP\d{3}\.md", Path(name).name)
                or "story-seed" in Path(name).name
                or name.startswith(("resources/stories/", "docs/production/", "docs/research/"))):
            raise UpdateError("新版本包含本地配置或制作资料路径，未自动更新。")


def validate_transition(root, current, target):
    validate_tree(root, target)
    added = command(["git", "diff", "--no-renames", "--name-only", "--diff-filter=A", "-z", current, target], root, strip=False)
    for name in added.split("\0"):
        if not name:
            continue
        destination = Path(root) / name
        if destination.exists() or destination.is_symlink():
            raise UpdateError("新版本的文件路径与本地文件重名，请先备份处理：" + name)
        for parent in destination.parents:
            if parent == Path(root):
                break
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise UpdateError("新版本目录与本地文件或链接冲突，请先备份处理：" + name)


def repository_state(root, *, expected_remote=REMOTE):
    root = Path(root).resolve()
    if not (root / ".git").exists():
        raise UpdateError("当前为 ZIP 或复制安装。请先使用 Git 克隆项目，才能一键更新。")
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise UpdateError("当前文件夹不是独立的项目 Git 仓库。")
    origin = git(root, "remote", "get-url", "origin")
    acceptable = {expected_remote, expected_remote.removesuffix(".git")}
    if expected_remote == REMOTE:
        acceptable.add("git@github.com:zivgogogo/aivideo-codex.git")
    if origin not in acceptable:
        raise UpdateError("当前项目关联了其他仓库，未自动更换代码来源。")
    branch = git(root, "symbolic-ref", "--quiet", "--short", "HEAD", allowed=(0, 1))
    if branch != BRANCH:
        raise UpdateError("请先切换到 main 分支，再使用一键更新。")
    if git(root, "status", "--porcelain", "--untracked-files=normal"):
        raise UpdateError("本地代码有未提交的修改，请先提交或备份处理后再更新。配置与素材不会触发此限制。")
    return git(root, "rev-parse", "HEAD")


class UpdateManager:
    def __init__(self, root, port, project, canvas_only=False, canvas_root=None, *, remote=REMOTE, python=None):
        self.root = Path(root).resolve()
        self.port = port
        self.remote = remote
        self.python = str(python or sys.executable)
        self.project = str(Path(project).absolute())
        self.canvas_only = canvas_only
        self.canvas_root = str(Path(canvas_root).absolute()) if canvas_root else None
        self.folder = self.root / "production/.updates"
        self.status_path = self.folder / "status.json"
        self.lock_path = self.folder / "update.lock"
        self.instance = uuid.uuid4().hex
        self.record = self.folder / "servers" / f"{os.getpid()}-{port}-{self.instance}.json"
        self.guard = threading.RLock()
        self.stopping = False
        try:
            self.running_sha = git(self.root, "rev-parse", "HEAD") if (self.root / ".git").exists() else ""
        except UpdateError:
            self.running_sha = ""
        self.checked = {}
        self.operation = None

    def locked(self):
        data = read_json(self.lock_path)
        if alive(data.get("pid")):
            return data
        if self.lock_path.exists():
            self.lock_path.unlink(missing_ok=True)
        return None

    def register(self):
        locked = self.locked()
        if locked and os.environ.get("AIVIDEO_RESTART_TOKEN") != locked.get("operation"):
            raise UpdateError("程序正在更新，请稍后再启动服务。")
        write_json(self.record, {"pid": os.getpid(), "port": self.port, "instance": self.instance})

    def close(self):
        self.record.unlink(missing_ok=True)
        if self.operation:
            write_json(self.folder / self.operation / "server-stopped.json", {"stopped": True})

    def peers(self):
        result = []
        for path in (self.folder / "servers").glob("*.json"):
            row = read_json(path)
            if row.get("instance") == self.instance:
                continue
            if alive(row.get("pid")):
                result.append(row)
            else:
                path.unlink(missing_ok=True)
        return result

    def snapshot(self):
        with self.guard:
            progress = read_json(self.status_path)
            if progress.get("phase") in ACTIVE_PHASES and not self.locked():
                progress.update(phase="failed", message="上次更新未正常完成，请重新检查更新。", updated_at=now())
                write_json(self.status_path, progress)
            state = {"repository": REPOSITORY, "branch": BRANCH, "running_sha": self.running_sha,
                     "supported": bool(self.running_sha), "available": False, "can_update": False,
                     "phase": "idle", **self.checked, **progress}
            state["running_sha"] = self.running_sha
            state["available"] = bool(state.get("target_sha") and state["target_sha"] != self.running_sha)
            if state.get("phase") in ACTIVE_PHASES:
                state["can_update"] = False
            return state

    def check(self):
        with self.guard:
            if self.locked():
                return self.snapshot()
            # A finished operation is history, not the next update candidate.
            previous = read_json(self.status_path)
            self.checked = {"checked_at": now(), "can_update": False, "target_sha": "", "commits": []}
            try:
                current = repository_state(self.root, expected_remote=self.remote)
                if current != self.running_sha:
                    raise UpdateError("磁盘代码已变化，请先重启当前服务后再检查更新。")
                git(self.root, "fetch", "--no-tags", self.remote, f"+refs/heads/{BRANCH}:{CANDIDATE}")
                target = git(self.root, "rev-parse", CANDIDATE)
                self.checked["target_sha"] = target
                if target != current:
                    result = subprocess.run(["git", "merge-base", "--is-ancestor", current, target], cwd=self.root)
                    if result.returncode:
                        raise UpdateError("本地版本与 GitHub 存在分歧，请先处理本地提交，再更新。")
                    validate_transition(self.root, current, target)
                    self.checked["commits"] = [line for line in git(self.root, "log", "-5", "--format=%h %s", f"{current}..{target}").splitlines() if line]
                self.checked.update(supported=True, can_update=target != current,
                                    message="发现新版本，可以更新。" if target != current else "当前已是最新版本。")
                expected_python = self.root / ".venv"
                try:
                    Path(self.python).absolute().relative_to(expected_python)
                except ValueError:
                    self.checked.update(can_update=False, message="请使用初始化创建的 .venv 启动服务后再一键更新。")
            except UpdateError as exc:
                self.checked.update(message=str(exc), can_update=False)
            # Persist only this process's candidate; never carry an old successful
            # operation's target into an apply request.
            write_json(self.status_path, {**self.checked, "phase": "idle", "updated_at": now(),
                                          "last_result": previous.get("phase"), "last_message": previous.get("message")})
            return self.snapshot()

    def apply(self, target, busy_reason, shutdown):
        with self.guard:
            if self.locked() or self.stopping:
                raise UpdateError("已有更新正在进行。")
            if not re.fullmatch(r"[0-9a-f]{40}", str(target)) or target != self.checked.get("target_sha") or not self.checked.get("can_update"):
                raise UpdateError("版本检查已过期，请重新检查更新。")
            current = repository_state(self.root, expected_remote=self.remote)
            if current != self.running_sha:
                raise UpdateError("当前代码已变化，请先重启服务。")
            reason = busy_reason()
            if reason:
                raise UpdateError(reason)
            if self.peers():
                raise UpdateError("同一项目还有其他服务正在运行，请只保留一个服务后更新。")
            operation = uuid.uuid4().hex
            self.folder.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:
                raise UpdateError("已有更新正在进行。") from exc
            try:
                with os.fdopen(descriptor, "w") as handle:
                    json.dump({"pid": os.getpid(), "operation": operation}, handle)
                destination = self.folder / operation
                destination.mkdir()
                plan = {"root": str(self.root), "operation": operation, "before_sha": current,
                        "target_sha": target, "remote": self.remote, "python": self.python,
                        "port": self.port, "project": self.project, "canvas_only": self.canvas_only,
                        "canvas_root": self.canvas_root}
                worker = destination / "worker.py"
                shutil.copyfile(Path(__file__), worker)
                write_json(destination / "plan.json", plan)
                write_json(self.status_path, {"phase": "stopping", "operation": operation, "target_sha": target,
                                              "message": "正在停止空闲服务，准备更新…", "updated_at": now()})
                with (destination / "worker.log").open("ab") as output:
                    child = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), str(worker), str(destination / "plan.json")],
                                             cwd=self.root, stdout=output, stderr=output, stdin=subprocess.DEVNULL,
                                             start_new_session=True)
                write_json(self.lock_path, {"pid": child.pid, "operation": operation})
            except Exception as exc:
                self.lock_path.unlink(missing_ok=True)
                try:
                    write_json(self.status_path, {"phase": "failed", "message": "无法准备更新，服务保持运行。"})
                except OSError:
                    pass
                raise UpdateError("无法准备更新，服务保持运行：" + redact(exc)) from exc
            self.operation = operation
            self.stopping = True
            threading.Thread(target=shutdown, name="update-shutdown", daemon=True).start()
            return self.snapshot()


def run_worker(plan):
    root = Path(plan["root"])
    folder = root / "production/.updates"
    operation = folder / plan["operation"]
    lock = folder / "update.lock"
    changed = False
    dependencies_changed = False
    old_requirements = ""
    snapshot = operation / "dependencies.txt"

    def status(phase, message):
        write_json(folder / "status.json", {"phase": phase, "message": message, "operation": plan["operation"],
                   "target_sha": plan["target_sha"], "updated_at": now()})

    def restart():
        args = [plan["python"], "-m", "backend", "serve", "--port", str(plan["port"]), "--project", plan["project"]]
        if plan["canvas_only"]:
            args.append("--canvas-only")
        if plan.get("canvas_root"):
            args.extend(["--canvas-root", plan["canvas_root"]])
        env = dict(os.environ, AIVIDEO_RESTART_TOKEN=plan["operation"])
        with (operation / "server.log").open("ab") as output:
            child = subprocess.Popen(args, cwd=root, env=env, stdout=output, stderr=output,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 25
        expected = git(root, "rev-parse", "HEAD")
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise UpdateError("新服务启动失败，请查看本地更新记录。")
            try:
                with opener.open(f'http://127.0.0.1:{plan["port"]}/api/system/update', timeout=1) as response:
                    data = json.load(response)
                if data.get("running_sha") == expected:
                    return child
            except Exception:
                pass
            time.sleep(.2)
        # This is only the worker's own child process, never another listener.
        child.terminate()
        child.wait(timeout=5)
        raise UpdateError("等待新服务就绪超时。")

    try:
        deadline = time.monotonic() + 30
        while not (operation / "server-stopped.json").exists():
            if time.monotonic() > deadline:
                raise UpdateError("原服务尚未停止，未修改代码。")
            time.sleep(.1)
        status("fetching", "正在获取 GitHub 最新代码…")
        before = repository_state(root, expected_remote=plan["remote"])
        if before != plan["before_sha"]:
            raise UpdateError("当前代码发生变化，未覆盖本地版本。")
        git(root, "fetch", "--no-tags", plan["remote"], f"+refs/heads/{BRANCH}:{CANDIDATE}")
        target = git(root, "rev-parse", CANDIDATE)
        if target != plan["target_sha"]:
            raise UpdateError("GitHub 又发布了新版本，请重新检查后更新。")
        git(root, "merge-base", "--is-ancestor", before, target)
        validate_transition(root, before, target)
        status("backing_up", "正在备份当前程序版本…")
        git(root, "bundle", "create", str(operation / "before.bundle"), "HEAD")
        snapshot.write_text(command([plan["python"], "-m", "pip", "freeze"], root), encoding="utf-8")
        old_requirements = (root / "requirements.txt").read_text(encoding="utf-8")
        status("updating", "正在安装新版本程序…")
        git(root, "merge", "--ff-only", "--no-edit", target)
        changed = True
        dependencies_changed = old_requirements != (root / "requirements.txt").read_text(encoding="utf-8")
        if dependencies_changed:
            status("dependencies", "正在安装新版本所需依赖…")
            command([plan["python"], "-m", "pip", "--disable-pip-version-check", "install", "-r", "requirements.txt"], root, timeout=600)
        command([plan["python"], "-m", "pip", "check"], root, timeout=60)
        status("restarting", "新版本已安装，正在重新启动服务…")
        restart()
        status("complete", "更新完成，正在加载新版本。")
        return True
    except Exception as exc:
        failure = redact(exc)
        try:
            if changed:
                status("rolling_back", "更新未完成，正在恢复之前的版本…")
                git(root, "reset", "--keep", plan["before_sha"])
                if dependencies_changed and snapshot.is_file():
                    command([plan["python"], "-m", "pip", "--disable-pip-version-check", "install", "-r", str(snapshot)], root, timeout=600)
            if (operation / "server-stopped.json").exists():
                status("restarting", "正在恢复原服务…")
                restart()
            status("failed", "更新未完成：" + failure + ("；已恢复之前的程序版本。" if changed else "；原代码未被替换。"))
        except Exception as recovery:
            status("failed", "更新未完成：" + failure + "；恢复服务也未完成，请重新初始化并启动。" + redact(recovery))
        return False
    finally:
        if read_json(lock).get("operation") == plan["operation"]:
            lock.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(0 if run_worker(read_json(sys.argv[1])) else 1)
