"""Offline request/response and CLI checks. Never calls the real provider."""

import base64
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError

from ai.image import client as qwen


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j9CkAAAAASUVORK5CYII="
)
IMAGE = "https://example.com/reference.png"


class PayloadTests(unittest.TestCase):
    def test_same_model_and_parameters_for_both_modes(self):
        text = qwen.build_payload("画一只猫", seed=0, n=2, size="1536x1024")
        edit = qwen.build_payload("换成蓝色背景", [IMAGE], seed=0, n=2, size="1536*1024")
        self.assertEqual(text["model"], "qwen-image-3.0-pro")
        self.assertEqual(text["model"], edit["model"])
        self.assertEqual(text["parameters"], edit["parameters"])
        self.assertEqual(text["input"]["messages"], [
            {"role": "user", "content": [{"text": "画一只猫"}]}
        ])
        self.assertEqual(edit["input"]["messages"][0]["content"], [
            {"image": IMAGE}, {"text": "换成蓝色背景"}
        ])
        self.assertEqual(text["parameters"]["seed"], 0)
        self.assertNotIn("size", qwen.build_payload("x", size="auto")["parameters"])

    def test_local_url_and_base64_inputs_preserve_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "参考图.jpg"  # Detect actual bytes, not suffix.
            path.write_bytes(PNG)
            uri = "data:image/png;base64," + base64.b64encode(PNG).decode()
            payload = qwen.build_payload("把图1放进图2，参考图3风格", [path, IMAGE, uri])
            self.assertEqual(payload["input"]["messages"][0]["content"], [
                {"image": uri}, {"image": IMAGE}, {"image": uri},
                {"text": "把图1放进图2，参考图3风格"}
            ])
            self.assertEqual(qwen.image_input(path), uri)
        self.assertEqual(qwen.build_payload("x", IMAGE)["input"]["messages"][0]["content"][0],
                         {"image": IMAGE})

    def test_provider_limits_and_agent_restriction(self):
        invalid = [
            {"n": 0}, {"n": 7}, {"n": True}, {"seed": -1}, {"seed": 2147483648},
            {"seed": False}, {"images": [IMAGE] * 4}, {"size": "100x100"},
            {"size": "4096x4096"}, {"size": "8192x256"}, {"size": "0x1024"},
            {"images": [IMAGE], "prompt_extend_mode": "agent"},
            {"prompt_extend": 1}, {"enable_thinking": "false"}, {"watermark": 0},
        ]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                qwen.build_payload("x", **options)
        # Limits apply to pixel area, not to each side separately.
        self.assertEqual(qwen.build_payload("x", size="2688x1536")["parameters"]["size"], "2688*1536")
        qwen.build_payload("x", prompt_extend_mode="agent")
        with self.assertRaises(ValueError):
            qwen.build_payload("  ")

    def test_bad_image_and_mime_are_rejected(self):
        for source in ("data:image/png;base64,%%%%", "ftp://example.com/a.png",
                       "data:image/jpeg;base64," + base64.b64encode(PNG).decode()):
            with self.subTest(source=source), self.assertRaises(ValueError):
                qwen.image_input(source)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.png"
            with path.open("wb") as handle:
                handle.truncate(qwen.MAX_IMAGE_BYTES + 1)
            with self.assertRaisesRegex(ValueError, "10MB"):
                qwen.image_input(path)
        with self.assertRaises(ValueError):
            qwen.image_input("data:image/png;base64," + base64.b64encode(b"not an image").decode())


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.status = 200
        self.body = None
        self.png = PNG
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.calls.append(("POST", self.path, dict(self.headers), data))
                body = owner.body if owner.body is not None else owner.response()
                content = json.dumps(body).encode("utf-8")
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def do_GET(self):
                owner.calls.append(("GET", self.path, dict(self.headers), None))
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(owner.png)))
                self.end_headers()
                self.wfile.write(owner.png)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        env = patch.dict(os.environ, {"DASHSCOPE_API_KEY": "offline-test-key"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        config = patch.object(qwen, "load_env")
        config.start()
        self.addCleanup(config.stop)

    def response(self):
        return {"request_id": "test-request", "output": {"choices": [
            {"message": {"role": "assistant", "content": [
                {"image": self.base + "/first.png"}, {"text": "extra metadata"}
            ]}},
            {"message": {"role": "assistant", "content": [{"image": self.base + "/second.png"}]}}
        ]}, "usage": {"output_image_count": 2}}

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = qwen.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_sync_endpoint_no_async_header_and_download_without_credentials(self):
        for images in (None, [IMAGE]):
            result = qwen.generate_image("测试", images, base_url=self.base)
            method, path, headers, payload = self.calls[-1]
            self.assertEqual((method, path), ("POST", "/api/v1" + qwen.API_PATH))
            self.assertEqual(headers["Authorization"], "Bearer offline-test-key")
            self.assertNotIn("X-Dashscope-Async", headers)
            self.assertEqual(payload["model"], qwen.MODEL)
            self.assertEqual(len(qwen.extract_image_urls(result)), 2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.png"
            qwen.download_image(self.base + "/first.png", path)
            self.assertEqual(path.read_bytes(), PNG)
            self.assertNotIn("Authorization", self.calls[-1][2])

    def test_cli_saves_raw_response_and_all_images(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "cat.png"
            code, stdout, stderr = self.run_cli(
                "--prompt", "猫", "--image", IMAGE, "--n", "2",
                "--output", str(output), "--base-url", self.base + "/api/v1/"
            )
            self.assertEqual(code, 0, stderr)
            summary = json.loads(stdout)
            self.assertEqual(len(summary["local_paths"]), 2)
            self.assertEqual((output.parent / "cat_1.png").read_bytes(), PNG)
            self.assertEqual((output.parent / "cat_2.png").read_bytes(), PNG)
            self.assertEqual(json.loads(output.with_suffix(".json").read_text()), self.response())

    def test_download_failure_keeps_response_and_existing_image(self):
        self.png = b'<html>upstream error</html>'
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "cat.png"
            first = output.with_name("cat_1.png")
            first.write_bytes(b"existing image")
            code, _, stderr = self.run_cli("--prompt", "猫", "--output", str(output),
                                          "--base-url", self.base)
            self.assertEqual(code, 1)
            self.assertIn("PNG", stderr)
            self.assertTrue(output.with_suffix(".json").is_file())
            self.assertEqual(first.read_bytes(), b"existing image")
            self.assertFalse(list(output.parent.glob("*.part")))
            self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)

    def test_existing_response_fails_before_paid_post(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "cat.png"
            output.with_suffix(".json").write_text("existing response")
            code, _, _ = self.run_cli("--prompt", "猫", "--output", str(output),
                                     "--base-url", self.base)
            self.assertEqual(code, 1)
            self.assertEqual(self.calls, [])

    def test_response_cannot_collide_with_numbered_images(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "cat.png"
            code, _, stderr = self.run_cli(
                "--prompt", "猫", "--n", "2", "--output", str(output),
                "--save-response", str(output.with_name("cat_1.png")),
                "--base-url", self.base)
            self.assertEqual(code, 1)
            self.assertIn("同一路径", stderr)
            self.assertEqual(self.calls, [])

    def test_invalid_output_count_is_rejected_before_allocating_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            code, _, stderr = self.run_cli(
                "--prompt", "猫", "--n", "999999999", "--base-url", self.base,
                "--output", str(Path(directory) / "cat.png"))
            self.assertEqual(code, 1)
            self.assertIn("1 到 6", stderr)
            self.assertEqual(self.calls, [])

    def test_no_download_keeps_urls_and_has_no_get(self):
        with tempfile.TemporaryDirectory() as directory:
            code, stdout, stderr = self.run_cli(
                "--prompt", "猫", "--no-download", "--output", str(Path(directory) / "cat.png"),
                "--base-url", self.base)
            self.assertEqual(code, 0, stderr)
            self.assertEqual(json.loads(stdout)["local_paths"], [])
            self.assertEqual([c[0] for c in self.calls], ["POST"])

    def test_api_failures_are_clear_and_not_retried(self):
        self.status = 401
        self.body = {"code": "InvalidApiKey", "message": "Invalid API-key", "request_id": "failure-id"}
        with self.assertRaisesRegex(qwen.ImageGenerationError, "InvalidApiKey.*failure-id"):
            qwen.generate_image("test", base_url=self.base)
        self.assertEqual(len(self.calls), 1)
        with patch.object(qwen, "urlopen", side_effect=URLError("timeout")) as request:
            with self.assertRaisesRegex(qwen.ImageGenerationError, "未自动重试"):
                qwen.generate_image("test", base_url=self.base)
            request.assert_called_once()
        self.status = 200
        with self.assertRaisesRegex(qwen.ImageGenerationError, "InvalidApiKey"):
            qwen.generate_image("test", base_url=self.base)

    def test_no_image_response_is_preserved_for_diagnosis(self):
        self.body = {"output": {"choices": []}, "request_id": "no-image"}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "cat.png"
            code, _, stderr = self.run_cli("--prompt", "猫", "--output", str(output),
                                          "--base-url", self.base)
            self.assertEqual(code, 1)
            self.assertIn("未返回生成图片", stderr)
            self.assertEqual(json.loads(output.with_suffix(".json").read_text()), self.body)


class EnvironmentAndEntryTests(unittest.TestCase):
    def test_env_loading_never_evaluates_shell_and_preserves_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text('export DASHSCOPE_API_KEY="file-key"\nKEEP=file\nLITERAL=\'$(whoami)\'\n',
                            encoding="utf-8")
            os.environ["KEEP"] = "environment"
            qwen.load_env(path)
            self.assertEqual(os.environ["DASHSCOPE_API_KEY"], "file-key")
            self.assertEqual(os.environ["KEEP"], "environment")
            self.assertEqual(os.environ["LITERAL"], "$(whoami)")

    def test_real_entry_from_another_directory_dry_run_without_key(self):
        script = Path(__file__).resolve().parents[2] / "scripts/ai/qwen_image.py"
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "prompt.md"
            prompt.write_text("改变参考图的背景", encoding="utf-8-sig")
            run = subprocess.run([
                sys.executable, str(script), "--prompt-file", str(prompt),
                "--image", IMAGE, "--dry-run", "--env-file", "/nonexistent/env",
            ], cwd=directory, env={"PATH": os.environ.get("PATH", "")},
                text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            content = json.loads(run.stdout)["input"]["messages"][0]["content"]
            self.assertEqual(content, [{"image": IMAGE}, {"text": "改变参考图的背景"}])
            self.assertFalse((Path(directory) / "outputs").exists())


if __name__ == "__main__":
    unittest.main()
