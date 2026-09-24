"""Dispatch one YouTube-based TikTok shorts job after a successful process run.

This module validates the upstream workflow artifact and committed state before
it calls GitHub's workflow_dispatch API through the GitHub CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID


TARGET_SLUG = "zingisukan2525"
DOWNSTREAM_WORKFLOW = "shorts_prep.yml"
YOUTUBE_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}\Z")


class DispatchError(ValueError):
    """Input is incomplete, inconsistent, or unsafe to dispatch."""


def canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise DispatchError("invalid source identifier")
    try:
        normalized = str(UUID(value))
    except (ValueError, AttributeError):
        raise DispatchError("invalid source identifier") from None
    if value != normalized:
        raise DispatchError("invalid source identifier")
    return normalized


def youtube_video_id(value: object) -> str:
    """Accept a bare YouTube watch/short URL and return its video ID."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme != "https" or parsed.username or parsed.password or port:
        return ""
    host = (parsed.hostname or "").lower()
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parsed.path != "/watch" or parsed.fragment:
            return ""
        query = parse_qs(parsed.query, keep_blank_values=True)
        if set(query) != {"v"} or len(query["v"]) != 1:
            return ""
        video_id = query["v"][0]
    elif host in {"youtu.be", "www.youtu.be"}:
        if parsed.query or parsed.fragment:
            return ""
        pieces = parsed.path.strip("/").split("/")
        if len(pieces) != 1:
            return ""
        video_id = pieces[0]
    else:
        return ""
    return video_id if YOUTUBE_ID_RE.fullmatch(video_id) else ""


def canonical_youtube_url(value: object) -> str:
    video_id = youtube_video_id(value)
    if not video_id:
        raise DispatchError("invalid YouTube video URL")
    return f"https://www.youtube.com/watch?v={video_id}"


def preflight_dispatch(
    meta: object,
    *,
    conclusion: str,
    upstream_event: str,
    head_branch: str,
    default_branch: str,
    test_enabled: str,
    autopublish_enabled: str,
    test_source_uuid: str = "",
) -> dict[str, str]:
    """Return skip or validated dispatch mode without reading state."""
    if conclusion != "success":
        return {"action": "skip", "reason": "upstream-not-successful"}
    if upstream_event != "workflow_dispatch":
        return {"action": "skip", "reason": "upstream-not-manual-process"}
    if not default_branch or head_branch != default_branch:
        return {"action": "skip", "reason": "upstream-not-default-branch"}
    if not isinstance(meta, dict):
        raise DispatchError("invalid source metadata")

    # Non-target sources are deliberately skipped before any state file is read.
    if meta.get("slug") != TARGET_SLUG:
        return {"action": "skip", "reason": "non-target-source"}

    test_mode = test_enabled == "true"
    autopublish_mode = autopublish_enabled == "true"
    if test_mode and autopublish_mode:
        raise DispatchError("conflicting TikTok modes")
    if not test_mode and not autopublish_mode:
        return {"action": "skip", "reason": "TikTok modes disabled"}

    source_uuid = canonical_uuid(meta.get("uuid"))
    if test_mode:
        try:
            expected_test_uuid = canonical_uuid(test_source_uuid)
        except DispatchError:
            return {"action": "skip", "reason": "test source VOD is not configured"}
        if source_uuid != expected_test_uuid:
            return {"action": "skip", "reason": "not the selected test source VOD"}
    source_url = meta.get("url")
    expected_source_url = f"https://kick.com/{TARGET_SLUG}/videos/{source_uuid}"
    if source_url != expected_source_url:
        raise DispatchError("source URL does not match source metadata")

    return {
        "action": "dispatch",
        "uuid": source_uuid,
        "mode": "test" if test_mode else "autopublish",
    }


def validate_state(meta: dict, state: object) -> str:
    if not isinstance(state, dict) or state.get("status") != "done":
        raise DispatchError("source state is not complete")
    source_uuid = canonical_uuid(meta.get("uuid"))
    if "uuid" in state and state["uuid"] != source_uuid:
        raise DispatchError("source state identifier mismatch")
    if "slug" in state and state["slug"] != TARGET_SLUG:
        raise DispatchError("source state slug mismatch")
    # mark_done.py omits parts for a single upload and stores parts only when
    # an upload was split, so any parts key is treated as multipart/ambiguous.
    if "parts" in state:
        raise DispatchError("multipart YouTube upload is not supported")
    return canonical_youtube_url(state.get("yt_url"))


def dispatch_fields(video_url: str, mode: str) -> list[tuple[str, str]]:
    if mode not in {"test", "autopublish"}:
        raise DispatchError("invalid dispatch mode")
    fields = [("video", video_url), ("platform", "youtube"), ("n", "1")]
    if mode == "test":
        fields.append(("tiktok_test_post", "true"))
    else:
        fields.append(("tiktok_auto_post", "true"))
    return fields


def _selftest() -> None:
    uuid = "123e4567-e89b-12d3-a456-426614174000"
    meta = {
        "slug": TARGET_SLUG,
        "uuid": uuid,
        "url": f"https://kick.com/{TARGET_SLUG}/videos/{uuid}",
    }
    good_state = {"status": "done", "yt_url": "https://youtu.be/abcdefghijk"}

    def preflight(**overrides):
        args = {
            "conclusion": "success",
            "upstream_event": "workflow_dispatch",
            "head_branch": "main",
            "default_branch": "main",
            "test_enabled": "true",
            "autopublish_enabled": "false",
            "test_source_uuid": uuid,
        }
        args.update(overrides)
        return preflight_dispatch(meta, **args)

    assert preflight()["action"] == "dispatch"
    assert preflight(test_source_uuid="")["action"] == "skip"
    assert preflight(test_source_uuid="223e4567-e89b-12d3-a456-426614174000")["action"] == "skip"
    assert dispatch_fields(validate_state(meta, good_state), "test") == [
        ("video", "https://www.youtube.com/watch?v=abcdefghijk"),
        ("platform", "youtube"),
        ("n", "1"),
        ("tiktok_test_post", "true"),
    ]

    wrong_slug = dict(meta, slug="another_channel")
    assert preflight_dispatch(
        wrong_slug,
        conclusion="success",
        upstream_event="workflow_dispatch",
        head_branch="main",
        default_branch="main",
        test_enabled="true",
        autopublish_enabled="false",
        test_source_uuid=uuid,
    ) == {"action": "skip", "reason": "non-target-source"}
    assert preflight(conclusion="failure")["action"] == "skip"
    assert preflight(upstream_event="schedule")["action"] == "skip"
    assert preflight(head_branch="feature/test")["action"] == "skip"
    assert preflight(test_enabled="false", autopublish_enabled="false")["action"] == "skip"
    try:
        preflight(test_enabled="true", autopublish_enabled="true")
    except DispatchError:
        pass
    else:
        raise AssertionError("both-on mode must fail closed")

    invalid_states = [
        {"status": "not-done", "yt_url": "https://youtu.be/abcdefghijk"},
        {"status": "done"},
        {"status": "done", "yt_url": "https://youtu.be/abcdefghijk", "parts": []},
        {"status": "done", "yt_url": "https://youtu.be/abcdefghijk", "parts": [{"url": "https://youtu.be/abcdefghijk"}]},
        {"status": "done", "yt_url": "https://example.com/abcdefghijk"},
    ]
    for state in invalid_states:
        try:
            validate_state(meta, state)
        except DispatchError:
            continue
        raise AssertionError("invalid state was accepted")
    print("selftest: dispatch, gate, and rejection cases passed")


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise DispatchError("required metadata is missing or malformed") from None


def _write_video_output(video_url: str, output_path: Path) -> None:
    if "\n" in video_url or "\r" in video_url:
        raise DispatchError("invalid output URL")
    with output_path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(f"video={video_url}\n")


def _run_dispatch(repo: str, default_branch: str, fields: list[tuple[str, str]]) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise DispatchError("invalid repository")
    if not default_branch or "\n" in default_branch or "\r" in default_branch:
        raise DispatchError("invalid default branch")
    if not os.environ.get("GH_TOKEN"):
        raise DispatchError("GitHub token is unavailable")

    command = [
        "gh",
        "workflow",
        "run",
        DOWNSTREAM_WORKFLOW,
        "--repo",
        repo,
        "--ref",
        default_branch,
    ]
    for key, value in fields:
        command.extend(["--field", f"{key}={value}"])
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
    except OSError:
        raise DispatchError("GitHub workflow dispatch could not be started") from None
    if result.returncode != 0:
        # Do not print CLI output: it may echo the video URL or credential context.
        raise DispatchError("GitHub workflow dispatch failed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--meta", type=Path, default=Path("out/meta.json"))
    parser.add_argument("--state-dir", type=Path, default=Path("state"))
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        _selftest()
        return 0

    try:
        meta = _read_json(args.meta)
        plan = preflight_dispatch(
            meta,
            conclusion=os.environ.get("UPSTREAM_CONCLUSION", ""),
            upstream_event=os.environ.get("UPSTREAM_EVENT", ""),
            head_branch=os.environ.get("UPSTREAM_BRANCH", ""),
            default_branch=os.environ.get("DEFAULT_BRANCH", ""),
            test_enabled=os.environ.get("TIKTOK_TEST_POST_ENABLED", ""),
            autopublish_enabled=os.environ.get("TIKTOK_AUTOPUBLISH_ENABLED", ""),
            test_source_uuid=os.environ.get("TIKTOK_TEST_SOURCE_VOD_UUID", ""),
        )
        if plan["action"] == "skip":
            print(f"skip: {plan['reason']}")
            return 0

        state = _read_json(args.state_dir / f"{plan['uuid']}.json")
        video_url = validate_state(meta, state)
        fields = dispatch_fields(video_url, plan["mode"])
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        default_branch = os.environ.get("DEFAULT_BRANCH", "")
        output_path = os.environ.get("GITHUB_OUTPUT", "")
        if not output_path:
            raise DispatchError("GitHub output path is unavailable")

        _run_dispatch(repo, default_branch, fields)
        _write_video_output(video_url, Path(output_path))
        print("queued shorts_prep workflow_dispatch")
        return 0
    except DispatchError:
        print("::error::post-upload dispatch validation or API request failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
