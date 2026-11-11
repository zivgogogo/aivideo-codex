#!/bin/zsh
set -eu
CANVAS_WORKSPACE="${0:A:h}"
cd "$CANVAS_WORKSPACE"
CANVAS_PYTHON="$CANVAS_WORKSPACE/.venv/bin/python"
if [[ ! -x "$CANVAS_PYTHON" ]]; then
  CANVAS_PYTHON="$(command -v python3)"
fi
if ! "$CANVAS_PYTHON" -c 'import PIL, imageio_ffmpeg' >/dev/null 2>&1; then
  print '缺少运行依赖。请在项目目录执行：python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt'
  read '?按回车退出…'
  exit 1
fi
"$CANVAS_PYTHON" - <<'PY'
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

root=Path.cwd()
url='http://127.0.0.1:8766'
def ready():
    try:
        with urlopen(url+'/api/canvas/projects',timeout=2) as response:
            return isinstance(json.load(response),list)
    except Exception:
        return False
if not ready():
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:sock.bind(('127.0.0.1',8766))
        except OSError:raise SystemExit('8766端口被其他程序占用，请先检查；未停止任何现有进程。')
    logs=root/'production/canvas-server.log'
    logs.parent.mkdir(exist_ok=True)
    with logs.open('ab') as output:
        process=subprocess.Popen([sys.executable,'-m','backend','serve','--canvas-only','--port','8766'],
                                 cwd=root,stdout=output,stderr=output,start_new_session=True,stdin=subprocess.DEVNULL)
    for _ in range(40):
        if ready():break
        if process.poll() is not None:raise SystemExit('画布启动失败，详见 production/canvas-server.log')
        time.sleep(.25)
    else:raise SystemExit('启动超时，详见 production/canvas-server.log')
print('映序短剧画布已就绪：'+url)
if os.environ.get('CANVAS_SKIP_OPEN') != '1':
    subprocess.run(['open',url],check=False)
PY
