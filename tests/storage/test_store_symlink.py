"""Real temporary symlinks: missing linked projects must never be reseeded."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from core.store import Store, safe_path


class LinkedProjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.target = self.base / "外盘 试验" / "projects" / "default"
        self.link = self.base / "workspace-project"
        self.state = {"schema_version": 1, "project": {"id": "default", "run_status": "paused"},
                      "characters": [], "locations": [], "episodes": [], "jobs": [], "exports": []}

    def tearDown(self):
        self.temp.cleanup()

    def _link(self, with_project=True):
        self.target.mkdir(parents=True)
        if with_project:
            Store(self.target).save(copy.deepcopy(self.state))
        self.link.symlink_to(self.target, target_is_directory=True)
        return Store(self.link)

    def _assert_blocked(self, store):
        callback = Mock()
        supplied_state = copy.deepcopy(self.state)
        before = copy.deepcopy(supplied_state)
        for operation in (lambda: store.initialize(self.base / "nonexistent-seed.json"),
                          store.read, lambda: store.save(supplied_state), lambda: store.mutate(callback)):
            with self.assertRaisesRegex(RuntimeError, "外盘或链接项目未就绪"):
                operation()
        callback.assert_not_called()
        self.assertEqual(supplied_state, before)

    def test_linked_project_reads_saves_mutates_and_keeps_media_paths_inside_target(self):
        store = self._link()
        self.assertEqual(store.input_root, self.link)
        self.assertEqual(store.root, self.target.resolve())
        self.assertEqual(store.initialize()["project"], self.state["project"])
        store.mutate(lambda state: state.update(counter=1))
        self.assertEqual(json.loads((self.target / "project.json").read_text())["counter"], 1)
        self.assertEqual(safe_path(self.link, "assets/scene.png"), self.target.resolve() / "assets/scene.png")
        with self.assertRaises(ValueError):
            safe_path(self.link, "../outside.png")

    def test_link_target_without_project_json_cannot_initialize_or_save_seed(self):
        store = self._link(with_project=False)
        self._assert_blocked(store)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_dangling_external_project_symlink_is_never_recreated(self):
        self.link.symlink_to(self.target, target_is_directory=True)
        store = Store(self.link)
        self._assert_blocked(store)
        self.assertFalse(self.target.exists())

    def test_disconnected_link_after_construction_blocks_writes_without_recreating_target(self):
        store = self._link()
        original = (self.target / "project.json").read_bytes()
        parked = self.target.with_name("detached-original")
        self.target.rename(parked)  # Simulates an unavailable volume using only a temporary directory.
        self._assert_blocked(store)
        self.assertFalse(self.target.exists())
        self.assertEqual((parked / "project.json").read_bytes(), original)

    def test_normal_nonlinked_new_project_can_still_initialize(self):
        seed = self.base / "seed.json"
        seed.write_text(json.dumps({"project": {"title": "新项目"}, "characters": [], "locations": [], "episodes": []}))
        store = Store(self.base / "ordinary-new-project")
        state = store.initialize(seed)
        self.assertEqual(state["project"]["title"], "新项目")
        self.assertTrue(store.path.is_file())


if __name__ == "__main__":
    unittest.main()
