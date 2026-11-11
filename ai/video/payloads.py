"""Pure request builders: validate inputs and produce native DashScope JSON."""

from collections.abc import Sequence
from os import PathLike

from .media import http_url, image_url, reference_video_url


T2V_MODEL = "happyhorse-1.1-t2v"
I2V_MODEL = "happyhorse-1.1-i2v"
R2V_MODEL = "happyhorse-1.1-r2v"
EDIT_MODEL = "happyhorse-1.0-video-edit"
WAN_MODEL = "wan3.0-video"
WAN_MODELS = {WAN_MODEL, "wan3.0-video-prime"}
WAN_REFERENCE_MODEL = "wan2.7-r2v-2026-06-12"
HAPPYHORSE_RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4", "4:5", "5:4", "9:21", "21:9"}
WAN_RATIOS = {"adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}
ImageSource = str | PathLike


def _choice(name, value, choices):
    if value not in choices:
        raise ValueError(f"{name} 必须为 {', '.join(sorted(choices))}")


def _prompt(value: str, *, wan: bool = False, required: bool = True) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError("prompt 必须为非空文本")
    # Reject overlong prompts rather than silently letting the server truncate them.
    chinese_count = sum("\u3400" <= char <= "\u9fff" for char in value)
    length = len(value) if wan else len(value) + chinese_count
    limit = 20000 if wan else 5000
    if length > limit:
        raise ValueError(
            "prompt 过长：Wan 最多 20000 字符；HappyHorse 最多 2500 中文/5000 非中文字符"
        )
    return value


def _parameters(
    *, resolution="1080P", duration=5, watermark=False, seed=None, wan=False, edit=False
):
    _choice(
        "resolution",
        resolution,
        {"720P", "1080P"} if edit else {"480P", "720P", "1080P"},
    )
    if type(watermark) is not bool:
        raise ValueError("watermark 必须为布尔值")
    parameters = {"resolution": resolution, "watermark": watermark}
    if not edit:
        low, high = (2, 30) if wan else (3, 15)
        if type(duration) is not int or not (
            low <= duration <= high or (wan and duration == -1)
        ):
            raise ValueError(
                f"duration 必须为 {low}~{high} 的整数秒"
                + ("，或 -1（智能时长）" if wan else "")
            )
        parameters["duration"] = duration
    if seed is not None:
        low = -1 if wan else 0
        if type(seed) is not int or not low <= seed <= 2147483647:
            raise ValueError(f"seed 必须为 [{low}, 2147483647] 的整数")
        parameters["seed"] = seed
    return parameters


def _wan_options(parameters, audio, prompt_extend):
    for key, value in (("audio", audio), ("prompt_extend", prompt_extend)):
        if value is not None:
            if type(value) is not bool:
                raise ValueError(f"{key} 必须为布尔值")
            parameters[key] = value


def _references(images, *, maximum, required=False):
    if images is None:
        images = []
    if isinstance(images, (str, bytes)) or not isinstance(images, Sequence):
        raise ValueError("reference_images 必须为图片列表")
    minimum = 1 if required else 0
    if not minimum <= len(images) <= maximum:
        raise ValueError(f"reference_images 数量必须为 {minimum}~{maximum}")
    return [{"type": "reference_image", "url": image_url(image)} for image in images]


def build_text_video_payload(
    prompt: str,
    *,
    model: str = T2V_MODEL,
    resolution="1080P",
    ratio="16:9",
    duration=5,
    watermark=False,
    seed=None,
    audio=None,
    prompt_extend=None,
) -> dict:
    """HappyHorse T2V, or explicitly selected Wan T2V (retained from original)."""
    _choice("model", model, {T2V_MODEL} | WAN_MODELS)
    wan = model in WAN_MODELS
    _choice("ratio", ratio, WAN_RATIOS if wan else HAPPYHORSE_RATIOS)
    parameters = _parameters(
        resolution=resolution,
        duration=duration,
        watermark=watermark,
        seed=seed,
        wan=wan,
    )
    if wan:
        _wan_options(parameters, audio, prompt_extend)
    elif audio is not None or prompt_extend is not None:
        raise ValueError(
            "HappyHorse 不支持 audio/prompt_extend 参数；音频由模型原生生成"
        )
    return {
        "model": model,
        "input": {"prompt": _prompt(prompt, wan=wan)},
        "parameters": {**parameters, "ratio": ratio},
    }


def build_image_video_payload(
    prompt: str = "",
    image_path: ImageSource | None = None,
    *,
    first_frame: ImageSource | None = None,
    last_frame: ImageSource | None = None,
    resolution="1080P",
    duration=5,
    watermark=False,
    seed=None,
) -> dict:
    """HappyHorse I2V: one first frame plus optional prompt. No last-frame mode."""
    if last_frame is not None:
        raise ValueError(
            "happyhorse-1.1-i2v 仅支持首帧；首尾帧请用 build_frame_video_payload / frames 命令（wan3.0-video）"
        )
    if image_path is not None and first_frame is not None:
        raise ValueError("image_path 与 first_frame 只能传一个")
    image = first_frame if first_frame is not None else image_path
    if image is None:
        raise ValueError("必须传入 first_frame 或 image_path")
    parameters = _parameters(
        resolution=resolution, duration=duration, watermark=watermark, seed=seed
    )
    return {
        "model": I2V_MODEL,
        "input": {
            "prompt": _prompt(prompt, required=False),
            "media": [{"type": "first_frame", "url": image_url(image)}],
        },
        "parameters": parameters,
    }


def build_reference_video_payload(
    prompt: str,
    reference_images: Sequence[ImageSource],
    *,
    resolution="1080P",
    ratio="16:9",
    duration=5,
    watermark=False,
    seed=None,
) -> dict:
    """HappyHorse R2V: ordered 1–9 images, referenced using [Image 1], etc."""
    _choice("ratio", ratio, HAPPYHORSE_RATIOS)
    parameters = _parameters(
        resolution=resolution, duration=duration, watermark=watermark, seed=seed
    )
    return {
        "model": R2V_MODEL,
        "input": {
            "prompt": _prompt(prompt),
            "media": _references(reference_images, maximum=9, required=True),
        },
        "parameters": {**parameters, "ratio": ratio},
    }


def build_video_edit_payload(
    prompt: str,
    video_url: str,
    *,
    reference_images: Sequence[ImageSource] | None = None,
    resolution="1080P",
    watermark=False,
    seed=None,
    audio_setting="auto",
) -> dict:
    """HappyHorse edit: one public video URL, optional 0–5 reference images.

    No duration/ratio: output follows the source (up to its first 15 seconds).
    """
    _choice("audio_setting", audio_setting, {"auto", "origin"})
    parameters = _parameters(
        resolution=resolution, watermark=watermark, seed=seed, edit=True
    )
    media = [{"type": "video", "url": http_url(video_url, "待编辑视频 URL")}]
    media.extend(_references(reference_images, maximum=5))
    return {
        "model": EDIT_MODEL,
        "input": {"prompt": _prompt(prompt), "media": media},
        "parameters": {**parameters, "audio_setting": audio_setting},
    }


def build_multimodal_reference_payload(
    prompt: str,
    reference_images: Sequence[ImageSource] | None = None,
    reference_videos: Sequence[str] | None = None,
    *, resolution="1080P", ratio="16:9", duration=5, watermark=False,
    seed=None, prompt_extend=False,
) -> dict:
    """Wan 2.7: 1–5 combined image/video references, separately numbered by kind.

    Contract: https://help.aliyun.com/zh/model-studio/wan-video-to-video-api-reference
    Video references limit output duration to 2–10s; image-only allows 2–15s.
    """
    videos=reference_videos if reference_videos is not None else []
    images=reference_images if reference_images is not None else []
    for value in (images,videos):
        if isinstance(value,(str,bytes)) or not isinstance(value,Sequence):
            raise ValueError('参考素材必须为列表')
    if not 1<=len(images)+len(videos)<=5:raise ValueError('Wan 2.7 参考图片与视频合计须为1~5个')
    high=10 if videos else 15
    if type(duration) is not int or not 2<=duration<=high:raise ValueError(f'Wan 2.7 当前输入的时长须为2~{high}秒')
    if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>5000:raise ValueError('Wan 2.7 Prompt须为1~5000字符')
    if type(prompt_extend) is not bool:raise ValueError('prompt_extend 必须为布尔值')
    _choice('ratio',ratio,{'16:9','9:16','1:1','4:3','3:4'})
    parameters=_parameters(resolution=resolution,watermark=watermark,seed=seed,edit=True)
    media=[{'type':'reference_image','url':image_url(im,allow_bmp=True)} for im in images]
    media += [{'type':'reference_video','url':reference_video_url(v)} for v in videos]
    return {'model':WAN_REFERENCE_MODEL,'input':{'prompt':prompt,'media':media},
            'parameters':{**parameters,'duration':duration,'ratio':ratio,'prompt_extend':prompt_extend}}


def build_frame_video_payload(
    prompt: str,
    first_frame: ImageSource,
    last_frame: ImageSource | None = None,
    *,
    model: str = WAN_MODEL,
    resolution="1080P",
    ratio="adaptive",
    duration=5,
    watermark=False,
    seed=None,
    audio=None,
    prompt_extend=None,
) -> dict:
    """Wan first-frame or first/last-frame generation; no silent model fallback."""
    _choice("model", model, WAN_MODELS)
    _choice("ratio", ratio, WAN_RATIOS)
    parameters = _parameters(
        resolution=resolution,
        duration=duration,
        watermark=watermark,
        seed=seed,
        wan=True,
    )
    _wan_options(parameters, audio, prompt_extend)
    media = [{"type": "first_frame", "url": image_url(first_frame, allow_bmp=True)}]
    if last_frame is not None:
        media.append(
            {"type": "last_frame", "url": image_url(last_frame, allow_bmp=True)}
        )
    return {
        "model": model,
        "input": {"prompt": _prompt(prompt, wan=True, required=False), "media": media},
        "parameters": {**parameters, "ratio": ratio},
    }
