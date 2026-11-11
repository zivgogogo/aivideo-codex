"""Technical decode checks and sampled visual review with recorded evidence."""
from __future__ import annotations

import base64
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
from typing import Any
import unicodedata

from PIL import Image, ImageDraw

from ai.llm import DashScopeLLMClient
from backend.workflow.render import probe_video, _run
from core.store import safe_path, write_json, now


def build_qa_shot(state: dict, episode: dict, shot: dict) -> dict:
    """Add review context to a copy; generation inputs and stored designs stay intact."""
    enriched = copy.deepcopy(shot)
    enriched["episode_context"] = {key: episode.get(key) for key in ("id", "title", "summary", "climax")}
    location = next((item for item in state.get("locations", []) if item["id"] == shot.get("location_id")), None)
    enriched["location_context"] = ({key: copy.deepcopy(location.get(key)) for key in ("id", "name", "canonical_prompt")}
                                     if location is not None else None)
    enriched["previous_shot_design"] = None
    for index, candidate in enumerate(episode.get("shots", [])):
        if candidate["id"] == shot["id"] and index:
            previous = episode["shots"][index - 1]
            enriched["previous_shot_design"] = {key: previous.get(key) for key in (
                "id", "action", "continuity_in", "continuity_out", "source_actions")}
            break
    for cast in enriched.get("characters", []):
        character = next((item for item in state["characters"] if item["id"] == cast["id"]), None)
        if character is None:
            continue
        looks = character.get("looks", [])
        look_id = cast.get("look_id") or (looks[0]["id"] if looks else None)
        look = next((item for item in looks if item["id"] == look_id), {})
        appearance = " ".join(str(look.get(key, "")) for key in ("name", "prompt_delta", "appearance", "description")).lower()
        explicit_coverage = cast.get("mouth_covered", look.get("mouth_covered"))
        covered = explicit_coverage if type(explicit_coverage) is bool else any(
            token in appearance for token in ("口罩", "n95", "kn95", "ffp2", "ffp3", "呼吸面罩"))
        cast.update(name=character.get("name", cast["id"]), look_name=look.get("name", look_id),
                    mouth_covered=covered, look_description=look.get("prompt_delta", ""))
    return enriched


def _data_image(path: Path) -> dict:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + base64.b64encode(path.read_bytes()).decode()}}


def _scene_response_json(value: str | dict) -> dict:
    """Do not coerce truthy strings, numbers or missing fields into approval."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("场景质检未返回JSON对象")
    for field in ("people_present", "single_space", "collage", "key_layout_pass", "pass"):
        if type(value.get(field)) is not bool:
            raise ValueError(f"场景质检缺少布尔字段：{field}")
    defects = value.get("layout_defects")
    if not isinstance(defects, list) or not all(isinstance(item, str) and item.strip() for item in defects):
        raise ValueError("场景质检layout_defects必须为缺陷文本数组")
    if not isinstance(value.get("reason"), str) or not value["reason"].strip():
        raise ValueError("场景质检缺少实际像素检查理由")
    return value


def _scene_visual_review(folder: Path, image: Path, location: dict) -> dict:
    messages = [{"role": "system", "content":
        "你是影视空场景母版质检员。必须检查提供图片的实际像素，不相信文件名、提示词或场景文字声称无人。"
        "合格母版必须是单一连续的真实空间的一张照片，完全无人：不得有完整人物、局部手臂/手/腿等肢体、"
        "人脸、床上或椅上坐卧的人、倒影中的人、墙面/屏幕中清晰的人像。仔细看病床、门后、窗边、画面边缘与反射。"
        "不得是九宫格、拼贴、多视图、分屏或多个不同空间并列。正常门窗、镜面、玻璃隔断、墙砖不等于分屏。"
        "另按canonical_prompt明确指定的关键空间布局、房门状态和单床房等核对实际像素。只拒明确互斥："
        "要求门扇完全关闭并与门框齐平却明显敞开，独立病房变走廊或多床房，或明确必需的病床缺失。"
        "canonical未明确门状态时不得自行要求关门，也不得因普通镜反射、摄影反打允许的左右变化或无关细节差异硬拒。"
        "证据不足不可编造布局缺陷。key_layout_pass为关键布局是否符合，layout_defects列明确可见的布局冲突字符串，没有则空数组。"
        "仅返回严格JSON，people_present/single_space/collage/key_layout_pass/pass均为布尔，layout_defects为字符串数组，reason为简短中文像素证据。"
        "任何人物/肢体/人脸存在、不是单一连续空间、存在拼贴、key_layout_pass=false或layout_defects非空，都必须pass=false。"
        "只有完全无人、单一空间、非拼贴且关键布局符合无冲突才pass=true。不要输出Markdown。"},
        {"role": "user", "content": [{"type": "text", "text":
            "检查这张待采用场景图。文字仅作空间上下文，不可替代像素证据：" +
            json.dumps({key: location.get(key) for key in ("id", "name", "canonical_prompt")}, ensure_ascii=False)},
            _data_image(image)]}]
    try:
        client = DashScopeLLMClient(model="qwen3-vl-plus", timeout=120, max_tokens=700,
                                   temperature=0.1, enable_thinking=False)
        response = client.chat(messages, extra_body={"response_format": {"type": "json_object"}})
    except Exception as exc:
        write_json(folder / "scene-qa-response.json", {"error": str(exc)[:1200], "model": "qwen3-vl-plus"})
        raise
    write_json(folder / "scene-qa-response.json", response)
    return _scene_response_json(response["choices"][0]["message"]["content"])


def inspect_scene_image(project_root: Path, asset: dict, location: dict) -> dict:
    """Gate new scene references only; unavailable vision is never an approval."""
    root = Path(project_root).resolve()
    image = safe_path(root, asset["path"])
    folder = image.parent
    folder.mkdir(parents=True, exist_ok=True)
    result = {"asset_id": asset.get("id"), "location_id": location.get("id"), "created_at": now(),
              "model": "qwen3-vl-plus", "technical_pass": False, "passed": None,
              "needs_review": True, "visual": None,
              "scene_qa_version": 2, "canonical_prompt": location.get("canonical_prompt"),
              "decision_rule": "pass=true AND people_present=false AND single_space=true AND collage=false AND key_layout_pass=true AND layout_defects=[]"}
    try:
        with Image.open(image) as opened:
            opened.verify()
        with Image.open(image) as opened:
            opened.load()
            result["actual"] = {"width": opened.width, "height": opened.height,
                                "format": opened.format, "frames": getattr(opened, "n_frames", 1),
                                "bytes": image.stat().st_size}
        if (result["actual"]["width"] <= 0 or result["actual"]["height"] <= 0
                or result["actual"]["frames"] != 1):
            raise ValueError("场景母版必须为单张可解码静态图片")
        result["technical_pass"] = True
    except Exception as exc:
        result.update(passed=False, needs_review=False, technical_error=str(exc)[:1200])
        write_json(folder / "scene-qa-response.json", {"not_called": "local_image_invalid", "error": str(exc)[:1200]})
    if result["technical_pass"]:
        try:
            visual = _scene_response_json(_scene_visual_review(folder, image, location))
            passed = (visual["pass"] is True and visual["people_present"] is False
                      and visual["single_space"] is True and visual["collage"] is False
                      and visual["key_layout_pass"] is True and not visual["layout_defects"])
            result.update(visual=visual, passed=passed, needs_review=False)
        except Exception as exc:
            result.update(visual_error=str(exc)[:1200], passed=None, needs_review=True)
            if not (folder / "scene-qa-response.json").exists():
                write_json(folder / "scene-qa-response.json", {"error": str(exc)[:1200], "model": "qwen3-vl-plus"})
    result["qa_path"] = str((folder / "scene-qa.json").relative_to(root))
    write_json(folder / "scene-qa.json", result)
    return result


def _response_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("视觉审查未返回JSON对象")
    for field in ("identity", "scene", "action"):
        value = result.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
            raise ValueError(f"视觉审查缺少0–100分数：{field}")
    if type(result.get("pass")) is not bool or not isinstance(result.get("defects"), list) or not isinstance(result.get("reason"), str):
        raise ValueError("视觉审查结果缺少 pass/defects/reason")
    if "core_plot_pass" in result or "blocking_defects" in result:
        blocking = result.get("blocking_defects")
        if (type(result.get("core_plot_pass")) is not bool or not isinstance(blocking, list)
                or not all(isinstance(item, str) for item in blocking)):
            raise ValueError("视觉审查的核心剧情或阻断缺陷字段格式无效")
    if "needs_review" in result:
        if type(result["needs_review"]) is not bool:
            raise ValueError("视觉审查needs_review必须为布尔")
        if result["needs_review"] and (not isinstance(result.get("limitations"), str) or not result["limitations"].strip()):
            raise ValueError("视觉待复核结果必须说明证据局限")
    return result


def _clean_speech(text: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", text).lower()
                   if char.isalnum())


def character_error_rate(expected: str, transcript: str) -> float:
    reference, hypothesis = _clean_speech(expected), _clean_speech(transcript)
    if not reference:
        return 0.0 if not hypothesis else 1.0
    row = list(range(len(hypothesis) + 1))
    for i, left in enumerate(reference, 1):
        following = [i]
        for j, right in enumerate(hypothesis, 1):
            following.append(min(following[-1] + 1, row[j] + 1,
                                 row[j - 1] + (left != right)))
        row = following
    return round(row[-1] / len(reference), 6)


NONVERBAL_CHARACTERS = frozenset("啊嗯呜呃哦噢唔哼哈呵唉哎欸呀咦哇嘶呼沙扑哔滴咚砰啪哗嗒嘀叮轰嗡喀咔咳嗬嗷呦呸咻啧")


def audio_verdict(expected: str, transcript: str) -> dict:
    """Keep CER mathematical; classify empty-dialogue sound transcriptions separately."""
    reference, hypothesis = _clean_speech(expected), _clean_speech(transcript)
    expected_nonverbal = not reference or set(reference) <= NONVERBAL_CHARACTERS
    nonverbal_only = not hypothesis or set(hypothesis) <= NONVERBAL_CHARACTERS
    cer = character_error_rate(expected, transcript)
    allowed = expected_nonverbal and nonverbal_only
    return {"audio_pass": nonverbal_only if expected_nonverbal else cer <= 0.2,
            "CER": cer, "cer": cer, "threshold": 0.2,
            "nonverbal_only": nonverbal_only, "expected_nonverbal": expected_nonverbal,
            "allow_reason": "无语义对白镜头仅转写出语气词、拟声或标点，未新增实词台词。" if allowed else None,
            "decision_rule": "nonverbal_whitelist" if expected_nonverbal else "cer"}


def _audio_review(video: Path, folder: Path, shot: dict) -> dict:
    """Qwen short-audio compatible API accepts local WAV data URIs."""
    audio = folder / "audio_16k_mono.wav"
    _run(["-y", "-i", str(video), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
          "-c:a", "pcm_s16le", str(audio)])
    client = DashScopeLLMClient(model="qwen3-asr-flash", timeout=120)
    # ASR does not use the text model's sampling or thinking parameters.
    client.config = replace(client.config, temperature=None, enable_thinking=None, max_tokens=None)
    encoded = base64.b64encode(audio.read_bytes()).decode()
    response = client.chat([{"role": "user", "content": [{"type": "input_audio", "input_audio": {
        "data": "data:audio/wav;base64," + encoded}}]}],
        extra_body={"asr_options": {"enable_itn": False, "language": "zh"}})
    write_json(folder / "asr-response.json", response)
    transcript = response["choices"][0]["message"]["content"]
    if not isinstance(transcript, str):
        raise ValueError("ASR没有返回转写文本")
    expected = "".join(str(item.get("text", "")) for item in shot.get("dialogue", []))
    return {"model": "qwen3-asr-flash", "transcript": transcript, "expected": expected,
            **audio_verdict(expected, transcript),
            "audio_checked": True, "comparison": "CER保留原始字符编辑距离；无语义对白按非言语白名单判定，有语义对白使用CER<=0.2。",
            "limitations": "仅核对整镜头台词文本；未验证说话人、音色、逐词时间或口型。"}


def _visual_review(qa_folder: Path, frames: list[Path], references: list[Path], shot: dict, video: Path | None = None) -> dict:
    # Every visible lead must be checked, including the third actor in a group.
    # compile_video places the location after the ordered character references.
    selected = references
    content: list[dict[str, Any]] = [{"type": "text", "text":
        "以下先是全部角色参考图及最后一张场景参考图，再是待检查的完整短视频（按每秒2帧采样）。"
        "仅根据可见证据检查，不要因为文字声称一致就判一致。角色参考用于脸型、五官、发型、年龄、服装；场景参考用于空间和光色。"
        "核对整个片段的动作与状态变化，不可声称听到了声音或识别了对白。镜头要求：" +
        json.dumps({k: shot.get(k) for k in ("id", "characters", "location_id", "location_context", "action", "camera", "continuity_in", "continuity_out", "overlays", "source_actions", "episode_context", "previous_shot_design")}, ensure_ascii=False)}]
    for index, path in enumerate(selected, 1):
        cast = shot.get("characters", [])
        if index == len(selected):
            label = "场景参考"
        else:
            character = cast[index - 1] if index <= len(cast) else {}
            label = (f"人物参考{index}，绑定身份{character.get('id', index)} {character.get('name', '')}，"
                     f"当前形象：{character.get('look_name', '')}，mouth_covered={character.get('mouth_covered', 'unknown')}")
        content += [{"type": "text", "text": label}, _data_image(path)]
    if video is not None:
        content.append({"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + base64.b64encode(video.read_bytes()).decode(), "fps": 2.0}})
    else:
        for index, path in enumerate(frames, 1):
            content += [{"type": "text", "text": f"视频采样帧{index}（按时间顺序）"}, _data_image(path)]
    messages = [{"role": "system", "content":
        "你是短剧影像质检员。返回严格JSON，字段 identity/scene/action 为0到100评分，defects为可见缺陷字符串数组，"
        "pass为布尔，reason为简短中文理由，limitations为证据局限；另返回core_plot_pass布尔、blocking_defects字符串数组、minor_observations字符串数组、needs_review布尔。"
        "blocking_defects仅记录有明确证据的阻断性缺陷，minor_observations记录非阻断的细节差异。identity评价实际可验证外观与绑定参考的兼容程度，不等于人脸可见度或身份核验置信度。"
        "若look明确要求PPE遮脸，或镜头合理采用背影、侧影、手部特写，应按可见眉眼、体态、服装与防护外观核对；不能单纯因未见完整脸就判错人、列wrong_identity或自动把identity降到阈值以下。"
        "不可见部位如实写limitations，不虚称完整验证人脸。若可见证据仍不足以判断外观兼容或错误、核心剧情成立且无明确blocking_defects，返回needs_review=true并说明具体局限；未知不等于明确失败，不编造缺陷。"
        "正常可评估时needs_review=false；实际可见换脸、身份复制、错衣或缺必要PPE必须列blocking_defects并pass=false，不能以遮挡或needs_review为其开脱。"
        "以身份、剧情核心动作、关键道具及剧情结果为准。严重换脸、错服装、身份复制、严重畸形、关键道具缺失、错台词或关键剧情反转必须列入blocking_defects并pass=false。"
        "人物当前look参考图是该look固定服装和防护装备的母版；逐人核对颜色、款式、配饰和防护件。若参考图、look_description、分镜action或continuity_in/out之间有冲突，或视频与之不符，必须明确报告对应人物、冲突内容和可见采样时点，不能自行编造合理变体、简化版本或剧情允许换装来消解。"
        "除非本镜明确拍摄穿脱过程且与人物阶段和look绑定相符，同一look在连续场景不得忽略换装；清楚的错服装仍按上述硬标准列入blocking_defects并pass=false，不能因剧情核心仍成立而放行。"
        "若三维评分均>=75、core_plot_pass=true且blocking_defects为空，必须pass=true，不得仅因非阻断细节给false。"
        "抽鼻、泪珠、轻微手位、手暂时垂下、微小表情或约0.5秒动作起势不属于阻断缺陷，只记minor_observations；不能据此否定已清楚完成的哀求抓袖、冷静应对或医护包围等剧情核心。"
        "episode_context的summary/climax仅用于理解全剧和本集走向，不是每个镜头都必须完成的任务，不得要求每镜实现整集高潮。"
        "source_actions可能是跨镜头原作动作段落；previous_shot_design仅是前镜设计摘要，不是本镜视频证据。若前镜已承担拍门等动作设计，本镜对话转为扶门聆听属于合理连续性，不要求持续重复每一微动作。"
        "在2fps视频抽样中未捕获拍、敲、抖手等瞬间，只能记为不确定；不能仅因看见手已落下或贴玻璃的起落姿态就断言动作未发生。"
        "当mouth_covered=true或外观包含N95、口罩、护目镜时，嘴部或原眼镜可能被遮挡；看不到嘴或眼镜不等于缺失、换脸或没说话。"
        "但若口鼻或眼部完整裸露，且可见证据明确本应佩戴的N95、口罩或护目镜实际缺失，必须列入blocking_defects并pass=false，不能以mouth_covered=true或遮挡为由放行；透明护目镜后能看到眼睛不等于装备缺失。"
        "遮挡下不得以未缓缓开口、未见嘴动拒绝，也不得用微小嘴形判断对白；语句内容与是否有指定对白交给独立ASR，视觉只核对可见身份、动作和位置。"
        "玻璃门的位置、角色在门内外的侧别、门的开关结果和真实身份改变仍是关键连续性；仅有明确相反证据时列为阻断缺陷，不可推测补造。"
        "结合location_context的场景名称和canonical_prompt区分前部隔离玻璃入口与房内后部卫生间门，不把任意房门都当隔离边界。"
        "在角色空间归属正确、要求关闭的隔离入口实际保持闭合、核心剧情表现成立的前提下，半身近景未拍到脚不等于缺鞋，门框入画或摄影机改在玻璃外不等于人物走到走廊；"
        "这些角度和裁切本身不作为blocking_defects，声音仍交独立ASR核验。门扇或脚部未入画时写limitations，不能凭门框推断开门或凭未拍脚断言鞋子缺失。"
        "但实际隔离入口明确被打开、人物真实跨越隔离边界，或可见人物服装/鞋/PPE与绑定look明显不符，仍必须列入blocking_defects并pass=false；不能用机位或裁切理由消解真实缺陷。"
        "逐帧检查C开头或char_开头的独立人物身份是否被复制：特别注意越肩镜头，同一人物不能同时作为前景背影和对面正脸各出现一次；"
        "若发生同一独立身份重复，必须判false并报告所在采样帧。G开头是群演池，可有不同脸的同制服成年人；G不是所有医护必须使用的身份前缀，具名C开头医护也可出现。"
        "不同人物可以穿着不同颜色和款式的服装，各按其绑定形象核对，不能仅因颜色不同就判错装或编造群演规则。"
        "按剧情意义判断动作和道具，不因合理遮挡、局部裁切、单肩双肩微小姿态差异而拒绝；但关键道具消失、换手导致剧情矛盾应拒绝。"
        "检查对象是尚未后期合成的云端raw视频。shot.overlays是后期添加计划，字幕、热搜虚影、浮动文字、停职通知等文件上的文字会在后期完成；"
        "不可因为这些后期文字在raw中未出现、不可读或未浮动而拒绝，也不可要求生成模型把文件文字写清楚。"
        "若动作要求人物拿着纸张、文件或展示手机，该实体道具本身仍应存在；仅文字不可读不等于道具缺失。"
        "手部、背影或道具特写若镜头本来不要求露脸，不可仅因未露脸拒绝，按可见服装肤色配饰核对，并在limitations说明无面部证据。"
        "不要因为抽帧没有捕获一次短暂手势就断言动作未发生；只有明确相反证据或剧情落点缺失才据此拒绝。"
        "如果镜头无出镜人物，则identity记100并注明不适用；若要求人物出镜却缺失不能这样处理。"
        "可见但无法确认的部分请在limitations中说明。不要输出Markdown。"}, {"role": "user", "content": content}]
    client = DashScopeLLMClient(model="qwen3-vl-plus", timeout=120, max_tokens=1800, temperature=0.1, enable_thinking=False)
    result = client.chat(messages, extra_body={"response_format": {"type": "json_object"}})
    write_json(qa_folder / "visual-response.json", result)
    parsed = _response_json(result["choices"][0]["message"]["content"])
    parsed["model"] = "qwen3-vl-plus"
    parsed["reference_paths"] = [str(p) for p in selected]
    parsed["sample_count"] = len(frames)
    parsed["postproduction_overlays"] = shot.get("overlays", [])
    parsed["episode_context"] = shot.get("episode_context")
    parsed["location_context"] = shot.get("location_context")
    parsed["previous_shot_design"] = shot.get("previous_shot_design")
    parsed["video_input_mode"] = "full_clip_2fps" if video is not None else "three_sample_frames"
    parsed["raw_model_pass"] = parsed["pass"]
    scores_pass = all(parsed[field] >= 75 for field in ("identity", "scene", "action"))
    blocking = parsed.get("blocking_defects")
    if isinstance(blocking, list) and all(isinstance(item, str) for item in blocking) and type(parsed.get("core_plot_pass")) is bool:
        parsed["pass"] = scores_pass and parsed["core_plot_pass"] and not blocking
    else:
        parsed["pass"] = parsed["pass"] and scores_pass
    return parsed


def inspect_video(project_root: Path, asset: dict, shot: dict, reference_paths: list) -> dict:
    """Inspect a real clip; vision errors retain technical evidence and require review.

    DRAMA_VISUAL_QA=0 / DRAMA_AUDIO_QA=0 explicitly skip paid calls for tests.
    ASR validates aggregate text only; speaker/timing/lipsync are not verified.
    """
    root = Path(project_root).resolve()
    video = safe_path(root, asset["path"])
    if not video.is_file():
        raise ValueError(f"质检视频不存在：{asset['path']}")
    references = [safe_path(root, p) for p in reference_paths]
    for path in references:
        if not path.is_file():
            raise ValueError(f"质检参考图不存在：{path}")
    folder = video.parent / "qa"
    folder.mkdir(parents=True, exist_ok=True)
    result = {"asset_id": asset.get("id"), "shot_id": shot.get("id"), "created_at": now(),
        "technical_pass": False, "visual_pass": None, "needs_human_review": True,
        "visual": None, "audio_checked": False, "audio_pass": None,
        "expected": "".join(str(item.get("text", "")) for item in shot.get("dialogue", [])),
        "transcript": None, "CER": None,
        "nonverbal_only": None, "allow_reason": None,
        "audio_limitations": "整镜头ASR文本核对不能验证说话人、声线、口型或字幕时间对齐。",
        "motion_limitations": "视觉模型对完整片段按2fps抽帧检查；不能保证抽帧间每一帧均无细微问题。"}
    try:
        actual = probe_video(video)
        _run(["-v", "error", "-xerror", "-err_detect", "explode", "-i", str(video),
              "-map", "0:v:0", "-map", "0:a:0?", "-f", "null", "-"])
        requested = float(shot["duration"])
        request_file = video.parent / "request.json"
        request = json.loads(request_file.read_text(encoding="utf-8")) if request_file.is_file() else {}
        resolution = request.get("resolution", shot.get("resolution", "720P"))
        floor = {"480P": 480, "720P": 720, "1080P": 1080}.get(resolution)
        checks = {"decodable": True, "has_video": actual["frames"] > 0,
                  "has_audio": actual["has_audio"], "requested_duration_valid": 3 <= requested <= 15,
                  "duration_matches": abs(actual["duration"] - requested) <= max(0.35, requested * 0.08),
                  "duration_in_model_range": 2.8 <= actual["duration"] <= 15.5,
                  "portrait": actual["height"] > actual["width"],
                  "ratio_9_16": abs(actual["width"] / actual["height"] - 9 / 16) <= 0.035,
                  "resolution": floor is not None and min(actual["width"], actual["height"]) >= floor * 0.90}
        result.update(actual=actual, checks=checks, technical_pass=all(checks.values()))
        frames = []
        for index, fraction in enumerate((0.12, 0.5, 0.88), 1):
            path = folder / f"frame_{index}.jpg"
            _run(["-y", "-ss", str(actual["duration"] * fraction), "-i", str(video),
                  "-frames:v", "1", "-vf", "scale=720:-2", "-q:v", "3", str(path)])
            if not path.is_file():
                raise ValueError("视频抽帧失败")
            frames.append(path)
        contact = Image.new("RGB", (840, 535), "#14171c")
        draw = ImageDraw.Draw(contact)
        for index, path in enumerate(frames):
            with Image.open(path) as frame:
                frame.thumbnail((270, 480))
                contact.paste(frame, (index * 280 + (280 - frame.width) // 2, 10))
            draw.text((index * 280 + 15, 502), f"{(0.12, 0.5, 0.88)[index] * actual['duration']:.2f}s", fill="white")
        contact.save(folder / "contact.jpg", quality=90)
        result["contact_path"] = str((folder / "contact.jpg").relative_to(root))
        result["frame_paths"] = [str(p.relative_to(root)) for p in frames]
        if actual["has_audio"] and os.environ.get("DRAMA_AUDIO_QA", "1").lower() not in {"0", "false", "off"}:
            try:
                audio = _audio_review(video, folder, shot)
                result.update(audio=audio, audio_pass=audio["audio_pass"], audio_checked=True,
                              expected=audio["expected"], transcript=audio["transcript"], CER=audio["CER"],
                              nonverbal_only=audio.get("nonverbal_only"), allow_reason=audio.get("allow_reason"))
            except Exception as exc:
                key = os.environ.get("DASHSCOPE_API_KEY", "")
                error = str(exc).replace(key, "[REDACTED]") if key else str(exc)
                result["audio_error"] = error[:1000]
        elif not actual["has_audio"]:
            result.update(audio_pass=False, audio_error="视频没有音轨")
        else:
            result["audio_error"] = "ASR审查已显式关闭"
        if os.environ.get("DRAMA_VISUAL_QA", "1").lower() not in {"0", "false", "off"}:
            try:
                visual = _visual_review(folder, frames, references, shot, video)
                pending = (visual.get("needs_review") is True and visual.get("core_plot_pass") is True
                           and visual.get("blocking_defects") == [])
                # Preserve every model score/verdict. Explicit uncertainty is
                # pending review, not a failed identity that can trigger repair.
                result.update(visual=visual, visual_pass=None if pending else visual["pass"],
                              visual_review_pending=pending)
            except Exception as exc:
                key = os.environ.get("DASHSCOPE_API_KEY", "")
                error = str(exc).replace(key, "[REDACTED]") if key else str(exc)
                result["visual_error"] = error[:1000]
        else:
            result["visual_error"] = "视觉模型审查已显式关闭"
        result["needs_human_review"] = (not result["technical_pass"] or result["visual_pass"] is not True
                                        or result["audio_pass"] is not True or bool(shot.get("dialogue")))
    except Exception as exc:
        result["technical_pass"] = False
        result["technical_error"] = str(exc)[:1200]
    result["qa_path"] = str((folder / "qa.json").relative_to(root))
    write_json(folder / "qa.json", result)
    return result
