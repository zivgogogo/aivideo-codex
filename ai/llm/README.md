# 百炼 LLM 原子工具

基于百炼的 **OpenAI 兼容 Chat Completions HTTP 接口**，使用 Python 3.10+ 标准库，无需安装第三方依赖。单次生成、多轮对话、流式输出均可从 Python 或命令行调用。

## 1. 配置 .env

在项目根目录编辑 `.env`。首次使用可从 `.env.example` 复制；已有 `.env` 时直接补充配置。

```dotenv
# 与图片/视频工具共用密钥。密钥和模型必须与接口地域一致。
DASHSCOPE_API_KEY=填写你的百炼API密钥

# LLM 专用地址，与图片/视频的 DASHSCOPE_BASE_URL 分开配置。
DASHSCOPE_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_LLM_MODEL=qwen-plus
DASHSCOPE_LLM_TIMEOUT=120
DASHSCOPE_LLM_TEMPERATURE=0.7
DASHSCOPE_LLM_MAX_TOKENS=2048
DASHSCOPE_LLM_ENABLE_THINKING=false
DASHSCOPE_LLM_SYSTEM_PROMPT="请使用中文，直接回答问题。"
```

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `DASHSCOPE_API_KEY` | 无，真实请求时必填 | 百炼 API Key |
| `DASHSCOPE_LLM_BASE_URL` | 北京旧域名的 `/compatible-mode/v1` | 兼容接口根地址，也可只填主机地址，自动补路径 |
| `DASHSCOPE_LLM_MODEL` | `qwen-plus` | 模型名称，需在当前地域可用并已开通 |
| `DASHSCOPE_LLM_TIMEOUT` | `120` | 连接及单次读取的超时秒数，不是生成总时限 |
| `DASHSCOPE_LLM_TEMPERATURE` | `0.7` | 随机程度；留空时不发送此参数 |
| `DASHSCOPE_LLM_MAX_TOKENS` | `2048` | 最大输出 token 数；留空时不发送此参数 |
| `DASHSCOPE_LLM_ENABLE_THINKING` | `false` | `true` / `false` / `auto`；`auto` 不发送此参数，适合不支持此开关的模型 |
| `DASHSCOPE_LLM_SYSTEM_PROMPT` | 空 | 单次生成、文本流式生成及 CLI prompt 输入的默认系统提示词 |

配置优先级：**调用时显式传入的非 None 参数 > 当前进程环境变量 > `.env` > 默认值**。单次方法参数为 `None` 时沿用客户端配置；若需省略可选参数，在 `.env` 留空或将思考模式设为 `auto`。`.env` 只在构建客户端时加载，不覆盖已有进程变量；修改后请重新启动调用进程。

默认读取项目根目录 `.env`，不依赖运行命令时所在目录。也可用 Python `env_file=` 或 CLI `--env-file` 指定其他文件。复用 `core.config.load_env`，支持注释、引号、`export` 前缀，不执行 shell 命令，也不展开 `$变量`。`.env` 已被 `.gitignore` 忽略，示例文件不放真实密钥。

百炼推荐业务空间专属域名，例如 `https://你的业务空间ID.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`，现有旧域名仍可使用。其他地域请按控制台信息配置，API Key 必须属于同一地域。参见 [官方接口与接入地址说明](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope) 和 [获取 API Key](https://help.aliyun.com/zh/model-studio/get-api-key)。

## 2. 最简 Python 调用

从项目根目录运行，或让项目根目录在 Python 的模块搜索路径中：

```python
from ai.llm import call_llm

text = call_llm("为一段15秒的咖啡广告写3条分镜。")
print(text)

# 当前调用覆盖 .env 默认参数
text = call_llm(
    "写一句简短的广告语",
    system_prompt="你是一名广告文案。",
    temperature=0.3,
    max_tokens=512,
)
```

重复调用时复用客户端。每次请求独立，调用方自行维护上下文：

```python
from ai.llm import DashScopeLLMClient

client = DashScopeLLMClient()
print(client.generate("写一个短视频选题"))

messages = [
    {"role": "system", "content": "你是一名短视频编导。"},
    {"role": "user", "content": "主题是手冲咖啡。"},
]
result = client.chat(messages)
messages.append(result["choices"][0]["message"])
messages.append({"role": "user", "content": "把它扩展成3个镜头。"})
result = client.chat(messages)
print(result["choices"][0]["message"]["content"])
print(result["usage"])
```

`chat(messages)` 使用传入的完整消息列表，不额外插入 `.env` 中的 system prompt。`generate()` 和 `stream()` 可用 `system_prompt=""` 禁用配置中的默认 system prompt。

## 3. 流式、JSON 与工具调用

```python
from ai.llm import DashScopeLLMClient

client = DashScopeLLMClient()
for text in client.stream("写一个30秒的旅行短片脚本"):
    print(text, end="", flush=True)
print()

# 如需用量、思考字段或 tool_calls 增量，读取完整数据块。
# 默认 stream_options.include_usage=true，最后的用量块 choices 可能为空。
for chunk in client.stream_chat([{"role": "user", "content": "你好"}]):
    if chunk.get("usage"):
        print(chunk["usage"])

# 模型需支持 JSON 输出；提示词中明确要求 JSON。
result = client.chat(
    [{"role": "user", "content": "返回 JSON 对象，包含 title 和 shots 数组。"}],
    extra_body={"response_format": {"type": "json_object"}},
)
print(result["choices"][0]["message"]["content"])
```

`extra_body` 可透传 `top_p`、`tools`、`tool_choice`、`enable_search` 等模型支持的字段，不得覆盖 `model/messages/stream/temperature/max_tokens/enable_thinking`；这些参数使用方法的同名关键字传入。HTTP 请求中这些额外字段在顶层，实际支持范围由模型决定。

`chat()` 保留完整 `choices`、`usage`、`finish_reason` 和 `tool_calls`。工具调用由调用方执行；本模块不自动执行模型返回的函数。`generate()` 适用于文本答案，工具调用无正文时会提示改用 `chat()`。如需判断是否达到输出上限，检查 `finish_reason`。

## 4. 命令行入口

在项目根目录执行：

```bash
# 无需密钥，预览请求体，不显示 Authorization。
python3 scripts/ai/bailian_llm.py --prompt "你好" --dry-run

# 单次调用，只输出回答正文。
python3 scripts/ai/bailian_llm.py --prompt "为咖啡广告写3条分镜"

# 流式输出。
python3 scripts/ai/bailian_llm.py --prompt "写一段短片脚本" --stream

# 文件输入，保存包含 token 用量的完整响应。
python3 scripts/ai/bailian_llm.py \
  --prompt-file examples/video_prompt.md \
  --json --output outputs/llm-response.json

# 多轮输入：文件内容是 messages JSON 数组。
python3 scripts/ai/bailian_llm.py --messages-file examples/llm_messages.json

# 从标准输入读取。
printf '给咖啡广告起一个标题' | python3 scripts/ai/bailian_llm.py --prompt-file -

# Python 模块入口提供相同选项。
python3 -m ai.llm --help
```

支持 `--env-file`、`--base-url`、`--model`、`--temperature`、`--max-tokens`、`--timeout`、`--thinking true|false`、`--system-prompt` 和 `--extra-body 参数.json`。`--json` 与 `--stream` 互斥；完整流式元数据使用 Python `stream_chat()`。脚本也可通过绝对路径在其他工作目录运行。

## 5. 异常与验证

```python
from ai.llm import call_llm, LLMAPIError, LLMError

try:
    answer = call_llm("你好")
except LLMAPIError as exc:
    print(exc.status_code, exc.code, exc.request_id)
except LLMError as exc:
    print(exc)
```

参数错误抛出 `ValueError`；供应商错误抛出 `LLMAPIError`；连接、超时或响应中断抛出 `LLMTransportError`；响应格式错误抛出 `LLMResponseError`。CLI 失败时向 stderr 输出错误，退出码为 1。

生成请求只提交一次，不自动重试。流式响应缺少 `[DONE]` 会显式报错，避免将中断输出当成完整答案。使用 [官方 SSE 流式协议](https://help.aliyun.com/zh/model-studio/stream)。

本地模拟服务测试，不调用真实百炼：

```bash
python3 -m unittest discover -s tests -v
```
