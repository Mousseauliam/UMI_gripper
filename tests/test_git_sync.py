import subprocess
import tempfile
import unittest
from pathlib import Path
from deploy.git_sync import sync


class GitSyncTests(unittest.TestCase):
    def test_clone_update_and_preserve_local_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, checkout = root / "source", root / "checkout"
            source.mkdir()

            def git(*args):
                return subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)

            git("init", "-b", "main")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            file = source / "sample.txt"
            file.write_text("one")
            git("add", ".")
            git("commit", "-m", "first")
            self.assertTrue(sync(str(source), "main", checkout).startswith("cloned"))
            file.write_text("two")
            git("commit", "-am", "second")
            self.assertTrue(sync(str(source), "main", checkout).startswith("updated"))
            self.assertEqual((checkout / "sample.txt").read_text(), "two")
            self.assertEqual(sync(str(source), "main", checkout), "up to date")
            (checkout / "sample.txt").write_text("local")
            with self.assertRaisesRegex(RuntimeError, "Local changes"):
                sync(str(source), "main", checkout)
            self.assertEqual((checkout / "sample.txt").read_text(), "local")
