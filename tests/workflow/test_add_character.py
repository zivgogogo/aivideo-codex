"""Independent character creation is metadata-only and atomic; no providers."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.workflow.pipeline import Pipeline
from core.store import Store


class AddCharacterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.store.save({"project": {"run_status": "idle"}, "characters": [], "locations": [], "jobs": [],
            "episodes": [{"id": "EP052", "characters": ["G007"], "shots": [{"id": "old", "characters": []}],
                          "export": {"path": "preserved.mp4"}}], "exports": []})
        self.pipeline = Pipeline(self.store)

    def tearDown(self):
        self.pipeline.executor.shutdown(wait=True)
        self.temp.cleanup()

    def test_two_relatives_have_independent_identity_looks_and_assets_without_generation(self):
        before = copy.deepcopy(self.store.read()["episodes"][0])
        with patch("backend.workflow.pipeline.generate_image") as image, \
             patch("backend.workflow.pipeline.DashScopeVideoClient") as video, \
             patch.object(self.pipeline, "enqueue") as enqueue:
            sister = self.pipeline.add_character({"name": "角色14姐姐", "base_prompt": "58岁中国女性，宽额圆脸，灰黑短卷发，敦实体态。",
                "aliases": ["姐姐", " 姐姐 "], "episode_id": "EP052",
                "look": {"name": "棕外套", "prompt_delta": "棕色外套，米色衬衫，黑色长裤，黑色布鞋。"}})["character"]
            brother = self.pipeline.add_character({"name": "角色14弟弟", "base_prompt": "49岁中国男性，瘦长脸，短黑发，身材高瘦。",
                "episode_id": "EP052", "look": {"name": "灰夹克", "prompt_delta": "灰夹克，白衬衫，深蓝长裤，黑鞋。"}})["character"]
        image.assert_not_called()
        video.assert_not_called()
        enqueue.assert_not_called()
        self.assertRegex(sister["id"], r"^char_[a-f0-9]{12}$")
        self.assertNotEqual(sister["id"], brother["id"])
        self.assertRegex(sister["looks"][0]["id"], r"^look_[a-f0-9]{12}$")
        self.assertNotEqual(sister["looks"][0]["id"], brother["looks"][0]["id"])
        self.assertEqual(sister["aliases"], ["姐姐"])
        self.assertEqual(sister["assets"], [])
        self.assertEqual(brother["assets"], [])
        self.assertIsNot(sister["assets"], brother["assets"])
        self.assertIsNot(sister["selected"], brother["selected"])
        self.assertNotIn("base_look_id", sister, "首服装look不是中性base资产")
        state = self.store.read()
        self.assertEqual(state["episodes"][0]["characters"], before["characters"] + [sister["id"], brother["id"]])
        self.assertEqual(state["episodes"][0]["shots"], before["shots"])
        self.assertEqual(state["episodes"][0]["export"], before["export"])
        self.assertEqual(state["jobs"], [])
        sister["assets"].append({"id": "sister-base"})
        self.assertEqual(brother["assets"], [])

    def test_invalid_parameters_or_unknown_episode_leave_no_partial_character(self):
        valid = {"name": "独立律师甲", "base_prompt": "40岁中国男性，方脸，板寸，壮实体态。"}
        cases = [None, [], valid | {"extra": True}, valid | {"name": 3}, valid | {"name": " "},
            valid | {"name": "名" * 101}, valid | {"base_prompt": False}, valid | {"base_prompt": ""},
            valid | {"base_prompt": "人" * 5001}, valid | {"aliases": "律师"}, valid | {"aliases": [1]},
            valid | {"aliases": [""]}, valid | {"aliases": ["名" * 101]}, valid | {"aliases": ["甲"] * 31},
            valid | {"episode_id": "unknown"}, valid | {"episode_id": ""}, valid | {"episode_id": None},
            valid | {"look": None}, valid | {"look": {}},
            valid | {"look": {"name": "灰西装", "prompt_delta": "完整灰西装。", "base": "pool"}},
            valid | {"look": {"name": False, "prompt_delta": "灰西装。"}},
            valid | {"look": {"name": "灰西装", "prompt_delta": "衣" * 1501}}]
        for request in cases:
            with self.subTest(request_type=type(request).__name__):
                before = self.store.read()
                with self.assertRaises(ValueError):
                    self.pipeline.add_character(request)
                self.assertEqual(self.store.read(), before)

    def test_identity_only_creation_allows_later_independent_outfit(self):
        original_episode = self.store.read()["episodes"][0]
        character = self.pipeline.add_character({"name": "律师乙", "base_prompt": "35岁中国女性，尖下颌，齐肩直发，纤瘦。"})["character"]
        self.assertEqual(character["looks"], [])
        self.assertEqual(self.store.read()["episodes"][0], original_episode)
        look = self.pipeline.add_look({"character_id": character["id"], "name": "庭审西装", "prompt_delta": "深蓝西装白衬衫黑皮鞋。"})["look"]
        stored = self.store.read()["characters"][0]
        self.assertEqual(stored["looks"], [look])
        self.assertEqual(stored["assets"], [])
        self.assertEqual(stored["selected"], {})


if __name__ == "__main__":
    unittest.main()
