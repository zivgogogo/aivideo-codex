"""Offline scene-image acceptance and recovery checks; no provider calls."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import backend.workflow.qa as qa
from backend.workflow.pipeline import Pipeline
from core.store import Store


def _reply(result):
    return {"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}]}


def _scene_result(**overrides):
    return {"people_present": False, "single_space": True, "collage": False,
            "key_layout_pass": True, "layout_defects": [], "pass": True,
            "reason": "单一空房，关键空间符合要求。", **overrides}


def _state():
    return {
        "schema_version": 1,
        "project": {"visual_style": "冷青灰医疗悬疑，自然肤质，情绪通过细微表情表达。", "run_status": "idle"},
        "characters": [], "jobs": [], "episodes": [], "exports": [],
        "locations": [{"id": "ER", "name": "急诊大厅", "canonical_prompt": "单一大厅，右侧玻璃门，左侧分诊台。", "assets": [], "selected": {}}],
    }


class SceneImageInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.image = self.root / "assets/scenes/ER/location/v0001/image.png"
        self.image.parent.mkdir(parents=True)
        Image.new("RGB", (120, 160), "#d0d0d0").save(self.image)
        self.asset = {"id": "scene1", "kind": "location", "path": str(self.image.relative_to(self.root))}
        self.location = _state()["locations"][0]

    def tearDown(self):
        self.temp.cleanup()

    def _inspect(self, result):
        with patch.object(qa, "DashScopeLLMClient") as client:
            client.return_value.chat.return_value = _reply(result)
            reviewed = qa.inspect_scene_image(self.root, self.asset, self.location)
        self.assertTrue(any(self.image.parent.rglob("scene-qa-response.json")))
        self.assertTrue(any(self.image.parent.rglob("scene-qa.json")))
        return reviewed

    def test_people_or_collage_hard_reject_even_if_model_pass_is_true(self):
        cases = [
            _scene_result(people_present=True, reason="画面床上有人。"),
            _scene_result(collage=True, reason="存在九宫格拼贴。"),
            _scene_result(single_space=False, reason="两个互不连续房间。"),
        ]
        for result in cases:
            with self.subTest(result=result):
                reviewed = self._inspect(result)
                self.assertIs(reviewed["passed"], False)
                self.assertTrue(self.image.is_file(), "拒收保留原候选图片，方便人工审查")

    def test_clean_single_empty_space_passes_and_uses_actual_image(self):
        result = _scene_result(reason="单一空大厅，无人无肢体。")
        with patch.object(qa, "DashScopeLLMClient") as client:
            client.return_value.chat.return_value = _reply(result)
            reviewed = qa.inspect_scene_image(self.root, self.asset, self.location)
        self.assertIs(reviewed["passed"], True)
        self.assertFalse(reviewed["needs_review"])
        request = client.return_value.chat.call_args.args[0]
        serialized = json.dumps(request, ensure_ascii=False)
        self.assertIn("image_url", serialized)
        self.assertIn("base64,", serialized, "质检应读取实际像素，不能只评估场景提示词")

    def test_unknown_network_or_nonboolean_schema_never_passes(self):
        with patch.object(qa, "DashScopeLLMClient") as client:
            client.return_value.chat.side_effect = TimeoutError("review timeout")
            reviewed = qa.inspect_scene_image(self.root, self.asset, self.location)
        self.assertIsNone(reviewed["passed"])
        self.assertTrue(reviewed["needs_review"])
        with patch.object(qa, "DashScopeLLMClient") as client:
            client.return_value.chat.return_value = _reply(_scene_result(people_present="false", reason="invalid bool"))
            reviewed = qa.inspect_scene_image(self.root, self.asset, self.location)
        self.assertIsNone(reviewed["passed"])
        self.assertTrue(reviewed["needs_review"])

    def test_invalid_local_image_never_invokes_visual_model(self):
        self.image.write_bytes(b"not an image")
        with patch.object(qa, "DashScopeLLMClient") as client:
            reviewed = qa.inspect_scene_image(self.root, self.asset, self.location)
        self.assertIs(reviewed["passed"], False)
        client.assert_not_called()

    def test_empty_room_with_explicitly_required_closed_door_open_is_rejected(self):
        self.location["canonical_prompt"] = "单床独立隔离病房，前景玻璃门扇完全关闭，与门框齐平，病床在房内。"
        for result in (
            _scene_result(key_layout_pass=False, layout_defects=["明确要求完全关闭的玻璃门实际敞开。"], reason="无人单房，但门扇离开门框敞开。"),
            _scene_result(key_layout_pass=True, layout_defects=["独立单床房实际是多床房。"], reason="返回pass=true与明确布局缺陷矛盾。"),
        ):
            with self.subTest(result=result):
                inspected = self._inspect(result)
                self.assertIs(inspected["passed"], False)
                self.assertFalse(inspected["needs_review"])
                self.assertEqual(inspected["visual"]["layout_defects"], result["layout_defects"])
                self.assertTrue(self.image.exists())

    def test_closed_single_bed_room_approves_with_layout_evidence(self):
        self.location["canonical_prompt"] = "单床独立隔离病房，玻璃门完全关闭且与门框齐平，房内一张病床。"
        inspected = self._inspect(_scene_result(reason="单一无人单床房，玻璃门扇与门框齐平关闭。"))
        self.assertIs(inspected["passed"], True)
        self.assertEqual(inspected["canonical_prompt"], self.location["canonical_prompt"])
        self.assertEqual(inspected["visual"]["layout_defects"], [])

    def test_unspecified_door_state_does_not_add_closed_door_requirement(self):
        self.location["canonical_prompt"] = "单床房，玻璃门、单张病床及床头柜，无人。"
        inspected = self._inspect(_scene_result(reason="玻璃门敞开但canonical未限定开关；单床独立房关键布局符合。"))
        self.assertIs(inspected["passed"], True)
        self.assertEqual(inspected["canonical_prompt"], self.location["canonical_prompt"])
        self.assertNotIn("关闭", inspected["canonical_prompt"])

    def test_missing_or_wrong_layout_schema_is_unknown_never_approved(self):
        missing_key = _scene_result()
        del missing_key["key_layout_pass"]
        missing_defects = _scene_result()
        del missing_defects["layout_defects"]
        for result in (missing_key, missing_defects,
                       _scene_result(key_layout_pass="true"),
                       _scene_result(layout_defects="none"),
                       _scene_result(layout_defects=[False])):
            with self.subTest(result=result):
                inspected = self._inspect(result)
                self.assertIsNone(inspected["passed"])
                self.assertTrue(inspected["needs_review"])


class SceneImagePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.store.save(_state())
        self.pipeline = Pipeline(self.store)
        self.request = {"kind": "location", "location_id": "ER"}

    def tearDown(self):
        self.pipeline.executor.shutdown(wait=True)
        self.temp.cleanup()

    @staticmethod
    def _download(_url, target):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (120, 160), "white").save(target)

    def _download_failed_asset(self):
        folder = self.root / "assets/scenes/ER/location/v0001"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "response.json").write_text(json.dumps({"mock": "saved response"}))
        asset = {"id": "scene-recover", "kind": "location", "look_id": None, "status": "download_failed", "path": str((folder / "image.png").relative_to(self.root)), "version": 1}
        self.store.mutate(lambda state: state["locations"][0]["assets"].append(asset))
        return asset

    def test_download_recovery_runs_scene_qa_without_another_image_submission(self):
        for passed, expected_status in ((False, "rejected"), (None, "needs_review"), (True, "ready")):
            with self.subTest(passed=passed):
                self.store.save(_state())
                asset = self._download_failed_asset()
                verdict = {"passed": passed, "needs_review": passed is None}
                with patch.object(self.pipeline, "_check_space"), \
                     patch("backend.workflow.pipeline.generate_image") as submit, \
                     patch("backend.workflow.pipeline.extract_image_urls", return_value=["https://example.invalid/previous.png"]), \
                     patch("backend.workflow.pipeline.download_image", side_effect=self._download), \
                     patch.object(qa, "inspect_scene_image", return_value=verdict) as inspect:
                    try:
                        self.pipeline.image(self.request)
                    except ValueError:
                        # Explicit reject/unknown may surface as a stopped image job.
                        pass
                submit.assert_not_called()
                inspect.assert_called_once()
                current = self.store.read()["locations"][0]["assets"][0]
                self.assertEqual(current["id"], asset["id"])
                self.assertEqual(current["status"], expected_status)
                self.assertEqual(current["qa"], verdict)
                self.assertTrue((self.root / current["path"]).is_file())
                self.assertFalse(self.store.read()["locations"][0]["selected"])

    def test_new_scene_rejects_unsafe_pixels_and_keeps_candidate(self):
        verdict = {"passed": False, "needs_review": False, "visual": {"people_present": True}}
        with patch.object(self.pipeline, "_check_space"), \
             patch("backend.workflow.pipeline.generate_image", return_value={"usage": {}}) as submit, \
             patch("backend.workflow.pipeline.extract_image_urls", return_value=["https://example.invalid/new.png"]), \
             patch("backend.workflow.pipeline.download_image", side_effect=self._download), \
             patch.object(qa, "inspect_scene_image", return_value=verdict) as inspect:
            try:
                self.pipeline.image(self.request)
            except ValueError:
                pass
        submit.assert_called_once()
        inspect.assert_called_once()
        current = self.store.read()["locations"][0]["assets"][0]
        self.assertEqual(current["status"], "rejected")
        self.assertEqual(current["qa"], verdict)
        self.assertTrue((self.root / current["path"]).is_file())
        self.assertFalse(self.store.read()["locations"][0]["selected"])
        self.assertNotIn("自然肤质", current["prompt"])
        self.assertNotIn("细微表情", current["prompt"])

    def test_rejected_or_unknown_scene_stops_ensure_without_paid_retry(self):
        for status in ("rejected", "needs_review"):
            with self.subTest(status=status):
                self.store.save(_state())
                self.store.mutate(lambda state: state["locations"][0]["assets"].append({
                    "id": "unsafe-scene", "kind": "location", "look_id": None,
                    "status": status, "path": "assets/unsafe.png"}))
                with patch.object(self.pipeline, "image") as generate:
                    with self.assertRaises(ValueError):
                        self.pipeline._ensure_image(self.request)
                generate.assert_not_called()

    def test_select_requires_new_scene_qa_pass_but_legacy_stays_usable(self):
        asset = {"id": "scene", "kind": "location", "look_id": None, "status": "ready", "path": "assets/scene.png"}
        for passed in (False, None):
            with self.subTest(passed=passed):
                self.store.save(_state())
                self.store.mutate(lambda state: state["locations"][0]["assets"].append(asset | {"qa": {"passed": passed}}))
                with self.assertRaises(ValueError):
                    self.pipeline.select(self.request | {"asset_id": "scene"})
                self.assertFalse(self.store.read()["locations"][0]["selected"])
        for legacy in (asset, asset | {"qa": {"passed": True, "visual": {
                "people_present": False, "single_space": True, "collage": False, "pass": True}}}):
            with self.subTest(legacy=legacy):
                self.store.save(_state())
                self.store.mutate(lambda state: state["locations"][0]["assets"].append(legacy))
                with patch.object(qa, "inspect_scene_image") as inspect:
                    self.pipeline.select(self.request | {"asset_id": "scene"})
                inspect.assert_not_called()
                self.assertEqual(self.store.read()["locations"][0]["selected"]["location"], "scene")


if __name__ == "__main__":
    unittest.main()
