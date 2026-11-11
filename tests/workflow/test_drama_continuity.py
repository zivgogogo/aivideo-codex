"""Offline transaction/repair tests; no credentials or provider calls are used."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ai.llm.client import LLMAPIError, LLMTransportError
from ai.llm.config import LLMConfig
from backend.workflow.continuity import ContinuityReviewError, review_episode


def response(patches=None, *, text=None, finish="stop"):
    content = text if text is not None else json.dumps({"patches": patches or [], "notes": []}, ensure_ascii=False)
    return {"choices": [{"finish_reason": finish, "message": {"content": content}}]}


class FakeClient:
    def __init__(self, replies):
        self.config = LLMConfig(model="kimi-k3", api_key="", temperature=0.7)
        self.chat = Mock(side_effect=replies)

    def build_payload(self, messages, **options):
        payload = {"model": self.config.model, "messages": copy.deepcopy(messages), "stream": False, **options}
        if self.config.temperature is not None:
            payload["temperature"] = self.config.temperature
        return payload


def fixture(shot_count=1):
    shots = [{"id": f"EP002_SH{i:04}", "duration": 7, "source_scene": "2-1 日 内 急诊大厅",
              "source_actions": ["△医护站在门旁。"], "dialogue": [{"speaker": "角色1", "text": "先保持冷静。", "voiceover": True}],
              "overlays": [], "action": "医护看向门口。", "camera": "固定中景", "continuity_in": "门关闭。",
              "continuity_out": "人物原位。", "location_id": "L001", "characters": [{"id": "C001", "look_id": "C001_doctor"}],
              "assets": [{"id": "existing-asset", "status": "ready"}], "selected": "existing-asset"}
             for i in range(1, shot_count + 1)]
    episode = {"id": "EP002", "number": 2, "source_text": "### 2-1 日 内 急诊大厅\n角色1VO：先保持冷静。", "shots": shots}
    state = {"project": {"visual_style": "自然肤质、冷白医院灯光"},
             "characters": [{"id": "C001", "name": "角色1", "looks": [{"id": "C001_doctor", "name": "医生"}]},
                            {"id": "G002", "name": "保安", "looks": [{"id": "G002_default", "name": "制服"}]}],
             "locations": [{"id": "L001", "name": "急诊大厅"}],
             "episodes": [{"id": "EP001", "number": 1, "source_text": "上一集的原文"}, episode,
                          {"id": "EP003", "number": 3, "source_text": "下一集的原文"}]}
    return state, episode


class ReviewRepairTests(unittest.TestCase):
    def run_review(self, directory, state, episode, client):
        original = copy.deepcopy(state)
        with patch("backend.workflow.continuity.DashScopeLLMClient", return_value=client):
            result = review_episode(Path(directory), state, episode)
        self.assertEqual(state, original)
        for before, after in zip(episode["shots"], result["episode"]["shots"]):
            for field in ["id", "duration", "dialogue", "assets", "selected"]:
                self.assertEqual(before[field], after[field])
        return result

    def test_director_camera_choice_cannot_be_undone_by_episode_review(self):
        state, episode = fixture()
        shot = episode["shots"][0]
        shot["creative_locks"] = ["camera", "characters"]
        bad = {"shot_id": shot["id"], "reason": "希望重新入画背景人物", "changes": {"camera": "重新改成三人全景"}}
        client = FakeClient([response([bad]), response()])
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_review(directory, state, episode, client)
        self.assertEqual(result["patches"], [])
        self.assertEqual(result["episode"]["shots"][0]["camera"], "固定中景")
        self.assertIn("creative_locks", client.chat.call_args_list[0].args[0][1]["content"])
        repair = json.loads(client.chat.call_args_list[1].args[0][-1]["content"])
        self.assertEqual(repair["protected_creative_fields"][shot["id"]], ["camera", "characters"])

    def test_adopted_real_clip_is_preserved_but_stale_clip_can_be_revised(self):
        for stale in (False, True):
            with self.subTest(stale=stale), tempfile.TemporaryDirectory() as directory:
                state, episode = fixture()
                shot = episode["shots"][0]
                shot["assets"][0].update(kind="video", path="clip.mp4", stale=stale, qa={"technical_pass": True})
                patch_value = {"shot_id": shot["id"], "reason": "更换动作设计", "changes": {"action": "重新设计人物动作。"}}
                client = FakeClient([response([patch_value])] if stale else [response([patch_value]), response()])
                result = self.run_review(directory, state, episode, client)
                self.assertEqual(bool(result["patches"]), stale)
                self.assertEqual(client.chat.call_count, 1 if stale else 2)

    def test_completed_review_reuses_unused_catalog_changes_but_not_used_look_changes(self):
        state, episode = fixture()
        with tempfile.TemporaryDirectory() as directory:
            first = self.run_review(directory, state, episode, FakeClient([response()]))
            episode["continuity_review"] = first["review"]
            state["locations"].append({"id": "NEW-HOME", "name": "后续家庭场景"})
            state["characters"][0]["looks"].append({"id": "C001_future", "name": "后续壮年"})
            client = FakeClient([])
            cached = self.run_review(directory, state, episode, client)
            self.assertTrue(cached["review"]["reused_completed_review"])
            client.chat.assert_not_called()
            state["characters"][0]["looks"][0]["prompt_delta"] = "已修改当前采用服装。"
            changed = FakeClient([response()])
            self.run_review(directory, state, episode, changed)
            self.assertEqual(changed.chat.call_count, 1)

    def test_legacy_full_catalog_cache_requires_matching_used_evidence(self):
        state, episode = fixture()
        with tempfile.TemporaryDirectory() as directory:
            first = self.run_review(directory, state, episode, FakeClient([response()]))
            episode["continuity_review"] = {**first["review"], "output_fingerprint": "legacy-full-catalog-hash"}
            state["locations"].append({"id": "NEW-HOME", "name": "与本集无关"})
            client = FakeClient([])
            result = self.run_review(directory, state, episode, client)
            self.assertTrue(result["review"]["reused_compatible_context"])
            client.chat.assert_not_called()

    def test_out_of_scope_next_shot_is_removed_in_repair(self):
        state, episode = fixture(5)
        bad = {"shot_id": "EP002_SH0005", "reason": "误把next_shot当本批范围", "changes": {"action": "不应在本批应用"}}
        client = FakeClient([response([bad]), response(), response()])
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_review(directory, state, episode, client)
            self.assertEqual(client.chat.call_count, 3)  # Two replies for batch 1; one for batch 2.
            first = result["review"]["batches"][0]
            self.assertEqual(first["repair_count"], 1)
            self.assertEqual(first["responses_received"], 2)
            repair_messages = client.chat.call_args_list[1].args[0]
            constraint = json.loads(repair_messages[-1]["content"])
            self.assertEqual(constraint["allowed_shot_ids"], [f"EP002_SH{i:04}" for i in range(1, 5)])
            self.assertIn("未知或重复镜头", constraint["validation_error"])
            self.assertEqual(result["patches"], [])

    def test_duplicate_cast_is_repaired_and_valid_action_intent_is_preserved(self):
        state, episode = fixture()
        cast = {"id": "G002", "look_id": "G002_default"}
        action = "保安站在门旁，示意人群停下。"
        bad = {"shot_id": "EP002_SH0001", "reason": "保安维持门口秩序", "changes": {"characters": [cast, cast], "action": action}}
        fixed = {**bad, "changes": {"characters": [cast], "action": action}}
        client = FakeClient([response([bad]), response([fixed])])
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_review(directory, state, episode, client)
            self.assertEqual(client.chat.call_count, 2)
            self.assertEqual(result["episode"]["shots"][0]["characters"], [cast])
            self.assertEqual(result["episode"]["shots"][0]["action"], action)
            provenance = result["review"]["batches"][0]
            self.assertTrue(provenance["accepted_request_from"].endswith(".repair-01.request.json"))
            folder = Path(directory) / result["review"]["version_path"]
            for name in ["batch-001.base.request.json", "batch-001.base.response.json", "batch-001.repair-01.request.json",
                         "batch-001.repair-01.response.json", "batch-001.request.json", "batch-001.response.json", "batch-001.metadata.json"]:
                self.assertTrue((folder / name).is_file(), name)
            self.assertNotIn("temperature", json.loads((folder / "batch-001.repair-01.request.json").read_text()))
            client.chat.reset_mock(side_effect=True)
            client.chat.side_effect = AssertionError("cache must prevent all network calls")
            cached = self.run_review(directory, state, episode, client)
            self.assertEqual(client.chat.call_count, 0)
            self.assertTrue(cached["review"]["batches"][0]["reused_from"])
            self.assertTrue(cached["review"]["batches"][0]["accepted_request_from"].endswith(".repair-01.request.json"))

    def test_three_invalid_replies_fail_without_mutating_input(self):
        state, episode = fixture()
        bad = {"shot_id": "EP002_SH9999", "reason": "范围错", "changes": {"action": "错误镜头"}}
        client = FakeClient([response([bad]), response([bad]), response([bad])])
        before = copy.deepcopy(state)
        with tempfile.TemporaryDirectory() as directory:
            with patch("backend.workflow.continuity.DashScopeLLMClient", return_value=client):
                with self.assertRaisesRegex(ContinuityReviewError, "最多3个响应"):
                    review_episode(Path(directory), state, episode)
            self.assertEqual(client.chat.call_count, 3)
            self.assertEqual(state, before)
            folder = next(Path(directory).glob("episodes/EP002/planning/review_*"))
            self.assertFalse((folder / "batch-001.response.json").exists())
            meta = json.loads((folder / "batch-001.metadata.json").read_text())
            self.assertFalse(meta["accepted"])
            self.assertEqual(len(meta["attempts"]), 3)

    def test_complete_invalid_json_is_repaired(self):
        state, episode = fixture()
        client = FakeClient([response(text='{"patches":'), response()])
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_review(directory, state, episode, client)
            self.assertEqual(client.chat.call_count, 2)
            self.assertEqual(result["review"]["batches"][0]["repair_count"], 1)

    def test_network_and_api_errors_are_never_retried(self):
        for error in [LLMTransportError("offline transport failure"),
                      LLMAPIError({"error": {"code": "AccessDenied", "message": "offline auth failure"}}, 403)]:
            with self.subTest(error=type(error).__name__):
                state, episode = fixture()
                client = FakeClient(error)
                before = copy.deepcopy(state)
                with tempfile.TemporaryDirectory() as directory:
                    with patch("backend.workflow.continuity.DashScopeLLMClient", return_value=client):
                        with self.assertRaises(ContinuityReviewError):
                            review_episode(Path(directory), state, episode)
                    self.assertEqual(client.chat.call_count, 1)
                    self.assertEqual(state, before)

    def test_incomplete_response_is_not_structural_repair(self):
        state, episode = fixture()
        client = FakeClient([response(text='{"patches":', finish="length")])
        with tempfile.TemporaryDirectory() as directory:
            with patch("backend.workflow.continuity.DashScopeLLMClient", return_value=client):
                with self.assertRaisesRegex(ContinuityReviewError, "未完整结束"):
                    review_episode(Path(directory), state, episode)
            self.assertEqual(client.chat.call_count, 1)


if __name__ == "__main__":
    unittest.main()
