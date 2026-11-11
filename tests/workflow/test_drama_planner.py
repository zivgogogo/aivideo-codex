"""Planning invariants that prevent losing an authored serial's actual script."""
import collections
import json
from pathlib import Path
import re
import tempfile
import unicodedata
import unittest

from backend.workflow.planner import (
    PlanningError, _cached_response, _parse_json, _split_speech, _validate_directed,
    parse_beats, source_overlays,
)


from tests.workflow.sample_script import script_fixture


class ScriptCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed = script_fixture()

    def test_synthetic_source_dialogue_survives_in_order(self):
        self.assertEqual(len(self.seed["episodes"]), 2)
        speech_count, character_count = 0, 0
        for episode in self.seed["episodes"]:
            with self.subTest(episode=episode["id"]):
                expected = []
                for raw in episode["source_text"].splitlines():
                    raw = raw.strip()
                    if not raw or raw.startswith(("#", "△", "【", "[", "人物", "（闪", "(闪")):
                        continue
                    match = re.fullmatch(r"([^：:\n]{1,90})[：:]\s*(.*)", raw)
                    if match and match[1].strip() not in {"字幕", "特写", "场景", "时间"} and match[2]:
                        expected.append((match[1], match[2]))
                beats, speeches = parse_beats(episode)
                self.assertEqual([(s["source_speaker"], s["text"]) for s in speeches], expected)
                restored = collections.defaultdict(str)
                for beat in beats:
                    self.assertGreaterEqual(beat["duration"], 3)
                    self.assertLessEqual(beat["duration"], 15)
                    for line in beat["dialogue"]:
                        # Punctuation-only tails belong to the preceding spoken
                        # segment. Any characters beyond 40 must be nonspoken.
                        self.assertTrue(all(ch.isspace() or unicodedata.category(ch).startswith("P")
                                            for ch in line["text"][40:]))
                        self.assertLessEqual(sum(not ch.isspace() and not unicodedata.category(ch).startswith("P")
                                                 for ch in line["text"]), 40)
                        restored[line["speech_id"]] += line["text"]
                self.assertEqual([restored[s["id"]] for s in speeches], [x[1] for x in expected])
                speech_count += len(speeches)
                character_count += sum(len(s["text"]) for s in speeches)
        self.assertEqual(speech_count, 4)
        self.assertEqual(character_count, 101)

    def test_all_explicit_caption_lines_survive_outside_dialogue(self):
        total = 0
        for episode in self.seed["episodes"]:
            expected = []
            for line in episode["source_text"].splitlines():
                expected.extend(re.findall(r"【字幕\s*[：:]\s*([^】]+)】", line))
                match = re.search(r"【字幕】\s*(.+)", line)
                if match:
                    expected.append(match[1].strip())
            beats, _ = parse_beats(episode)
            actual = [o["text"] for b in beats for o in source_overlays(b["source_actions"], b["duration"])]
            self.assertEqual(actual, expected, episode["id"])
            total += len(actual)
        self.assertEqual(total, 2)

    def test_visual_actions_are_preserved_once_in_order(self):
        for episode in self.seed["episodes"]:
            expected = [x.strip() for x in episode["source_text"].splitlines()
                        if x.strip().startswith(("△", "【", "[", "（闪", "(闪"))]
            beats, _ = parse_beats(episode)
            actual = [x for b in beats for x in b["source_actions"]
                      if x.startswith(("△", "【", "[", "（闪", "(闪"))]
            self.assertEqual(actual, expected, episode["id"])

    def test_long_speech_keeps_punctuation_and_characters(self):
        original = "这不是一句可以删掉的台词，" * 8 + "每一个字都要保留。"
        pieces = _split_speech(original)
        self.assertEqual("".join(pieces), original)
        self.assertTrue(all(len(piece) <= 40 for piece in pieces))

    def test_forty_spoken_characters_and_double_ellipsis_keep_one_segment(self):
        original = "字" * 40 + "……"
        pieces = _split_speech(original)
        self.assertEqual("".join(pieces), original)
        self.assertEqual(pieces, [original])
        self.assertTrue(all(any(not ch.isspace() and not unicodedata.category(ch).startswith("P")
                                for ch in piece) for piece in pieces))
        self.assertEqual(sum(not unicodedata.category(ch).startswith("P") for ch in pieces[0]), 40)


class LLMValidationTests(unittest.TestCase):
    def test_eight_character_references_fit_but_nine_leave_no_scene_slot(self):
        characters = [{"id": f"C{i:03}", "looks": [{"id": f"C{i:03}_look"}]} for i in range(1, 10)]
        cast = [{"id": c["id"], "look_id": c["looks"][0]["id"]} for c in characters]
        shot = {"beat_id": "B0001", "location_id": "L001", "characters": cast[:8],
                "camera": "群像中景", "action": "众人看向门口", "continuity_in": "原站位", "continuity_out": "停留反应"}
        self.assertEqual(len(_validate_directed([{"beat_id": "B0001"}], {"shots": [shot]}, characters, [{"id": "L001"}])), 1)
        shot["characters"] = cast
        with self.assertRaises(PlanningError):
            _validate_directed([{"beat_id": "B0001"}], {"shots": [shot]}, characters, [{"id": "L001"}])

    def test_exact_successful_batch_is_reused_but_changed_request_is_not(self):
        shot = {"beat_id": "B0001", "location_id": "L001", "characters": [],
                "camera": "近景", "action": "门缓缓关闭", "continuity_in": "门开着", "continuity_out": "门关闭"}
        request = {"model": "kimi-k3", "messages": [{"role": "user", "content": "不可修改的输入"}], "max_tokens": 8192}
        response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"shots": [shot]})}}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prior = root / "v_20260101"
            prior.mkdir()
            (prior / "batch-001.request.json").write_text(json.dumps(request))
            (prior / "batch-001.response.json").write_text(json.dumps(response))
            cached, origin = _cached_response(root, request, [{"beat_id": "B0001"}], [], [{"id": "L001"}])
            self.assertEqual(cached, response)
            self.assertEqual(origin, prior / "batch-001.response.json")
            changed = {**request, "model": "other-model"}
            self.assertEqual(_cached_response(root, changed, [{"beat_id": "B0001"}], [], [{"id": "L001"}]), (None, None))
            response["choices"][0]["finish_reason"] = "length"
            (prior / "batch-001.response.json").write_text(json.dumps(response))
            self.assertEqual(_cached_response(root, request, [{"beat_id": "B0001"}], [], [{"id": "L001"}]), (None, None))

    def test_truncated_json_is_an_explicit_failure(self):
        with self.assertRaises(PlanningError):
            _parse_json('{"shots":[{"beat_id":"B0001"}')

    def test_missing_or_reordered_beat_cannot_be_accepted(self):
        with self.assertRaises(PlanningError):
            _validate_directed([{"beat_id": "B0001"}, {"beat_id": "B0002"}],
                               {"shots": [{"beat_id": "B0002"}]}, [], [])

    def test_another_characters_look_cannot_be_accepted(self):
        shot = {"beat_id": "B0001", "location_id": "L001",
                "characters": [{"id": "C001", "look_id": "C002_travel"}],
                "camera": "中近景", "action": "抬眼", "continuity_in": "低头", "continuity_out": "抬眼"}
        with self.assertRaises(PlanningError):
            _validate_directed([{"beat_id": "B0001"}], {"shots": [shot]},
                               [{"id": "C001", "looks": [{"id": "C001_doctor"}]}], [{"id": "L001"}])


if __name__ == "__main__":
    unittest.main()
