"""Production orchestrator over the existing atomic image/LLM/video tools."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import json
import os
import shutil
from pathlib import Path
import re
import threading
import traceback
import uuid

from ai.image.client import generate_image, extract_image_urls, download_image
from ai.video import DashScopeVideoClient, build_reference_video_payload, download_video
from core.store import Store, find, selected_asset, timestamp, now, write_json, media_url, safe_path, fingerprint
from backend.workflow.prompts import compile_video, identity_for_look


def selection_key(kind, look_id=None):
    return f"turnaround:{look_id}" if kind == "turnaround" else look_id if kind == "look" else kind


class Pipeline:
    def __init__(self, store: Store):
        self.store = store
        self.video_concurrency = max(1, min(12, int(os.environ.get("DRAMA_VIDEO_CONCURRENCY", "8"))))
        self.executor = ThreadPoolExecutor(max_workers=self.video_concurrency + 4, thread_name_prefix="studio")
        self.pause_event = threading.Event()
        self.guard = threading.RLock()
        self.active = {}
        self.planning_locks = {}
        self.review_locks = {}
        self.run_thread = None
        # A killed synchronous POST can have succeeded remotely. Never retry it.
        def recover(state):
            for job in state["jobs"]:
                if job["status"] in {"running", "queued"}:
                    job.update(status="needs_review", error="本地服务中断；已保留资产和远端task_id。续跑会查询已有视频；同步图像需核实调用记录。", updated_at=now())
            if state["project"].get("run_status") == "running":
                state["project"]["run_status"] = "paused"
        self.store.mutate(recover)

    def _new_job(self, kind, target):
        job_id = uuid.uuid4().hex
        job = dict(id=job_id, kind=kind, target=target, status="queued", created_at=now(), updated_at=now(), error=None)
        self.store.mutate(lambda state: state["jobs"].append(job))
        return job_id

    def _check_space(self, minimum=1024 ** 3):
        free = shutil.disk_usage(self.store.root).free
        if free < minimum:
            raise RuntimeError(f"输出盘剩余{free / 1024**3:.2f}GB，低于本阶段所需{minimum / 1024**3:.2f}GB；已停止派发新生成，保留全部已完成资产。请释放空间后续跑。")

    def _update_job(self, job_id, **fields):
        def update(state):
            find(state["jobs"], job_id).update(**fields, updated_at=now())
        self.store.mutate(update)

    def _perform(self, kind, target, fn):
        job_id = self._new_job(kind, target)
        return self._execute(job_id, fn)

    def _execute(self, job_id, fn):
        self._update_job(job_id, status="running")
        try:
            result = fn()
            self._update_job(job_id, status="succeeded")
            return result
        except Exception as exc:
            error = str(exc)
            key = os.environ.get("DASHSCOPE_API_KEY", "")
            if key:
                error = error.replace(key, "[REDACTED]")
            self._update_job(job_id, status="failed", error=error)
            raise

    def enqueue(self, kind, target, fn):
        token = (kind, str(target))
        with self.guard:
            old = self.active.get(token)
            if old and not old[1].done():
                return old[0]
            job_id = self._new_job(kind, target)
            future = self.executor.submit(self._execute, job_id, fn)
            self.active[token] = (job_id, future)
            return job_id

    def plan(self, episode_id=None):
        from backend.workflow.planner import plan_episode
        if not episode_id:
            todo = iter(ep["id"] for ep in self.store.read()["episodes"] if not ep["shots"])
            with ThreadPoolExecutor(max_workers=3, thread_name_prefix="director") as pool:
                futures = {}
                for _ in range(3):
                    ep_id = next(todo, None)
                    if ep_id:
                        futures[pool.submit(self._perform, "plan", ep_id, lambda eid=ep_id: self.plan(eid))] = ep_id
                while futures:
                    completed = next(as_completed(futures))
                    try:
                        completed.result()
                    except Exception:
                        for future in futures:
                            future.cancel()
                        raise
                    futures.pop(completed)
                    ep_id = next(todo, None) if not self.pause_event.is_set() else None
                    if ep_id:
                        futures[pool.submit(self._perform, "plan", ep_id, lambda eid=ep_id: self.plan(eid))] = ep_id
            return {"ok": True}
        with self.guard:
            episode_lock = self.planning_locks.setdefault(episode_id, threading.Lock())
        with episode_lock:
            return self._plan_one(episode_id, plan_episode)

    def _plan_one(self, episode_id, plan_episode):
        state = self.store.read()
        episodes = [find(state["episodes"], episode_id)]
        for episode in episodes:
            if self.pause_event.is_set():
                break
            if episode["shots"]:
                continue
            planned = plan_episode(self.store.root, state, episode)
            def update(current):
                live = find(current["episodes"], episode["id"])
                # Keep manual generations/selections if another request already planned it.
                if not live["shots"]:
                    live.update(planned, status="planned")
                    for shot in live["shots"]:
                        shot.setdefault("assets", [])
                        shot.setdefault("selected", None)
                    write_json(self.store.root / "episodes" / live["id"] / "storyboard.json", live)
            self.store.mutate(update)
        return {"ok": True}

    def review(self, episode_id=None):
        from backend.workflow.continuity import review_episode, _shot
        if not episode_id:
            targets = [e["id"] for e in self.store.read()["episodes"] if e["shots"]]
            with ThreadPoolExecutor(max_workers=3, thread_name_prefix="continuity") as pool:
                futures = [pool.submit(self._perform, "continuity", eid, lambda eid=eid: self.review(eid)) for eid in targets]
                for future in as_completed(futures):
                    future.result()
            return {"ok": True}
        with self.guard:
            review_lock = self.review_locks.setdefault(episode_id, threading.Lock())
        with review_lock:
            state = self.store.read()
            episode = find(state["episodes"], episode_id)
            original = fingerprint([_shot(s) for s in episode["shots"]])
            result = review_episode(self.store.root, state, episode)
            def update(current):
                live = find(current["episodes"], episode_id)
                if fingerprint([_shot(s) for s in live["shots"]]) != original:
                    raise ValueError("复核期间分镜被修改；未覆盖新编辑，可重新复核并复用已成功批次")
                for patch in result["patches"]:
                    shot = find(live["shots"], patch["shot_id"])
                    shot.update(patch["changes"])
                    for asset in shot["assets"]:
                        asset["stale"] = True
                    live["export"] = None
                if result["patches"]:
                    for exported in current["exports"]:
                        if exported.get("episode_id") in {None, episode_id}:
                            exported["stale"] = True
                live["continuity_review"] = result["review"]
                write_json(self.store.root / "episodes" / episode_id / "storyboard.json", live)
            self.store.mutate(update)
            return {"ok": True, "patch_count": len(result["patches"])}

    def _owner(self, state, request):
        if request.get("character_id"):
            return find(state["characters"], request["character_id"]), "characters"
        return find(state["locations"], request["location_id"]), "scenes"

    def image(self, request):
        self._check_space()
        state = self.store.read()
        owner, group = self._owner(state, request)
        kind = request["kind"]
        if kind not in {"base", "look", "turnaround", "location"}:
            raise ValueError("未知图片类型")
        look_id = request.get("look_id")
        look = find(owner.get("looks", []), look_id) if kind in {"look", "turnaround"} else None
        retry_download = next((a for a in reversed(owner["assets"]) if a["kind"] == kind and a.get("look_id") == look_id
            and a["status"] == "download_failed" and not a.get("stale")), None)
        if retry_download and not request.get("force_new"):
            folder = self.store.root / Path(retry_download["path"]).parent
            response = json.loads((folder / "response.json").read_text(encoding="utf-8"))
            download_image(extract_image_urls(response)[0], self.store.root / retry_download["path"])
            return self._finish_image_download(request, retry_download, owner, response.get("usage", {}))
        references, parents = [], []
        # Disease treatment belongs to a shot, never to a neutral identity sheet.
        style = state["project"]["visual_style"].split("疾病场面", 1)[0]
        if kind == "base":
            prompt = f"人物基础身份母版，单独一位{owner['name']}。{owner['base_prompt']}\n这是一张健康时期的演员定妆照，皮肤健康完整，无伤口、无红点、无瘀斑、无病情。纯浅灰摄影棚背景，自然柔光，必须从头顶到两只鞋底完整全身入画，鞋底下面保留15%空白地面，头顶上方保留10%背景空间，人物仅占画面高度75%。相机后退，不得裁去腿或脚。三分之二正面站姿，脸清晰可辨，单人单一视图。穿无图案中性灰色短袖、深色长裤和白鞋，不带剧情服装，不持道具，无文字，无其他人。保留真实年龄与独特五官，不美颜过度。{style}"
        elif kind == "look":
            base = selected_asset(owner, "base")
            if not base:
                raise ValueError("请先采用满意的人物基础图")
            references.append(str(safe_path(self.store.root, base["path"])))
            parents.append(base["id"])
            prompt = f"图1是{owner['name']}唯一基础身份参考。严格保留同一个人的五官骨骼、脸型、发际线与身材身份。若当前形象明确指定新年龄，则在同一身份基础上自然增龄或减龄；未指定新年龄时保持基础年龄，只改变服装和剧情状态。当前完整身份锁：{identity_for_look(owner, look)}。当前形象：{look['name']}；{look.get('prompt_delta','')}。如有图2/图3衣服参考只继承服装材质剪裁配色，不继承模特脸、姿势或背景。单人头顶至鞋底完整全身，三分之二正面，自然站立，纯浅灰棚拍背景，面部细节清楚。不做多视图，不生成拼贴，无文字。{style}"
        elif kind == "turnaround":
            master = selected_asset(owner, look_id)
            if not master:
                raise ValueError("请先采用满意的当前形象图，再生成三视图")
            references.append(str(safe_path(self.store.root, master["path"])))
            parents.append(master["id"])
            prompt = f"为图1同一位{owner['name']}制作严格一致的角色三视图设定页。横向三栏：正面全身、人物左侧全身、背面全身，同一身高同一比例、同一服装每处细节，无姿势透视夸张，脸型/发型/配饰与图1完全一致。当前身份锁：{identity_for_look(owner, look)}。当前形象锁：{look.get('prompt_delta','')}。自然中性站立，浅灰背景，脚底对齐，三栏不加文字标签、不加其他角色、无水印，真实摄影。"
        else:
            # Identity photography cues can cause people to appear in an empty set.
            scene_style = "、".join(part.strip() for part in re.split(r"[，,；;。\n、]+", style)
                if part.strip() and not any(word in part for word in (
                    "肤质", "皮肤", "人物", "表情", "情绪", "视线", "年龄", "五官", "动作", "汗水", "恐惧",
                    "白天", "夜晚", "35mm", "50mm", "警报", "运动", "景深")))
            prompt = f"拍摄一张{owner['name']}的真实实景照片。{owner['canonical_prompt']}。{scene_style}。一台35mm摄影机、一个固定机位、一次曝光，整张竖幅画布从顶部到底部呈现同一个连续空间的完整透视。只有这一个光照状态，真实材质、清楚的空间纵深与自然曝光。固定门窗、家具、墙体与光源的相对位置。空间完全空置，床、椅、门后与玻璃反射中均无人、无人脸或局部肢体，墙面不放人物照片。整张图片是单一摄影照片，不能是拼贴、分屏或多视图设计页，无可辨文字。"
        if request.get("prompt"):
            prompt += "\n用户本次要求：" + request["prompt"]
        for asset_id in request.get("reference_asset_ids", []):
            a = find(owner["assets"], asset_id)
            references.append(str(safe_path(self.store.root, a["path"])))
            parents.append(asset_id)
        for rel in request.get("reference_paths", []):
            path = safe_path(self.store.root, rel)
            if not path.is_relative_to(self.store.root / "uploads"):
                raise ValueError("额外参考路径必须来自上传目录")
            references.append(str(path))
        if len(references) > 3:
            raise ValueError("图像工具最多3张参考图；基础图加最多2张服装参考")
        version = 1 + sum(a["kind"] == kind and a.get("look_id") == look_id for a in owner["assets"])
        asset_id = uuid.uuid4().hex
        folder = Path("assets") / group / owner["id"] / (look_id or kind) / f"v{version:04d}_{timestamp()}"
        full = self.store.root / folder
        full.mkdir(parents=True, exist_ok=True)
        metadata = dict(id=asset_id, kind=kind, look_id=look_id, status="submitting", path=str(folder / "image.png"),
            url=media_url(folder / "image.png"), prompt=prompt, version=version, created_at=now(), parent_asset_ids=parents,
            prompt_hash=fingerprint(prompt), references=[str(Path(p).relative_to(self.store.root)) for p in references])
        self.store.mutate(lambda current: self._owner(current, request)[0]["assets"].append(metadata))
        write_json(full / "request.json", {"model": "qwen-image-3.0-pro", "prompt": prompt, "references": metadata["references"]})
        try:
            result = generate_image(prompt, images=references, size="1800x1024" if kind == "turnaround" else "1024x1536", seed=int(asset_id[:7], 16), prompt_extend=False, enable_thinking=False)
            write_json(full / "response.json", result)  # Persist before downloading.
            self._asset_update(request, asset_id, status="downloading")
            download_image(extract_image_urls(result)[0], full / "image.png")
        except Exception as exc:
            status = "download_failed" if (full / "response.json").exists() else "needs_review" if "网络" in str(exc) or "超时" in str(exc) else "failed"
            self._asset_update(request, asset_id, status=status, error=str(exc))
            raise
        # Keep QA outside the provider/download exception handler: an unavailable
        # reviewer must never be mislabeled download_failed and trigger recovery.
        return self._finish_image_download(request, metadata, owner, result.get("usage", {}))

    def _finish_image_download(self, request, asset, owner, usage):
        if request["kind"] == "location":
            from backend.workflow.qa import inspect_scene_image
            qa = inspect_scene_image(self.store.root, asset, owner)
            passed = qa.get("passed")
            status = "ready" if passed is True else "rejected" if passed is False else "needs_review"
            error = None if passed is True else (qa.get("visual") or {}).get("reason") or qa.get("visual_error") or qa.get("technical_error") or "场景图片质检结果未知"
            fields = dict(status=status, error=error, usage=usage, qa=qa)
            self._asset_update(request, asset["id"], **fields)
            if passed is not True:
                raise ValueError(f"场景图片质检{('未通过' if passed is False else '结果未知，需复核')}：{error}")
            return asset | fields
        try:
            from PIL import Image
            with Image.open(safe_path(self.store.root, asset["path"])) as image:
                image.verify()
        except Exception as exc:
            self._asset_update(request, asset["id"], status="download_failed", error=str(exc))
            raise
        fields = dict(status="ready", error=None, usage=usage)
        self._asset_update(request, asset["id"], **fields)
        return asset | fields

    def _asset_update(self, request, asset_id, **fields):
        def update(state):
            find(self._owner(state, request)[0]["assets"], asset_id).update(**fields)
        self.store.mutate(update)

    def _invalidate(self, state, character_id=None, location_id=None, look_id=None):
        affected_episodes = set()
        for ep in state["episodes"]:
            affected = False
            for shot in ep["shots"]:
                character_matches = character_id and any(
                    c["id"] == character_id and (look_id is None or c.get("look_id") == look_id)
                    for c in shot["characters"])
                if character_matches or (location_id is not None and shot["location_id"] == location_id):
                    for a in shot["assets"]:
                        a["stale"] = True
                    affected = True
            if affected:
                affected_episodes.add(ep["id"])
                ep["export"] = None
                ep["status"] = "planned"
        for exported in state["exports"]:
            if affected_episodes and (exported.get("episode_id") is None or exported.get("episode_id") in affected_episodes):
                exported["stale"] = True

    def select(self, request):
        def update(state):
            owner, _ = self._owner(state, request)
            a = find(owner["assets"], request["asset_id"])
            if a["status"] != "ready" or a.get("stale"):
                raise ValueError("只能采用成功且依赖未过期的资产")
            kind, look_id = request["kind"], request.get("look_id")
            if a["kind"] != kind or a.get("look_id") != look_id:
                raise ValueError("资产类型与目标形象不匹配")
            if kind == "location" and "qa" in a and (not isinstance(a["qa"], dict) or a["qa"].get("passed") is not True):
                raise ValueError("场景图片必须通过无人单空间质检后才能采用")
            key = selection_key(kind, look_id)
            if owner["selected"].get(key) == a["id"]:
                return
            old = owner["selected"].get(key)
            owner["selected"][key] = a["id"]
            if old:
                invalid = {old}
                changed = True
                while changed:
                    changed = False
                    for derived in owner["assets"]:
                        # A valid edited candidate may use the previous master
                        # as its reference. Adopting it must not stale itself.
                        if derived["id"] != a["id"] and derived["id"] not in invalid and invalid.intersection(derived.get("parent_asset_ids", [])):
                            derived["stale"] = True
                            invalid.add(derived["id"])
                            changed = True
                # Video references the single-person look master, never its grid.
                if kind != "turnaround":
                    self._invalidate(state, request.get("character_id"), request.get("location_id"),
                                     look_id=look_id if kind == "look" else None)
        self.store.mutate(update)
        return {"ok": True}

    def edit_prompt(self, request):
        prompt = request["prompt"].strip()
        if not prompt or len(prompt) > 5000:
            raise ValueError("提示词需为1–5000字符")
        def update(state):
            if request.get("episode_id"):
                ep = find(state["episodes"], request["episode_id"])
                shot = find(ep["shots"], request["shot_id"])
                if shot.get("prompt", "") == prompt:
                    return
                shot["prompt"] = prompt
                for a in shot["assets"]:
                    a["stale"] = True
                ep["export"] = None
                for exported in state["exports"]:
                    if exported.get("episode_id") in {None, ep["id"]}:
                        exported["stale"] = True
            else:
                owner, _ = self._owner(state, request)
                if request.get("look_id"):
                    look = find(owner["looks"], request["look_id"])
                    if look.get("prompt_delta", "") == prompt:
                        return
                    look["prompt_delta"] = prompt
                    kinds = {request["look_id"], "turnaround:" + request["look_id"]}
                    for a in owner["assets"]:
                        if a.get("look_id") == request["look_id"]:
                            a["stale"] = True
                elif request.get("character_id"):
                    if owner["base_prompt"] == prompt:
                        return
                    owner["base_prompt"] = prompt
                    kinds = set(owner["selected"])
                    for a in owner["assets"]:
                        a["stale"] = True
                else:
                    if owner["canonical_prompt"] == prompt:
                        return
                    owner["canonical_prompt"] = prompt
                    kinds = set(owner["selected"])
                    for a in owner["assets"]:
                        a["stale"] = True
                for key in kinds:
                    old = owner["selected"].get(key)
                    if old:
                        find(owner["assets"], old)["stale"] = True
                self._invalidate(state, request.get("character_id"), request.get("location_id"),
                                 look_id=request.get("look_id") if request.get("character_id") else None)
        self.store.mutate(update)
        return {"ok": True}

    def add_character(self, request):
        allowed = {"name", "base_prompt", "aliases", "episode_id", "look"}
        if not isinstance(request, dict) or set(request) - allowed:
            raise ValueError("人物仅接受名称、基础身份、别名、剧集和可选首形象")

        def text(value, label, maximum):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError(f"{label}需要1–{maximum}字符文本")
            return value.strip()

        name = text(request.get("name"), "人物名称", 100)
        base_prompt = text(request.get("base_prompt"), "基础身份描述", 5000)
        aliases = request.get("aliases", [])
        if not isinstance(aliases, list) or len(aliases) > 30:
            raise ValueError("人物别名需为最多30项短文本列表")
        aliases = list(dict.fromkeys(text(alias, "人物别名", 100) for alias in aliases))
        episode_id = text(request["episode_id"], "剧集ID", 100) if "episode_id" in request else None
        looks = []
        if "look" in request:
            initial = request["look"]
            if not isinstance(initial, dict) or set(initial) != {"name", "prompt_delta"}:
                raise ValueError("首形象必须仅含name和prompt_delta")
            looks.append(dict(id="look_" + uuid.uuid4().hex[:12],
                name=text(initial["name"], "形象名称", 100),
                prompt_delta=text(initial["prompt_delta"], "形象服装描述", 1500),
                continuity="同一独立人物身份；当前服装及状态按镜头显式绑定，不替换其他人物身份。", created_at=now()))
        character = dict(id="char_" + uuid.uuid4().hex[:12], name=name, aliases=aliases,
            base_prompt=base_prompt, looks=looks, assets=[], selected={}, is_ensemble=False,
            identity_invariants="独立人物身份；固定脸型、五官、发际线和体态，不与其他人物或群演池共用基础图。年龄、性别和体态身份写入基础描述，服装及剧情状态由形象层维护。",
            created_at=now())
        def update(state):
            # Validate the optional episode before mutating either collection.
            episode = find(state["episodes"], episode_id) if episode_id is not None else None
            state["characters"].append(character)
            if episode is not None:
                episode.setdefault("characters", []).append(character["id"])
        self.store.mutate(update)
        return {"ok": True, "character": character}

    def add_look(self, request):
        name, delta = request["name"].strip(), request["prompt_delta"].strip()
        if not name or not delta or len(delta) > 1500:
            raise ValueError("需要形象名称和1–1500字描述")
        look = dict(id="look_" + uuid.uuid4().hex[:12], name=name, prompt_delta=delta,
                    continuity="同一人物身份；按剧集/镜头显式绑定，不替换其他历史形象", created_at=now())
        self.store.mutate(lambda state: find(state["characters"], request["character_id"])["looks"].append(look))
        return {"ok": True, "look": look}

    def add_location(self, request):
        name = str(request.get("name", "")).strip()
        prompt = str(request.get("canonical_prompt", "")).strip()
        aliases = request.get("aliases", [])
        if not name or len(name) > 100 or not prompt or len(prompt) > 5000:
            raise ValueError("需要1–100字场景名称和1–5000字固定空间描述")
        if not isinstance(aliases, list) or any(not isinstance(a, str) or not a.strip() or len(a) > 100 for a in aliases):
            raise ValueError("场景别名需为非空短文本列表")
        location = dict(id="loc_" + uuid.uuid4().hex[:12], name=name,
                        aliases=list(dict.fromkeys(a.strip() for a in aliases)), canonical_prompt=prompt,
                        continuity="同一单一空间母版；固定建筑布局、门窗、材质和主光方向，不混合多个住宅或房间。",
                        assets=[], selected={}, created_at=now())
        def update(state):
            episode = find(state["episodes"], request["episode_id"]) if request.get("episode_id") else None
            state["locations"].append(location)
            if episode is not None:
                episode.setdefault("locations", []).append(location["id"])
        self.store.mutate(update)
        return {"ok": True, "location": location}

    def bind_look(self, request):
        def update(state):
            character = find(state["characters"], request["character_id"])
            find(character["looks"], request["look_id"])
            ep = find(state["episodes"], request["episode_id"])
            shot = find(ep["shots"], request["shot_id"])
            cast = find(shot["characters"], request["character_id"])
            shot["creative_locks"] = sorted(set(shot.get("creative_locks", [])) | {"characters"})
            if cast["look_id"] == request["look_id"]:
                write_json(self.store.root / "episodes" / ep["id"] / "storyboard.json", ep)
                return
            cast["look_id"] = request["look_id"]
            for asset in shot["assets"]:
                asset["stale"] = True
            ep["export"] = None
            for exported in state["exports"]:
                if exported.get("episode_id") in {None, ep["id"]}:
                    exported["stale"] = True
            write_json(self.store.root / "episodes" / ep["id"] / "storyboard.json", ep)
        self.store.mutate(update)
        return {"ok": True}

    def edit_shot(self, request):
        allowed = {"action", "camera", "continuity_in", "continuity_out", "location_id", "characters", "duration", "overlays"}
        changes = request.get("changes", {})
        if not isinstance(changes, dict) or not changes or set(changes) - allowed:
            raise ValueError("仅能编辑动作、机位、入出状态、场景、形象绑定、时长和源字幕")
        lock_creative = request.get("lock_creative", True)
        if type(lock_creative) is not bool:
            raise ValueError("lock_creative必须为布尔值")
        def update(state):
            ep = find(state["episodes"], request["episode_id"])
            shot = find(ep["shots"], request["shot_id"])
            for key in ["action", "camera", "continuity_in", "continuity_out"]:
                if key in changes and (not isinstance(changes[key], str) or not changes[key].strip() or len(changes[key]) > 2000):
                    raise ValueError(f"{key}需要1–2000字符")
            if "duration" in changes and (type(changes["duration"]) is not int or not 3 <= changes["duration"] <= 15):
                raise ValueError("视频时长必须为3–15整数秒")
            if "location_id" in changes:
                find(state["locations"], changes["location_id"])
            if "characters" in changes:
                if not isinstance(changes["characters"], list) or len(changes["characters"]) > 8:
                    raise ValueError("最多绑定8位主要人物")
                for cast in changes["characters"]:
                    find(find(state["characters"], cast["id"])["looks"], cast["look_id"])
            added_character_ids = list(dict.fromkeys(cast["id"] for cast in changes.get("characters", [])
                                                     if cast["id"] not in ep.get("characters", [])))
            before = {key: shot.get(key) for key in changes}
            locks_before = list(shot.get("creative_locks", []))
            creative = {"action", "camera", "continuity_in", "continuity_out", "location_id", "characters"}
            locks_after = list(dict.fromkeys(locks_before + [key for key in changes if key in creative])) if lock_creative else locks_before
            if before == changes and locks_after == locks_before and not added_character_ids:
                return
            write_json(self.store.root / "episodes" / ep["id"] / "planning/edits" / (timestamp() + ".json"),
                {"shot_id": shot["id"], "reason": request.get("reason", "用户编辑"), "before": before, "after": changes,
                 "creative_locks_before": locks_before, "creative_locks_after": locks_after,
                 "episode_character_ids_added": added_character_ids})
            ep.setdefault("characters", []).extend(added_character_ids)
            if locks_after != locks_before:
                shot["creative_locks"] = locks_after
            if before == changes:
                write_json(self.store.root / "episodes" / ep["id"] / "storyboard.json", ep)
                return
            shot.update(changes)
            if "location_id" in changes and changes["location_id"] not in ep.setdefault("locations", []):
                ep["locations"].append(changes["location_id"])
            previous_location = before.get("location_id")
            if previous_location and previous_location != shot["location_id"] and not any(
                    other["location_id"] == previous_location for other in ep["shots"]):
                ep["locations"] = [value for value in ep["locations"] if value != previous_location]
            if set(changes) != {"overlays"}:
                for asset in shot["assets"]:
                    asset["stale"] = True
            ep["export"] = None
            for exported in state["exports"]:
                if exported.get("episode_id") in {None, ep["id"]}:
                    exported["stale"] = True
            write_json(self.store.root / "episodes" / ep["id"] / "storyboard.json", ep)
        self.store.mutate(update)
        return {"ok": True}

    def insert_reaction(self, request):
        fields = {"episode_id", "before_shot_id", "source_action_indices", "duration", "location_id", "characters",
                  "action", "camera", "continuity_in", "continuity_out"}
        if set(request) != fields:
            raise ValueError("反应镜仅接受指定字段，不允许传入对白、字幕或资产")
        if type(request["duration"]) is not int or not 3 <= request["duration"] <= 15:
            raise ValueError("反应镜时长必须为3–15整数秒")
        for key in ("action", "camera", "continuity_in", "continuity_out"):
            if not isinstance(request[key], str) or not request[key].strip() or len(request[key]) > 2000:
                raise ValueError(f"{key}需要1–2000字符")
        cast = request["characters"]
        if (not isinstance(cast, list) or len(cast) > 8 or any(not isinstance(c, dict)
                or set(c) != {"id", "look_id"} or not isinstance(c["id"], str)
                or not isinstance(c["look_id"], str) for c in cast)):
            raise ValueError("反应镜最多8位人物，每位只含id和look_id")
        if len({c["id"] for c in cast}) != len(cast):
            raise ValueError("反应镜不能重复同一人物身份")
        indices = request["source_action_indices"]
        if (not isinstance(indices, list) or not indices or any(type(i) is not int for i in indices)
                or len(set(indices)) != len(indices)):
            raise ValueError("来源动作索引必须是非空且不重复的整数列表")
        indices = sorted(indices)
        if indices != list(range(len(indices))):
            raise ValueError("插在锚点之前的反应镜只能移入源动作前缀，以保留原作动作总顺序")
        def update(state):
            ep = find(state["episodes"], request["episode_id"])
            anchor = find(ep["shots"], request["before_shot_id"])
            if anchor.get("assets") or anchor.get("selected"):
                raise ValueError("锚点已有资产或选用指针，不能插改已有素材镜")
            actions = anchor.get("source_actions", [])
            if not isinstance(actions, list) or any(not isinstance(line, str) for line in actions) or indices[-1] >= len(actions):
                raise ValueError("来源动作索引无效或原作动作格式不完整")
            find(state["locations"], request["location_id"])
            for character in cast:
                find(find(state["characters"], character["id"])["looks"], character["look_id"])
            relevant = anchor.get("characters", []) + cast
            locations = {anchor["location_id"], request["location_id"]}
            for job in state["jobs"]:
                if job.get("status") not in {"queued", "running"}:
                    continue
                tokens = set(re.findall(r"[A-Za-z0-9_]+", json.dumps(job.get("target"), ensure_ascii=False)))
                if str(job.get("kind", "")).startswith("video") and anchor["id"] in tokens:
                    raise ValueError("锚点存在活跃视频任务，不能修改其结构")
                if str(job.get("kind", "")).startswith("image"):
                    related = bool(locations.intersection(tokens)) or anchor["id"] in tokens
                    for character in relevant:
                        owner = find(state["characters"], character["id"])
                        known = {look["id"] for look in owner["looks"]}
                        if character.get("look_id") in tokens or (character["id"] in tokens and ("base" in tokens or not known.intersection(tokens))):
                            related = True
                    if related:
                        raise ValueError("锚点涉及的形象或场景存在活跃图片任务")
            before_episode = copy.deepcopy(ep)
            existing_ids = {s["id"] for s in ep["shots"]}
            existing_beats = {s.get("beat_id") for s in ep["shots"]}
            number = 1
            while True:
                suffix = f"_R{number:02d}"
                new_id = anchor["id"] + suffix
                beat_id = (anchor.get("beat_id") or anchor["id"]) + suffix
                if new_id not in existing_ids and beat_id not in existing_beats:
                    break
                number += 1
            reaction = {"id": new_id, "beat_id": beat_id, "source_scene": anchor.get("source_scene"),
                "source_actions": copy.deepcopy(actions[:len(indices)]), "dialogue": [], "overlays": [],
                **{key: copy.deepcopy(request[key]) for key in ("duration", "location_id", "characters", "action", "camera", "continuity_in", "continuity_out")},
                "prompt": "", "assets": [], "selected": None,
                "creative_locks": ["action", "camera", "continuity_in", "continuity_out", "location_id", "characters"]}
            anchor["source_actions"] = copy.deepcopy(actions[len(indices):])
            ep["shots"].insert(ep["shots"].index(anchor), reaction)
            if request["location_id"] not in ep.setdefault("locations", []):
                ep["locations"].append(request["location_id"])
            for character in cast:
                if character["id"] not in ep.setdefault("characters", []):
                    ep["characters"].append(character["id"])
            ep.update(export=None, status="planned")
            for exported in state["exports"]:
                if exported.get("episode_id") in {None, ep["id"]}:
                    exported["stale"] = True
            audit = self.store.root / "episodes" / ep["id"] / "planning/edits" / (timestamp() + "_insert_reaction.json")
            write_json(audit, {"operation": "insert_reaction", "created_at": now(), "request": copy.deepcopy(request),
                               "source_action_indices": indices, "inserted_shot_id": new_id,
                               "before_episode": before_episode, "after_episode": copy.deepcopy(ep)})
            write_json(self.store.root / "episodes" / ep["id"] / "storyboard.json", ep)
            return {"ok": True, "shot": reaction, "inserted_before": anchor["id"], "audit_path": str(audit.relative_to(self.store.root))}
        return self.store.mutate(update)

    def _quality_repair_notes(self, asset, shot):
        qa = asset.get("qa", {})
        checks = [qa.get(key) for key in ("technical_pass", "visual_pass", "audio_pass")]
        if asset.get("status") != "rejected" or asset.get("stale") or any(type(value) is not bool for value in checks) or all(checks):
            raise ValueError("只有技术、视觉、ASR均有明确结果的质检失败视频可以自动修复；质检服务不可用时停止")
        if asset.get("repair_attempt", 0) >= 2:
            raise ValueError("本镜头已用完两次自动质量修复，请人工检查失败版本")
        notes = []
        if qa["technical_pass"] is False:
            failed = [key for key, value in qa.get("checks", {}).items() if value is False]
            notes.append(f"技术修复：生成完整可解码的{shot['duration']}秒9:16竖屏视频，保持指定分辨率和原生音轨；失败项：{'、'.join(failed) or '技术规格不合格'}。")
        if qa["visual_pass"] is False:
            visual = qa.get("visual") or {}
            defects = "；".join(str(item) for item in visual.get("defects", [])[:3])
            notes.append("视觉修复：严格遵守编号参考图的人脸、年龄、服装、场景空间及原定动作，不添加人物；上版可见问题："
                         + (defects or str(visual.get("reason", "人物、场景或动作与参考不符"))) + "。")
        if qa["audio_pass"] is False:
            expected = "".join(str(item.get("text", "")) for item in shot.get("dialogue", []))
            notes.append("声音修复：" + ("逐字完整说出既定台词：" + expected + "；不得漏字、改词、增添旁白，各人只说自己的台词。" if expected
                         else "本镜头没有台词，只生成既定环境声和动作声，不得增加人声或旁白。"))
        return "\n".join(notes)

    def video(self, episode_id, shot_id, *, repair_from=None):
        self._check_space()
        state = self.store.read()
        ep = find(state["episodes"], episode_id)
        shot = find(ep["shots"], shot_id)
        pending = next((a for a in reversed(shot["assets"]) if a["status"] in {"pending", "running", "succeeded", "downloading", "download_failed"} and a.get("task_id") and not a.get("stale")), None)
        repair, notes, attempt = None, None, 0
        compiled_shot = shot
        if repair_from and not pending:
            repair = find(shot["assets"], repair_from)
            notes = self._quality_repair_notes(repair, shot)
            attempt = repair.get("repair_attempt", 0) + 1
            compiled_shot = copy.deepcopy(shot)
            compiled_shot["prompt"] = (shot.get("prompt", "") + f"\n质量修复第{attempt}轮：\n" + notes).strip()
        # Validate the complete request; never truncate an identity lock or a line.
        prompt, references, prompt_hash, parents = compile_video(state, ep, compiled_shot)
        if repair:
            parents.append(repair["id"])
            prompt_hash = fingerprint({"compiled": prompt_hash, "repair_parent_asset_id": repair["id"]})
        client = DashScopeVideoClient(request_timeout=60)
        if pending:
            asset = pending
            folder = self.store.root / Path(asset["path"]).parent
        else:
            if any(a["status"] in {"submitting", "needs_review"} for a in shot["assets"]):
                raise ValueError("上次视频提交结果不明确，请先核实控制台，避免重复计费")
            version = len(shot["assets"]) + 1
            folder_rel = Path("episodes") / episode_id / "videos" / shot_id / f"v{version:04d}_{timestamp()}"
            folder = self.store.root / folder_rel
            folder.mkdir(parents=True, exist_ok=True)
            asset = dict(id=uuid.uuid4().hex, kind="video", version=version, created_at=now(), status="submitting", path=str(folder_rel / "clip.mp4"),
                url=media_url(folder_rel / "clip.mp4"), prompt=prompt, prompt_hash=prompt_hash, parent_asset_ids=parents, references=references,
                repair_attempt=attempt, repair_notes=notes, repair_parent_asset_id=repair["id"] if repair else None)
            self._shot_mutate(episode_id, shot_id, lambda s: s["assets"].append(asset))
            write_json(folder / "request.json", {"model": "happyhorse-1.1-r2v", "prompt": prompt, "reference_paths": references,
                "duration": shot["duration"], "resolution": state["project"]["resolution"], "ratio": state["project"]["ratio"],
                "repair_attempt": attempt, "repair_notes": notes, "repair_parent_asset_id": repair["id"] if repair else None})
            payload = build_reference_video_payload(prompt, [self.store.root / r for r in references], duration=shot["duration"],
                resolution=state["project"]["resolution"], ratio=state["project"]["ratio"], seed=int(asset["id"][:7], 16))
            try:
                created = client.submit(payload)
                write_json(folder / "submitted.json", created)
                task_id = created["output"]["task_id"]
                self._video_update(episode_id, shot_id, asset["id"], task_id=task_id, status="pending")
                asset["task_id"] = task_id
            except Exception as exc:
                self._video_update(episode_id, shot_id, asset["id"], status="needs_review" if "Transport" in type(exc).__name__ else "failed", error=str(exc))
                raise
        qa_rejected = False
        try:
            def progress(result):
                write_json(folder / "status.json", result)
                self._video_update(episode_id, shot_id, asset["id"], status=result["output"]["task_status"].lower())
            result = client.wait_for_task(asset["task_id"], timeout=3600, poll_interval=15, on_progress=progress)
            write_json(folder / "response.json", result)
            self._video_update(episode_id, shot_id, asset["id"], status="downloading")
            download_video(result["output"]["video_url"], self.store.root / asset["path"])
            from backend.workflow.qa import build_qa_shot, inspect_video
            qa_shot = build_qa_shot(state, ep, shot)
            qa = inspect_video(self.store.root, asset, qa_shot, asset.get("references", references))
            passed = qa["technical_pass"] and qa["visual_pass"] is True and qa["audio_pass"] is True
            self._video_update(episode_id, shot_id, asset["id"], status="ready" if passed else "rejected", usage=result.get("usage", {}), qa=qa)
            if not passed:
                qa_rejected = True
                reasons = qa.get("visual", {}).get("reason", "") if qa.get("visual") else qa.get("visual_error", "")
                raise ValueError(f"真实视频质检未通过：technical={qa['technical_pass']} visual={qa['visual_pass']} audio={qa['audio_pass']} CER={qa.get('CER')}；{reasons}；{qa.get('audio_error','')}")
        except Exception as exc:
            status = "rejected" if qa_rejected else "pending" if "Timeout" in type(exc).__name__ else "download_failed" if (folder / "response.json").exists() else "failed"
            self._video_update(episode_id, shot_id, asset["id"], status=status, error=str(exc))
            raise
        return asset | {"status": "ready"}

    def _shot_mutate(self, episode_id, shot_id, fn):
        def update(state):
            return fn(find(find(state["episodes"], episode_id)["shots"], shot_id))
        return self.store.mutate(update)

    def _video_update(self, episode_id, shot_id, asset_id, **fields):
        self._shot_mutate(episode_id, shot_id, lambda s: find(s["assets"], asset_id).update(**fields))

    def review_video(self, request):
        if request.get("decision") != "approve" or request.get("reviewer") not in {"codex", "user"}:
            raise ValueError("复核需明确approve决定及codex或user操作员")
        notes = request.get("notes")
        if not isinstance(notes, str) or not notes.strip() or len(notes) > 5000:
            raise ValueError("请记录1–5000字的实际复核依据")
        def update(state):
            ep = find(state["episodes"], request["episode_id"])
            shot = find(ep["shots"], request["shot_id"])
            asset = find(shot["assets"], request["asset_id"])
            if asset.get("status") in {"failed", "canceled", "unknown"}:
                raise ValueError("供应商失败、取消或未知的视频任务不能通过内部复核批准")
            if asset.get("stale") or asset.get("qa", {}).get("technical_pass") is not True:
                raise ValueError("技术质检未通过或依赖已过期的视频不能批准")
            path = safe_path(self.store.root, asset.get("path", ""))
            if not path.is_file() or path.stat().st_size == 0 or path.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm"}:
                raise ValueError("必须先有本地可复核的视频，不能覆盖供应商失败或审核结果")
            parents = {a["id"]: a for owner in state["characters"] + state["locations"] for a in owner.get("assets", [])}
            for parent_id in asset.get("parent_asset_ids", []):
                parent = parents.get(parent_id)
                if parent is not None and (parent.get("stale") or parent.get("status") != "ready"):
                    raise ValueError("视频依赖的角色或场景版本已过期，不能批准")
            asset["manual_review"] = {"decision": "approve", "notes": notes.strip(),
                                      "reviewer": request["reviewer"], "at": now()}
            asset["status"] = "ready"
        self.store.mutate(update)
        return {"ok": True, "asset_id": request["asset_id"]}

    def select_video(self, request):
        def update(state):
            ep = find(state["episodes"], request["episode_id"])
            shot = find(ep["shots"], request["shot_id"])
            asset = find(shot["assets"], request["asset_id"])
            if asset["status"] != "ready" or asset.get("stale"):
                raise ValueError("只能采用成功且依赖未过期的视频")
            shot["selected"] = asset["id"]
            ep["export"] = None
            for export in state["exports"]:
                if export.get("episode_id") in {None, ep["id"]}:
                    export["stale"] = True
        self.store.mutate(update)
        return {"ok": True}

    def render(self, episode_id=None):
        from backend.workflow.render import render_episode, render_series
        state = self.store.read()
        episodes = [find(state["episodes"], episode_id)] if episode_id else state["episodes"]
        for ep in episodes:
            if ep.get("export"):
                continue
            result = render_episode(self.store.root, ep)
            result.update(episode_id=ep["id"], title=ep["source_title"], url=media_url(result["path"]), created_at=now())
            def update(current):
                live = find(current["episodes"], ep["id"])
                live.update(export=result, status="exported")
                current["exports"].append(result)
                write_json(self.store.root / "episodes" / ep["id"] / "manifest.json", live)
            self.store.mutate(update)
        if not episode_id:
            state = self.store.read()
            size = sum((self.store.root / ep["export"]["path"]).stat().st_size for ep in state["episodes"])
            self._check_space(size * 2 + 512 * 1024 ** 2)
            result = render_series(self.store.root, state["episodes"])
            result.update(title="全剧合辑", episode_id=None, url=media_url(result["path"]), created_at=now())
            self.store.mutate(lambda current: current["exports"].append(result))
        return {"ok": True}

    def _ensure_image(self, request):
        state = self.store.read()
        owner, _ = self._owner(state, request)
        key = selection_key(request["kind"], request.get("look_id"))
        if selected_asset(owner, key):
            return
        candidates = [a for a in owner["assets"] if a["kind"] == request["kind"] and a.get("look_id") == request.get("look_id") and not a.get("stale")]
        ready = next((a for a in reversed(candidates) if a["status"] == "ready"), None)
        if not ready and candidates and candidates[-1]["status"] == "download_failed":
            ready = self._perform("image-download", owner["id"] + ":" + key, lambda: self.image(request))
        if not ready and candidates:
            raise ValueError(f"{owner['name']}的{key}上次生成未完成，请先处理失败或核实调用记录，再手动重新生成")
        if not ready:
            ready = self._perform("image", owner["id"] + ":" + key, lambda: self.image(request))
        self.select(request | {"asset_id": ready["id"]})

    def _generate_and_select(self, ep_id, shot_id):
        while True:
            if self.pause_event.is_set():
                return
            current = find(find(self.store.read()["episodes"], ep_id)["shots"], shot_id)
            if selected_asset(current):
                return
            active = [a for a in current["assets"] if not a.get("stale")]
            ready = next((a for a in reversed(active) if a["status"] == "ready"), None)
            if ready:
                self.select_video(dict(episode_id=ep_id, shot_id=shot_id, asset_id=ready["id"]))
                return
            if any(a["status"] in {"needs_review", "submitting"} for a in active):
                raise ValueError(f"{ep_id}/{shot_id} 提交结果不明确，停止自动生成以避免重复计费")
            latest = active[-1] if active else None
            repair_from = None
            if latest and latest["status"] == "rejected":
                self._quality_repair_notes(latest, current)
                repair_from = latest["id"]
            elif latest and latest["status"] == "failed":
                raise ValueError(f"{ep_id}/{shot_id} 需要先处理上次生成错误，再手动重生成")
            try:
                ready = self._perform("video-repair" if repair_from else "video", ep_id + "/" + shot_id,
                                      lambda: self.video(ep_id, shot_id, repair_from=repair_from))
            except Exception:
                updated = find(find(self.store.read()["episodes"], ep_id)["shots"], shot_id)
                latest = next((a for a in reversed(updated["assets"]) if not a.get("stale")), None)
                if not latest or latest["status"] != "rejected":
                    raise
                if repair_from and latest["id"] == repair_from:
                    # Validation failed before creating a new version.
                    raise
                # Unknown model results and exhausted repair chains stop here.
                self._quality_repair_notes(latest, updated)
                continue
            self.select_video(dict(episode_id=ep_id, shot_id=shot_id, asset_id=ready["id"]))
            return

    def run(self):
        """Episode-scoped dependencies; first real clip gates the expensive batch."""
        self.pause_event.clear()
        self.store.mutate(lambda state: state["project"].update(run_status="running", running=True, last_error=None))
        try:
            for ep_seed in self.store.read()["episodes"]:
                if self.pause_event.is_set():
                    break
                ep_id = ep_seed["id"]
                if ep_seed.get("export"):
                    continue
                if not ep_seed["shots"]:
                    self._perform("plan", ep_id, lambda: self.plan(ep_id))
                self._perform("continuity", ep_id, lambda: self.review(ep_id))
                state = self.store.read()
                episode = find(state["episodes"], ep_id)
                gated = any(a["status"] == "ready" and not a.get("stale") for e in state["episodes"] for s in e["shots"] for a in s["assets"])
                futures = []
                for shot in episode["shots"]:
                    if self.pause_event.is_set():
                        return
                    # Gate the large batch on a real first clip, then grow only
                    # the dependencies of each upcoming shot.
                    for cast in shot["characters"]:
                        for kind in ["base", "look", "turnaround"]:
                            if self.pause_event.is_set():
                                return
                            self._ensure_image(dict(character_id=cast["id"], kind=kind,
                                **({"look_id": cast["look_id"]} if kind != "base" else {})))
                    self._ensure_image(dict(location_id=shot["location_id"], kind="location"))
                    dependencies = self.store.read()
                    for cast in shot["characters"]:
                        write_json(self.store.root / "episodes" / ep_id / "characters" / (cast["id"] + ".json"), find(dependencies["characters"], cast["id"]))
                    write_json(self.store.root / "episodes" / ep_id / "scenes" / (shot["location_id"] + ".json"), find(dependencies["locations"], shot["location_id"]))
                    current = find(find(self.store.read()["episodes"], ep_id)["shots"], shot["id"])
                    if selected_asset(current):
                        continue
                    if not gated:
                        self._generate_and_select(ep_id, shot["id"])
                        gated = True
                    else:
                        futures.append(self.executor.submit(self._generate_and_select, ep_id, shot["id"]))
                        if len(futures) >= self.video_concurrency:
                            # Bounded concurrency, and surface a failed generation
                            # before dispatching the next expensive request.
                            completed = next(as_completed(futures))
                            completed.result()
                            futures.remove(completed)
                for future in futures:
                    future.result()
                self._perform("render", ep_id, lambda: self.render(ep_id))
            if not self.pause_event.is_set():
                self._perform("render", "series", lambda: self.render())
                self.store.mutate(lambda state: state["project"].update(run_status="complete", running=False))
        except Exception as exc:
            self.store.mutate(lambda state: state["project"].update(run_status="failed", running=False, last_error=str(exc)))
            raise
        finally:
            if self.pause_event.is_set():
                self.store.mutate(lambda state: state["project"].update(run_status="paused", running=False))

    def start_run(self):
        with self.guard:
            if self.run_thread and self.run_thread.is_alive():
                return {"job_id": "run", "already_running": True}
            production_kinds = {"plan", "continuity", "image", "image-download", "video", "video-repair", "render"}
            draining = [job["id"] for job in self.store.read()["jobs"]
                        if job.get("kind") in production_kinds and job.get("status") in {"queued", "running"}]
            if draining:
                # pause may stop the dispatcher before already-paid workers
                # finish. Do not clear its event or overlap a second run.
                return {"draining": True, "active_job_ids": draining,
                        "message": "先等待已提交的制作任务保存完成，再继续全剧制作。"}
            job_id = self._new_job("run", "series")
            self.run_thread = threading.Thread(target=lambda: self._execute(job_id, self.run), name="production", daemon=True)
            self.run_thread.start()
            return {"job_id": job_id}

    def pause(self):
        self.pause_event.set()
        self.store.mutate(lambda state: state["project"].update(run_status="pausing", running=False))
        return {"ok": True}
