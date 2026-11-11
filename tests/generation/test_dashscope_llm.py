"""Offline contract tests against a local HTTP server; no paid API requests."""

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

from ai.llm import (
    DashScopeLLMClient, LLMAPIError, LLMConfig, LLMResponseError,
    LLMTransportError, build_messages, call_llm,
)
from ai.llm.cli import main


ROOT = Path(__file__).resolve().parents[2]


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env_file = Path(self.directory.name) / ".env"
        self.env_file.write_text("", encoding="utf-8")
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_all_env_options_and_precedence(self):
        self.env_file.write_text(
            'export DASHSCOPE_API_KEY="local-secret"\n'
            'DASHSCOPE_LLM_BASE_URL=https://workspace.cn-beijing.maas.aliyuncs.com\n'
            'DASHSCOPE_LLM_MODEL=qwen-plus\nDASHSCOPE_LLM_TIMEOUT=31\n'
            'DASHSCOPE_LLM_TEMPERATURE=0.2\nDASHSCOPE_LLM_MAX_TOKENS=888\n'
            'DASHSCOPE_LLM_ENABLE_THINKING=true\n'
            'DASHSCOPE_LLM_SYSTEM_PROMPT="请用中文 # 保留注释符"\n', encoding="utf-8",
        )
        os.environ["DASHSCOPE_LLM_MODEL"] = "system-model"
        config = LLMConfig.from_env(self.env_file, temperature=0)
        self.assertEqual(config.api_key, "local-secret")
        self.assertNotIn("local-secret", repr(config))
        self.assertEqual(config.model, "system-model")
        self.assertEqual(config.temperature, 0)
        self.assertEqual(config.timeout, 31)
        self.assertEqual(config.max_tokens, 888)
        self.assertTrue(config.enable_thinking)
        self.assertEqual(config.system_prompt, "请用中文 # 保留注释符")
        self.assertTrue(config.base_url.endswith("/compatible-mode/v1"))
        self.assertEqual(LLMConfig.from_env(self.env_file, model="explicit-model").model, "explicit-model")

    def test_auto_and_blank_generation_settings_are_omitted(self):
        self.env_file.write_text(
            "DASHSCOPE_LLM_ENABLE_THINKING=auto\nDASHSCOPE_LLM_TEMPERATURE=\n"
            "DASHSCOPE_LLM_MAX_TOKENS=\n", encoding="utf-8",
        )
        client = DashScopeLLMClient(env_file=self.env_file)
        payload = client.build_payload(build_messages("你好"))
        self.assertEqual(set(payload), {"model", "messages", "stream"})

    def test_bad_settings_and_video_url_fail_before_network(self):
        for settings in (
            {"timeout": 0}, {"timeout": float("nan")}, {"timeout": True},
            {"temperature": -1}, {"temperature": float("inf")},
            {"max_tokens": True}, {"max_tokens": 1.5}, {"model": " "},
            {"enable_thinking": "false"}, {"api_key": "key\nheader"},
            {"base_url": "https://dashscope.aliyuncs.com/api/v1"},
            {"base_url": "https://user:secret@example.com"},
            {"base_url": "https://example.com:invalid"},
            {"base_url": "https://example.com/compatible-mode/v1?key=x"},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                DashScopeLLMClient(env_file=self.env_file, **settings)
        for name, value in (("MAX_TOKENS", "1.2"), ("TIMEOUT", "bad"), ("ENABLE_THINKING", "perhaps")):
            with patch.dict(os.environ, {f"DASHSCOPE_LLM_{name}": value}):
                with self.assertRaisesRegex(ValueError, f"DASHSCOPE_LLM_{name}"):
                    DashScopeLLMClient(env_file=self.env_file)

    def test_import_does_not_load_environment(self):
        result = subprocess.run(
            [sys.executable, "-c", "import os; import ai.llm; print(os.getenv('DASHSCOPE_LLM_MODEL'))"],
            cwd=ROOT, env={"PATH": os.defpath}, text=True, capture_output=True, check=True,
        )
        self.assertEqual(result.stdout.strip(), "None")


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                state = cls.state
                state["calls"].append((self.path, dict(self.headers), payload))
                self.send_response(state["status"])
                self.send_header("Content-Type", state["content_type"])
                self.send_header("Content-Length", str(len(state["body"])))
                self.send_header("x-request-id", "header-request-id")
                if state["status"] == 302:
                    self.send_header("Location", "/redirect-target")
                self.end_headers()
                self.wfile.write(state["body"])

            def do_GET(self):
                cls.state["calls"].append((self.path, dict(self.headers), None))
                self.send_response(200)
                self.end_headers()

        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env_file = Path(self.directory.name) / ".env"
        self.env_file.write_text("", encoding="utf-8")
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.result = {
            "id": "chatcmpl-offline", "model": "qwen-plus",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "你好，世界"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        }
        type(self).state = {"calls": [], "status": 200, "content_type": "application/json",
                            "body": json.dumps(self.result, ensure_ascii=False).encode("utf-8")}
        self.client = DashScopeLLMClient(
            env_file=self.env_file, api_key="test-secret", base_url=self.base,
            system_prompt="请简洁回答", timeout=2,
        )

    def set_json(self, result, status=200):
        self.state.update(body=json.dumps(result).encode(), status=status, content_type="application/json")

    def set_stream(self, *chunks, done=True):
        frames = [": heartbeat\r\n\r\n"]
        frames.extend("data: " + json.dumps(chunk, ensure_ascii=False) + "\r\n\r\n" for chunk in chunks)
        if done:
            frames.append("data: [DONE]\r\n\r\n")
        self.state.update(body="".join(frames).encode("utf-8"), content_type="text/event-stream; charset=utf-8")

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--env-file", str(self.env_file), "--base-url", self.base, *args])
        return code, out.getvalue(), err.getvalue()

    def test_atomic_function_and_reusable_client_contract(self):
        self.assertEqual(self.client.generate("你好", temperature=0, max_tokens=33), "你好，世界")
        path, headers, payload = self.state["calls"][0]
        self.assertEqual(path, "/compatible-mode/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer test-secret")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotIn("X-Dashscope-Async", headers)
        self.assertEqual(payload["messages"], build_messages("你好", "请简洁回答"))
        self.assertFalse(payload["enable_thinking"])
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(payload["max_tokens"], 33)
        self.assertEqual(call_llm("hi", env_file=self.env_file, api_key="another-key", base_url=self.base), "你好，世界")

    def test_multiturn_raw_usage_json_and_tool_calls(self):
        messages = build_messages("记住数字7") + [
            {"role": "assistant", "content": "记住了"}, {"role": "user", "content": "刚才是什么数字？"},
        ]
        before = json.dumps(messages)
        result = self.client.chat(messages, extra_body={"response_format": {"type": "json_object"}})
        self.assertEqual(result["usage"]["total_tokens"], 7)
        self.assertEqual(json.dumps(messages), before)
        self.assertEqual(self.state["calls"][-1][2]["messages"], messages)
        self.assertEqual(self.state["calls"][-1][2]["response_format"], {"type": "json_object"})
        self.result["choices"][0]["message"] = {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "tool-1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
        }
        self.set_json(self.result)
        self.assertEqual(self.client.chat(messages)["choices"][0]["message"]["tool_calls"][0]["id"], "tool-1")
        with self.assertRaisesRegex(LLMResponseError, "chat"):
            self.client.generate("工具调用")

    def test_stream_preserves_raw_chunks_and_returns_only_answer_text(self):
        chunks = [
            {"choices": [{"delta": {"role": "assistant", "content": None}}]},
            {"choices": [{"delta": {"reasoning_content": "reasoning-only"}}]},
            {"choices": [{"delta": {"content": "你好，"}}]},
            {"choices": [{"delta": {"content": "世界"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"total_tokens": 12}},
        ]
        self.set_stream(*chunks)
        self.assertEqual(list(self.client.stream("hi")), ["你好，", "世界"])
        self.assertEqual(list(self.client.stream_chat(build_messages("hi"))), chunks)
        payload = self.state["calls"][-1][2]
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["stream_options"], {"include_usage": True})

    def test_sse_multiline_fields_and_tool_deltas(self):
        self.state.update(
            body=b'event: message\ndata: {"choices":\ndata: [{"delta": {"tool_calls": []}}]}\n\ndata: [DONE]\n\n',
            content_type="text/event-stream",
        )
        self.assertEqual(list(self.client.stream_chat(build_messages("hi"))),
                         [{"choices": [{"delta": {"tool_calls": []}}]}])

    def test_incomplete_and_malformed_streams_fail_explicitly(self):
        chunk = {"choices": [{"delta": {"content": "partial"}}]}
        self.set_stream(chunk, done=False)
        iterator = self.client.stream("hi")
        self.assertEqual(next(iterator), "partial")
        with self.assertRaisesRegex(LLMResponseError, "DONE"):
            next(iterator)
        for invalid in ({"choices": [{}]}, {"choices": [{"delta": {"content": 4}}]}, {"unrelated": []}):
            self.set_stream(invalid)
            with self.assertRaises(LLMResponseError):
                list(self.client.stream("hi"))
        self.state.update(body=b"data: not-json\n\n", content_type="text/event-stream")
        with self.assertRaisesRegex(LLMResponseError, "JSON"):
            list(self.client.stream("hi"))

    def test_api_error_metadata_no_retry_or_secret_in_message(self):
        self.set_json({"error": {"code": "invalid_api_key", "message": "bad test-secret"}}, status=401)
        with self.assertRaises(LLMAPIError) as context:
            self.client.generate("hi")
        error = context.exception
        self.assertEqual((error.status_code, error.code, error.request_id), (401, "invalid_api_key", "header-request-id"))
        self.assertNotIn("test-secret", str(error))
        self.assertEqual(len(self.state["calls"]), 1)
        self.set_stream({"error": {"code": "rate_limit", "message": "wait"}})
        with self.assertRaises(LLMAPIError):
            list(self.client.stream("hi"))
        self.set_json({"code": "BadRequest", "message": "bad input"})
        with self.assertRaises(LLMAPIError):
            self.client.generate("hi")

    def test_bad_response_and_http_error_pages(self):
        for raw in (b"<html>bad gateway</html>", b"[]", b"{}", b'{"choices":[null]}'):
            self.state["body"] = raw
            with self.assertRaises(LLMResponseError):
                self.client.generate("hi")
        self.state.update(body=b"<html>gateway</html>", status=502)
        with self.assertRaises(LLMAPIError) as context:
            self.client.generate("hi")
        self.assertEqual(context.exception.status_code, 502)
        self.state.update(body=b"", status=302)
        count = len(self.state["calls"])
        with self.assertRaises(LLMAPIError):
            self.client.generate("hi")
        self.assertEqual(len(self.state["calls"]), count + 1)

    def test_transport_failure_is_wrapped_and_not_retried(self):
        for failure in (URLError("offline"), TimeoutError("timeout")):
            with patch.object(self.client._opener, "open", side_effect=failure) as opened:
                with self.assertRaises(LLMTransportError):
                    self.client.generate("hi")
                opened.assert_called_once()

    def test_validation_dry_run_and_missing_credentials_do_not_request(self):
        for messages, options in (([], {}), ([{}], {}), ("hi", {}),
                                  (build_messages("hi"), {"extra_body": {"stream": True}}),
                                  (build_messages("hi"), {"extra_body": {"top_p": float("nan")}})):
            with self.subTest(messages=messages, options=options), self.assertRaises(ValueError):
                self.client.chat(messages, **options)
        code, out, err = self.run_cli("--prompt", "hi", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["url"], self.base + "/compatible-mode/v1/chat/completions")
        self.assertNotIn("Authorization", out)
        with self.assertRaisesRegex(ValueError, "DASHSCOPE_API_KEY"):
            DashScopeLLMClient(env_file=self.env_file).generate("hi")
        self.assertEqual(self.state["calls"], [])

    def test_cli_text_json_file_stream_and_errors(self):
        os.environ["DASHSCOPE_API_KEY"] = "cli-key"
        prompt = Path(self.directory.name) / "prompt.md"
        prompt.write_text("请写分镜", encoding="utf-8")
        output = Path(self.directory.name) / "answer.json"
        code, out, err = self.run_cli("--prompt-file", str(prompt), "--json", "--output", str(output))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), self.result)
        self.assertEqual(json.loads(output.read_text()), self.result)
        messages = Path(self.directory.name) / "messages.json"
        messages.write_text(json.dumps(build_messages("你好")), encoding="utf-8")
        code, out, err = self.run_cli("--messages-file", str(messages))
        self.assertEqual((code, out.strip(), err), (0, "你好，世界", ""))
        self.set_stream({"choices": [{"delta": {"content": "流式输出"}}]})
        code, out, err = self.run_cli("--prompt", "你好", "--stream")
        self.assertEqual((code, out.strip(), err), (0, "流式输出", ""))
        self.set_json({"error": {"message": "invalid"}}, status=400)
        code, out, err = self.run_cli("--prompt", "你好")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("HTTP 400", err)
        self.assertNotIn("Traceback", err)

    def test_script_runs_from_another_directory(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/ai/bailian_llm.py"), "--env-file", str(self.env_file),
             "--prompt", "hello", "--dry-run"],
            cwd=self.directory.name, env={"PATH": os.defpath}, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["body"]["model"], "qwen-plus")


if __name__ == "__main__":
    unittest.main()
