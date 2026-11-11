"""Atomic project storage. Media are immutable; selections are pointers."""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from zoneinfo import ZoneInfo

from .paths import PROJECT_ROOT, DEFAULT_PROJECT_DIR

WORKSPACE = PROJECT_ROOT
DEFAULT_PROJECT = DEFAULT_PROJECT_DIR
LOCK = threading.RLock()


def timestamp():
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S_%f")


def now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def safe_path(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("路径必须在当前项目目录内")
    return path


def media_url(relative):
    from urllib.parse import quote
    return "/media/" + quote(str(relative), safe="/")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class Store:
    def __init__(self, root=None):
        root = Path(root or os.environ.get("AIVIDEO_PROJECT_DIR") or DEFAULT_PROJECT).expanduser()
        if not root.is_absolute():
            root = WORKSPACE / root
        self.input_root = root.absolute()
        self._linked_project = self.input_root.is_symlink()
        self.root = self.input_root.resolve()
        self.path = self.root / "project.json"

    def _require_linked_project(self):
        if not self._linked_project:
            return
        try:
            target = self.input_root.resolve(strict=True)
            ready = (self.input_root.is_symlink() and target == self.root
                     and target.is_dir() and (target / "project.json").is_file())
        except (OSError, RuntimeError):
            ready = False
        if not ready:
            raise RuntimeError(f"外盘或链接项目未就绪：{self.input_root}。请挂载原外盘并确认目标目录和project.json完整后重启工作台；不会新建或覆盖链接项目。")

    def initialize(self, seed_path=None):
        with LOCK:
            self._require_linked_project()
            if self.path.exists():
                return self.read()
            seed = (json.loads(Path(seed_path).expanduser().read_text(encoding="utf-8"))
                    if seed_path is not None else {})
            project = {"id": self.input_root.name, "title": "未命名项目", "created_at": now(),
                       "visual_style": "写实电影质感，自然光，人物身份与场景保持连续。",
                       "ratio": "9:16", "resolution": "720P", "run_status": "idle", "auto_select": True,
                       **seed.get("project", {})}
            state = {"schema_version": 1, "project": project, "source": seed.get("source", {}),
                     "characters": seed.get("characters", []), "locations": seed.get("locations", []),
                     "episodes": seed.get("episodes", []), "jobs": [], "exports": []}
            for ensemble in seed.get("ensembles", []):
                state["characters"].append({**ensemble, "is_ensemble": True,
                    "base_prompt": ensemble.get("base_prompt", ensemble.get("name", "")),
                    "looks": ensemble.get("looks", [])})
            for character in state["characters"]:
                character.update(assets=[], selected={})
            for location in state["locations"]:
                location.update(assets=[], selected={})
            for ep in state["episodes"]:
                ep.update(shots=[], status="unplanned", export=None)
                folder = self.root / "episodes" / ep["id"]
                for sub in ["source", "planning", "characters", "scenes", "shots", "videos", "subtitles", "exports"]:
                    (folder / sub).mkdir(parents=True, exist_ok=True)
                text = ep.get("source_text", "")
                if not text and ep.get("source_path"):
                    text = (WORKSPACE / ep["source_path"]).read_text(encoding="utf-8")
                (folder / "source/script.md").write_text(text, encoding="utf-8")
                ep["source_text"] = text
            self.save(state)
            return state

    def read(self):
        with LOCK:
            self._require_linked_project()
            return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, state):
        with LOCK:
            self._require_linked_project()
            state["updated_at"] = now()
            write_json(self.path, state)

    def mutate(self, fn):
        with LOCK:
            state = self.read()
            result = fn(state)
            self.save(state)
            return result

    def snapshot(self):
        state = self.read()
        for ep in state["episodes"]:
            ep.pop("source_text", None)
        state["jobs"] = state["jobs"][-120:]
        shots = [shot for ep in state["episodes"] for shot in ep["shots"]]
        ready = lambda shot: any(a["id"] == shot.get("selected") and a.get("status") == "ready" and not a.get("stale") for a in shot.get("assets", []))
        state["stats"] = {"episodes": len(state["episodes"]), "planned": sum(bool(ep["shots"]) for ep in state["episodes"]),
            "shots": len(shots), "videos_ready": sum(ready(s) for s in shots), "episodes_exported": sum(bool(ep.get("export")) for ep in state["episodes"]),
            "planned_seconds": sum(s["duration"] for s in shots), "failed_jobs": sum(j["status"] in {"failed", "needs_review"} for j in state["jobs"]),
            "images_ready": sum(a["status"] == "ready" for c in state["characters"] + state["locations"] for a in c["assets"])}
        return state


def find(items, item_id):
    for item in items:
        if item["id"] == item_id:
            return item
    raise ValueError(f"未找到对象: {item_id}")


def selected_asset(owner, key="base"):
    selected = owner.get("selected", {})
    asset_id = selected.get(key) if isinstance(selected, dict) else selected
    if not asset_id:
        return None
    return next((a for a in owner["assets"] if a["id"] == asset_id and a["status"] == "ready" and not a.get("stale")), None)
