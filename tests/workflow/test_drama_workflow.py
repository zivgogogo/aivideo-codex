"""Offline state, dependency, prompt and QA contracts; no provider calls."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from PIL import Image

from ai.llm.config import LLMConfig
from ai.video import DashScopeAPIError, DashScopeTransportError
from core.store import Store, safe_path, selected_asset
from backend.workflow.pipeline import Pipeline
from backend.workflow.prompts import compile_video, weight
from backend.workflow.qa import inspect_video, character_error_rate, audio_verdict, build_qa_shot, _response_json, _audio_review, _visual_review


def fixture():
    character = {"id": "C001", "name": "林晚", "base_prompt": "26岁中国女性，黑色短发，椭圆脸。",
        "looks": [{"id": "L001", "name": "急诊医生", "prompt_delta": "浅蓝手术服、白大褂。"}],
        "assets": [
            {"id": "base-old", "kind": "base", "look_id": None, "status": "ready", "path": "assets/base.png"},
            {"id": "base-new", "kind": "base", "look_id": None, "status": "ready", "path": "assets/base-new.png"},
            {"id": "look-old", "kind": "look", "look_id": "L001", "status": "ready", "path": "assets/look.png", "parent_asset_ids": ["base-old"]},
            {"id": "turn-old", "kind": "turnaround", "look_id": "L001", "status": "ready", "path": "assets/turn.png", "parent_asset_ids": ["look-old"]}],
        "selected": {"base": "base-old", "L001": "look-old", "turnaround:L001": "turn-old"}}
    location = {"id": "ER", "name": "急诊大厅", "canonical_prompt": "冷白灯，右侧玻璃门、左侧分诊台。",
        "assets": [{"id": "scene", "kind": "location", "status": "ready", "path": "assets/scene.png"}],
        "selected": {"location": "scene"}}
    shot = {"id": "S001", "duration": 8, "characters": [{"id": "C001", "look_id": "L001"}],
        "location_id": "ER", "action": "林晚按下红色报警按钮。", "camera": "中景缓推。",
        "dialogue": [{"speaker": "林晚", "text": "立即封锁急诊室！"}],
        "assets": [{"id": "video-old", "kind": "video", "status": "ready", "path": "episodes/EP001/videos/clip.mp4"}],
        "selected": "video-old"}
    return {"schema_version": 1, "project": {"visual_style": "写实、冷青灰医疗悬疑，9:16竖屏。", "run_status": "idle"},
        "characters": [character], "locations": [location], "jobs": [],
        "episodes": [{"id": "EP001", "title": "测试片段", "source_title": "测试片段", "shots": [shot], "export": {"path": "old.mp4"}}],
        "exports": [{"episode_id": "EP001", "path": "old.mp4"}, {"episode_id": None, "path": "series.mp4"}]}


class StateAndSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.store.save(fixture())
        self.pipeline = Pipeline(self.store)

    def tearDown(self):
        self.pipeline.executor.shutdown(wait=True)
        self.temp.cleanup()

    def test_safe_path_rejects_parent_and_symlink_escape(self):
        with self.assertRaises(ValueError):
            safe_path(self.root, "../secret")
        with tempfile.TemporaryDirectory() as outside:
            (self.root / "escape").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                safe_path(self.root, "escape/secret")
        self.assertEqual(safe_path(self.root, "assets/look.png"), self.root.resolve() / "assets/look.png")

    def test_distinct_homes_have_independent_references_and_shot_bindings(self):
        first = self.pipeline.add_location({"name": "周家书房", "canonical_prompt": "深色书柜，单一书房。", "episode_id": "EP001"})["location"]
        second = self.pipeline.add_location({"name": "苏家客厅", "canonical_prompt": "米色沙发，单一客厅。"})["location"]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["assets"], [])
        self.pipeline.edit_shot({"episode_id": "EP001", "shot_id": "S001", "changes": {"location_id": second["id"]}})
        state = self.store.read()
        episode = state["episodes"][0]
        self.assertEqual(episode["shots"][0]["location_id"], second["id"])
        self.assertIn(first["id"], episode["locations"])
        self.assertIn(second["id"], episode["locations"])
        self.assertTrue(episode["shots"][0]["assets"][0]["stale"])
        self.assertEqual(state["locations"][0]["selected"], {"location": "scene"})

    def test_new_location_rejects_unknown_episode_without_partial_creation(self):
        before = self.store.read()
        with self.assertRaises(ValueError):
            self.pipeline.add_location({"name": "书房", "canonical_prompt": "书柜。", "episode_id": "missing"})
        self.assertEqual(self.store.read(), before)

    def test_explicit_look_selection_locks_cast_without_staling_same_selection(self):
        self.pipeline.bind_look({"episode_id": "EP001", "shot_id": "S001", "character_id": "C001", "look_id": "L001"})
        state = self.store.read()
        shot = state["episodes"][0]["shots"][0]
        self.assertIn("characters", shot["creative_locks"])
        self.assertFalse(shot["assets"][0].get("stale", False))
        self.assertEqual(state["episodes"][0]["export"], {"path": "old.mp4"})

    def test_atomic_mutations_do_not_lose_parallel_updates(self):
        self.store.mutate(lambda state: state.update(counter=0))
        def increment(_):
            self.store.mutate(lambda state: state.update(counter=state["counter"] + 1))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(increment, range(32)))
        self.assertEqual(self.store.read()["counter"], 32)
        self.assertFalse((self.root / "project.json.tmp").exists())

    def test_selected_asset_rejects_stale_and_incomplete_versions(self):
        owner = self.store.read()["characters"][0]
        self.assertEqual(selected_asset(owner)["id"], "base-old")
        owner["assets"][0]["stale"] = True
        self.assertIsNone(selected_asset(owner))
        owner["assets"][0].update(stale=False, status="submitting")
        self.assertIsNone(selected_asset(owner))

    def test_select_validates_kind_and_preserves_all_candidate_files(self):
        request = {"character_id": "C001", "kind": "look", "look_id": "L001", "asset_id": "base-new"}
        with self.assertRaises(ValueError):
            self.pipeline.select(request)
        self.assertEqual(len(self.store.read()["characters"][0]["assets"]), 4)
        self.pipeline.select({"character_id": "C001", "kind": "base", "asset_id": "base-new"})
        state = self.store.read()
        self.assertEqual(state["characters"][0]["selected"]["base"], "base-new")
        self.assertEqual(len(state["characters"][0]["assets"]), 4)
        self.assertTrue(state["episodes"][0]["shots"][0]["assets"][0]["stale"])
        self.assertIsNone(state["episodes"][0]["export"])
        self.assertTrue(all(item["stale"] for item in state["exports"]))

    def test_replacing_base_invalidates_transitive_turnaround(self):
        self.pipeline.select({"character_id": "C001", "kind": "base", "asset_id": "base-new"})
        owner = self.store.read()["characters"][0]
        self.assertIsNone(selected_asset(owner, "L001"))
        self.assertIsNone(selected_asset(owner, "turnaround:L001"))

    def test_adopting_edit_of_selected_master_keeps_new_look_or_scene_usable(self):
        for kind in ("look", "location"):
            with self.subTest(kind=kind):
                state = fixture()
                owner = state["characters"][0] if kind == "look" else state["locations"][0]
                old_id, key = ("look-old", "L001") if kind == "look" else ("scene", "location")
                candidate = {"id": "edited-master", "kind": kind, "look_id": "L001" if kind == "look" else None,
                    "status": "ready", "path": "assets/edited.png", "parent_asset_ids": [old_id]}
                if kind == "location":
                    candidate["qa"] = {"passed": True}
                owner["assets"] += [candidate, {"id": "old-sibling", "kind": kind,
                    "status": "ready", "path": "assets/sibling.png", "parent_asset_ids": [old_id]},
                    {"id": "old-descendant", "kind": kind, "status": "ready", "path": "assets/descendant.png",
                     "parent_asset_ids": ["old-sibling"]}]
                self.store.save(state)
                paths = [self.root / asset["path"] for asset in owner["assets"]]
                for path in paths:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"original-preserved")
                request = {"kind": kind, "asset_id": candidate["id"]}
                request.update({"character_id": "C001", "look_id": "L001"} if kind == "look" else {"location_id": "ER"})
                self.pipeline.select(request)
                after = self.store.read()
                chosen_owner = after["characters"][0] if kind == "look" else after["locations"][0]
                self.assertEqual(selected_asset(chosen_owner, key)["id"], "edited-master")
                self.assertFalse(next(a for a in chosen_owner["assets"] if a["id"] == "edited-master").get("stale", False))
                self.assertTrue(all(next(a for a in chosen_owner["assets"] if a["id"] == asset_id)["stale"]
                                    for asset_id in ("old-sibling", "old-descendant")))
                if kind == "look":
                    self.assertIsNone(selected_asset(chosen_owner, "turnaround:L001"))
                self.assertTrue(after["episodes"][0]["shots"][0]["assets"][0]["stale"])
                self.assertIsNone(after["episodes"][0]["export"])
                self.assertTrue(all(path.read_bytes() == b"original-preserved" for path in paths))

    def test_adopting_derived_candidate_does_not_bypass_stale_or_qa_rejection(self):
        for condition in ("stale", "rejected", "scene_qa_false"):
            with self.subTest(condition=condition):
                state = fixture()
                scene = condition == "scene_qa_false"
                owner = state["locations"][0] if scene else state["characters"][0]
                candidate = {"id": "blocked-edit", "kind": "location" if scene else "look",
                    "look_id": None if scene else "L001", "status": "rejected" if condition == "rejected" else "ready",
                    "stale": condition == "stale", "path": "assets/blocked.png",
                    "parent_asset_ids": ["scene" if scene else "look-old"]}
                if scene:
                    candidate["qa"] = {"passed": False}
                owner["assets"].append(candidate)
                self.store.save(state)
                before = self.store.read()
                request = {"kind": candidate["kind"], "asset_id": candidate["id"]}
                request.update({"location_id": "ER"} if scene else {"character_id": "C001", "look_id": "L001"})
                with self.assertRaises(ValueError):
                    self.pipeline.select(request)
                self.assertEqual(self.store.read(), before)

    def _two_stage_state(self):
        state = fixture()
        owner = state["characters"][0]
        owner["looks"][0]["name"] = "青年"
        owner["looks"].append({"id": "L002", "name": "壮年", "prompt_delta": "45岁，深色外套"})
        owner["assets"] += [
            {"id": "adult-old", "kind": "look", "look_id": "L002", "status": "ready", "path": "assets/adult-old.png", "parent_asset_ids": ["base-old"]},
            {"id": "adult-new", "kind": "look", "look_id": "L002", "status": "ready", "path": "assets/adult-new.png", "parent_asset_ids": ["base-old"]},
            {"id": "adult-turn-old", "kind": "turnaround", "look_id": "L002", "status": "ready", "path": "assets/adult-turn-old.png", "parent_asset_ids": ["adult-old"]},
            {"id": "adult-turn-new", "kind": "turnaround", "look_id": "L002", "status": "ready", "path": "assets/adult-turn-new.png", "parent_asset_ids": ["adult-old"]}]
        owner["selected"].update({"L002": "adult-old", "turnaround:L002": "adult-turn-old"})
        state["episodes"][0]["shots"][0]["assets"][0]["parent_asset_ids"] = ["look-old", "scene"]
        adult_episode = copy.deepcopy(state["episodes"][0])
        adult_episode.update(id="EP002", title="壮年剧情", export={"path": "adult.mp4"})
        adult_shot = adult_episode["shots"][0]
        adult_shot.update(id="S002", characters=[{"id": "C001", "look_id": "L002"}], selected="adult-video")
        adult_shot["assets"] = [{"id": "adult-video", "kind": "video", "status": "ready", "path": "episodes/EP002/videos/clip.mp4", "parent_asset_ids": ["adult-old", "scene"]}]
        state["episodes"].append(adult_episode)
        state["exports"].insert(1, {"episode_id": "EP002", "path": "adult.mp4"})
        self.store.save(state)
        return state

    def test_editing_adult_look_keeps_youth_video_and_export_current(self):
        self._two_stage_state()
        self.pipeline.edit_prompt({"character_id": "C001", "look_id": "L002", "prompt": "45岁，灰西装，鬓角微白"})
        state = self.store.read()
        youth, adult = state["episodes"]
        self.assertFalse(youth["shots"][0]["assets"][0].get("stale", False))
        self.assertEqual(youth["export"], {"path": "old.mp4"})
        self.assertTrue(adult["shots"][0]["assets"][0]["stale"])
        self.assertIsNone(adult["export"])
        self.assertFalse(state["exports"][0].get("stale", False))
        self.assertTrue(state["exports"][1]["stale"])
        self.assertEqual(state["characters"][0]["selected"]["L002"], "adult-old")

    def test_replacing_adult_master_only_invalidates_that_look(self):
        self._two_stage_state()
        self.pipeline.select({"character_id": "C001", "kind": "look", "look_id": "L002", "asset_id": "adult-new"})
        state = self.store.read()
        self.assertFalse(state["episodes"][0]["shots"][0]["assets"][0].get("stale", False))
        self.assertEqual(state["episodes"][0]["export"], {"path": "old.mp4"})
        self.assertTrue(state["episodes"][1]["shots"][0]["assets"][0]["stale"])
        self.assertIsNone(state["episodes"][1]["export"])
        owner = state["characters"][0]
        self.assertEqual(owner["selected"]["L002"], "adult-new")
        self.assertTrue(next(a for a in owner["assets"] if a["id"] == "adult-turn-old")["stale"])
        self.assertFalse(next(a for a in owner["assets"] if a["id"] == "turn-old").get("stale", False))

    def test_base_identity_edit_invalidates_every_age_stage(self):
        self._two_stage_state()
        self.pipeline.edit_prompt({"character_id": "C001", "prompt": "固定身份母版：新的统一脸型描述"})
        state = self.store.read()
        for episode in state["episodes"]:
            self.assertTrue(episode["shots"][0]["assets"][0]["stale"])
            self.assertIsNone(episode["export"])
        self.assertTrue(all(e["stale"] for e in state["exports"]))

    def test_auxiliary_turnaround_selection_does_not_expire_unused_video_dependencies(self):
        self._two_stage_state()
        self.pipeline.select({"character_id": "C001", "kind": "turnaround", "look_id": "L002", "asset_id": "adult-turn-new"})
        state = self.store.read()
        self.assertEqual(state["characters"][0]["selected"]["turnaround:L002"], "adult-turn-new")
        for episode in state["episodes"]:
            self.assertFalse(episode["shots"][0]["assets"][0].get("stale", False))
            self.assertIsNotNone(episode["export"])
        self.assertTrue(all(not e.get("stale", False) for e in state["exports"]))

    def test_editing_future_unused_stage_keeps_existing_series_export_valid(self):
        state = self._two_stage_state()
        state["episodes"] = state["episodes"][:1]
        state["exports"] = [e for e in state["exports"] if e.get("episode_id") != "EP002"]
        self.store.save(state)
        self.pipeline.edit_prompt({"character_id": "C001", "look_id": "L002", "prompt": "未来壮年，调整新外套"})
        state = self.store.read()
        self.assertIsNotNone(state["episodes"][0]["export"])
        self.assertTrue(all(not e.get("stale", False) for e in state["exports"]))

    def test_stale_video_cannot_be_selected(self):
        self.store.mutate(lambda state: state["episodes"][0]["shots"][0]["assets"][0].update(stale=True))
        with self.assertRaises(ValueError):
            self.pipeline.select_video({"episode_id": "EP001", "shot_id": "S001", "asset_id": "video-old"})

    def test_shot_prompt_edit_invalidates_series_export(self):
        self.pipeline.edit_prompt({"episode_id": "EP001", "shot_id": "S001", "prompt": "按铃后定住半秒"})
        state = self.store.read()
        self.assertTrue(state["episodes"][0]["shots"][0]["assets"][0]["stale"])
        self.assertIsNone(state["episodes"][0]["export"])
        self.assertTrue(all(item.get("stale") for item in state["exports"]))

    def test_remote_success_recovers_without_another_paid_submission(self):
        def pending(state):
            state["episodes"][0]["shots"][0]["assets"][0].update(
                status="succeeded", task_id="existing-task", references=[])
        self.store.mutate(pending)
        (self.root / "episodes/EP001/videos").mkdir(parents=True)
        client = MagicMock()
        client.wait_for_task.return_value = {"output": {"task_id": "existing-task", "task_status": "SUCCEEDED", "video_url": "https://example.invalid/existing.mp4"}}
        def download(url, path):
            Path(path).write_bytes(b"offline-video")
        qa = {"technical_pass": True, "visual_pass": True, "audio_pass": True, "needs_human_review": True}
        with patch.object(self.pipeline, "_check_space"), \
             patch("backend.workflow.pipeline.DashScopeVideoClient", return_value=client), \
             patch("backend.workflow.pipeline.download_video", side_effect=download), \
             patch("backend.workflow.qa.inspect_video", return_value=qa):
            result = self.pipeline.video("EP001", "S001")
        client.submit.assert_not_called()
        self.assertEqual(client.wait_for_task.call_args.args[0], "existing-task")
        self.assertEqual(result["id"], "video-old")

    def test_manual_review_can_approve_ai_false_positive_without_overwriting_qa(self):
        qa = {"technical_pass": True, "visual_pass": False, "audio_pass": True,
              "visual": {"reason": "停职通知文字不可读"}}
        def rejected(state):
            shot = state["episodes"][0]["shots"][0]
            shot["selected"] = None
            shot["assets"][0].update(status="rejected", qa=qa)
        self.store.mutate(rejected)
        path = self.root / "episodes/EP001/videos/clip.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"offline-validated-video")
        self.pipeline.review_video({"episode_id": "EP001", "shot_id": "S001", "asset_id": "video-old",
                                    "decision": "approve", "reviewer": "codex", "notes": "纸张实体在画面中，通知文字将由overlays后期合成"})
        state = self.store.read()
        shot = state["episodes"][0]["shots"][0]
        asset = shot["assets"][0]
        self.assertEqual(asset["status"], "ready")
        self.assertEqual(asset["qa"], qa)
        self.assertEqual(asset["manual_review"]["reviewer"], "codex")
        self.assertIn("at", asset["manual_review"])
        self.assertIsNone(shot["selected"])
        self.assertEqual(state["episodes"][0]["export"], {"path": "old.mp4"})

    def test_manual_review_rejects_missing_stale_failed_and_bad_dependencies(self):
        request = {"episode_id": "EP001", "shot_id": "S001", "asset_id": "video-old",
                   "decision": "approve", "reviewer": "user", "notes": "已查看实际视频"}
        path = self.root / "episodes/EP001/videos/clip.mp4"
        path.parent.mkdir(parents=True)
        for case in ("missing", "stale", "technical", "provider_failed", "dependency"):
            with self.subTest(case=case):
                state = fixture()
                asset = state["episodes"][0]["shots"][0]["assets"][0]
                asset.update(status="rejected", qa={"technical_pass": case != "technical"})
                if case == "stale":
                    asset["stale"] = True
                if case == "provider_failed":
                    asset["status"] = "failed"
                if case == "dependency":
                    asset["parent_asset_ids"] = ["look-old"]
                    state["characters"][0]["assets"][2]["stale"] = True
                self.store.save(state)
                if case != "missing":
                    path.write_bytes(b"offline-video")
                else:
                    path.unlink(missing_ok=True)
                with self.assertRaises(ValueError):
                    self.pipeline.review_video(request)
                self.assertNotIn("manual_review", self.store.read()["episodes"][0]["shots"][0]["assets"][0])


class QualityRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        state = fixture()
        state["project"].update(resolution="720P", ratio="9:16")
        state["episodes"][0]["shots"][0].update(assets=[], selected=None, prompt="保留用户原始补充")
        self.store.save(state)
        for name in ("look.png", "scene.png"):
            path = self.root / "assets" / name
            path.parent.mkdir(exist_ok=True)
            Image.new("RGB", (720, 1280)).save(path)
        self.pipeline = Pipeline(self.store)
        self.client = MagicMock()
        self.client.submit.side_effect = lambda payload: {"output": {"task_id": f"task-{self.client.submit.call_count}", "task_status": "PENDING"}}
        self.client.wait_for_task.return_value = {"output": {"task_status": "SUCCEEDED", "video_url": "https://example.invalid/result.mp4"}}

    def tearDown(self):
        self.pipeline.executor.shutdown(wait=True)
        self.temp.cleanup()

    def _drive(self, results, submit_error=None):
        if submit_error is not None:
            self.client.submit.side_effect = submit_error
        def download(url, path):
            Path(path).write_bytes(b"offline-video")
        with patch.object(self.pipeline, "_check_space"), \
             patch("backend.workflow.pipeline.DashScopeVideoClient", return_value=self.client), \
             patch("backend.workflow.pipeline.download_video", side_effect=download), \
             patch("backend.workflow.qa.inspect_video", side_effect=results):
            self.pipeline._generate_and_select("EP001", "S001")

    def _shot(self):
        return self.store.read()["episodes"][0]["shots"][0]

    def test_rejected_audio_gets_one_targeted_new_version_and_selects_success(self):
        rejected = {"technical_pass": True, "visual_pass": True, "audio_pass": False, "CER": 0.5}
        passed = {"technical_pass": True, "visual_pass": True, "audio_pass": True}
        self._drive([rejected, passed])
        shot = self._shot()
        self.assertEqual(self.client.submit.call_count, 2)
        first, repaired = shot["assets"]
        self.assertEqual((first["status"], repaired["status"]), ("rejected", "ready"))
        self.assertEqual(repaired["repair_attempt"], 1)
        self.assertEqual(repaired["repair_parent_asset_id"], first["id"])
        self.assertIn(first["id"], repaired["parent_asset_ids"])
        self.assertIn("声音修复", repaired["repair_notes"])
        self.assertIn("立即封锁急诊室", repaired["prompt"])
        self.assertEqual(shot["prompt"], "保留用户原始补充")
        self.assertEqual(shot["selected"], repaired["id"])

    def test_three_rejected_versions_exhaust_two_repair_limit(self):
        rejected = {"technical_pass": True, "visual_pass": False, "audio_pass": True,
                    "visual": {"defects": ["白大褂变成红外套"], "reason": "服装不一致"}}
        with self.assertRaisesRegex(ValueError, "两次自动质量修复"):
            self._drive([rejected, rejected, rejected])
        assets = self._shot()["assets"]
        self.assertEqual(self.client.submit.call_count, 3)
        self.assertEqual([asset["repair_attempt"] for asset in assets], [0, 1, 2])
        self.assertEqual(assets[2]["repair_parent_asset_id"], assets[1]["id"])
        self.assertIn("白大褂变成红外套", assets[1]["repair_notes"])
        self.assertIsNone(self._shot()["selected"])

    def test_unavailable_visual_or_asr_never_triggers_paid_regeneration(self):
        qa = {"technical_pass": True, "visual_pass": None, "audio_pass": False,
              "visual_error": "HTTP401", "audio_error": "ASR unavailable"}
        with self.assertRaisesRegex(ValueError, "质检服务不可用"):
            self._drive([qa])
        self.assertEqual(self.client.submit.call_count, 1)
        self.assertEqual(len(self._shot()["assets"]), 1)

    def test_ambiguous_transport_and_authentication_failures_are_not_retried(self):
        with self.assertRaises(DashScopeTransportError):
            self._drive([], DashScopeTransportError("POST transport uncertain"))
        self.assertEqual(self.client.submit.call_count, 1)
        self.assertEqual(self._shot()["assets"][0]["status"], "needs_review")
        with self.assertRaisesRegex(ValueError, "提交结果不明确"):
            self._drive([])
        self.assertEqual(self.client.submit.call_count, 1)

    def test_provider_auth_failure_stops_after_one_submission(self):
        error = DashScopeAPIError({"code": "InvalidApiKey", "message": "authentication failed"}, 401)
        with self.assertRaises(DashScopeAPIError):
            self._drive([], error)
        self.assertEqual(self.client.submit.call_count, 1)
        self.assertEqual(self._shot()["assets"][0]["status"], "failed")

    def test_repair_over_prompt_limit_stops_before_new_paid_submission(self):
        rejected = {"technical_pass": True, "visual_pass": False, "audio_pass": True,
                    "visual": {"defects": ["中" * 3000]}}
        with self.assertRaisesRegex(ValueError, "HappyHorse上限"):
            self._drive([rejected])
        self.assertEqual(self.client.submit.call_count, 1)
        self.assertEqual(len(self._shot()["assets"]), 1)
        self.assertEqual(self._shot()["prompt"], "保留用户原始补充")


class PromptContractTests(unittest.TestCase):
    def test_reference_order_and_identity_locks_are_stable(self):
        state = fixture()
        episode = state["episodes"][0]
        prompt, refs, fingerprint, parents = compile_video(state, episode, episode["shots"][0])
        self.assertEqual(refs, ["assets/look.png", "assets/scene.png"])
        self.assertEqual(parents, ["look-old", "scene"])
        self.assertIn("[Image 1]仅绑定林晚", prompt)
        self.assertIn("[Image 2]只参考空间布局", prompt)
        self.assertIn(state["characters"][0]["base_prompt"], prompt)
        self.assertEqual(len(fingerprint), 64)
        self.assertLessEqual(weight(prompt), 5000)

    def test_overlong_chinese_prompt_fails_before_paid_request(self):
        state = fixture()
        shot = state["episodes"][0]["shots"][0]
        shot["prompt"] = "中" * 2501
        with self.assertRaisesRegex(ValueError, "HappyHorse上限"):
            compile_video(state, state["episodes"][0], shot)

    def test_missing_look_and_excessive_references_are_rejected(self):
        state = fixture()
        shot = state["episodes"][0]["shots"][0]
        state["characters"][0]["assets"][2]["stale"] = True
        with self.assertRaisesRegex(ValueError, "尚未采用"):
            compile_video(state, state["episodes"][0], shot)
        state = fixture()
        shot = state["episodes"][0]["shots"][0]
        shot["characters"] *= 9
        with self.assertRaisesRegex(ValueError, "超过9张"):
            compile_video(state, state["episodes"][0], shot)


class ExportCleanupTests(unittest.TestCase):
    def test_concat_trims_each_aac_padding_to_exact_video_frame_timeline(self):
        from backend.workflow.render import _run, _concat, _chapters, probe_video
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for index, duration in enumerate((1.0, 1.25, 0.75)):
                path = root / f"part_{index}.mp4"
                _run(["-y", "-f", "lavfi", "-i", f"color=c=navy:s=720x1280:r=24:d={duration}",
                      "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration + 0.25}",
                      "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)])
                paths.append(path)
            result = _concat(paths, root / "joined.mp4")
            self.assertEqual(result["frames"], 72)
            self.assertEqual(result["fps"], 24)
            self.assertAlmostEqual(result["duration"], 3.0, places=5)
            timeline = json.loads((root / "concat-timeline.json").read_text())
            self.assertEqual([part["start"] for part in timeline], [0.0, 1.0, 2.25])
            self.assertTrue(all(path.is_file() for path in paths))
            _chapters(root / "joined.mp4", [{"title": "第一集", "start": 0, "duration": 1},
                                           {"title": "第二集", "start": 1, "duration": 2}])
            chaptered = probe_video(root / "joined.mp4")
            self.assertEqual(chaptered["frames"], 72)
            self.assertEqual(chaptered["fps"], 24)
            self.assertAlmostEqual(chaptered["duration"], 3, places=5)

    def test_completed_export_removes_only_rebuildable_encoded_intermediates(self):
        from backend.workflow.render import _run, render_episode
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "episodes/EP001/videos/S001/v001/clip.mp4"
            source.parent.mkdir(parents=True)
            _run(["-y", "-f", "lavfi", "-i", "color=c=navy:s=240x400:r=24:d=1",
                  "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)])
            original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            alternate = source.parent.parent / "v002/clip.mp4"
            alternate.parent.mkdir()
            alternate.write_bytes(source.read_bytes())
            episode = {"id": "EP001", "title": "清理验证", "shots": [{"id": "S001", "selected": "a",
                "assets": [{"id": "a", "status": "ready", "path": str(source.relative_to(root))}],
                "dialogue": [{"speaker": "旁白", "text": "保留成片和创意版本"}]}]}
            result = render_episode(root, episode)
            export = (root / result["path"]).parent
            qc = json.loads((root / result["qc_path"]).read_text())
            self.assertTrue((root / result["path"]).is_file())
            self.assertTrue((root / result["subtitles_path"]).is_file())
            self.assertTrue((export / "source_captions.srt").is_file())
            self.assertTrue((export / "shot_001_subtitle_01.txt").is_file())
            self.assertFalse((export / "shot_001.mp4").exists())
            self.assertEqual(qc["intermediates_removed"], ["shot_001.mp4"])
            self.assertGreater(qc["intermediates_removed_bytes"], 0)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)
            self.assertTrue(alternate.is_file())
            with patch("backend.workflow.render._concat", side_effect=AssertionError("不应重复编码")):
                reused = render_episode(root, episode)
            self.assertEqual(reused["path"], result["path"])
            self.assertTrue(reused["cache_reused"])


class QualityInspectionTests(unittest.TestCase):
    def test_visual_context_names_masked_cast_and_prior_design_without_changing_shot(self):
        state = fixture()
        episode = state["episodes"][0]
        shot = episode["shots"][0]
        episode.update(summary="角色6在玻璃门外求情，医生坚持隔离", climax="病毒不会因为是孩子就不传染")
        state["characters"][0]["looks"][0].update(name="隔离衣与N95", prompt_delta="蓝隔离衣，N95口罩、护目镜，眼镜在护目镜后")
        state["locations"][0].update(name="独立隔离病房", canonical_prompt="前部密闭玻璃入口，门扇完全关闭；房内后部卫生间门，一张病床。")
        shot["source_actions"] = ["角色6拍玻璃，医生在门内听他说话"]
        episode["shots"] = [{**shot, "id": "S000", "action": "角色6拍打玻璃，最后手扶玻璃停住"}, shot]
        enriched = build_qa_shot(state, episode, shot)
        self.assertEqual(enriched["previous_shot_design"]["id"], "S000")
        self.assertEqual(enriched["characters"][0]["name"], "林晚")
        self.assertEqual(enriched["characters"][0]["look_name"], "隔离衣与N95")
        self.assertTrue(enriched["characters"][0]["mouth_covered"])
        self.assertNotIn("mouth_covered", shot["characters"][0])
        self.assertEqual(enriched["location_context"], {key: state["locations"][0][key] for key in ("id", "name", "canonical_prompt")})
        self.assertNotIn("location_context", shot)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            reference = folder / "reference.png"
            Image.new("RGB", (8, 8)).save(reference)
            client = MagicMock()
            client.chat.return_value = {"choices": [{"message": {"content": json.dumps({
                "identity": 95, "scene": 98, "action": 85, "pass": True, "defects": [], "reason": "核心正确"})}}]}
            with patch("backend.workflow.qa.DashScopeLLMClient", return_value=client):
                _visual_review(folder, [], [reference, reference], enriched)
            messages = client.chat.call_args.args[0]
            labels = [part.get("text", "") for part in messages[1]["content"] if part["type"] == "text"]
            self.assertTrue(any("林晚" in text and "mouth_covered=True" in text for text in labels))
            self.assertIn("角色6在玻璃门外求情", labels[0])
            self.assertIn("角色6拍打玻璃", labels[0])
            self.assertIn("source_actions", labels[0])
            self.assertIn("房内后部卫生间门", labels[0])
            self.assertIn('"location_context"', labels[0])
            self.assertIn("不是每个镜头都必须完成", messages[0]["content"])
            self.assertIn("看不到嘴或眼镜不等于缺失", messages[0]["content"])
            self.assertIn("角色在门内外的侧别", messages[0]["content"])

    def test_location_context_and_framing_do_not_override_real_boundary_or_wardrobe_defects(self):
        state = fixture()
        episode = state["episodes"][0]
        shot = episode["shots"][0]
        state["locations"][0].update(name="独立单床病房", canonical_prompt="前部玻璃隔离入口完全关闭，后部为房内卫生间门。")
        before = copy.deepcopy(state)
        enriched = build_qa_shot(state, episode, shot)
        cases = [(["半身镜头未拍脚，门框入画，摄影机在玻璃外"], [], True),
                 (["前部隔离玻璃入口敞开，患者跨越到走廊"], ["真实打开隔离入口并跨越边界"], False),
                 (["应穿浅蓝隔离衣却实际灰T恤裸脸"], ["可见服装与PPE不符"], False)]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            reference = folder / "reference.png"
            Image.new("RGB", (8, 8)).save(reference)
            for observations, blocking, passed in cases:
                with self.subTest(blocking=blocking):
                    client = MagicMock()
                    client.chat.return_value = {"choices": [{"message": {"content": json.dumps({
                        "identity": 95, "scene": 98, "action": 85, "pass": True, "core_plot_pass": True,
                        "blocking_defects": blocking, "minor_observations": observations if not blocking else [],
                        "defects": observations, "reason": "离线场景上下文与阻断判据契约"})}}]}
                    with patch("backend.workflow.qa.DashScopeLLMClient", return_value=client):
                        inspected = _visual_review(folder, [], [reference, reference], enriched)
                    context = json.loads(client.chat.call_args.args[0][1]["content"][0]["text"].split("镜头要求：", 1)[1])
                    self.assertEqual(context["location_context"], enriched["location_context"])
                    self.assertEqual(inspected["location_context"], enriched["location_context"])
                    self.assertIs(inspected["pass"], passed)
        self.assertEqual(state, before)

    def test_visual_contract_exposes_look_conflicts_and_separates_missing_ppe_from_occlusion(self):
        state = fixture()
        episode = state["episodes"][0]
        shot = episode["shots"][0]
        state["characters"][0]["looks"][0].update(
            name="隔离衣与N95", prompt_delta="浅蓝隔离衣，N95口罩、护目镜，原眼镜在护目镜后")
        shot.update(action="林晚穿灰T恤，口鼻与眼部完全裸露。", continuity_in="同一look，浅蓝隔离衣、N95与护目镜。")
        before = copy.deepcopy(state)
        enriched = build_qa_shot(state, episode, shot)
        cases = [("林晚穿浅蓝隔离衣、N95与护目镜，原眼镜被遮挡。", ["原眼镜被护目镜遮挡，不可见"], [], True),
                 (shot["action"], ["口鼻裸露，N95与护目镜实际缺失"], ["本应佩戴的N95与护目镜实际缺失"], False)]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            reference = folder / "look.png"
            Image.new("RGB", (8, 8)).save(reference)
            for action, defects, blocking, expected in cases:
                with self.subTest(blocking=blocking):
                    enriched["action"] = action
                    client = MagicMock()
                    client.chat.return_value = {"choices": [{"message": {"content": json.dumps({
                        "identity": 95, "scene": 98, "action": 85, "pass": True,
                        "core_plot_pass": True, "blocking_defects": blocking,
                        "defects": defects, "reason": "仅用于离线请求契约核验"})}}]}
                    with patch("backend.workflow.qa.DashScopeLLMClient", return_value=client):
                        result = _visual_review(folder, [], [reference, reference], enriched)
                    client.chat.assert_called_once()
                    messages = client.chat.call_args.args[0]
                    system = messages[0]["content"]
                    for rule in ("固定服装和防护装备的母版", "必须明确报告对应人物、冲突内容",
                                 "不能自行编造合理变体、简化版本或剧情允许换装", "本镜明确拍摄穿脱过程且与人物阶段和look绑定相符",
                                 "实际缺失，必须列入blocking_defects并pass=false", "看不到嘴或眼镜不等于缺失",
                                 "不得用微小嘴形判断对白"):
                        self.assertIn(rule, system)
                    context = messages[1]["content"][0]["text"]
                    self.assertIn("浅蓝隔离衣，N95口罩、护目镜", context)
                    self.assertIn(action, context)
                    self.assertEqual(result["pass"], expected)
        self.assertEqual(state, before)

    def test_empty_dialogue_allows_only_nonverbal_environment_transcriptions(self):
        for expected in ("", "……", "啊"):
            for transcript in ("", "。", "啊啊啊", "嗯，呜呜", "沙沙沙…", "扑扑扑", "哔滴咚砰"):
                with self.subTest(expected=expected, transcript=transcript):
                    verdict = audio_verdict(expected, transcript)
                    self.assertTrue(verdict["audio_pass"])
                    self.assertTrue(verdict["nonverbal_only"])
                    self.assertTrue(verdict["allow_reason"])
        self.assertEqual(character_error_rate("", "沙沙沙"), 1)
        self.assertEqual(audio_verdict("", "沙沙沙")["CER"], 1)

    def test_empty_dialogue_does_not_allow_new_semantic_lines(self):
        for transcript in ("快走", "救我", "求求你", "啊啊救我", "嗯，快走", "扑扑扑，求求你"):
            with self.subTest(transcript=transcript):
                verdict = audio_verdict("……", transcript)
                self.assertFalse(verdict["audio_pass"])
                self.assertFalse(verdict["nonverbal_only"])
                self.assertIsNone(verdict["allow_reason"])
        self.assertFalse(audio_verdict("立即封锁急诊室", "啊啊啊")["audio_pass"])
        self.assertTrue(audio_verdict("立即，封锁急诊室！", "立即封锁急诊室。")["audio_pass"])
        self.assertEqual(audio_verdict("立即封锁急诊室", "立即封锁急诊室")["decision_rule"], "cer")

    def test_minor_visual_details_do_not_block_but_severe_defects_still_do(self):
        cases = [([], True, False, True), (["同一个C001同时出现正面与背影分身"], True, True, False),
                 (["蓝隔离衣角色换成红外套"], True, True, False), (["严重手臂畸形"], True, True, False),
                 (["关键剧情结果相反"], False, True, False), ([], False, True, False)]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for blocking, core, raw_pass, expected in cases:
                with self.subTest(blocking=blocking, core=core):
                    response = {"identity": 95, "scene": 98, "action": 82, "pass": raw_pass,
                        "core_plot_pass": core, "blocking_defects": blocking,
                        "minor_observations": ["未清晰看到泪珠，起势约0.5秒，手短暂垂下"],
                        "defects": blocking, "reason": "核心抓袖哀求清楚，女主冷静，白PPE医护围住学生"}
                    client = MagicMock()
                    client.chat.return_value = {"choices": [{"message": {"content": json.dumps(response)}}]}
                    with patch("backend.workflow.qa.DashScopeLLMClient", return_value=client):
                        result = _visual_review(folder, [], [], fixture()["episodes"][0]["shots"][0])
                    self.assertEqual(result["pass"], expected)
                    self.assertEqual(result["raw_model_pass"], raw_pass)
                    system = client.chat.call_args.args[0][0]["content"]
                    self.assertIn("不同人物可以穿着不同颜色和款式的服装", system)
                    self.assertIn("G不是所有医护必须使用的身份前缀", system)

    def test_raw_visual_review_accounts_for_postproduction_overlays(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            video = folder / "clip.mp4"
            video.write_bytes(b"offline-video")
            shot = fixture()["episodes"][0]["shots"][0]
            shot["overlays"] = [{"kind": "source_caption", "text": "停职通知", "start": 0, "end": 2, "placement": "top"}]
            client = MagicMock()
            client.chat.return_value = {"choices": [{"message": {"content": json.dumps({
                "identity": 95, "scene": 90, "action": 85, "pass": True, "defects": [], "reason": "文件实体存在"})}}]}
            with patch("backend.workflow.qa.DashScopeLLMClient", return_value=client):
                result = _visual_review(folder, [], [], shot, video)
            messages = client.chat.call_args.args[0]
            self.assertIn("尚未后期合成", messages[0]["content"])
            self.assertIn("实体道具本身仍应存在", messages[0]["content"])
            self.assertIn("停职通知", messages[1]["content"][0]["text"])
            self.assertEqual(result["postproduction_overlays"], shot["overlays"])

    def test_cer_ignores_punctuation_and_detects_missing_or_extra_lines(self):
        self.assertEqual(character_error_rate("立即，封锁！", "立即封锁。"), 0)
        self.assertGreater(character_error_rate("立即封锁急诊室", "你好"), 0.2)
        self.assertEqual(character_error_rate("", ""), 0)
        self.assertEqual(character_error_rate("", "多余旁白"), 1)

    def test_visual_response_requires_real_scores_and_explicit_boolean(self):
        result = _response_json('{"identity":80,"scene":90,"action":75,"defects":[],"pass":true,"reason":"一致"}')
        self.assertTrue(result["pass"])
        with self.assertRaises(ValueError):
            _response_json('{"identity":true,"scene":90,"action":75,"defects":[],"pass":true,"reason":"一致"}')
        with self.assertRaises(ValueError):
            _response_json('{"identity":95,"scene":90,"action":85,"defects":[],"pass":true,"reason":"格式不完整","core_plot_pass":true,"blocking_defects":[{"identity_duplicate":true}]}')
        with self.assertRaises(ValueError):
            _response_json('{"identity":65,"scene":90,"action":85,"defects":[],"pass":false,"reason":"不可确认","needs_review":"true"}')

    def _inspect(self, visual_error=False, audio_pass=True, visual_result=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        folder = root / "episodes/EP001/videos/S001/v001"
        folder.mkdir(parents=True)
        (folder / "clip.mp4").write_bytes(b"offline-placeholder")
        asset = {"id": "v1", "path": str((folder / "clip.mp4").relative_to(root))}
        shot = fixture()["episodes"][0]["shots"][0]
        def fake_ffmpeg(args):
            if args[-1].endswith(".jpg"):
                Image.new("RGB", (720, 1280), "#556677").save(args[-1])
        info = {"duration": 8, "frames": 192, "width": 720, "height": 1280, "has_audio": True, "fps": 24}
        visual = visual_result if visual_result is not None else {"identity": 85, "scene": 90, "action": 80, "pass": True, "defects": [], "reason": "可见参考一致"}
        audio = {"audio_pass": audio_pass, "audio_checked": True, "expected": "立即封锁急诊室！", "transcript": "立即封锁急诊室！" if audio_pass else "没有对白", "CER": 0 if audio_pass else 1}
        with patch.dict(os.environ, {"DRAMA_VISUAL_QA": "1", "DRAMA_AUDIO_QA": "1"}), \
             patch("backend.workflow.qa.probe_video", return_value=info), \
             patch("backend.workflow.qa._run", side_effect=fake_ffmpeg), \
             patch("backend.workflow.qa._audio_review", return_value=audio), \
             patch("backend.workflow.qa._visual_review", side_effect=RuntimeError("VL不可用") if visual_error else None, return_value=visual):
            result = inspect_video(root, asset, shot, [])
        self.assertTrue((root / result["contact_path"]).is_file())
        self.assertTrue((root / result["qa_path"]).is_file())
        return result

    def test_inspect_keeps_technical_result_when_visual_model_unavailable(self):
        result = self._inspect(visual_error=True)
        self.assertTrue(result["technical_pass"])
        self.assertIsNone(result["visual_pass"])
        self.assertTrue(result["needs_human_review"])
        self.assertIn("VL不可用", result["visual_error"])

    def test_expected_ppe_uncertainty_keeps_original_score_and_stops_paid_repair(self):
        response = {"identity": 65, "scene": 98, "action": 90, "pass": False, "core_plot_pass": True,
                    "blocking_defects": [], "defects": [], "needs_review": True,
                    "reason": "PPE与体态相容，背侧机位未提供充分身份细节。",
                    "limitations": "N95与面屏遮挡、当前机位未提供完整人脸。"}
        with tempfile.TemporaryDirectory() as directory:
            client = MagicMock()
            client.chat.return_value = {"choices": [{"message": {"content": json.dumps(response)}}]}
            with patch("backend.workflow.qa.DashScopeLLMClient", return_value=client):
                visual = _visual_review(Path(directory), [], [], fixture()["episodes"][0]["shots"][0])
        self.assertEqual(visual["identity"], 65)
        self.assertFalse(visual["raw_model_pass"])
        self.assertFalse(visual["pass"])
        result = self._inspect(visual_result=visual)
        self.assertTrue(result["technical_pass"])
        self.assertTrue(result["audio_pass"])
        self.assertIsNone(result["visual_pass"])
        self.assertTrue(result["visual_review_pending"])
        self.assertTrue(result["needs_human_review"])
        self.assertEqual(result["visual"]["identity"], 65)
        with self.assertRaises(ValueError):
            Pipeline._quality_repair_notes(None, {"status": "rejected", "qa": result}, fixture()["episodes"][0]["shots"][0])

    def test_pending_identity_evidence_does_not_hide_visible_material_defects(self):
        for defect in ("实际可见不同人脸", "同一独立身份同时出现两次", "实际换成错误外套", "口鼻裸露且必要N95缺失"):
            with self.subTest(defect=defect):
                visual = {"identity": 65, "scene": 98, "action": 90, "pass": False, "core_plot_pass": True,
                          "blocking_defects": [defect], "defects": [defect], "needs_review": True,
                          "reason": "虽有遮挡，但已存在明确可见缺陷。", "limitations": "部分面部不可见。"}
                result = self._inspect(visual_result=visual)
                self.assertIs(result["visual_pass"], False)
                self.assertFalse(result["visual_review_pending"])
                self.assertEqual(result["visual"]["blocking_defects"], [defect])

    def test_passed_sample_and_asr_do_not_claim_speaker_or_time_alignment(self):
        result = self._inspect()
        self.assertTrue(result["visual_pass"])
        self.assertTrue(result["audio_pass"])
        self.assertTrue(result["needs_human_review"])
        self.assertIn("字幕时间", result["audio_limitations"])
        failed = self._inspect(audio_pass=False)
        self.assertFalse(failed["audio_pass"])

    def test_asr_uses_base64_audio_and_omits_text_model_options(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            client = MagicMock()
            client.config = LLMConfig(api_key="test-only")
            client.chat.return_value = {"choices": [{"message": {"content": "立即封锁急诊室！"}}]}
            def fake_extract(args):
                Path(args[-1]).write_bytes(b"RIFFofflinewav")
            with patch("backend.workflow.qa.DashScopeLLMClient", return_value=client), \
                 patch("backend.workflow.qa._run", side_effect=fake_extract):
                result = _audio_review(folder / "clip.mp4", folder, fixture()["episodes"][0]["shots"][0])
            self.assertTrue(result["audio_pass"])
            self.assertIsNone(client.config.temperature)
            self.assertIsNone(client.config.enable_thinking)
            self.assertIsNone(client.config.max_tokens)
            messages = client.chat.call_args.args[0]
            data = messages[0]["content"][0]["input_audio"]["data"]
            self.assertTrue(data.startswith("data:audio/wav;base64,"))
            self.assertNotIn("立即封锁", json.dumps(messages, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
