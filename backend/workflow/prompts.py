"""Explicit, bounded prompt compiler with ordered reference bindings."""
import re
from core.store import find, fingerprint, selected_asset


def weight(text):
    return sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in text)


def identity_for_look(character, look):
    """A new life stage changes apparent age, while retaining facial identity."""
    identity = character["base_prompt"]
    match = re.search(r"(?<!\d)(\d{1,3})\s*岁", look.get("prompt_delta", ""))
    age = look.get("age") or (int(match[1]) if match else None)
    if age is not None:
        identity = re.sub(r"(?<!\d)\d{1,3}\s*岁", f"{age}岁", identity, count=1)
        identity += f"；当前形象年龄为{age}岁，在基础身份上自然增龄或减龄，保持五官骨骼身份连续性。"
    return identity


def compile_video(state, episode, shot):
    refs, bindings, locks, dependencies = [], [], [], []
    for cast in shot["characters"]:
        character = find(state["characters"], cast["id"])
        look_id = cast.get("look_id") or character["looks"][0]["id"]
        look = find(character["looks"], look_id)
        image = selected_asset(character, look_id)
        if not image:
            raise ValueError(f"{character['name']} / {look['name']} 尚未采用形象图")
        # The wardrobe master is one image of one person. Turnarounds are for QA,
        # not a grid shown to the video model, which could spawn duplicate actors.
        refs.append(image["path"])
        bindings.append(f"[Image {len(refs)}]仅绑定{character['name']}的脸、发型、体形、该形象服装和饰品，不复制背景、文字或多人物。")
        locks.append(f"{character['name']}当前完整人物锁：{identity_for_look(character, look)} 当前形象锁：{look.get('prompt_delta','')}")
        dependencies.append(image["id"])
    location = find(state["locations"], shot["location_id"])
    image = selected_asset(location, "location")
    if not image:
        raise ValueError(f"场景 {location['name']} 尚未采用场景母版")
    refs.append(image["path"])
    bindings.append(f"[Image {len(refs)}]只参考空间布局、固定设施、材质和光线，不继承图中任何人物。")
    dependencies.append(image["id"])
    if len(refs) > 9:
        raise ValueError("单镜头主角过多，需拆镜：参考图超过9张")
    dialogue = "\n".join(f"{d['speaker']}（{'画外音，不做口型' if d.get('voiceover') else '普通话；可见发言者口型自然同步，画外同期声遵循编导补充'}）：“{d['text']}”" for d in shot.get("dialogue", []))
    visual_style = state["project"]["visual_style"].split("疾病场面", 1)[0]
    parts = [f"本镜头{shot['duration']}秒，一个连续的叙事片段。{visual_style}",
        "参考绑定（顺序固定）：" + "".join(bindings), "全局人物锁：" + "\n".join(locks),
        f"固定场景：{location['canonical_prompt']}", f"入镜状态：{shot.get('continuity_in','延续上镜头')}",
        f"动作与表演：{shot['action']}\n镜头：{shot['camera']}",
        f"台词与声音：{dialogue or '无台词，只保留医院环境底噪与人物动作声。'}",
        f"出镜状态：{shot.get('continuity_out','停留在动作落点以便剪辑')}",
        "连续性与声音锁：同一人物全程保持脸型、年龄、五官比例、发际线、体态与声音音色一致；医院方位、道具持握手、眼线和屏幕方向不跳变。画外音不让画面人物开口。两人交谈不交换声音或脸。动作前有起势，末尾留0.5秒稳定剪辑余量；保持上一镜头的情绪和动作。无背景配乐；原生中文人声清楚，无多余旁白，无烧录字幕、无文字水印、无分屏、无多视图、无画面变形。"]
    if shot.get("prompt"):
        parts.insert(-1, "编导补充：" + shot["prompt"])
    prompt = "\n\n".join(parts)
    # Never silently cut off a face lock or a line. Force a smaller shot instead.
    if weight(prompt) > 5000:
        raise ValueError(f"镜头提示词超过HappyHorse上限（权重{weight(prompt)}/5000），请拆镜或精简锁定描述")
    return prompt, refs, fingerprint({"prompt": prompt, "refs": dependencies}), dependencies
