"""Small, synchronous Bailian LLM primitives. Python 3.10+, stdlib only."""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from http.client import HTTPException
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import LLMConfig


class LLMError(RuntimeError):
    """Base for API, network and malformed-response failures."""


class LLMAPIError(LLMError):
    def __init__(self, result: dict, status_code: int, request_id: str = "", *, api_key=""):
        self.result = result
        self.status_code = status_code
        detail = result.get("error", result)
        if not isinstance(detail, dict):
            detail = {"message": str(detail)}
        self.code = detail.get("code") or detail.get("type") or "APIError"
        self.request_id = result.get("request_id") or request_id
        message = str(detail.get("message", "请求失败"))
        text = f"HTTP {status_code} [{self.code}] {message} (request_id={self.request_id})"
        super().__init__(text.replace(api_key, "[REDACTED]") if api_key else text)


class LLMTransportError(LLMError):
    """Connection, timeout or interrupted-body failure. Never retried implicitly."""


class LLMResponseError(LLMError):
    """The server did not return the expected JSON or SSE structure."""


class _NoRedirect(HTTPRedirectHandler):
    # Do not forward the bearer token to redirect targets.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_messages(prompt: str, system_prompt: str | None = None) -> list[dict]:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt 不能为空")
    if system_prompt is not None and not isinstance(system_prompt, str):
        raise ValueError("system_prompt 必须为字符串")
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    return messages


def _json_object(raw: bytes | str) -> dict:
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise LLMResponseError("接口返回无效 JSON") from None
    if not isinstance(result, dict):
        raise LLMResponseError("接口 JSON 响应必须为对象")
    return result


def extract_text(result: dict) -> str:
    """Extract the first answer; tool-only responses remain available via chat()."""
    try:
        message = result["choices"][0]["message"]
        content = message.get("content")
    except (KeyError, IndexError, TypeError, AttributeError):
        raise LLMResponseError("响应缺少 choices[0].message") from None
    if not isinstance(content, str):
        raise LLMResponseError("响应没有文本答案；工具调用请使用 chat() 读取完整 message")
    return content


def _sse_data(response) -> Iterator[str]:
    """Read SSE frames, including comments, CRLF and multiline data fields."""
    data = []
    for raw_line in response:
        try:
            line = raw_line.decode("utf-8").rstrip("\r\n")
        except UnicodeError:
            raise LLMResponseError("流式响应不是有效 UTF-8") from None
        if not line:
            if data:
                yield "\n".join(data)
                data.clear()
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        yield "\n".join(data)


class DashScopeLLMClient:
    """Each method makes one request; conversation state stays with the caller.

    Construction loads .env but makes no network calls. Use build_payload() for
    credential-free previews. timeout is a socket connect/read timeout, not an
    overall generation deadline. Paid POSTs are never automatically retried.
    """

    def __init__(self, *, env_file: str | Path | None = None, **config):
        self.config = LLMConfig.from_env(env_file, **config)
        self._opener = build_opener(_NoRedirect())

    @property
    def endpoint(self) -> str:
        return self.config.base_url + "/chat/completions"

    def build_payload(
        self, messages: Sequence[Mapping], *, model: str | None = None,
        temperature: float | None = None, max_tokens: int | None = None,
        enable_thinking: bool | None = None, stream: bool = False,
        extra_body: Mapping | None = None,
    ) -> dict:
        """Build a request; extra_body handles response_format, tools, top_p, etc.

        Pass a model's actual supported parameters. No input list is modified.
        None for a per-call option means use the client's configured default.
        """
        if isinstance(messages, (str, bytes)) or not isinstance(messages, Sequence) or not messages:
            raise ValueError("messages 必须为非空消息列表")
        copied = []
        for message in messages:
            if not isinstance(message, Mapping) or not isinstance(message.get("role"), str) or not message["role"].strip():
                raise ValueError("每条 message 必须包含非空 role")
            if "content" not in message and "tool_calls" not in message:
                raise ValueError("每条 message 必须包含 content 或 tool_calls")
            copied.append(dict(message))
        if type(stream) is not bool:
            raise ValueError("stream 必须为布尔值")
        overrides = {key: value for key, value in {
            "model": model, "temperature": temperature, "max_tokens": max_tokens,
            "enable_thinking": enable_thinking,
        }.items() if value is not None}
        config = replace(self.config, **overrides)
        payload = {"model": config.model, "messages": copied, "stream": stream}
        for name in ("temperature", "max_tokens", "enable_thinking"):
            value = getattr(config, name)
            if value is not None:
                payload[name] = value
        if extra_body is not None:
            if not isinstance(extra_body, Mapping) or any(not isinstance(key, str) for key in extra_body):
                raise ValueError("extra_body 必须为字符串键的对象")
            conflicts = extra_body.keys() & {
                "model", "messages", "stream", "temperature", "max_tokens", "enable_thinking"
            }
            if conflicts:
                raise ValueError(f"extra_body 不能覆盖参数，请使用同名关键字参数: {', '.join(sorted(conflicts))}")
            payload.update(extra_body)
        if stream:
            payload.setdefault("stream_options", {"include_usage": True})
        try:
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("请求参数必须可序列化为 JSON，且不能包含 NaN/Infinity") from None
        return payload

    def _open(self, payload):
        key = self.config.api_key
        if not key or key == "your-dashscope-api-key":
            raise ValueError("请在项目根目录 .env 或系统环境变量中设置 DASHSCOPE_API_KEY")
        request = Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "Accept": "text/event-stream" if payload["stream"] else "application/json"},
            method="POST",
        )
        try:
            return self._opener.open(request, timeout=self.config.timeout)
        except HTTPError as exc:
            try:
                raw = exc.read()
                try:
                    result = _json_object(raw)
                except LLMResponseError:
                    result = {"error": {"code": "HTTPError", "message": "接口返回非 JSON 错误响应"}}
                self._raise_api_error(result, exc.code, exc.headers)
            finally:
                exc.close()
        except (URLError, OSError, HTTPException) as exc:
            raise LLMTransportError("LLM 连接失败或超时；请求未自动重试") from exc

    def _raise_api_error(self, result, status, headers):
        request_id = headers.get("x-request-id", "") or headers.get("x-dashscope-request-id", "")
        raise LLMAPIError(result, status, request_id, api_key=self.config.api_key)

    def chat(self, messages: Sequence[Mapping], **options) -> dict:
        """Return the complete non-stream response (choices, usage, tool_calls)."""
        payload = self.build_payload(messages, **options)
        if payload["stream"]:
            raise ValueError("流式调用请使用 stream_chat()")
        try:
            with self._open(payload) as response:
                result = _json_object(response.read())
                if result.get("error") or result.get("code"):
                    self._raise_api_error(result, response.status, response.headers)
        except (OSError, HTTPException) as exc:
            raise LLMTransportError("LLM 响应读取失败或超时；请求未自动重试") from exc
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict) or not isinstance(choices[0].get("message"), dict):
            raise LLMResponseError("响应缺少 choices[0].message")
        return result

    def generate(self, prompt: str, *, system_prompt: str | None = None, **options) -> str:
        """One prompt in, one answer string out. An empty system_prompt disables it."""
        system = self.config.system_prompt if system_prompt is None else system_prompt
        return extract_text(self.chat(build_messages(prompt, system), **options))

    def stream_chat(self, messages: Sequence[Mapping], **options) -> Iterator[dict]:
        """Yield raw SSE JSON chunks, preserving reasoning/tool deltas and usage."""
        payload = self.build_payload(messages, stream=True, **options)
        try:
            with self._open(payload) as response:
                if response.headers.get_content_type() != "text/event-stream":
                    result = _json_object(response.read())
                    if result.get("error") or result.get("code"):
                        self._raise_api_error(result, response.status, response.headers)
                    raise LLMResponseError("流式接口未返回 text/event-stream")
                for data in _sse_data(response):
                    if data.strip() == "[DONE]":
                        return
                    result = _json_object(data)
                    if result.get("error") or result.get("code"):
                        self._raise_api_error(result, response.status, response.headers)
                    if not isinstance(result.get("choices"), list):
                        raise LLMResponseError("流式响应缺少 choices 列表")
                    for choice in result["choices"]:
                        if not isinstance(choice, dict) or not isinstance(choice.get("delta"), dict):
                            raise LLMResponseError("流式响应缺少 choice.delta 对象")
                        content = choice["delta"].get("content")
                        if content is not None and not isinstance(content, str):
                            raise LLMResponseError("流式文本 content 必须为字符串")
                    yield result
        except (OSError, HTTPException) as exc:
            raise LLMTransportError("LLM 流式连接中断或超时，输出可能不完整；请求未自动重试") from exc
        raise LLMResponseError("流式响应缺少 [DONE]，输出可能不完整")

    def stream(self, prompt: str, *, system_prompt: str | None = None, **options) -> Iterator[str]:
        """Yield answer text only. Use stream_chat() for metadata or tool calls."""
        system = self.config.system_prompt if system_prompt is None else system_prompt
        for chunk in self.stream_chat(build_messages(prompt, system), **options):
            choices = chunk["choices"]
            if choices:
                try:
                    content = choices[0]["delta"].get("content")
                except (KeyError, TypeError, AttributeError):
                    raise LLMResponseError("流式响应缺少 choices[0].delta") from None
                if content is not None and not isinstance(content, str):
                    raise LLMResponseError("流式文本 content 必须为字符串")
                if content:
                    yield content


def call_llm(
    prompt: str, *, system_prompt: str | None = None, env_file: str | Path | None = None,
    api_key: str | None = None, base_url: str | None = None,
    timeout: float | None = None, **options,
) -> str:
    """Convenience atomic tool; use a client instance for repeated calls."""
    client = DashScopeLLMClient(env_file=env_file, api_key=api_key, base_url=base_url, timeout=timeout)
    return client.generate(prompt, system_prompt=system_prompt, **options)
