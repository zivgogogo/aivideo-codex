from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.dev.check_repository import check


class RepositoryCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-b", "main")
        (self.root / ".gitignore").write_text(".env\nproduction/\n")
        self.git("add", ".gitignore")
        (self.root / "core").mkdir()
        self.secret = "test-only-secret-for-upload-regression"
        (self.root / ".env").write_text("DASHSCOPE_API_KEY=" + self.secret + "\n")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def commit(self):
        self.git("-c", "user.name=Tests", "-c", "user.email=tests@example.invalid",
                 "commit", "-m", "fixture")

    def test_ignored_local_config_and_production_data_are_not_upload_candidates(self):
        (self.root / "production").mkdir()
        (self.root / "production/project.json").write_text(self.secret)
        (self.root / "README.md").write_text("A clean project\n")
        errors, files, _ = check(self.root)
        self.assertEqual(errors, [])
        self.assertEqual(files, 2)

    def test_secret_already_in_history_is_found_after_working_file_is_cleaned(self):
        path = self.root / "core/config.py"
        path.write_text(self.secret)
        self.git("add", "core/config.py")
        self.commit()
        path.write_text("clean\n")
        self.git("add", "core/config.py")
        self.commit()
        errors, _, _ = check(self.root)
        self.assertTrue(any("历史：core/config.py" in error for error in errors))
        self.assertTrue(all(self.secret not in error for error in errors))

    def test_secret_in_staging_is_found_even_after_working_copy_is_cleaned(self):
        path = self.root / "core/config.py"
        path.write_text(self.secret)
        self.git("add", "core/config.py")
        path.write_text("clean\n")
        errors, _, _ = check(self.root)
        self.assertTrue(any("暂存区：core/config.py" in error for error in errors))
        self.assertTrue(all(self.secret not in error for error in errors))

    def test_force_added_local_config_is_rejected_without_printing_its_value(self):
        self.git("add", "--force", ".env")
        errors, _, _ = check(self.root)
        self.assertTrue(any("应排除的文件" in error for error in errors))
        self.assertTrue(all(self.secret not in error for error in errors))

    def test_episode_source_cannot_be_added_under_resources(self):
        path = self.root / "resources/stories/source/EP001.md"
        path.parent.mkdir(parents=True)
        path.write_text("Invented local episode, not application source.")
        self.git("add", str(path.relative_to(self.root)))
        errors, _, _ = check(self.root)
        self.assertTrue(any("应排除的文件" in error for error in errors))

    def test_removed_local_material_is_still_rejected_in_history(self):
        path = self.root / "local/notes.md"
        path.parent.mkdir()
        path.write_text("Local-only notes.")
        self.git("add", "local/notes.md")
        self.commit()
        self.git("rm", "local/notes.md")
        self.commit()
        errors, _, _ = check(self.root)
        self.assertTrue(any("历史包含本地内容" in error for error in errors))
