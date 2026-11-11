import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from core.paths import PROJECT_ROOT
from scripts.setup_project import initialize, environment_python


class SetupProjectTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for key in list(os.environ):
            if key.startswith("AIVIDEO_"):
                os.environ.pop(key)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ["requirements.txt", ".env.example"]:
            shutil.copy2(PROJECT_ROOT / name, self.root / name)
        for name in ["backend", "core", "resources"]:
            shutil.copytree(PROJECT_ROOT / name, self.root / name, ignore=shutil.ignore_patterns("__pycache__"))
        # Use the test interpreter for offline CLI initialization, without pip or downloads.
        python = environment_python(self.root)
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)

    def initialize(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            initialize(self.root, install_dependencies=False, **kwargs)

    def test_new_checkout_initializes_and_rerun_preserves_configuration_and_state(self):
        self.initialize()
        env = self.root / ".env"
        state = self.root / "production/projects/default/project.json"
        self.assertTrue(state.is_file())
        self.assertEqual(json.loads(state.read_text())["episodes"], [])
        self.assertEqual(json.loads(state.read_text())["characters"], [])
        self.assertEqual(env.read_bytes(), (self.root / ".env.example").read_bytes())
        if os.name != "nt":
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        env.write_text("DASHSCOPE_API_KEY=preserve-local-configuration\n")
        before = state.read_bytes()
        self.initialize()
        self.assertEqual(env.read_text(), "DASHSCOPE_API_KEY=preserve-local-configuration\n")
        self.assertEqual(state.read_bytes(), before)

    def test_missing_external_project_stays_a_dangling_link(self):
        project = self.root / "production/projects/default"
        project.parent.mkdir(parents=True)
        target = self.root / "unmounted-volume/project"
        project.symlink_to(target)
        self.initialize()
        self.assertTrue(project.is_symlink())
        self.assertEqual(project.readlink(), target)
        self.assertFalse(target.exists())

    def test_nonempty_partial_project_is_not_initialized_over_existing_files(self):
        project = self.root / "production/projects/default"
        project.mkdir(parents=True)
        sentinel = project / "existing-data.txt"
        sentinel.write_text("keep")
        with self.assertRaisesRegex(RuntimeError, "缺少 project.json"):
            self.initialize()
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertFalse((project / "project.json").exists())

    def test_environment_only_setup_leaves_template_project_uncreated(self):
        self.initialize(create_project=False)
        self.assertTrue((self.root / "production/canvas-projects").is_dir())
        self.assertFalse((self.root / "production/projects/default").exists())
