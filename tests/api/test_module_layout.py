"""Regression coverage for module aliases and frontend files after reorganization."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

from core.paths import FRONTEND_ROOT, PROJECT_ROOT


class ModuleLayoutTests(unittest.TestCase):
    def test_server_serves_relocated_frontend_with_isolated_empty_projects(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        opener = build_opener(ProxyHandler({}))
        base = f"http://127.0.0.1:{port}"
        with tempfile.TemporaryDirectory() as folder:
            process = subprocess.Popen(
                [sys.executable, "-m", "backend", "serve", "--canvas-only", "--port", str(port),
                 "--project", str(Path(folder) / "legacy"), "--canvas-root", str(Path(folder) / "canvas")],
                cwd=PROJECT_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 10
                while True:
                    self.assertIsNone(process.poll(), "isolated server exited before startup")
                    try:
                        with opener.open(base + "/api/canvas/projects", timeout=0.5) as response:
                            self.assertEqual(json.load(response), [])
                        break
                    except URLError:
                        if time.monotonic() >= deadline:
                            self.fail("isolated server failed to start")
                        time.sleep(0.05)
                files = {"/": "canvas/canvas.html", "/canvas": "canvas/canvas.html",
                         "/studio": "workbench/index.html",
                         "/app-update.js": "shared/app-update.js", "/app-update.css": "shared/app-update.css"}
                for name in ["canvas.js", "canvas.css", "canvas-sync.js", "canvas-compose.js"]:
                    files["/" + name] = "canvas/" + name
                for url, relative in files.items():
                    with self.subTest(url=url), opener.open(base + url, timeout=2) as response:
                        self.assertEqual(response.read(), (FRONTEND_ROOT / relative).read_bytes())
                with opener.open(base + "/api/system/update", timeout=2) as response:
                    self.assertEqual(json.load(response)["repository"], "https://github.com/zivgogogo/aivideo-codex")
                with self.assertRaises(HTTPError) as error:
                    opener.open(base + "/.env", timeout=2)
                self.assertEqual(error.exception.code, 404)
                self.assertFalse((Path(folder) / "legacy/project.json").exists())
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
