"""No-network regression tests for the BridgeClip hourly artifact."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import hourly_pack
import plan
from hourly_pack import build_index, build_pack, candidate_board, segment_entry, write_index, write_pack


class HourlyPackTest(unittest.TestCase):
    def setUp(self):
        self.segments = [
            {"idx": 0, "start": 0, "end": 3600},
            {"idx": 1, "start": 3600, "end": 7200},
            {"idx": 2, "start": 7200, "end": 7800},
        ]
        self.meta = {"slug": "zingisukan2525", "uuid": "vod-id",
                     "segments": self.segments}
        self.rows = [{"rel": 3599.999, "content": "前"},
                     {"rel": 3600, "content": "[emote:1:Pog]ｗｗ"},
                     {"rel": 7200, "content": "末尾"},
                     {"rel": 7800, "content": "範囲外"}]

    def test_boundaries_and_relative_time(self):
        packs = [build_pack(self.meta, self.rows, i) for i in range(3)]
        self.assertEqual([len(p["messages"]) for p in packs], [1, 1, 1])
        self.assertEqual(packs[0]["messages"][0]["seconds"], 3599.999)
        self.assertEqual(packs[1]["messages"][0]["seconds"], 0)
        self.assertEqual(packs[1]["messages"][0]["text"], "Pogｗｗ")
        self.assertEqual(packs[2]["sourceStartSeconds"], 7200)
        self.assertEqual(packs[2]["durationSeconds"], 600)

    def test_empty_and_reject_other_channel(self):
        self.assertEqual(build_pack(self.meta, [], 0)["messages"], [])
        near_edge = build_pack(self.meta, [{"rel": 3599.9998, "content": "境界前"}], 0)
        self.assertLess(near_edge["messages"][0]["seconds"], 3600)
        with self.assertRaises(ValueError):
            build_pack({**self.meta, "slug": "other"}, self.rows, 0)

    def test_candidate_board_schema(self):
        board = candidate_board(build_pack(self.meta, self.rows, 1))
        self.assertEqual(len(board["cands"]), 12)
        self.assertEqual(board["cands"][0],
                         {"start": 0, "end": 300, "score": 0.65, "rel": 0.2})

    def test_index_ranks_full_hours_before_hot_short_tail(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            rows = ([{"rel": 10, "content": "通常"}]
                    + [{"rel": 3600 + i, "content": "www"} for i in range(10)]
                    + [{"rel": 7200 + i, "content": "www"} for i in range(50)])
            meta = {**self.meta, "title": "試験アーカイブ",
                    "url": "https://kick.com/zingisukan2525/videos/vod-id"}
            entries = []
            for hour in range(3):
                pack = build_pack(meta, rows, hour)
                entries.append(segment_entry(pack, candidate_board(pack), hour))
            entries[1]["durationSeconds"] = 3599.9  # ffprobe frame rounding, still a planned full hour
            index = build_index(meta, entries, "12345", require_all=True)
            self.assertEqual(index["runId"], 12345)
            self.assertEqual([hour["index"] for hour in index["segments"]], [1, 0, 2])
            self.assertEqual(index["segments"][0]["artifactName"], "seg-1")
            self.assertEqual(index["segments"][0]["commentsFile"],
                             "seg_001-comments.json")
            self.assertEqual(index["segments"][0]["wwwCount"], 10)
            board = candidate_board(build_pack(meta, rows, 1))["cands"]
            best = sorted((entry["score"] for entry in board), reverse=True)[:3]
            expected_score = round(sum(entry["score"] for entry in board) / len(board)
                                   + sum(best) / len(best), 3)
            self.assertEqual(index["segments"][0]["score"], expected_score)
            meta_path = directory / "meta.json"
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            for entry in entries:
                folder = directory / "hours" / f'bridgeclip-hour-{entry["index"]}'
                folder.mkdir(parents=True)
                (folder / f'seg_{entry["index"]:03d}-index.json').write_text(
                    json.dumps(entry), encoding="utf-8")
            out = directory / "bridgeclip-index.json"
            self.assertEqual(write_index(meta_path, directory / "hours", "12345", out, True), 3)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), index)

    def test_index_accepts_empty_comments_and_requires_all_only_for_trial(self):
        meta = {**self.meta, "title": "archive"}
        packs = [build_pack(meta, [], hour) for hour in range(3)]
        entries = [segment_entry(pack, candidate_board(pack), hour)
                   for hour, pack in enumerate(packs)]
        index = build_index(meta, entries, "12345", require_all=True)
        self.assertEqual([hour["score"] for hour in index["segments"]], [0, 0, 0])
        self.assertEqual([hour["commentCount"] for hour in index["segments"]], [0, 0, 0])
        self.assertEqual(len(build_index(meta, entries[:1], "12345")["segments"]), 1)
        with self.assertRaisesRegex(ValueError, "receipts are missing"):
            build_index(meta, entries[:1], "12345", require_all=True)
        with self.assertRaisesRegex(ValueError, "run id"):
            build_index(meta, entries, "not-an-id")

    def test_receipt_uses_final_actual_comment_count(self):
        with tempfile.TemporaryDirectory() as td:
            paths = self._fixtures(Path(td))
            entry = Path(td) / "seg_001-index.json"
            paths[1].write_text(json.dumps({"rel": 7199.5, "content": "映像外"}) + "\n",
                                encoding="utf-8")
            with patch.object(hourly_pack.subprocess, "run",
                              return_value=SimpleNamespace(stdout="3599.0\n")):
                self.assertEqual(write_pack(*paths[:2], 1, *paths[2:], entry), 0)
            receipt = json.loads(entry.read_text(encoding="utf-8"))
            self.assertEqual(receipt["commentCount"], 0)
            self.assertEqual(receipt["durationSeconds"], 3599)
            listed = build_index({**self.meta, "title": "archive"}, [receipt], "12345")
            self.assertEqual(listed["segments"][0]["commentCount"], 0)

    def test_saved_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            (directory / "meta.json").write_text(json.dumps(self.meta), encoding="utf-8")
            (directory / "chat.jsonl").write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in self.rows) + "\n",
                encoding="utf-8")
            comments = directory / "seg_001-comments.json"
            candidates = directory / "seg_001-candidates.json"
            video = directory / "seg_001.mp4"
            video.write_bytes(b"fake video; ffprobe is mocked")
            with patch.object(hourly_pack.subprocess, "run",
                              return_value=SimpleNamespace(stdout="3600.5\n")) as probe:
                self.assertEqual(write_pack(directory / "meta.json", directory / "chat.jsonl",
                                            1, video, comments, candidates), 1)
                self.assertIn(str(video), probe.call_args.args[0])
            saved = json.loads(comments.read_text(encoding="utf-8"))
            self.assertEqual(saved["messages"][0]["id"], "chat-line-2")
            self.assertEqual(saved["messages"][0]["user"], "")
            self.assertEqual(saved["durationSeconds"], 3600.5)
            self.assertEqual(json.loads(candidates.read_text(encoding="utf-8")),
                             candidate_board(saved))

    def _fixtures(self, directory):
        meta_path = directory / "meta.json"
        chat_path = directory / "chat.jsonl"
        video = directory / "seg_001.mp4"
        comments = directory / "seg_001-comments.json"
        candidates = directory / "seg_001-candidates.json"
        meta_path.write_text(json.dumps(self.meta), encoding="utf-8")
        chat_path.write_text(json.dumps(self.rows[1], ensure_ascii=False) + "\n",
                             encoding="utf-8")
        video.write_bytes(b"fake video; ffprobe is mocked")
        return meta_path, chat_path, video, comments, candidates

    def test_duration_mismatch_does_not_distribute_pair(self):
        with tempfile.TemporaryDirectory() as td:
            paths = self._fixtures(Path(td))
            with patch.object(hourly_pack.subprocess, "run",
                              return_value=SimpleNamespace(stdout="3597.9\n")):
                with self.assertRaisesRegex(ValueError, "duration mismatch"):
                    write_pack(paths[0], paths[1], 1, paths[2], paths[3], paths[4])
            self.assertFalse(paths[3].exists())
            self.assertFalse(paths[4].exists())


    def test_short_video_drops_out_of_range_tail_comment(self):
        with tempfile.TemporaryDirectory() as td:
            paths = self._fixtures(Path(td))
            paths[1].write_text(json.dumps({"rel": 7199.5, "content": "映像外"}) + "\n",
                                encoding="utf-8")
            with patch.object(hourly_pack.subprocess, "run",
                              return_value=SimpleNamespace(stdout="3599.0\n")):
                self.assertEqual(write_pack(paths[0], paths[1], 1,
                                            paths[2], paths[3], paths[4]), 0)
            pack = json.loads(paths[3].read_text(encoding="utf-8"))
            self.assertEqual(pack["durationSeconds"], 3599)
            self.assertEqual(pack["messages"], [])

    def test_partial_write_is_cleaned(self):
        with tempfile.TemporaryDirectory() as td:
            paths = self._fixtures(Path(td))
            real_write = hourly_pack._atomic_json

            def write_then_fail(dest, value):
                real_write(dest, value)
                if Path(dest) == paths[4]:
                    raise OSError("simulated failure after writing candidates")

            with patch.object(hourly_pack, "probe_video_duration", return_value=3600.0):
                with patch.object(hourly_pack, "_atomic_json", side_effect=write_then_fail):
                    with self.assertRaisesRegex(OSError, "simulated failure"):
                        write_pack(paths[0], paths[1], 1, paths[2], paths[3], paths[4])
            self.assertFalse(paths[3].exists())
            self.assertFalse(paths[4].exists())

    def test_malformed_written_json_is_cleaned(self):
        with tempfile.TemporaryDirectory() as td:
            paths = self._fixtures(Path(td))
            real_write = hourly_pack._atomic_json

            def corrupt_candidates(dest, value):
                real_write(dest, {"not-cands": []} if Path(dest) == paths[4] else value)

            with patch.object(hourly_pack, "probe_video_duration", return_value=3600.0):
                with patch.object(hourly_pack, "_atomic_json", side_effect=corrupt_candidates):
                    with self.assertRaisesRegex(ValueError, "does not match"):
                        write_pack(paths[0], paths[1], 1, paths[2], paths[3], paths[4])
            self.assertFalse(paths[3].exists())
            self.assertFalse(paths[4].exists())


class TrialPlanTest(unittest.TestCase):
    def test_normal_mode_still_commits_emote_cache(self):
        with tempfile.TemporaryDirectory() as td:
            meta = {"title": "通常", "duration_s": 3600, "start_time": "2026-09-30T00:00:00Z",
                    "channel_id": 123, "is_live": False, "source": "https://example.test/vod.m3u8"}
            args = ["plan.py", "zingisukan2525", "vod-id", "--out", td]
            with (patch.object(sys, "argv", args),
                  patch.object(plan, "resolve_meta_any", return_value=meta),
                  patch.object(plan.chat_fetch, "fetch_all_chat", return_value=[]),
                  patch.object(plan.emotes_mod, "collect_ids", return_value=[]),
                  patch.object(plan.emotes_mod, "download_missing", return_value=["new"]),
                  patch("repo_state.commit_paths") as commit):
                plan.main()
            commit.assert_called_once()

    def test_trial_does_not_commit_emote_cache(self):
        with tempfile.TemporaryDirectory() as td:
            meta = {"title": "試験", "duration_s": 3600, "start_time": "2026-09-30T00:00:00Z",
                    "channel_id": 123, "is_live": False, "source": "https://example.test/vod.m3u8"}
            args = ["plan.py", "zingisukan2525", "vod-id", "--out", td,
                    "--trial-mode", "true"]
            with (patch.object(sys, "argv", args),
                  patch.object(plan, "resolve_meta_any", return_value=meta),
                  patch.object(plan.chat_fetch, "fetch_all_chat", return_value=[(1.0, "www")]),
                  patch.object(plan.emotes_mod, "collect_ids", return_value=[]),
                  patch.object(plan.emotes_mod, "download_missing", return_value=["new"]),
                  patch("repo_state.commit_paths") as commit):
                plan.main()
            commit.assert_not_called()
            output = json.loads((Path(td) / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(output["segments"], [{"idx": 0, "start": 0, "end": 3600}])

    def test_trial_rejects_missing_source_without_state_write(self):
        with tempfile.TemporaryDirectory() as td:
            meta = {"title": "試験", "duration_s": 3600, "start_time": "2026-09-30T00:00:00Z",
                    "channel_id": 123, "is_live": False, "source": None}
            args = ["plan.py", "zingisukan2525", "vod-id", "--out", td,
                    "--trial-mode", "true"]
            with (patch.object(sys, "argv", args),
                  patch.object(plan, "resolve_meta_any", return_value=meta),
                  patch("repo_state.commit_paths") as commit):
                with self.assertRaisesRegex(RuntimeError, "state was not changed"):
                    plan.main()
            commit.assert_not_called()
            self.assertFalse((Path(td) / "meta.json").exists())


if __name__ == "__main__":
    unittest.main()
