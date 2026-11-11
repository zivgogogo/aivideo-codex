import base64
from collections import deque
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

from ai.video import (
    DashScopeAPIError,
    DashScopeError,
    DashScopeTaskError,
    DashScopeTimeoutError,
    DashScopeTransportError,
    DashScopeVideoClient,
    build_frame_video_payload,
    build_image_video_payload,
    build_reference_video_payload,
    build_text_video_payload,
    build_video_edit_payload,
    build_multimodal_reference_payload,
    download_video,
)
from ai.video.cli import main
from ai.video.config import PROJECT_ROOT, load_env
from ai.video.media import IMAGE_MAX_BYTES, image_url


IMAGE_A = "https://example.com/first.png"
IMAGE_B = "https://example.com/last.png"
VIDEO = "https://example.com/input.mp4"
# Tiny fixture exercises transport/encoding only. Provider dimension checks are remote.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j9CkAAAAASUVORK5CYII="
)


class PayloadTests(unittest.TestCase):
    def test_wan_multimodal_reference_contract_and_limits(self):
        p=build_multimodal_reference_payload('视频1与视频2在图1的场景交谈。',[IMAGE_A],[VIDEO,'https://example.com/other.mp4'],duration=7,ratio='9:16')
        self.assertEqual(p['model'],'wan2.7-r2v-2026-06-12')
        self.assertEqual([m['type'] for m in p['input']['media']],['reference_image','reference_video','reference_video'])
        self.assertEqual(p['parameters']['duration'],7);self.assertFalse(p['parameters']['prompt_extend'])
        with self.assertRaisesRegex(ValueError,'2~10'):build_multimodal_reference_payload('x',[],[VIDEO],duration=11)
        self.assertEqual(build_multimodal_reference_payload('x',[IMAGE_A],duration=15)['parameters']['duration'],15)
        with self.assertRaisesRegex(ValueError,'1~5'):build_multimodal_reference_payload('x',[IMAGE_A]*4,[VIDEO]*2)
        with self.assertRaisesRegex(ValueError,'1~5'):build_multimodal_reference_payload('x')
        with self.assertRaisesRegex(ValueError,'列表'):build_multimodal_reference_payload('x',reference_videos=VIDEO)
        with self.assertRaisesRegex(ValueError,'5000'):build_multimodal_reference_payload('x'*5001,[],[VIDEO])
        with self.assertRaisesRegex(ValueError,'resolution'):build_multimodal_reference_payload('x',[],[VIDEO],resolution='480P')

    def test_wan_reference_cli_dry_run_without_credentials(self):
        out=io.StringIO()
        with patch.dict(os.environ,{},clear=True),redirect_stdout(out):
            status=main(['wan-ref','--prompt','视频1和视频2交谈','--reference-video',VIDEO,'--reference-video','https://example.com/b.mp4','--duration','6','--dry-run'])
        self.assertEqual(status,0);p=json.loads(out.getvalue())
        self.assertEqual(p['model'],'wan2.7-r2v-2026-06-12');self.assertEqual(len(p['input']['media']),2)

    def test_four_happyhorse_request_contracts(self):
        cases = [
            (
                build_text_video_payload("test", duration=15, ratio="9:21", seed=0),
                "happyhorse-1.1-t2v",
                None,
            ),
            (
                build_image_video_payload("test", first_frame=IMAGE_A),
                "happyhorse-1.1-i2v",
                [{"type": "first_frame", "url": IMAGE_A}],
            ),
            (
                build_reference_video_payload(
                    "[Image 1] with [Image 2]", [IMAGE_A, IMAGE_B]
                ),
                "happyhorse-1.1-r2v",
                [
                    {"type": "reference_image", "url": IMAGE_A},
                    {"type": "reference_image", "url": IMAGE_B},
                ],
            ),
            (
                build_video_edit_payload(
                    "edit", VIDEO, reference_images=[IMAGE_B], audio_setting="origin"
                ),
                "happyhorse-1.0-video-edit",
                [
                    {"type": "video", "url": VIDEO},
                    {"type": "reference_image", "url": IMAGE_B},
                ],
            ),
        ]
        for payload, model, media in cases:
            with self.subTest(model=model):
                self.assertEqual(payload["model"], model)
                self.assertEqual(payload["input"].get("media"), media)
                self.assertFalse(payload["parameters"]["watermark"])
        self.assertNotIn("ratio", cases[1][0]["parameters"])
        self.assertNotIn("duration", cases[3][0]["parameters"])
        self.assertNotIn("ratio", cases[3][0]["parameters"])
        self.assertEqual(cases[3][0]["parameters"]["audio_setting"], "origin")

    def test_first_last_frames_use_explicit_wan_model(self):
        payload = build_frame_video_payload(
            "transition",
            IMAGE_A,
            IMAGE_B,
            duration=30,
            audio=False,
            prompt_extend=False,
        )
        self.assertEqual(payload["model"], "wan3.0-video")
        self.assertEqual(
            payload["input"]["media"],
            [
                {"type": "first_frame", "url": IMAGE_A},
                {"type": "last_frame", "url": IMAGE_B},
            ],
        )
        self.assertEqual(payload["parameters"]["ratio"], "adaptive")
        self.assertFalse(payload["parameters"]["audio"])
        self.assertFalse(payload["parameters"]["prompt_extend"])
        self.assertEqual(
            len(build_frame_video_payload("", IMAGE_A)["input"]["media"]), 1
        )

    def test_unsupported_model_capabilities_fail_before_submission(self):
        with self.assertRaisesRegex(ValueError, "仅支持首帧"):
            build_image_video_payload("x", IMAGE_A, last_frame=IMAGE_B)
        with self.assertRaises(ValueError):
            build_text_video_payload("x", audio=False)
        with self.assertRaises(ValueError):
            build_frame_video_payload("x", IMAGE_A, model="happyhorse-1.1-i2v")
        with self.assertRaises(TypeError):
            build_video_edit_payload("x", VIDEO, duration=5)
        with self.assertRaises(TypeError):
            build_image_video_payload("x", IMAGE_A, ratio="16:9")

    def test_parameter_ranges_and_types(self):
        for options in (
            {"duration": 2},
            {"duration": 16},
            {"duration": True},
            {"duration": 5.0},
            {"seed": -1},
            {"seed": 2147483648},
            {"seed": False},
            {"watermark": 0},
            {"resolution": "4K"},
            {"ratio": "adaptive"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                build_text_video_payload("test", **options)
        for resolution in ("480P", "720P", "1080P"):
            self.assertEqual(
                build_text_video_payload("test", resolution=resolution)["parameters"][
                    "resolution"
                ],
                resolution,
            )
        with self.assertRaises(ValueError):
            build_video_edit_payload("edit", VIDEO, resolution="480P")
        for value in ("", " ", "中" * 2501, "x" * 5001):
            with self.subTest(prompt_length=len(value)), self.assertRaises(ValueError):
                build_text_video_payload(value)
        self.assertEqual(
            build_image_video_payload(first_frame=IMAGE_A)["input"]["prompt"], ""
        )

    def test_wan_text_generation_retained(self):
        payload = build_text_video_payload(
            "test", model="wan3.0-video", duration=-1, seed=-1, audio=False
        )
        self.assertEqual(payload["parameters"]["duration"], -1)
        self.assertEqual(payload["parameters"]["seed"], -1)
        with self.assertRaises(ValueError):
            build_text_video_payload("test", model="wan3.0-video", ratio="4:5")

    def test_reference_count_and_order(self):
        for count in (0, 10):
            with self.subTest(count=count), self.assertRaises(ValueError):
                build_reference_video_payload("test", [IMAGE_A] * count)
        self.assertEqual(
            len(build_reference_video_payload("test", [IMAGE_A] * 9)["input"]["media"]),
            9,
        )
        with self.assertRaises(ValueError):
            build_reference_video_payload("test", IMAGE_A)
        with self.assertRaises(ValueError):
            build_video_edit_payload("edit", VIDEO, reference_images=[IMAGE_A] * 6)
        self.assertEqual(
            len(build_video_edit_payload("edit", VIDEO)["input"]["media"]), 1
        )
        for source in (
            "input.mp4",
            "data:video/mp4;base64,AAAA",
            "file:///tmp/input.mp4",
            "ftp://example.com/a.mp4",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                build_video_edit_payload("edit", source)

    def test_local_images_data_uri_and_format_restrictions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            path.write_bytes(PNG)
            uri = image_url(path)
            self.assertEqual(base64.b64decode(uri.split(",")[1]), PNG)
            self.assertEqual(image_url(uri), uri)
            with self.assertRaisesRegex(ValueError, "MIME"):
                image_url(uri.replace("image/png", "image/jpeg"))
            with self.assertRaises(ValueError):
                image_url("data:image/png;base64,====")
            path.write_bytes(b"BM" + b"0" * 30)
            with self.assertRaises(ValueError):
                image_url(path)
            self.assertTrue(
                image_url(path, allow_bmp=True).startswith("data:image/bmp;base64,")
            )
            with path.open("wb") as handle:
                handle.truncate(IMAGE_MAX_BYTES + 1)
            with self.assertRaisesRegex(ValueError, "20MB"):
                image_url(path)


class ConfigurationTests(unittest.TestCase):
    def test_dotenv_is_data_and_does_not_override_environment(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"DASHSCOPE_API_KEY": "existing"}, clear=True),
        ):
            env = Path(directory) / ".env"
            env.write_text(
                'export DASHSCOPE_API_KEY="from-file"\nDASHSCOPE_BASE_URL="https://example.com" # comment\nLITERAL="$(do-not-execute)"\n',
                encoding="utf-8",
            )
            load_env(env)
            self.assertEqual(os.environ["DASHSCOPE_API_KEY"], "existing")
            self.assertEqual(os.environ["DASHSCOPE_BASE_URL"], "https://example.com")
            self.assertEqual(os.environ["LITERAL"], "$(do-not-execute)")
            client = DashScopeVideoClient(env_file=env)
            self.assertEqual(client.base_url, "https://example.com/api/v1")

    def test_import_and_preview_do_not_need_credentials(self):
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("DASHSCOPE_")
        }
        result = subprocess.run(
            [sys.executable, "-c", "import ai.video"],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with patch(
            "ai.video.cli.DashScopeVideoClient",
            side_effect=AssertionError("must not construct client"),
        ):
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(["t2v", "--prompt", "test", "--dry-run"]), 0)
            self.assertEqual(json.loads(out.getvalue())["model"], "happyhorse-1.1-t2v")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _handle(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.records.append(
            (
                self.command,
                self.path,
                dict(self.headers),
                json.loads(body) if body else None,
            )
        )
        status, payload, content_type = (
            self.server.responses.popleft()
            if self.server.responses
            else (500, {"code": "UnexpectedRequest"}, "application/json")
        )
        data = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_POST = _handle
    do_GET = _handle


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = Path(self.directory.name) / ".env"
        self.env.write_text("", encoding="utf-8")
        self.server.responses = deque()
        self.server.records = []
        self.client = DashScopeVideoClient(
            api_key="unit-test-key",
            base_url=self.base,
            env_file=self.env,
            request_timeout=2,
        )

    def respond(self, result, status=200, content_type="application/json"):
        self.server.responses.append((status, result, content_type))

    def task(self, status, **extra):
        return {
            "request_id": "request-1",
            "output": {"task_id": "task-1", "task_status": status, **extra},
        }

    def test_http_submit_poll_download_and_headers(self):
        self.respond(self.task("PENDING"))
        self.respond(self.task("RUNNING"))
        self.respond(
            {
                **self.task("SUCCEEDED", video_url=self.base + "/video.mp4"),
                "usage": {"duration": 5},
            }
        )
        self.respond(b"fake-mp4-video-bytes", content_type="video/mp4")
        target = Path(self.directory.name) / "nested" / "result.mp4"
        submitted, progress = [], []
        result = self.client.run(
            build_text_video_payload("test"),
            output_path=target,
            poll_interval=0.001,
            on_submitted=submitted.append,
            on_progress=progress.append,
        )
        self.assertEqual(result["local_path"], str(target.resolve()))
        self.assertEqual(result["usage"], {"duration": 5})
        self.assertEqual(target.read_bytes(), b"fake-mp4-video-bytes")
        self.assertEqual(len(submitted), 1)
        self.assertEqual(len(progress), 2)
        post, get, _, download = self.server.records
        self.assertEqual(
            post[:2], ("POST", "/api/v1/services/aigc/video-generation/video-synthesis")
        )
        self.assertEqual(post[2]["Authorization"], "Bearer unit-test-key")
        self.assertEqual(post[2]["X-Dashscope-Async"], "enable")
        self.assertEqual(post[3]["model"], "happyhorse-1.1-t2v")
        self.assertEqual(get[1], "/api/v1/tasks/task-1")
        self.assertNotIn("Authorization", download[2])

    def test_each_create_method_submits_correct_payload(self):
        cases = [
            (lambda: self.client.create_video_task("test"), "happyhorse-1.1-t2v"),
            (
                lambda: self.client.create_image_video_task("test", IMAGE_A),
                "happyhorse-1.1-i2v",
            ),
            (
                lambda: self.client.create_reference_video_task("test", [IMAGE_A]),
                "happyhorse-1.1-r2v",
            ),
            (
                lambda: self.client.create_video_edit_task("test", VIDEO),
                "happyhorse-1.0-video-edit",
            ),
            (
                lambda: self.client.create_frame_video_task("test", IMAGE_A, IMAGE_B),
                "wan3.0-video",
            ),
        ]
        for create, model in cases:
            self.respond(self.task("PENDING"))
            self.assertEqual(create()["output"]["task_id"], "task-1")
            self.assertEqual(self.server.records[-1][3]["model"], model)

    def test_create_errors_are_not_retried(self):
        self.respond(
            {"code": "Throttling", "message": "busy", "request_id": "req"}, status=503
        )
        with self.assertRaises(DashScopeAPIError) as context:
            self.client.create_video_task("test")
        self.assertEqual(context.exception.request_id, "req")
        self.assertEqual(len(self.server.records), 1)
        with patch(
            "ai.video.client.urlopen", side_effect=URLError("offline")
        ) as opened:
            with self.assertRaisesRegex(DashScopeTransportError, "未自动重试"):
                self.client.create_video_task("test")
            self.assertEqual(opened.call_count, 1)

    def test_query_transient_errors_retry_but_auth_does_not(self):
        self.respond({"code": "Throttling"}, status=429)
        self.respond({"code": "InternalError"}, status=503)
        self.respond(self.task("SUCCEEDED", video_url=VIDEO))
        self.assertEqual(
            self.client.wait_for_task("task-1", poll_interval=0.001)["output"][
                "task_status"
            ],
            "SUCCEEDED",
        )
        self.respond({"code": "InvalidApiKey"}, status=401)
        with self.assertRaises(DashScopeAPIError):
            self.client.wait_for_task("task-1", poll_interval=0.001)
        self.assertEqual(len(self.server.records), 4)

    def test_terminal_failures_and_missing_state_are_distinct(self):
        for status in ("FAILED", "CANCELED", "UNKNOWN"):
            self.respond(self.task(status, code="TaskError", message="reason"))
            with self.assertRaises(DashScopeTaskError) as context:
                self.client.wait_for_task("task-1")
            self.assertEqual(context.exception.status, status)
            self.assertEqual(context.exception.task_id, "task-1")
        self.respond({"output": {}})
        with self.assertRaisesRegex(DashScopeError, "task_status"):
            self.client.wait_for_task("task-1")

    def test_timeout_can_resume_without_another_post(self):
        with patch.object(
            self.client, "query_task_result", return_value=self.task("RUNNING")
        ):
            with self.assertRaises(DashScopeTimeoutError) as context:
                self.client.wait_for_task("task-1", timeout=0.02, poll_interval=0.01)
        self.assertEqual(context.exception.task_id, "task-1")
        self.assertEqual(
            context.exception.last_result["output"]["task_status"], "RUNNING"
        )
        self.respond(self.task("SUCCEEDED", video_url=VIDEO))
        self.client.wait_for_task("task-1")
        self.assertEqual([record[0] for record in self.server.records], ["GET"])

    def test_bad_responses_and_success_without_url(self):
        for response in (b"not JSON", {"output": {"task_status": "PENDING"}}, []):
            if isinstance(response, list):
                response = b"[]"
            self.respond(response)
            with self.assertRaises(DashScopeError):
                self.client.create_video_task("test")
        self.respond(self.task("SUCCEEDED"))
        with self.assertRaisesRegex(DashScopeError, "video_url"):
            self.client.wait_for_task("task-1")

    def test_invalid_wait_settings_do_not_create_paid_task(self):
        for options in (
            {"timeout": 0},
            {"poll_interval": -1},
            {"timeout": float("nan")},
            {"timeout": float("inf")},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.client.run(build_text_video_payload("test"), **options)
        self.assertEqual(self.server.records, [])

    def test_download_failure_keeps_previous_file_and_cleans_partial(self):
        target = Path(self.directory.name) / "result.mp4"
        target.write_bytes(b"previous")
        for payload, content_type in (
            (b"<html>Expired</html>", "text/html"),
            (b"", "video/mp4"),
        ):
            self.respond(payload, content_type=content_type)
            with self.assertRaises(DashScopeError):
                download_video(self.base + "/video.mp4", target)
            self.assertEqual(target.read_bytes(), b"previous")
            self.assertEqual(list(target.parent.glob("*.part")), [])

    def test_cli_submit_save_then_resume_and_download(self):
        task_file = Path(self.directory.name) / "task.json"
        target = Path(self.directory.name) / "result.mp4"
        connection = ["--base-url", self.base, "--env-file", str(self.env)]
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "unit-test-key"}):
            self.respond(self.task("PENDING"))
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                code = main(
                    [
                        "t2v",
                        "--prompt",
                        "test",
                        "--no-wait",
                        "--save-task",
                        str(task_file),
                        *connection,
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out.getvalue())["output"]["task_id"], "task-1")
            self.assertEqual(
                json.loads(task_file.read_text())["base_url"], self.base + "/api/v1"
            )
            self.respond(self.task("SUCCEEDED", video_url=self.base + "/video.mp4"))
            self.respond(b"mp4", content_type="video/mp4")
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                code = main(["wait", "task-1", "--output", str(target), *connection])
            self.assertEqual(code, 0)
            self.assertEqual(
                json.loads(out.getvalue())["local_path"], str(target.resolve())
            )
        self.assertEqual(sum(record[0] == "POST" for record in self.server.records), 1)

    def test_standalone_script_previews_all_modes_from_another_directory(self):
        cases = [
            ("happyhorse_t2v.py", ["--prompt", "test"], "happyhorse-1.1-t2v"),
            ("happyhorse_i2v.py", ["--first-frame", IMAGE_A], "happyhorse-1.1-i2v"),
            (
                "happyhorse_r2v.py",
                ["--prompt", "test", "--reference-image", IMAGE_A],
                "happyhorse-1.1-r2v",
            ),
            (
                "happyhorse_video_edit.py",
                ["--prompt", "edit", "--video-url", VIDEO],
                "happyhorse-1.0-video-edit",
            ),
            (
                "wan_first_last_frame.py",
                ["--first-frame", IMAGE_A, "--last-frame", IMAGE_B],
                "wan3.0-video",
            ),
        ]
        for script, arguments, model in cases:
            with self.subTest(script=script):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(PROJECT_ROOT / "scripts/ai" / script),
                        *arguments,
                        "--dry-run",
                    ],
                    cwd=self.directory.name,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["model"], model)


if __name__ == "__main__":
    unittest.main()
