"""Render selected generated shots with their audio and script subtitles."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
from zoneinfo import ZoneInfo

import imageio_ffmpeg
from PIL import ImageFont


WIDTH, HEIGHT, FPS = 720, 1280, 24


def _stamp() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S_%f")


def _run(arguments: list[str]) -> subprocess.CompletedProcess:
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostdin", *arguments]
    result = subprocess.run(command, capture_output=True, text=True, errors="replace")
    if result.returncode:
        raise RuntimeError(f"视频处理失败（{result.returncode}）：{result.stderr[-3000:]}")
    return result


def probe_video(path: Path) -> dict:
    """Decode the complete video; collect actual frame duration without ffprobe."""
    result = _run(["-i", str(path), "-map", "0:v:0", "-an", "-progress", "pipe:1",
                   "-nostats", "-f", "null", "-"])
    source_streams = result.stderr.split("Output #", 1)[0]
    video = next((line for line in source_streams.splitlines() if "Video:" in line), "")
    dimensions = re.search(r"(?<![\w])([1-9]\d{1,4})x([1-9]\d{1,4})(?![\w])", video)
    durations = re.findall(r"^out_time_us=(\d+)$", result.stdout, re.M)
    frames = re.findall(r"^frame=(\d+)$", result.stdout, re.M)
    if not dimensions or not durations or not frames:
        raise ValueError(f"无法读取完整视频信息：{path}")
    duration = int(durations[-1]) / 1_000_000
    frame_count = int(frames[-1])
    rate = re.search(r"([\d.]+) fps(?:,|$)", video)
    if duration <= 0 or frame_count <= 0:
        raise ValueError(f"视频没有可播放画面：{path}")
    return {"width": int(dimensions[1]), "height": int(dimensions[2]),
            "duration": round(duration, 6), "frames": frame_count,
            "fps": float(rate[1]) if rate else round(frame_count / duration, 4),
            "has_audio": any("Audio:" in line for line in source_streams.splitlines()),
            "video_codec": video.split("Video:", 1)[-1].strip().split(",", 1)[0],
            "bytes": path.stat().st_size}


def _inside(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("导出素材必须位于当前项目内")
    return path


def _selected(root: Path, shot: dict) -> tuple[dict, Path]:
    identifier = shot.get("selected")
    asset = next((item for item in shot.get("assets", []) if item.get("id") == identifier), None)
    if not asset or not asset.get("path"):
        raise ValueError(f"镜头 {shot.get('id', '?')} 尚未选用可导出的视频")
    if asset.get("status") != "ready" or asset.get("stale"):
        raise ValueError(f"镜头 {shot.get('id', '?')} 采用的视频尚未完成或依赖已经过期")
    path = _inside(root, asset["path"])
    if not path.is_file() or path.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm"}:
        raise ValueError(f"镜头 {shot.get('id', '?')} 选用的视频不存在或格式无效：{path}")
    return asset, path


def _selection_signature(root: Path, episode: dict) -> str:
    if not episode.get("shots"):
        raise ValueError("本集没有分镜，不能导出成片")
    shots = []
    for shot in episode["shots"]:
        asset, path = _selected(root, shot)
        info = path.stat()
        shots.append({"id": shot.get("id"), "selected": asset.get("id"),
                      "path": str(path.relative_to(root)), "bytes": info.st_size,
                      "mtime_ns": info.st_mtime_ns, "dialogue": shot.get("dialogue", []),
                      "overlays": shot.get("overlays", [])})
    value = {"title": episode.get("title"), "source_title": episode.get("source_title"),
             "shots": shots, "render_version": 3}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _font() -> Path:
    choices = [Path("/System/Library/Fonts/STHeiti Medium.ttc"),
               Path("/System/Library/Fonts/PingFang.ttc"),
               Path("/System/Library/Fonts/Supplemental/Songti.ttc"),
               Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
               Path("C:/Windows/Fonts/msyh.ttc")]
    for path in choices:
        if path.is_file():
            return path
    raise RuntimeError("没有中文字体；请安装 Noto Sans CJK 字体后再导出中文字幕")


def _wrap(text: str, font_path: Path, size: int, max_width: int) -> str:
    font = ImageFont.truetype(str(font_path), size)
    lines = []
    for paragraph in text.splitlines() or [""]:
        line = ""
        for char in paragraph:
            if line and font.getlength(line + char) > max_width:
                lines.append(line)
                line = ""
            line += char
        lines.append(line)
    return "\n".join(lines)


def _filter_path(path: Path) -> str:
    # FFmpeg filter arguments have separate escaping rules from subprocess argv.
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "'\\''")


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def _subtitle_entries(shot: dict, duration: float) -> list[dict]:
    dialogue = [item for item in shot.get("dialogue", []) if str(item.get("text", "")).strip()]
    total = sum(max(1, len(str(item["text"]))) for item in dialogue)
    cursor, usable = 0.15, max(0.1, duration - 0.3)
    entries = []
    for item in dialogue:
        length = usable * max(1, len(str(item["text"]))) / total
        speaker = str(item.get("speaker", "")).strip()
        text = (speaker + "：" if speaker else "") + str(item["text"]).strip()
        entries.append({"start": round(cursor, 4), "end": round(min(duration, cursor + length), 4),
                        "text": text, "speaker": speaker})
        cursor += length
    return entries


def _drawtext(text_path: Path, font_path: Path, *, size: int, y: str, start: float, end: float) -> str:
    return (f"drawtext=fontfile='{_filter_path(font_path)}':textfile='{_filter_path(text_path)}':"
            f"expansion=none:fontsize={size}:fontcolor=white:borderw=2:bordercolor=black:"
            f"line_spacing=10:x=(w-text_w)/2:y={y}:box=1:boxcolor=black@0.42:boxborderw=10:"
            f"enable='between(t,{start:.4f},{end:.4f})'")


def _source_captions(shot: dict, duration: float, font: Path) -> list[dict]:
    """Place original on-screen text above the dialogue's reserved lower area."""
    captions = []
    for overlay in shot.get("overlays", []):
        if overlay.get("kind") != "source_caption" or overlay.get("placement", "top") != "top":
            raise ValueError(f"镜头 {shot.get('id')} 暂只支持顶部 source_caption 叠字")
        text = str(overlay.get("text", "")).strip()
        start, end = overlay.get("start", 0), overlay.get("end", duration)
        if (not text or isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                or not math.isfinite(start) or not math.isfinite(end)
                or not 0 <= start < end or start >= duration):
            raise ValueError(f"镜头 {shot.get('id')} 原作字幕文本或起止时间无效")
        wrapped = _wrap(text, font, 28, 620)
        height = len(wrapped.splitlines()) * 38
        y = 230
        # Simultaneous captions occupy separate lanes; sequential text reuses y.
        for previous in captions:
            if start < previous["end"] and end > previous["start"] and y < previous["y"] + previous["height"] + 18:
                y = previous["y"] + previous["height"] + 18
        if y + height > 850:
            raise ValueError(f"镜头 {shot.get('id')} 同时显示的顶部字幕过多，请拆镜或分段显示")
        captions.append({"kind": "source_caption", "text": text, "wrapped": wrapped,
                         "placement": "top", "start": float(start), "end": min(float(end), duration),
                         "y": y, "height": height})
    return captions


def _concat(paths: list[Path], destination: Path) -> dict:
    """Join content timelines; encode AAC once so per-file priming cannot accumulate."""
    if not paths:
        raise ValueError("没有可拼接的视频")
    arguments, filters, labels, timeline = ["-y"], [], [], []
    total_frames = 0
    for index, path in enumerate(paths):
        info = probe_video(path)
        if (info["width"], info["height"]) != (WIDTH, HEIGHT) or abs(info["fps"] - FPS) > 0.001 or not info["has_audio"]:
            raise ValueError(f"拼接输入必须是720x1280、24fps且带音轨：{path}")
        duration = info["frames"] / FPS
        samples = info["frames"] * (48000 // FPS)
        arguments += ["-threads", "1", "-i", str(path)]
        filters += [f"[{index}:v:0]trim=end_frame={info['frames']},setpts=PTS-STARTPTS,setsar=1[v{index}]",
                    f"[{index}:a:0]aresample=48000,atrim=end_sample={samples},asetpts=PTS-STARTPTS,"
                    f"apad=whole_len={samples},atrim=end_sample={samples}[a{index}]"]
        labels.append(f"[v{index}][a{index}]")
        timeline.append({"path": str(path), "start_frame": total_frames, "frames": info["frames"],
                         "start": total_frames / FPS, "duration": duration})
        total_frames += info["frames"]
    filters.append("".join(labels) + f"concat=n={len(paths)}:v=1:a=1[v][a]")
    script = ";\n".join(filters)
    (destination.parent / "concat-filter.txt").write_text(script + "\n", encoding="utf-8")
    (destination.parent / "concat-timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    arguments += ["-filter_complex_threads", "1", "-filter_complex", script,
                  "-map", "[v]", "-map", "[a]", "-fps_mode", "cfr", "-r", str(FPS),
                  "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                  "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
                  "-movflags", "+faststart", str(destination)]
    _run(arguments)
    output = probe_video(destination)
    expected = total_frames / FPS
    if output["frames"] != total_frames or abs(output["fps"] - FPS) > 0.001 or abs(output["duration"] - expected) > 0.002:
        raise RuntimeError(f"拼接内容时间轴校验失败：预期{total_frames}帧/{expected:.6f}s，实际{output}")
    return output


def _write_srt(path: Path, entries: list[dict]) -> None:
    path.write_text("\n\n".join(f"{i}\n{_srt_time(item['start'])} --> {_srt_time(item['end'])}\n{item['text']}"
                               for i, item in enumerate(entries, 1)) + "\n", encoding="utf-8")


def _export_result(root: Path, directory: Path, filename: str, metadata: dict) -> dict:
    qc_path = directory / "qc.json"
    qc_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result = {"path": str((directory / filename).relative_to(root)),
            "duration": metadata["output"]["duration"],
            "subtitles_path": str((directory / "subtitles.srt").relative_to(root)),
            "qc_path": str(qc_path.relative_to(root))}
    if (directory / "source_captions.srt").is_file():
        result["source_captions_path"] = str((directory / "source_captions.srt").relative_to(root))
    return result


def remove_render_intermediates(directory: Path) -> dict:
    """Remove only this completed export's rebuildable encoded shot files."""
    directory = Path(directory).resolve()
    qc_path, master = directory / "qc.json", directory / "episode.mp4"
    metadata = json.loads(qc_path.read_text(encoding="utf-8"))
    count = metadata.get("shot_count")
    if (metadata.get("status") != "complete" or type(count) is not int or count < 1
            or len(metadata.get("shots", [])) != count or not master.is_file()
            or master.stat().st_size != metadata.get("output", {}).get("bytes")
            or not (directory / "subtitles.srt").is_file()
            or not (directory / "source_captions.srt").is_file()):
        raise ValueError("成片、字幕及完整QC尚未核实，保留全部编码中间件")
    removed = list(metadata.get("intermediates_removed", []))
    reclaimed = metadata.get("intermediates_removed_bytes", 0)
    errors = []
    for index in range(1, count + 1):
        path = directory / f"shot_{index:03}.mp4"
        if not path.is_file() or path.is_symlink():
            continue
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError as exc:
            errors.append({"path": path.name, "error": str(exc)})
        else:
            if path.name not in removed:
                removed.append(path.name)
                reclaimed += size
    metadata.update(intermediates_removed=removed, intermediates_removed_bytes=reclaimed,
                    intermediates_cleanup_at=_stamp())
    if errors:
        metadata["intermediates_cleanup_errors"] = errors
    temporary = qc_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(qc_path)
    return {"intermediates_removed": removed, "intermediates_removed_bytes": reclaimed, "errors": errors}


def _cached_episode(root: Path, episode: dict, signature: str) -> dict | None:
    exports = root / "exports" / episode["id"]
    for qc_path in sorted(exports.glob("*/qc.json"), reverse=True):
        directory = qc_path.parent.resolve()
        if not directory.is_relative_to(exports.resolve()):
            continue
        try:
            qc = json.loads(qc_path.read_text(encoding="utf-8"))
            if (qc.get("status") != "complete" or qc.get("selection_signature") != signature
                    or qc.get("shot_count") != len(episode["shots"])):
                continue
            master = directory / "episode.mp4"
            if not master.is_file() or not all((directory / name).is_file() for name in ("subtitles.srt", "source_captions.srt")):
                continue
            actual, expected = probe_video(master), qc["output"]
            if ((actual["width"], actual["height"]) != (WIDTH, HEIGHT) or abs(actual["fps"] - FPS) > 0.001
                    or not actual["has_audio"] or actual["frames"] != expected["frames"]
                    or actual["bytes"] != expected["bytes"] or abs(actual["duration"] - expected["duration"]) > 0.002):
                continue
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            # Incomplete or invalid previous exports do not block a fresh render.
            continue
        return {"path": str(master.relative_to(root)), "duration": actual["duration"],
                "subtitles_path": str((directory / "subtitles.srt").relative_to(root)),
                "source_captions_path": str((directory / "source_captions.srt").relative_to(root)),
                "qc_path": str(qc_path.relative_to(root)), "cache_reused": True}
    return None


def render_episode(project_root: Path, episode: dict) -> dict:
    root = Path(project_root).resolve()
    signature = _selection_signature(root, episode)
    shots = episode.get("shots", [])
    if not shots:
        raise ValueError("本集没有分镜，不能导出成片")
    episode_id = str(episode.get("id", "EP001"))
    if not re.fullmatch(r"[A-Za-z0-9_-]+", episode_id):
        raise ValueError("集号只能包含字母、数字、下划线或连字符")
    cached = _cached_episode(root, {**episode, "id": episode_id}, signature)
    if cached:
        return cached
    # Resolve and decode every selection before creating an export version.
    inputs = [(shot, *_selected(root, shot)) for shot in shots]
    inspected = [(shot, asset, path, probe_video(path)) for shot, asset, path in inputs]
    directory = root / "exports" / episode_id / _stamp()
    directory.mkdir(parents=True, exist_ok=False)
    font = _font()
    subtitles, source_captions, clips, records, offset = [], [], [], [], 0.0
    for index, (shot, asset, source, source_info) in enumerate(inspected, 1):
        clip = directory / f"shot_{index:03}.mp4"
        entries = _subtitle_entries(shot, source_info["duration"])
        filters = [f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease:force_divisible_by=2",
                   f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black",
                   "setpts=PTS-STARTPTS", f"fps={FPS}:round=up:eof_action=pass", "setsar=1"]
        if index == 1:
            title = directory / "title.txt"
            episode_title = str(episode.get("title", episode_id))
            source_title = str(episode.get("source_title", "")).strip()
            if source_title and source_title != episode_title:
                episode_title = source_title + "｜" + episode_title
            title.write_text(_wrap(episode_title, font, 30, 630), encoding="utf-8")
            filters.append(_drawtext(title, font, size=30, y="100", start=0, end=min(2.5, source_info["duration"])))
        captions = _source_captions(shot, source_info["duration"], font)
        for number, caption in enumerate(captions, 1):
            text_path = directory / f"shot_{index:03}_source_caption_{number:02}.txt"
            text_path.write_text(caption["wrapped"], encoding="utf-8")
            filters.append(_drawtext(text_path, font, size=28, y=str(caption["y"]),
                                     start=caption["start"], end=caption["end"]))
            source_captions.append({**caption, "shot_id": shot.get("id"),
                                    "start": offset + caption["start"], "end": offset + caption["end"]})
        for number, entry in enumerate(entries, 1):
            text_path = directory / f"shot_{index:03}_subtitle_{number:02}.txt"
            text_path.write_text(_wrap(entry["text"], font, 30, 620), encoding="utf-8")
            filters.append(_drawtext(text_path, font, size=30, y="h-text_h-155", start=entry["start"], end=entry["end"]))
            subtitles.append({**entry, "start": offset + entry["start"], "end": offset + entry["end"]})
        arguments = ["-y", "-i", str(source)]
        if not source_info["has_audio"]:
            arguments += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        arguments += ["-map", "0:v:0", "-map", "0:a:0" if source_info["has_audio"] else "1:a:0",
                      "-vf", ",".join(filters), "-af", "apad", "-t", str(source_info["duration"]),
                      "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                      "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
                      "-movflags", "+faststart", str(clip)]
        _run(arguments)
        info = probe_video(clip)
        if (info["width"], info["height"], round(info["fps"])) != (WIDTH, HEIGHT, FPS) or not info["has_audio"]:
            raise RuntimeError(f"镜头 {shot.get('id', index)} 规格验证失败")
        records.append({"shot_id": shot.get("id", str(index)), "selected_asset": asset.get("id"),
                        "source": str(source.relative_to(root)), "source_info": source_info,
                        "output_info": info, "start": round(offset, 6),
                        "source_captions": captions,
                        "audio": "native_preserved" if source_info["has_audio"] else "silence_added"})
        offset += info["duration"]
        clips.append(clip)
    destination = directory / "episode.mp4"
    output = _concat(clips, destination)
    if abs(output["duration"] - offset) > 0.002:
        raise RuntimeError("合并后的实际时长与镜头总时长不符")
    _write_srt(directory / "subtitles.srt", subtitles)
    _write_srt(directory / "source_captions.srt", source_captions)
    result = _export_result(root, directory, "episode.mp4", {
        "episode_id": episode_id, "status": "complete", "created_at": _stamp(), "shots": records,
        "selection_signature": signature,
        "shot_count": len(clips), "output": output,
        "source_caption_count": len(source_captions), "source_captions": source_captions,
        "subtitle_alignment": "Script dialogue distributed within each shot by character count; not ASR aligned.",
        "visual_review": "Manual review required for faces, action continuity, and script/audio correspondence.",
        "transform": "Aspect ratio preserved with letterbox; no time stretching.",
        "render_version": 3, "concat_timing": "Each audio trimmed/padded to video frame count; one final AAC encode.",
    })
    remove_render_intermediates(directory)
    return result


def _parse_srt_time(text: str) -> float:
    hours, minutes, seconds, milliseconds = map(int, re.split(r"[:,]", text))
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def _read_srt(path: Path, offset: float) -> list[dict]:
    entries = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip()):
        lines = block.splitlines()
        if len(lines) >= 3 and " --> " in lines[1]:
            start, end = lines[1].split(" --> ")
            entries.append({"start": offset + _parse_srt_time(start),
                            "end": offset + _parse_srt_time(end), "text": "\n".join(lines[2:])})
    return entries


def _chapters(destination: Path, episodes: list[dict]) -> None:
    metadata = destination.parent / "chapters.ffmetadata"
    lines = [";FFMETADATA1"]
    for episode in episodes:
        title = str(episode["title"])
        for character in ("\\", "=", ";", "#"):
            title = title.replace(character, "\\" + character)
        title = title.replace("\n", " ")
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={round(episode['start'] * 1000)}",
                  f"END={round((episode['start'] + episode['duration']) * 1000)}", "title=" + title]
    metadata.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary = destination.with_name("series_with_chapters.mp4")
    _run(["-y", "-i", str(destination), "-i", str(metadata), "-map", "0", "-map_metadata", "1",
          "-map_chapters", "1", "-c", "copy", "-movflags", "+faststart", str(temporary)])
    temporary.replace(destination)


def render_series(project_root: Path, episodes: list[dict]) -> dict:
    root = Path(project_root).resolve()
    if not episodes:
        raise ValueError("没有剧集，不能导出全剧")
    paths, records, subtitles, source_captions, offset = [], [], [], [], 0.0
    for episode in episodes:
        signature = _selection_signature(root, episode)
        previous = episode.get("export") or episode.get("render")
        reusable = bool(previous and previous.get("path") and previous.get("qc_path")
                        and _inside(root, previous["path"]).is_file()
                        and _inside(root, previous["qc_path"]).is_file())
        if reusable:
            qc = json.loads(_inside(root, previous["qc_path"]).read_text(encoding="utf-8"))
            reusable = qc.get("selection_signature") == signature
        if not reusable:
            previous = render_episode(root, episode)
        path = _inside(root, previous["path"])
        info = probe_video(path)
        if (info["width"], info["height"], round(info["fps"])) != (WIDTH, HEIGHT, FPS) or not info["has_audio"]:
            raise ValueError(f"{episode.get('id')} 成片规格不符，请重新导出该集")
        subtitle_path = _inside(root, previous["subtitles_path"])
        subtitles.extend(_read_srt(subtitle_path, offset))
        if previous.get("source_captions_path"):
            source_captions.extend(_read_srt(_inside(root, previous["source_captions_path"]), offset))
        records.append({"episode_id": episode.get("id"), "title": episode.get("title", episode.get("id")),
                        "path": str(path.relative_to(root)),
                        "start": round(offset, 6), "duration": info["duration"]})
        paths.append(path)
        offset += info["duration"]
    directory = root / "exports" / "series" / _stamp()
    directory.mkdir(parents=True, exist_ok=False)
    destination = directory / "series.mp4"
    _concat(paths, destination)
    _chapters(destination, records)
    output = probe_video(destination)
    if abs(output["duration"] - offset) > 0.002 or abs(output["fps"] - FPS) > 0.001:
        raise RuntimeError("全剧合并时长校验失败")
    _write_srt(directory / "subtitles.srt", subtitles)
    _write_srt(directory / "source_captions.srt", source_captions)
    return _export_result(root, directory, "series.mp4", {"status": "complete", "created_at": _stamp(),
        "episode_count": len(episodes), "episodes": records, "output": output,
        "source_caption_count": len(source_captions),
        "subtitle_alignment": "Script timeline; not ASR aligned."})
