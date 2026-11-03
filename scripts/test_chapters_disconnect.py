import http.client
import importlib.util
import json
import tempfile
import types
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

repo = types.ModuleType("repo_state")
repo.STATE_DIR = Path(".")
repo.commit_paths = lambda *args, **kwargs: None
spec = importlib.util.spec_from_file_location("chapters_under_test", Path(__file__).with_name("chapters.py"))
chapters = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"kick_api": types.ModuleType("kick_api"), "repo_state": repo}):
    spec.loader.exec_module(chapters)

class GeminiRollbackTests(unittest.TestCase):
    def test_remote_disconnect_rolls_back_attempt_and_commits(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            state = {"uuid": "test-uuid", "chapters_attempts": 1}
            with patch.object(chapters, "gemini_chapters", side_effect=http.client.RemoteDisconnected("closed")), patch.object(chapters, "commit_paths") as commit:
                result = chapters._gemini_with_rollback(path, state, [], {}, "key", False)
            self.assertIsNone(result)
            self.assertEqual(state["chapters_attempts"], 0)
            self.assertEqual(json.loads(path.read_text())["chapters_attempts"], 0)
            commit.assert_called_once()
    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            state = {"chapters_attempts": 1}
            with patch.object(chapters, "gemini_chapters", side_effect=http.client.RemoteDisconnected()), patch.object(chapters, "commit_paths") as commit:
                self.assertIsNone(chapters._gemini_with_rollback(path, state, [], {}, "key", True))
            self.assertEqual(state["chapters_attempts"], 1)
            self.assertFalse(path.exists())
            commit.assert_not_called()
    def test_http_400_is_not_swallowed(self):
        error = urllib.error.HTTPError("https://example.invalid", 400, "bad", {}, None)
        with patch.object(chapters, "gemini_chapters", side_effect=error):
            with self.assertRaises(urllib.error.HTTPError):
                chapters._gemini_with_rollback(Path("unused"), {"chapters_attempts": 1}, [], {}, "key", False)
    def test_success_is_returned_without_mutation(self):
        state = {"chapters_attempts": 1}
        with patch.object(chapters, "gemini_chapters", return_value="ok"), patch.object(chapters, "commit_paths") as commit:
            self.assertEqual(chapters._gemini_with_rollback(Path("unused"), state, [], {}, "key", False), "ok")
        self.assertEqual(state["chapters_attempts"], 1)
        commit.assert_not_called()

if __name__ == "__main__":
    unittest.main()
