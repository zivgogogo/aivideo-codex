#!/bin/zsh
set -e
TASK_ROOT="${0:A:h}"
cd "$TASK_ROOT"
STUDIO_URL="http://127.0.0.1:8765/studio"
STUDIO_PYTHON="$TASK_ROOT/.venv/bin/python"
if [ ! -x "$STUDIO_PYTHON" ]; then
  STUDIO_PYTHON="$(command -v python3)"
fi

# Read-only probe: distinguish our existing project from an unrelated listener.
studio_probe() {
  "$STUDIO_PYTHON" - "$TASK_ROOT" <<'PY'
import json
from pathlib import Path
import socket
import sys
from urllib.request import ProxyHandler, build_opener

try:
    with socket.create_connection(("127.0.0.1", 8765), timeout=0.6):
        pass
except OSError:
    raise SystemExit(1)  # Nothing listening: starting a server is permitted.

try:
    root = Path(sys.argv[1])
    sys.path.insert(0, str(root))
    from core.config import load_env
    from core.store import Store
    load_env()
    state_path = Store().path
    expected = json.loads(state_path.read_text())["project"] if state_path.exists() else {"id": "default"}
    url = "http://127.0.0.1:8765/api/state"
    with build_opener(ProxyHandler({})).open(url, timeout=0.6) as response:
        if response.geturl() != url:
            raise ValueError("unexpected redirect")
        state = json.load(response)
    valid = (isinstance(state, dict) and type(state.get("schema_version")) is int and state["schema_version"] == 1
             and isinstance(state.get("project"), dict)
             and isinstance(expected.get("id"), str) and bool(expected["id"])
             and state["project"].get("id") == expected.get("id")
             and (not expected.get("title") or state["project"].get("title") == expected["title"])
             and all(isinstance(state.get(key), list) for key in ("characters", "locations", "episodes", "jobs", "exports"))
             and isinstance(state.get("stats"), dict))
    if not valid:
        raise ValueError("different service or project")
except Exception:
    print("8765端口已被其他服务占用，或工作台尚未就绪/项目不匹配。请检查现有进程后再启动；未修改项目或中断任何服务。", file=sys.stderr)
    raise SystemExit(2)
PY
}

if studio_probe; then
  print "已检测到本项目工作台，直接打开现有页面。"
  open "$STUDIO_URL"
  exit 0
else
  STUDIO_PROBE_RESULT=$?
  if [ "$STUDIO_PROBE_RESULT" -ne 1 ]; then
    exit "$STUDIO_PROBE_RESULT"
  fi
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi
STUDIO_PYTHON="$TASK_ROOT/.venv/bin/python"
# Wait in a small background helper; keep the server in the foreground for logs.
(
  for STUDIO_READY_ATTEMPT in {1..50}; do
    if studio_probe >/dev/null 2>&1; then
      open "$STUDIO_URL"
      exit 0
    fi
    sleep 0.2
  done
  print -u2 "工作台未在等待时间内就绪，请查看此终端中的启动日志。"
) &
exec "$STUDIO_PYTHON" -m backend serve --port 8765
