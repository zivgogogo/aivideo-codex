"""Atomic DashScope submit/query/wait/download operations, using only stdlib."""

from collections.abc import Callable
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from .config import DEFAULT_BASE_URL, load_env
from .media import http_url
from .payloads import (
    build_frame_video_payload,
    build_image_video_payload,
    build_reference_video_payload,
    build_text_video_payload,
    build_video_edit_payload,
)


class DashScopeError(RuntimeError):
    """Base class for provider, transport, protocol, and task errors."""


class DashScopeAPIError(DashScopeError):
    def __init__(self, result: dict, status_code: int):
        self.result = result
        self.status_code = status_code
        self.code = result.get("code", "HTTPError")
        self.request_id = result.get("request_id", "")
        super().__init__(
            f"HTTP {status_code} [{self.code}] {result.get('message', '请求失败')} (request_id={self.request_id})"
        )


class DashScopeTransportError(DashScopeError):
    pass


class DashScopeTaskError(DashScopeError):
    def __init__(self, result: dict, task_id: str):
        self.result = result
        self.task_id = task_id
        output = result.get("output", {})
        self.status = output.get("task_status", "UNKNOWN")
        self.code = output.get("code", "")
        super().__init__(
            f"任务 {task_id} {self.status}: [{self.code}] {output.get('message', '任务取消、过期或不存在')}"
        )


class DashScopeTimeoutError(DashScopeError, TimeoutError):
    def __init__(self, task_id: str, last_result: dict | None):
        self.task_id = task_id
        self.last_result = last_result
        super().__init__(
            f"等待超时，task_id={task_id}。远端任务可能仍在运行，请用 query/wait 继续查询，不要重复提交。"
        )


def _positive_seconds(name, value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} 必须为大于 0 的有限秒数")


def _output(result: dict) -> dict:
    output = result.get("output")
    if not isinstance(output, dict):
        raise DashScopeError("接口响应缺少 output 对象")
    return output


def _video_url(result: dict) -> str:
    value = _output(result).get("video_url")
    if not value:
        raise DashScopeError("任务成功，但响应缺少 video_url")
    return http_url(value, "结果视频 URL")


def download_video(
    video_url: str, output_path: str | Path, *, request_timeout: float = 60
) -> str:
    """Credential-free, streamed, atomic download of an existing result URL."""
    _positive_seconds("request_timeout", request_timeout)
    http_url(video_url, "视频下载 URL")
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        request = Request(
            video_url, headers={"Accept": "video/*, application/octet-stream"}
        )
        with urlopen(request, timeout=request_timeout) as response:
            content_type = (
                response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            )
            if (
                content_type.startswith("text/")
                or "json" in content_type
                or "xml" in content_type
            ):
                raise DashScopeError("下载地址返回文本错误页，未写入视频文件")
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".dashscope-", suffix=".part", delete=False
            ) as handle:
                temporary = Path(handle.name)
                count = 0
                while chunk := response.read(1024 * 1024):
                    handle.write(chunk)
                    count += len(chunk)
            expected = response.headers.get("Content-Length")
            if not count or (expected is not None and count != int(expected)):
                raise DashScopeError(
                    "视频下载为空或不完整，请使用现有视频 URL 重试下载"
                )
        temporary.replace(path)
        return str(path.resolve())
    except (URLError, OSError, HTTPException) as exc:
        raise DashScopeTransportError(
            "视频下载失败；请使用现有结果 URL 重试下载，无需重新生成"
        ) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class DashScopeVideoClient:
    """Load credentials on construction, never on module import.

    ``base_url`` is a host root or an API root ending in /api/v1.
    The API key, model, and endpoint must belong to the same region.
    Task creation is never retried automatically to avoid duplicate paid tasks.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        env_file: str | Path | None = None,
        request_timeout: float = 60,
    ):
        load_env(env_file)
        self.api_key = (
            api_key if api_key is not None else os.getenv("DASHSCOPE_API_KEY", "")
        )
        if not self.api_key.strip():
            raise ValueError(
                "未配置 DASHSCOPE_API_KEY，请在项目根目录 .env 或系统环境变量中设置"
            )
        self.api_key = self.api_key.strip()
        root = (base_url or os.getenv("DASHSCOPE_BASE_URL") or DEFAULT_BASE_URL).rstrip(
            "/"
        )
        http_url(root, "DashScope base_url")
        self.base_url = root if root.endswith("/api/v1") else root + "/api/v1"
        _positive_seconds("request_timeout", request_timeout)
        self.request_timeout = request_timeout

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        timeout: float | None = None,
    ) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        data = None
        if payload is not None:
            headers.update(
                {"Content-Type": "application/json", "X-DashScope-Async": "enable"}
            )
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if any(str(item.get('url','')).startswith('oss://') for item in payload.get('input',{}).get('media',[])):
                headers['X-DashScope-OssResourceResolve']='enable'
        request = Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urlopen(
                request, timeout=self.request_timeout if timeout is None else timeout
            ) as response:
                status_code = response.status
                raw = response.read()
        except HTTPError as exc:
            status_code = exc.code
            try:
                raw = exc.read()
            finally:
                exc.close()
        except (URLError, OSError, HTTPException) as exc:
            detail = (
                "创建请求未自动重试；服务端可能已经接受任务，请先核实任务记录。"
                if method == "POST"
                else "查询可稍后重试。"
            )
            raise DashScopeTransportError(
                f"DashScope 网络请求失败（{type(exc).__name__}）。{detail}"
            ) from exc
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            result = {"code": "InvalidResponse", "message": "接口返回非 JSON 响应"}
            if status_code < 400:
                raise DashScopeError(
                    f"HTTP {status_code}: 接口返回非 JSON 响应"
                ) from None
        if not isinstance(result, dict):
            raise DashScopeError("接口 JSON 响应必须为对象")
        if not 200 <= status_code < 300 or result.get("code"):
            raise DashScopeAPIError(result, status_code)
        return result

    def upload_file(self, file_path: str | Path, model: str) -> str:
        """Upload a local video to private, model-bound temporary storage.

        No generation request is made. The returned object lasts 48 hours.
        https://help.aliyun.com/zh/model-studio/get-temporary-file-url
        """
        import uuid
        path=Path(file_path)
        if not path.is_file() or not 0<path.stat().st_size<=100*1024*1024:
            raise ValueError('视频文件须为0~100MB')
        policy=self._request('GET','/uploads?'+urlencode({'action':'getPolicy','model':model})).get('data',{})
        required=('upload_host','upload_dir','oss_access_key_id','policy','signature','x_oss_object_acl','x_oss_forbid_overwrite')
        if any(not policy.get(k) for k in required):raise DashScopeError('上传凭证不完整，请重试上传')
        host=policy['upload_host'];parsed=urlsplit(host)
        if parsed.scheme!='https' or not (parsed.hostname or '').endswith('.aliyuncs.com'):
            raise DashScopeError('上传存储地址无效')
        if path.stat().st_size>float(policy.get('max_file_size_mb',100))*1024*1024:
            raise ValueError('视频超过当前模型的上传大小限制')
        key=policy['upload_dir'].rstrip('/')+'/'+uuid.uuid4().hex+path.suffix.lower()
        boundary='canvas-'+uuid.uuid4().hex
        fields={'OSSAccessKeyId':policy['oss_access_key_id'],'policy':policy['policy'],'Signature':policy['signature'],
                'key':key,'x-oss-object-acl':policy['x_oss_object_acl'],
                'x-oss-forbid-overwrite':policy['x_oss_forbid_overwrite'],'success_action_status':'200'}
        prefix=b''.join((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode() for name,value in fields.items())
        prefix+=(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="video{path.suffix.lower()}"\r\nContent-Type: video/mp4\r\n\r\n').encode()
        suffix=f'\r\n--{boundary}--\r\n'.encode()
        def chunks():
            yield prefix
            with path.open('rb') as source:
                while chunk:=source.read(1024*1024):yield chunk
            yield suffix
        request=Request(host,data=chunks(),method='POST',headers={'Content-Type':'multipart/form-data; boundary='+boundary,
                        'Content-Length':str(len(prefix)+path.stat().st_size+len(suffix))})
        try:
            with urlopen(request,timeout=max(180,self.request_timeout)) as response:
                if response.status not in {200,204}:raise DashScopeError('视频临时上传失败')
        except (HTTPError,URLError,OSError,HTTPException) as exc:
            raise DashScopeTransportError('视频临时上传失败，请重试；尚未提交生成任务') from exc
        return 'oss://'+key

    def submit(self, payload: dict) -> dict:
        """Submit a builder's payload once, returning the complete create response."""
        result = self._request(
            "POST", "/services/aigc/video-generation/video-synthesis", payload
        )
        if not _output(result).get("task_id"):
            raise DashScopeError(
                "创建响应缺少 task_id；不要直接重试，请先核实服务端任务记录"
            )
        return result

    def query_task_result(
        self, task_id: str, *, request_timeout: float | None = None
    ) -> dict:
        """Single query; returns the raw response including unsuccessful task states."""
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id 不能为空")
        if request_timeout is not None:
            _positive_seconds("request_timeout", request_timeout)
        return self._request(
            "GET", "/tasks/" + quote(task_id, safe=""), timeout=request_timeout
        )

    def wait_for_task(
        self,
        task_id: str,
        *,
        timeout: float = 600,
        poll_interval: float = 15,
        on_progress: Callable[[dict], None] | None = None,
    ) -> dict:
        """Wait for success; raise on FAILED/CANCELED/UNKNOWN or timeout.

        Transient GET transport failures, HTTP 429 and 5xx are retried within the
        wait budget. Timeout only stops local waiting, never the remote task.
        """
        _positive_seconds("timeout", timeout)
        _positive_seconds("poll_interval", poll_interval)
        deadline = time.monotonic() + timeout
        last_result = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DashScopeTimeoutError(task_id, last_result)
            try:
                result = self.query_task_result(
                    task_id, request_timeout=min(self.request_timeout, remaining)
                )
            except DashScopeAPIError as exc:
                if exc.status_code != 429 and not 500 <= exc.status_code < 600:
                    raise
            except DashScopeTransportError:
                pass
            else:
                last_result = result
                status = _output(result).get("task_status")
                if status not in {
                    "PENDING",
                    "RUNNING",
                    "SUCCEEDED",
                    "FAILED",
                    "CANCELED",
                    "UNKNOWN",
                }:
                    raise DashScopeError(
                        f"任务 {task_id} 返回无效 task_status: {status!r}"
                    )
                if on_progress is not None:
                    on_progress(result)
                if status == "SUCCEEDED":
                    _video_url(result)
                    return result
                if status in {"FAILED", "CANCELED", "UNKNOWN"}:
                    raise DashScopeTaskError(result, task_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DashScopeTimeoutError(task_id, last_result)
            time.sleep(min(poll_interval, remaining))

    def download_video(self, video_url: str, output_path: str | Path) -> str:
        """Stream into a temporary file, atomically replace destination on success.

        Does not send the DashScope API key to the media server. Failed downloads
        leave any existing destination untouched and remove the partial file.
        """
        return download_video(
            video_url, output_path, request_timeout=self.request_timeout
        )

    def run(
        self,
        payload: dict,
        *,
        output_path: str | Path | None = None,
        timeout=600,
        poll_interval=15,
        on_submitted: Callable[[dict], None] | None = None,
        on_progress: Callable[[dict], None] | None = None,
    ) -> dict:
        """Submit → wait → optional download; retain provider output/usage/request_id."""
        _positive_seconds("timeout", timeout)
        _positive_seconds("poll_interval", poll_interval)
        created = self.submit(payload)
        if on_submitted is not None:
            on_submitted(created)
        task_id = _output(created)["task_id"]
        result = self.wait_for_task(
            task_id,
            timeout=timeout,
            poll_interval=poll_interval,
            on_progress=on_progress,
        )
        if output_path is not None:
            result = {
                **result,
                "local_path": self.download_video(_video_url(result), output_path),
            }
        return result

    def create_video_task(self, prompt: str, **parameters) -> dict:
        return self.submit(build_text_video_payload(prompt, **parameters))

    def create_image_video_task(
        self, prompt: str = "", image_path=None, **parameters
    ) -> dict:
        return self.submit(build_image_video_payload(prompt, image_path, **parameters))

    def create_reference_video_task(
        self, prompt: str, reference_images, **parameters
    ) -> dict:
        return self.submit(
            build_reference_video_payload(prompt, reference_images, **parameters)
        )

    def create_video_edit_task(self, prompt: str, video_url: str, **parameters) -> dict:
        return self.submit(build_video_edit_payload(prompt, video_url, **parameters))

    def create_frame_video_task(
        self, prompt: str, first_frame, last_frame=None, **parameters
    ) -> dict:
        return self.submit(
            build_frame_video_payload(prompt, first_frame, last_frame, **parameters)
        )

    def _generate(
        self,
        builder,
        *args,
        output_path=None,
        timeout=600,
        poll_interval=15,
        **parameters,
    ) -> str:
        result = self.run(
            builder(*args, **parameters),
            output_path=output_path,
            timeout=timeout,
            poll_interval=poll_interval,
        )
        return result.get("local_path") or _video_url(result)

    def text_to_video(self, prompt: str, **parameters) -> str:
        return self._generate(build_text_video_payload, prompt, **parameters)

    def image_to_video(self, prompt: str = "", image_path=None, **parameters) -> str:
        return self._generate(
            build_image_video_payload, prompt, image_path, **parameters
        )

    def reference_to_video(self, prompt: str, reference_images, **parameters) -> str:
        return self._generate(
            build_reference_video_payload, prompt, reference_images, **parameters
        )

    def edit_video(self, prompt: str, video_url: str, **parameters) -> str:
        return self._generate(build_video_edit_payload, prompt, video_url, **parameters)

    def first_last_frame_to_video(
        self, prompt: str, first_frame, last_frame=None, **parameters
    ) -> str:
        return self._generate(
            build_frame_video_payload, prompt, first_frame, last_frame, **parameters
        )
