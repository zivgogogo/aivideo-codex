# DashScope / HappyHorse 视频原子能力

Python 3.10+，仅使用标准库。原始脚本的创建任务 → 轮询 → 下载流程已拆成可独立组合的方法，同时提供五个独立命令行脚本。

## 配置

以下命令均在 `aivideo-codex` 根目录执行：

```bash
cp -n .env.example .env  # 保留已有配置
```

编辑 `.env`：

```dotenv
DASHSCOPE_API_KEY=你的百炼密钥
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/api/v1
```

系统环境变量优先于 `.env`。环境文件默认从项目根目录读取，不依赖当前工作目录；可用 `--env-file /path/to/.env` 指定。支持引号、注释和 `export`，不展开变量、不执行 shell 内容。密钥在调用客户端时读取，导入模块和 `--dry-run` 无需密钥。

推荐按账号配置业务空间域名，例如 `https://你的业务空间ID.cn-beijing.maas.aliyuncs.com/api/v1`。支持通过 `DASHSCOPE_BASE_URL`、`--base-url` 或客户端 `base_url=` 配置。模型、API Key、endpoint 必须同地域；默认仍使用原始脚本的北京域名。域名说明见[官方 T2V API](https://help.aliyun.com/zh/model-studio/happyhorse-text-to-video-api-reference)。

## 五个独立脚本

图片参数可以是本地路径、HTTP(S) URL 或 Base64 data URI；本地图片会自动编码，不会自动上传视频。下面的 `first.png`、`last.png` 和 URL 是待替换的素材示例。

### 1. happyhorse-1.1-t2v：文生视频

```bash
python3 scripts/ai/happyhorse_t2v.py \
  --prompt-file examples/video_prompt.md \
  --resolution 1080P --ratio 16:9 --duration 5 --seed 42 \
  --output outputs/happyhorse-t2v.mp4
```

### 2. happyhorse-1.1-i2v：文本 + 首帧

```bash
python3 scripts/ai/happyhorse_i2v.py \
  --prompt "人物自然转头微笑，镜头缓慢推进" \
  --first-frame first.png --duration 5 \
  --output outputs/happyhorse-i2v.mp4
```

`--prompt` 可省略，也支持 `--prompt-file`。输出比例随首帧近似保持，不传 `ratio`，不支持尾帧。图片要求见[官方 I2V API](https://help.aliyun.com/zh/model-studio/happyhorse-image-to-video-api-reference)：JPEG/PNG/WEBP，宽高均至少 300px，比例在 1:2.5～2.5:1，单张不超过 20MB。

### 3. happyhorse-1.1-r2v：文本 + 参考图片

```bash
python3 scripts/ai/happyhorse_r2v.py \
  --prompt "[Image 1]中的人物在山间行走，手持[Image 2]中的背包，保持人物和背包一致" \
  --reference-image person.png \
  --reference-image backpack.png \
  --duration 8 --ratio 9:16 --output outputs/happyhorse-r2v.mp4
```

参考图支持 1～9 张，`--reference-image` 可重复，顺序对应提示词中的 `[Image 1]`、`[Image 2]`。参考图用于约束人物、物体、风格等，不固定首尾帧。JPEG/PNG/WEBP，短边至少 400px，单张不超过 20MB。见[官方 R2V API](https://help.aliyun.com/zh/model-studio/happyhorse-reference-to-video-api-reference)。

### 4. happyhorse-1.0-video-edit：视频编辑

```bash
python3 scripts/ai/happyhorse_video_edit.py \
  --prompt "把视频中的人物服装替换成参考图的服装，保持原来的动作和镜头" \
  --video-url "https://你的域名/source.mp4" \
  --reference-image clothes.png \
  --audio-setting origin --resolution 1080P \
  --output outputs/happyhorse-edit.mp4
```

参考图可省略，最多 5 张。`audio_setting=auto` 由模型生成声音，`origin` 保留原声。此模型只接受公网视频 URL，不支持本地视频路径或视频 Base64。视频应为 MP4/MOV、3～60s、≤100MB、8～60fps，短边≥360px、长边≤4096px、比例 1:2.5～2.5:1。参考图宽高≥300px、≤20MB。

编辑接口不接收 `duration` 或 `ratio`：输出跟随源视频，超过 15 秒时处理前 15 秒；分辨率支持 720P/1080P。详见[官方视频编辑 API](https://help.aliyun.com/zh/model-studio/happyhorse-video-edit-api-reference)。

### 5. wan3.0-video：文本 + 首尾帧

```bash
python3 scripts/ai/wan_first_last_frame.py \
  --prompt "人物从站立缓慢走向镜头，最后停在尾帧姿势，动作连贯" \
  --first-frame first.png --last-frame last.png \
  --duration 8 --ratio adaptive --no-prompt-extend \
  --output outputs/wan-first-last.mp4
```

省略 `--last-frame` 即 Wan 首帧模式。支持 `--audio` / `--no-audio`、`--prompt-extend` / `--no-prompt-extend`；不指定时使用服务端默认值。支持 `--model wan3.0-video-prime` 显式使用高速版。

Wan 图片支持 JPEG/PNG/BMP/WEBP，单边 240～8000px、长宽比≤8:1、≤20MB，PNG 不支持透明通道。首尾帧模式不混入其他参考素材。详见[官方 Wan3 API](https://help.aliyun.com/zh/model-studio/wan3-video-generation-api-reference)。

### 6. Wan 2.7：文本＋多图/多视频参考

```bash
python3 scripts/ai/wan_reference_video.py \
  --prompt "视频1与视频2在图1的咖啡馆交谈" \
  --reference-video "https://你的域名/person1.mp4" \
  --reference-video "https://你的域名/person2.mp4" \
  --reference-image cafe.png \
  --duration 6 --ratio 9:16 --resolution 720P \
  --output outputs/wan-reference.mp4
```

`--reference-image`和`--reference-video`可重复，图片与视频合计1–5个。图片支持本地路径、URL、Base64；视频使用公网URL。含视频时输出2–10秒，纯图2–15秒。两类素材分别编号为“图1、图2”和“视频1、视频2”。支持720P/1080P；默认关闭提示词改写，保留用户原意。使用固定版本`wan2.7-r2v-2026-06-12`。

Python入口为`build_multimodal_reference_payload(prompt, reference_images, reference_videos, ...)`，返回原生请求，可交给`DashScopeVideoClient.submit()`；查询、断点恢复与其他原子能力一致。也可运行`python3 -m ai.video wan-ref ... --dry-run`检查请求。

依据[官方API](https://help.aliyun.com/zh/model-studio/wan-video-to-video-api-reference)。这是参考生成；HappyHorse编辑仍使用单个源视频，不混用请求格式。

## 参数约定

| 参数 | HappyHorse T2V / I2V / R2V | HappyHorse 编辑 | Wan 首帧/首尾帧 |
| --- | --- | --- | --- |
| 时长 | 整数 3～15 秒，默认 5 | 跟随源视频，最多 15 秒 | 整数 2～30 秒，或 -1 智能时长 |
| 分辨率 | 480P / 720P / 1080P | 720P / 1080P | 480P / 720P / 1080P |
| 比例 | T2V、R2V 可设；I2V 跟随首帧 | 跟随源视频 | 默认 adaptive |
| 随机种子 | 0～2147483647 | 0～2147483647 | -1 或 0～2147483647 |
| 水印 | 本模块默认关闭，`--watermark` 开启 | 同左 | 同左 |
| 音频 | 原生音频，不传 Wan 的音频开关 | auto / origin | 可开关，服务端默认开启 |

统一默认分辨率为 1080P。HappyHorse 的 T2V、R2V 比例可选 `16:9, 9:16, 1:1, 4:3, 3:4, 4:5, 5:4, 9:21, 21:9`；Wan 可选 `adaptive, 21:9, 16:9, 4:3, 1:1, 3:4, 9:16`。

HappyHorse 提示词上限约 2500 中文或 5000 非中文字符；本模块按中文双权重保守校验混合文本。Wan 最多 20000 字符。图片本地校验格式、编码和字节大小，像素尺寸、透明通道、远端图片及视频的实际规格由服务端验证。

## 提交、查询、等待、下载分别调用

五个生成脚本默认提交后等待；加 `--no-wait` 只创建任务。加 `--dry-run` 只打印请求 JSON，不访问 API；本地图片预览会包含完整 Base64。

```bash
# 只提交，立即保存 task_id；不要为了查进度重复执行创建命令
python3 scripts/ai/happyhorse_t2v.py \
  --prompt "夕阳下的海浪" --no-wait --save-task outputs/task.json

# 从响应或 outputs/task.json 的 output.task_id 取得任务 ID
python3 -m ai.video query YOUR_TASK_ID

# 本地超时/中断后恢复等待，不重新创建任务
python3 -m ai.video wait YOUR_TASK_ID \
  --timeout 1200 --poll-interval 15 --output outputs/result.mp4

# 独立下载，无需密钥
python3 -m ai.video download "https://你的结果地址/video.mp4" \
  --output outputs/result.mp4
```

标准输出为 JSON，进度和错误写入标准错误；`--save-task` 在创建成功后立即保存任务及 endpoint，不保存密钥。`query` 返回原始状态，不等待；`wait` 在失败、取消、过期时返回非零退出码。下载成功的结果带 `local_path`，同时保留原始 `output`、`usage`、`request_id`。

默认单次网络超时 60s、轮询间隔 15s、等待预算 600s，分别使用 `--request-timeout`、`--poll-interval`、`--timeout` 调整。POST 创建任务不自动重试，避免重复计费；GET 查询遇到网络错误、429 或 5xx 时会在等待预算内重试。超时只停止本地等待，远端任务仍可能执行。

任务 ID 和结果视频 URL 的查询/访问有效期为 24 小时，建议成功后及时下载。下载按块写入临时文件，完整后替换目标文件；失败时保留原文件并清理临时文件，不会把文本错误页保存成 MP4。

## Python 原子接口

```python
from ai.video import (
    DashScopeVideoClient,
    build_reference_video_payload,
    build_frame_video_payload,
    download_video,
)

# 1. 构造请求：纯本地操作，不需要 API Key
payload = build_reference_video_payload(
    prompt="[Image 1]中的人物拿着[Image 2]中的包走过街道",
    reference_images=["person.png", "bag.png"],
    duration=8,
    ratio="9:16",
)

# 2. 只提交一次，保存原始响应中的 task_id
client = DashScopeVideoClient()
created = client.submit(payload)
task_id = created["output"]["task_id"]
print(task_id, flush=True)  # 应持久化到自己的任务存储

# 3. 查询一次；或等待到完成
snapshot = client.query_task_result(task_id)
result = client.wait_for_task(task_id, timeout=1200, poll_interval=15)

# 4. 独立下载；结果中保留 usage 等字段供上层记账
path = download_video(result["output"]["video_url"], "outputs/result.mp4")
```

| 能力 | 构造请求（不联网） | 只提交任务 | 完整生成（返回 URL/本地路径） |
| --- | --- | --- | --- |
| T2V | `build_text_video_payload` | `client.create_video_task` | `client.text_to_video` |
| I2V | `build_image_video_payload` | `client.create_image_video_task` | `client.image_to_video` |
| R2V | `build_reference_video_payload` | `client.create_reference_video_task` | `client.reference_to_video` |
| 编辑 | `build_video_edit_payload` | `client.create_video_edit_task` | `client.edit_video` |
| 首尾帧 | `build_frame_video_payload` | `client.create_frame_video_task` | `client.first_last_frame_to_video` |

完整生成方法支持 `output_path`、`timeout`、`poll_interval`，其余模型参数与构造方法一致。直接 `client.run(payload, ...)` 返回含用量的完整结果，支持 `on_submitted(created)` 和 `on_progress(result)` 回调。

```python
from ai.video import DashScopeVideoClient, image_to_video

client = DashScopeVideoClient()
url = client.text_to_video("夕阳下的海浪", duration=5)
path = image_to_video(
    prompt="人物转头微笑",
    image_path="first.png",  # 也可使用 first_frame=，两者不能同时传
    duration=5,
    output_path="outputs/portrait.mp4",
)
url = client.reference_to_video("[Image 1]中的人物在跑步", ["person.png"])
url = client.edit_video("画面变成水彩风格", "https://你的域名/source.mp4", audio_setting="origin")
url = client.first_last_frame_to_video("从清晨过渡到黄昏", "first.png", "last.png", duration=10)
```

五个完整生成方法也可直接从 `ai.video` 导入为模块函数。原脚本使用的 Wan 文生视频仍可显式选择：`client.text_to_video("提示词", model="wan3.0-video", duration=30)`。参数以当前官方接口为准，不发送原脚本中未在 Wan3 当前 API 列出的 `negative_prompt`。

异常可从包中导入：`DashScopeAPIError`（含 `status_code`、`code`、`request_id`、`result`）、`DashScopeTaskError`（含 `task_id`、`status`、`result`）、`DashScopeTimeoutError`（含 `task_id`、`last_result`）和 `DashScopeTransportError`。输入校验使用 `ValueError`。

## 目录与验证

- `payloads.py`：五种请求构造和参数校验。
- `media.py`：图片 URL / 本地文件 / Base64 转换。
- `client.py`：提交、查询、等待、下载及完整流程便捷方法。
- `cli.py`：统一命令行；`../../scripts/ai/` 提供按模型分开的入口。
- `../../tests/generation/test_dashscope_video.py`：本地 HTTP 模拟与命令行测试。

```bash
python3 -m unittest discover -s tests -v
```

测试不调用真实模型，不产生视频生成费用。真实账号的模型开通状态、地域权限和生成效果需要配置密钥后实际调用确认。
