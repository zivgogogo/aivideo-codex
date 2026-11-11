# Qwen Image 3.0 文生图 / 文本 + 图片生图

入口是 `scripts/ai/qwen_image.py`，Python 3.10+，只使用标准库，不需要安装依赖。实现位于 `ai/image/client.py`，入口脚本依赖本仓库中的 `ai/` 和 `core/`；默认读取项目根目录 `.env`，也可以通过 `--env-file` 指定。

同一个模型、同一个同步接口，通过可选的 `--image` / Python `images` 参数切换：

| 输入 | 模式 | 默认模型 |
| --- | --- | --- |
| 文本 | 文生图 | `qwen-image-3.0-pro` |
| 文本 + 1~3 张图片 | 图生图 / 图片编辑 / 多图参考 | `qwen-image-3.0-pro` |

根据[千问图像生成与编辑 API 文档](https://help.aliyun.com/zh/model-studio/qwen-image-generation-and-editing-api-reference)，使用 `POST /api/v1/services/aigc/multimodal-generation/generation`，请求内容为 `input.messages[0].content`：图片依次放在 `{"image": "..."}` 中，最后放一个 `{"text": "..."}`。不传图片时只保留文本。

## 配置

从项目根目录执行。已有 `.env` 时直接补充配置：

```dotenv
DASHSCOPE_API_KEY=你的百炼API密钥
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/api/v1
```

也可在终端设置环境变量，优先级高于 `.env`。API Key、模型和接口地址需要属于同一地域。推荐使用控制台中业务空间的专属域名，例如：

```dotenv
DASHSCOPE_BASE_URL=https://你的业务空间ID.cn-beijing.maas.aliyuncs.com/api/v1
```

## 命令行

文生图：

```bash
python3 scripts/ai/qwen_image.py \
  --prompt "一只橘猫坐在窗边，午后阳光，写实摄影" \
  --size 1024x1024 \
  --output outputs/cat.png
```

文本 + 本地参考图：

```bash
python3 scripts/ai/qwen_image.py \
  --prompt "保留猫的外观和姿态，将背景改成海边日落" \
  --image outputs/cat.png \
  --output outputs/cat_beach.png
```

文本 + 多张参考图（按参数顺序对应图 1、图 2、图 3；也可混合 URL 和本地文件）：

```bash
python3 scripts/ai/qwen_image.py \
  --prompt "将图1中的产品放到图2的桌面上，保留产品细节" \
  --image ./product.png \
  --image "https://example.com/table.png" \
  --n 2 \
  --output outputs/product_scene.png
```

URL 示例是占位地址，实际调用时请替换为可公网访问的图片地址。本地图片会自动转换为 Base64 data URI，也可直接通过 `--image 'data:image/png;base64,...'` 传入。支持 JPEG、PNG、BMP、TIFF、WEBP、GIF，单图不超过 10MB（编码前）。输入图片宽高须为 1~8000 像素，长短边比例不超过 10:1；尺寸、宽高比和远程图片内容由服务端验证。

预览请求，无需密钥，也不会调用 API：

```bash
python3 scripts/ai/qwen_image.py --prompt "一只猫" --dry-run
```

长提示词、反向提示词和种子：

```bash
python3 scripts/ai/qwen_image.py \
  --prompt-file examples/image_prompt.md \
  --negative-prompt "模糊、变形、多余肢体" \
  --seed 42 --n 2 \
  --no-prompt-extend --no-enable-thinking \
  --output outputs/poster.png
```

## 常用参数

| 参数 | 说明 |
| --- | --- |
| `--prompt` / `--prompt-file` | 二选一，必填；推荐提示词不超过 4500 Token |
| `--image` | 可选，可重复，最多 3 张；顺序保留 |
| `--size` | `宽x高` 或 `"宽*高"`；省略或 `auto` 时由模型推荐 |
| `--n` | 输出 1~6 张，默认 1 |
| `--seed` | 0~2147483647 |
| `--negative-prompt` | 反向提示词 |
| `--no-prompt-extend` | 关闭提示词改写，默认开启 |
| `--prompt-extend-mode` | `direct`（默认）或 `agent`；`agent` 只支持文生图 |
| `--no-enable-thinking` | 关闭思考；思考仅在提示词改写开启时生效 |
| `--watermark` | 添加水印，默认关闭 |
| `--model` | 默认 `qwen-image-3.0-pro`，也支持 `qwen-image-3.0` |
| `--output` | PNG 输出路径，默认 `outputs/qwen_时间戳.png` |
| `--save-response` | 原始响应 JSON 路径，默认输出路径同名 `.json` |
| `--no-download` | 只保存响应、返回 URL，不下载图片 |
| `--timeout` | 同步生成请求超时秒数，默认 300 |
| `--env-file` / `--base-url` | 覆盖配置文件 / API 地址 |
| `--dry-run` | 只打印请求，不调用 API；本地参考图会显示为 Base64 |

输出尺寸按**总像素面积**验证：512×512 至 2048×2048，宽高比 1:8 至 8:1。例如 `2688x1536` 符合要求；并非每条边都必须不大于 2048。

## 输出与错误恢复

默认生成并下载所有结果。单张按 `--output` 保存，多张自动保存为 `文件名_1.png`、`文件名_2.png` 等。标准输出是包含 `request_id`、`image_urls`、`local_paths`、`response_file`、`usage` 的 JSON；进度和错误输出到标准错误。

原始 API 响应会在下载前保存为 JSON。响应文件已存在时，脚本会在调用生成接口前报错；需要再次生成时请使用新的输出文件名，或指定新的 `--save-response` 路径。

生成 POST 不自动重试。生成超时后请先核实百炼调用记录，避免重复生成；下载失败后可以使用响应 JSON 中的已有 URL 重试下载。生成链接有效期为 24 小时，需及时保存。

## Python 调用

从项目根目录导入。`generate_image` 返回原始响应，下载由独立函数完成：

```python
from ai.image import generate_image, extract_image_urls, download_image

# 文生图
result = generate_image("一只橘猫坐在窗边", size="1024x1024")
for index, url in enumerate(extract_image_urls(result), 1):
    download_image(url, f"outputs/cat_{index}.png")

# 文本 + 图片；images 可以是单个路径/URL，也可以是列表
result = generate_image(
    "保留猫的外观，把背景改成雪山",
    images=["outputs/cat_1.png"],
    size="1024x1024",
    seed=42,
)
download_image(extract_image_urls(result)[0], "outputs/cat_snow.png")
```

仅重试下载，不重新生成，也无需 API Key：

```python
import json
from pathlib import Path
from ai.image import extract_image_urls, download_image

result = json.loads(Path("outputs/cat.json").read_text(encoding="utf-8"))
for index, url in enumerate(extract_image_urls(result), 1):
    download_image(url, f"outputs/recovered_{index}.png")
```

离线验证：`python3 -m unittest discover -s tests -v`，使用本地模拟 HTTP 服务，不请求真实百炼接口。
