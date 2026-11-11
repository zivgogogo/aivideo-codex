"""Offline conservation and locking tests for protected source corrections."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from backend.workflow.pipeline import Pipeline
from core.store import Store


def state_fixture():
    anchor = {"id": "EP013_SH0009", "beat_id": "B0009", "source_scene": "13-2 日 内 图书馆入口",
        "source_actions": ["△角色2低头看着书页，双手扶书。", "△快切结束。", "△角色6坐在门旁，低声询问。"],
        "dialogue": [{"speaker": "角色6", "text": "角色1，这本书可以带走吗？", "speech_id": "D0006"}],
        "overlays": [{"kind": "source_caption", "text": "门旁", "start": 0, "end": 3}],
        "duration": 11, "location_id": "boundary", "characters": [{"id": "C006", "look_id": "suit"}],
        "action": "角色6坐在门旁询问。", "camera": "中景固定", "continuity_in": "角色6在门旁。", "continuity_out": "角色6仍在门旁。",
        "prompt": "", "assets": [], "selected": None}
    previous = copy.deepcopy(anchor)
    previous.update(id="EP013_SH0008", beat_id="B0008", source_actions=["△此前的动作。"],
                    assets=[{"id": "completed", "status": "ready", "path": "existing.mp4"}], selected="completed")
    return {"project": {"run_status": "idle"}, "jobs": [],
        "characters": [{"id": "C002", "looks": [{"id": "travel"}]}, {"id": "C006", "looks": [{"id": "suit"}]}],
        "locations": [{"id": "boundary"}, {"id": "cell"}],
        "episodes": [{"id": "EP013", "locations": ["boundary"], "shots": [previous, anchor], "export": {"path": "ep13.mp4"}, "status": "exported"},
                     {"id": "EP014", "locations": ["boundary"], "shots": [], "export": {"path": "ep14.mp4"}, "status": "exported"}],
        "exports": [{"episode_id": "EP013", "path": "ep13.mp4"}, {"episode_id": "EP014", "path": "ep14.mp4"}, {"episode_id": None, "path": "series.mp4"}]}


def request_fixture():
    return {"episode_id": "EP013", "before_shot_id": "EP013_SH0009", "source_action_indices": [0], "duration": 3,
        "location_id": "cell", "characters": [{"id": "C002", "look_id": "travel"}],
        "action": "角色2低头看着书页，双手扶书。", "camera": "中近景固定", "continuity_in": "角色2在阅览室内。", "continuity_out": "他仍低头看书。"}


class InsertReactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.store.save(state_fixture())
        self.pipeline = Pipeline(self.store)

    def tearDown(self):
        self.pipeline.executor.shutdown(wait=True)
        self.temp.cleanup()

    def test_insertion_conserves_dialogue_overlays_and_source_order_and_is_scoped(self):
        before = self.store.read()
        original = before["episodes"][0]
        source_order = [line for shot in original["shots"] for line in shot["source_actions"]]
        result = self.pipeline.insert_reaction(request_fixture())
        state = self.store.read()
        episode = state["episodes"][0]
        self.assertEqual([shot["id"] for shot in episode["shots"]], ["EP013_SH0008", "EP013_SH0009_R01", "EP013_SH0009"])
        reaction, anchor = episode["shots"][1:]
        self.assertEqual(reaction["beat_id"], "B0009_R01")
        self.assertEqual(reaction["source_scene"], original["shots"][1]["source_scene"])
        self.assertEqual(reaction["dialogue"], [])
        self.assertEqual(reaction["overlays"], [])
        self.assertEqual(anchor["dialogue"], original["shots"][1]["dialogue"])
        self.assertEqual(anchor["overlays"], original["shots"][1]["overlays"])
        self.assertEqual([line for shot in episode["shots"] for line in shot["source_actions"]], source_order)
        self.assertIn("cell", episode["locations"])
        self.assertIsNone(episode["export"])
        self.assertEqual(state["episodes"][1], before["episodes"][1])
        self.assertTrue(state["exports"][0]["stale"])
        self.assertNotIn("stale", state["exports"][1])
        self.assertTrue(state["exports"][2]["stale"])
        audit = json.loads((self.root / result["audit_path"]).read_text())
        self.assertEqual(audit["before_episode"], original)
        self.assertEqual(audit["after_episode"], episode)
        self.assertTrue((self.root / "episodes/EP013/storyboard.json").is_file())
        second = self.pipeline.insert_reaction(request_fixture())
        self.assertEqual(second["shot"]["id"], "EP013_SH0009_R02")
        self.assertEqual(self.store.read()["episodes"][0]["shots"][-1]["dialogue"], original["shots"][1]["dialogue"])

    def test_existing_assets_busy_jobs_and_invalid_source_or_cast_are_rejected(self):
        for case in ("assets", "video", "image", "duplicate_indices", "invalid_index", "boolean_index", "nonprefix", "duplicate_cast", "look", "duration", "dialogue"):
            with self.subTest(case=case):
                state, request = state_fixture(), request_fixture()
                anchor = state["episodes"][0]["shots"][1]
                if case == "assets":
                    anchor["assets"] = [{"id": "failed-but-preserved", "status": "failed"}]
                elif case in {"video", "image"}:
                    state["jobs"] = [{"id": "busy", "kind": "video-repair" if case == "video" else "image",
                        "status": "running", "target": ["EP013", anchor["id"]] if case == "video" else ["C002", "travel", "look"]}]
                elif case == "duplicate_indices":request["source_action_indices"] = [0, 0]
                elif case == "invalid_index":request["source_action_indices"] = [0, 1, 2, 3]
                elif case == "boolean_index":request["source_action_indices"] = [False]
                elif case == "nonprefix":request["source_action_indices"] = [1]
                elif case == "duplicate_cast":request["characters"] *= 2
                elif case == "look":request["characters"][0]["look_id"] = "missing"
                elif case == "duration":request["duration"] = 2
                elif case == "dialogue":request["dialogue"] = [{"speaker": "X", "text": "不允许新增"}]
                self.store.save(state)
                with self.assertRaises(ValueError):self.pipeline.insert_reaction(request)
                self.assertEqual(self.store.read()["episodes"], state["episodes"])

    def test_identical_director_edit_adds_locks_without_expiring_existing_media(self):
        before = self.store.read()
        shot = before["episodes"][0]["shots"][0]
        self.pipeline.edit_shot({"episode_id": "EP013", "shot_id": shot["id"], "reason": "已实际核验的单人近景，防止自动回退",
            "changes": {"action": shot["action"], "characters": copy.deepcopy(shot["characters"])}})
        state = self.store.read()
        changed = state["episodes"][0]["shots"][0]
        self.assertEqual(set(changed["creative_locks"]), {"action", "characters"})
        self.assertEqual(changed["assets"], shot["assets"])
        self.assertEqual(state["episodes"][0]["export"], before["episodes"][0]["export"])
        self.assertEqual(state["exports"], before["exports"])
        self.pipeline.edit_shot({"episode_id": "EP013", "shot_id": shot["id"], "lock_creative": False,
                                 "changes": {"camera": shot["camera"]}})
        self.assertEqual(set(self.store.read()["episodes"][0]["shots"][0]["creative_locks"]), {"action", "characters"})
        audits = [json.loads(p.read_text()) for p in (self.root / "episodes/EP013/planning/edits").glob('*.json')]
        self.assertEqual(len(audits), 1)
        self.assertIn("防止自动回退", audits[0]["reason"])
        self.assertEqual(audits[0]["before"], audits[0]["after"])

    def test_new_cast_and_reaction_join_only_target_episode_character_cards(self):
        def add_source_roles(state):
            state["episodes"][0]["characters"] = ["C006"]
            state["episodes"][1]["characters"] = ["C006"]
        self.store.mutate(add_source_roles)
        actor = self.pipeline.add_character({"name": "独立律师甲", "base_prompt": "45岁中国男性，方脸，短发，宽肩体态。",
            "look": {"name": "深灰西装", "prompt_delta": "深灰西装、白衬衫、黑长裤与黑皮鞋。"}})["character"]
        before = self.store.read()
        cast = [{"id": actor["id"], "look_id": actor["looks"][0]["id"]}]
        self.pipeline.edit_shot({"episode_id": "EP013", "shot_id": "EP013_SH0009", "changes": {"characters": cast}})
        edited = self.store.read()
        self.assertEqual(edited["episodes"][0]["characters"], ["C006", actor["id"]])
        self.assertEqual(edited["episodes"][0]["shots"][1]["characters"], cast)
        self.assertEqual(edited["episodes"][1], before["episodes"][1])
        self.assertEqual(edited["exports"][1], before["exports"][1])
        reaction_cast = [{"id": "C002", "look_id": "travel"}]
        result = self.pipeline.insert_reaction(request_fixture() | {"characters": reaction_cast})
        final = self.store.read()
        self.assertEqual(result["shot"]["characters"], reaction_cast)
        self.assertEqual(final["episodes"][0]["characters"], ["C006", actor["id"], "C002"])
        self.assertEqual(final["episodes"][1], before["episodes"][1])
        self.assertEqual(final["exports"][1], before["exports"][1])


if __name__ == '__main__':
    unittest.main()
