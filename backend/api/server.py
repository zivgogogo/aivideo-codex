"""Loopback-only studio server. Never serves workspace secrets."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, unquote
import uuid

from core.store import Store, DEFAULT_PROJECT, safe_path, media_url, find, timestamp
from core.paths import FRONTEND_ROOT
from backend.workflow.pipeline import Pipeline


def serve(root=DEFAULT_PROJECT, port=8765, canvas_only=False, canvas_root=None):
    store = Store(root)
    static = FRONTEND_ROOT / "canvas"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # URLs never contain provider credentials; don't log request bodies.
            super().log_message(fmt, *args)

        def json_response(self, value, status=200):
            raw = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)

        def error_response(self, exc, status=400):
            self.json_response({"error": str(exc)}, status)

        def check_origin(self):
            host = self.headers.get("Host", "")
            if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                raise ValueError("此工作台只接受本机访问")
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
                raise ValueError("禁止跨站操作")

        def file_response(self, path):
            if not path.is_file():
                return self.json_response({"error": "文件不存在"}, 404)
            size = path.stat().st_size
            start, end, status = 0, size - 1, 200
            byte_range = self.headers.get("Range", "")
            if byte_range.startswith("bytes=") and "," not in byte_range:
                lo, _, hi = byte_range[6:].partition("-")
                try:
                    if lo:
                        start = int(lo)
                        end = min(int(hi), size - 1) if hi else size - 1
                    else:
                        start = max(0, size - int(hi))
                    if start > end or start < 0:
                        raise ValueError()
                    status = 206
                except ValueError:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
            self.send_response(status)
            self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("X-Content-Type-Options", "nosniff")
            if path.suffix in {'.html','.js','.css'}:self.send_header("Cache-Control", "no-store")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            try:
                with path.open("rb") as f:
                    f.seek(start)
                    remaining = end - start + 1
                    while remaining > 0:
                        block = f.read(min(1024 * 256, remaining))
                        if not block:
                            break
                        self.wfile.write(block)
                        remaining -= len(block)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            try:
                self.check_origin()
                parsed = urlsplit(self.path)
                if parsed.path.startswith('/api/canvas/'):
                    query = parse_qs(parsed.query)
                    pid = query.get('project_id', [''])[0]
                    eid = query.get('episode_id', [''])[0]
                    if parsed.path == '/api/canvas/projects':
                        return self.json_response(canvas.projects())
                    if parsed.path == '/api/canvas/project':
                        return self.json_response(canvas.detail(pid))
                    if parsed.path == '/api/canvas/graph':
                        return self.json_response(canvas.media_graph(pid,eid) if query.get('display')==['media'] else canvas.graph(pid, eid))
                    if parsed.path == '/api/canvas/source':
                        return self.json_response({'text': canvas.source(pid, eid)})
                    if parsed.path == '/api/canvas/plan-history':
                        return self.json_response(canvas.history(pid, eid))
                    if parsed.path == '/api/canvas/save-history':
                        return self.json_response(canvas.save_history(pid,eid))
                    return self.json_response({'error': '未找到接口'}, 404)
                if parsed.path.startswith('/canvas-media/'):
                    parts = unquote(parsed.path[len('/canvas-media/'):]).split('/', 1)
                    if len(parts) != 2 or not parts[1].startswith('assets/'):
                        raise ValueError('媒体路径无效')
                    return self.file_response(safe_path(canvas.path(parts[0]) / 'assets', parts[1][7:]))
                if parsed.path == "/api/state":
                    return self.json_response(store.snapshot())
                if parsed.path == "/api/source":
                    ep_id = parse_qs(parsed.query).get("episode_id", [""])[0]
                    return self.json_response({"text": find(store.read()["episodes"], ep_id)["source_text"]})
                if parsed.path.startswith("/media/"):
                    relative = unquote(parsed.path[len("/media/"):])
                    path = safe_path(store.root, relative)
                    if Path(relative).parts[0] not in {"assets", "episodes", "exports", "uploads"}:
                        raise ValueError("文件不在媒体目录")
                    return self.file_response(path)
                if parsed.path in {'/', '/canvas'}:
                    return self.file_response(static / 'canvas.html')
                if parsed.path in {'/canvas.js', '/canvas-sync.js', '/canvas-compose.js', '/canvas.css'}:
                    return self.file_response(static / parsed.path[1:])
                if parsed.path == "/studio":
                    return self.file_response(FRONTEND_ROOT / "workbench/index.html")
                return self.json_response({"error": "未找到接口"}, 404)
            except Exception as exc:
                self.error_response(exc)

        def do_POST(self):
            try:
                self.check_origin()
                parsed=urlsplit(self.path)
                if parsed.path=='/api/canvas/upload-media':
                    if self.headers.get('Content-Type','').split(';')[0]!='application/octet-stream':raise ValueError('上传文件格式无效')
                    query=parse_qs(parsed.query)
                    value=lambda key:query.get(key,[''])[0]
                    length=int(self.headers.get('Content-Length','0'))
                    return self.json_response(canvas.upload_media(value('project_id'),value('episode_id'),value('node_id'),self.rfile,length,value('filename')))
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("请使用 application/json")
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 30 * 1024 * 1024:
                    raise ValueError("请求体最大30MB")
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("请求必须是JSON对象")
                path = urlsplit(self.path).path
                if path.startswith('/api/canvas/'):
                    pid, eid, nid = (body.get(k, '') for k in ('project_id', 'episode_id', 'node_id'))
                    if path == '/api/canvas/create':
                        result = canvas.create(body)
                    elif path == '/api/canvas/initialize':
                        result = canvas.initialize(pid)
                    elif path == '/api/canvas/save':
                        result = canvas.save_graph(pid, eid, body)
                    elif path == '/api/canvas/plan':
                        result = canvas.plan(pid, eid)
                    elif path == '/api/canvas/prepare':
                        result = canvas.prepare(pid)
                    elif path == '/api/canvas/replan':
                        result = canvas.plan(pid,eid,rebuild=True)
                    elif path == '/api/canvas/restore-plan':
                        result = canvas.restore(pid,eid,body.get('archive_id'))
                    elif path == '/api/canvas/layout':
                        result = canvas.arrange(pid,eid,body.get('revision'),body.get('sizes'),body.get('mode','tidy'))
                    elif path == '/api/canvas/character-prompt':
                        result = canvas.edit_character_prompt(pid,body.get('master_id'),body.get('text'),body.get('prompt_revision'))
                    elif path == '/api/canvas/preview':
                        result = canvas.preview(pid, eid, nid)
                    elif path == '/api/canvas/reference-video':
                        result = canvas.reference_video(pid, eid, nid, body.get('url', ''))
                    elif path == '/api/canvas/generate':
                        result = canvas.generate(pid, eid, nid, bool(body.get('force_new')))
                    elif path == '/api/canvas/upload':
                        result = canvas.upload(pid, eid, nid, body)
                    elif path == '/api/canvas/select':
                        result = canvas.select(pid, eid, nid, body.get('asset_ids') or body.get('asset_id'))
                    else:
                        return self.json_response({'error': '未找到接口'}, 404)
                    return self.json_response(result)
                if pipeline is None:
                    raise ValueError('当前为画布服务；传统工作台请使用8765端口')
                if path == "/api/plan":
                    pipeline.pause_event.clear()
                    result = {"job_id": pipeline.enqueue("plan", body.get("episode_id", "series"), lambda: pipeline.plan(body.get("episode_id")))}
                elif path == "/api/review":
                    result = {"job_id": pipeline.enqueue("continuity", body.get("episode_id", "series"), lambda: pipeline.review(body.get("episode_id")))}
                elif path == "/api/image":
                    target = (body.get("character_id") or body.get("location_id"), body.get("look_id"), body.get("kind"))
                    result = {"job_id": pipeline.enqueue("image", target, lambda: pipeline.image(body))}
                elif path == "/api/select":
                    result = pipeline.select(body)
                elif path == "/api/prompt":
                    result = pipeline.edit_prompt(body)
                elif path == "/api/look":
                    result = pipeline.add_look(body)
                elif path == "/api/character":
                    result = pipeline.add_character(body)
                elif path == "/api/location":
                    result = pipeline.add_location(body)
                elif path == "/api/shot-look":
                    result = pipeline.bind_look(body)
                elif path == "/api/shot":
                    result = pipeline.edit_shot(body)
                elif path == "/api/insert-reaction":
                    result = pipeline.insert_reaction(body)
                elif path == "/api/video":
                    result = {"job_id": pipeline.enqueue("video", (body["episode_id"], body["shot_id"]), lambda: pipeline.video(body["episode_id"], body["shot_id"]))}
                elif path == "/api/select-video":
                    result = pipeline.select_video(body)
                elif path == "/api/review-video":
                    result = pipeline.review_video(body)
                elif path == "/api/render":
                    result = {"job_id": pipeline.enqueue("render", body.get("episode_id", "series"), lambda: pipeline.render(body.get("episode_id")))}
                elif path == "/api/run":
                    result = pipeline.start_run()
                elif path == "/api/pause":
                    result = pipeline.pause()
                elif path == "/api/upload":
                    from PIL import Image
                    import io
                    data = base64.b64decode(body["data"], validate=True)
                    if len(data) > 10 * 1024 * 1024:
                        raise ValueError("参考图不能超过10MB")
                    image = Image.open(io.BytesIO(data))
                    if image.format not in {"PNG", "JPEG", "WEBP"} or image.width > 8000 or image.height > 8000:
                        raise ValueError("请上传8000像素以内PNG/JPEG/WEBP")
                    image.load()
                    rel = Path("uploads") / f"{timestamp()}_{uuid.uuid4().hex[:8]}.png"
                    full = store.root / rel
                    full.parent.mkdir(parents=True, exist_ok=True)
                    image.convert("RGB").save(full)
                    result = {"path": str(rel), "url": media_url(rel)}
                else:
                    return self.json_response({"error": "未找到接口"}, 404)
                self.json_response(result)
            except Exception as exc:
                self.error_response(exc)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    pipeline = None
    canvas = None
    try:
        # Bind/listen first: a second launcher must never recover live jobs
        # before discovering that another process already owns this port.
        if not canvas_only:
            store.initialize()
            pipeline = Pipeline(store)
        from backend.canvas.studio import StudioCanvas
        canvas = StudioCanvas(canvas_root, store)
        print(f"短剧制作工作台 http://127.0.0.1:{port}", flush=True)
        server.serve_forever()
    finally:
        try:
            if canvas is not None:
                canvas.pool.shutdown(wait=False)
            if pipeline is not None:
                pipeline.pause()
        finally:
            server.server_close()
