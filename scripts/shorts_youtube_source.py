"""Validate a published Omoshiro Movies YouTube source for the shorts lane.

This helper only inspects yt-dlp metadata. It never downloads the video or
publishes anything. The expected YouTube channel ID must be supplied by the
caller; it is deliberately not inferred from repository configuration.
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
KICK_URL_RE = re.compile(
    r"(?i)https?://(?:www\.)?kick\.com/([^/?#\s]+)/videos/([0-9a-f-]{16,})/?"
)
KICK_HOST_RE = re.compile(r"(?i)(?:https?://)?(?:www\.)?kick\.com\b")
PART_PATTERNS = (
    re.compile(r"(?i)\bpart\s*[-#:]?\s*\d+\b"),
    re.compile(r"(?i)\b(?:pt|episode|ep)\s*\.?\s*\d+\b"),
    re.compile(r"(?:前編|中編|後編|前半|後半)"),
    re.compile(r"第\s*[0-9０-９一二三四五六七八九十]+\s*[部話章回]"),
    re.compile(r"(?:その|其の)\s*[0-9０-９一二三四五六七八九十]+"),
    re.compile(r"[（(]\s*[0-9０-９]+\s*[/／]\s*[0-9０-９]+\s*[）)]\s*$"),
    re.compile(r"[【\[]\s*(?:part\s*)?[0-9０-９]+\s*[】\]]\s*$", re.I),
)


class SourceValidationError(ValueError):
    """A source cannot be safely used by the downstream shorts workflow."""


def canonical_video_url(value):
    """Return (video_id, canonical_url) for a supported single-video URL."""
    try:
        parsed = urlsplit((value or "").strip())
        host = (parsed.hostname or "").lower()
        if parsed.scheme.lower() not in ("http", "https"):
            raise SourceValidationError("URL must use http or https")
        if parsed.username or parsed.password or parsed.port:
            raise SourceValidationError("URL authority is not a supported YouTube host")
        if host not in {"youtube.com", "www.youtube.com", "m.youtube.com",
                        "youtu.be", "www.youtu.be"}:
            raise SourceValidationError("URL must be a youtube.com or youtu.be video URL")
        query = parse_qsl(parsed.query, keep_blank_values=True)
    except (ValueError, UnicodeError) as exc:
        raise SourceValidationError("URL is malformed") from exc

    if any(key.lower() == "list" for key, _ in query):
        raise SourceValidationError("playlist URLs are not accepted")

    video_id = None
    path = parsed.path.rstrip("/")
    if host in {"youtu.be", "www.youtu.be"}:
        parts = path.lstrip("/").split("/") if path else []
        if len(parts) == 1:
            video_id = parts[0]
    elif path == "/watch":
        values = [value for key, value in query if key == "v"]
        if len(values) == 1:
            video_id = values[0]
    else:
        match = re.fullmatch(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]{11})", path)
        if match:
            video_id = match.group(1)

    if not video_id or not VIDEO_ID_RE.fullmatch(video_id):
        raise SourceValidationError("URL must identify exactly one valid YouTube video")
    return video_id, f"https://www.youtube.com/watch?v={video_id}"


def _description_kick_url(description):
    """Require exactly one Kick host reference and one well-formed VOD URL."""
    description = description or ""
    host_refs = list(KICK_HOST_RE.finditer(description))
    urls = list(KICK_URL_RE.finditer(description))
    if len(host_refs) != 1 or len(urls) != 1:
        raise SourceValidationError(
            "description must contain exactly one Kick VOD URL"
        )
    slug, vod_id = urls[0].groups()
    if slug != "zingisukan2525":
        raise SourceValidationError("Kick VOD slug must exactly equal zingisukan2525")
    return f"https://kick.com/{slug}/videos/{vod_id}"


def _part_hint(metadata, title_template):
    """Return a fail-closed explanation when metadata signals split parts."""
    title = metadata.get("title") or ""
    description = metadata.get("description") or ""
    # The configured description template places the source title immediately
    # before its Kick URL. Inspecting it catches part numbering retained there.
    source_title = ""
    kick_match = KICK_URL_RE.search(description)
    if kick_match:
        before_url = description[:kick_match.start()].rstrip()
        source_title = before_url.splitlines()[-1].strip() if before_url else ""

    heading = title_template.split("{title}", 1)[0]
    title_core = title[len(heading):] if title.startswith(heading) else title
    suffix_template = title_template.split("{title}", 1)[1]
    suffix_lead = suffix_template.split("{", 1)[0]
    if suffix_lead and suffix_lead in title_core:
        title_core = title_core.split(suffix_lead, 1)[0]

    for label, text in (("YouTube title", title_core), ("source title", source_title)):
        for pattern in PART_PATTERNS:
            match = pattern.search(text)
            if match:
                return f"unresolved: possible multipart marker in {label}"

    # yt-dlp may expose playlist position/count even for a URL resolved from a
    # playlist page. Accept only an explicit one-entry position if these fields
    # exist; partial playlist metadata cannot establish the original VOD offset.
    index = metadata.get("playlist_index")
    count = metadata.get("playlist_count")
    if index is not None or count is not None:
        try:
            if int(index) != 1 or int(count) != 1:
                return "unresolved: metadata indicates or cannot rule out multiple parts"
        except (TypeError, ValueError):
            return "unresolved: playlist part number is not reliably available in metadata"
    return None


def validate_pipeline_state(metadata, state_dir="state"):
    """Require the same Kick VOD and YouTube upload in the repository ledger."""
    match = KICK_URL_RE.fullmatch(str(metadata.get("kick_video_url") or ""))
    if not match:
        raise SourceValidationError("associated Kick VOD URL is invalid")
    slug, kick_uuid = match.groups()
    if slug != "zingisukan2525":
        raise SourceValidationError("associated Kick VOD is not from zingisukan2525")
    try:
        state = json.loads((Path(state_dir) / f"{kick_uuid}.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceValidationError("matching completed upload state is unavailable") from exc
    if (not isinstance(state, dict) or state.get("slug") != slug
            or state.get("uuid") != kick_uuid or state.get("status") != "done"):
        raise SourceValidationError("Kick VOD does not have a matching completed upload state")
    if "parts" in state:
        raise SourceValidationError("multipart YouTube uploads are not supported")
    try:
        state_video_id, _ = canonical_video_url(state.get("yt_url"))
    except SourceValidationError as exc:
        raise SourceValidationError("upload state has no valid YouTube video URL") from exc
    if state_video_id != metadata.get("video_id"):
        raise SourceValidationError("YouTube video and Kick VOD do not match the upload state")


def validate_metadata(metadata, expected_channel_id, requested_video_id,
                      title_template, state_dir="state"):
    """Pure metadata validator; returns only fields allowed in the queue JSON."""
    if not isinstance(metadata, dict):
        raise SourceValidationError("yt-dlp metadata is not a JSON object")
    if not expected_channel_id:
        raise SourceValidationError("expected channel ID is required")
    if metadata.get("_type") not in (None, "video") or isinstance(metadata.get("entries"), list):
        raise SourceValidationError("yt-dlp did not return one standalone video")
    if metadata.get("is_playlist") is True:
        raise SourceValidationError("playlist metadata is not accepted")
    if metadata.get("id") != requested_video_id:
        raise SourceValidationError("yt-dlp video ID does not match the requested URL")
    if metadata.get("availability") != "public":
        raise SourceValidationError("YouTube video is not confirmed public")

    channel_id = metadata.get("channel_id")
    if channel_id != expected_channel_id:
        raise SourceValidationError("YouTube channel_id does not match expected channel ID")

    if not isinstance(title_template, str) or "{title}" not in title_template:
        raise SourceValidationError("Zingisukan title template is missing {title}")
    heading = title_template.split("{title}", 1)[0]
    title = metadata.get("title")
    if not isinstance(title, str) or not title.startswith(heading):
        raise SourceValidationError("title does not start with the configured Zingisukan heading")

    duration = metadata.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise SourceValidationError("duration must be a finite positive number")
    duration = float(duration)
    if not math.isfinite(duration) or duration <= 0:
        raise SourceValidationError("duration must be a finite positive number")

    kick_vod_url = _description_kick_url(metadata.get("description"))
    part_error = _part_hint(metadata, title_template)
    if part_error:
        raise SourceValidationError(part_error)

    validated = {
        "youtube_url": f"https://www.youtube.com/watch?v={requested_video_id}",
        "video_id": requested_video_id,
        "channel_id": channel_id,
        "title": title,
        "duration": duration,
        "kick_video_url": kick_vod_url,
    }
    validate_pipeline_state(validated, state_dir)
    return validated


def load_zingisukan_title_template(config_path):
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        channel = config["channel_settings"]["zingisukan2525"]
        template = channel["title_template"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SourceValidationError(
            "config.json channel_settings.zingisukan2525.title_template is unavailable"
        ) from exc
    if not isinstance(template, str) or "{title}" not in template:
        raise SourceValidationError("configured Zingisukan title template is invalid")
    return template


def inspect_url(url, expected_channel_id, config_path="config.json", state_dir="state"):
    video_id, canonical_url = canonical_video_url(url)
    command = [
        "yt-dlp", "--dump-single-json", "--no-warnings", "--no-playlist",
        "--skip-download", canonical_url,
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=True
        )
        metadata = json.loads(result.stdout)
    except subprocess.CalledProcessError as exc:
        raise SourceValidationError(
            f"yt-dlp metadata lookup failed (exit {exc.returncode})"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SourceValidationError("yt-dlp returned invalid JSON metadata") from exc

    return validate_metadata(
        metadata,
        expected_channel_id,
        video_id,
        load_zingisukan_title_template(config_path),
        state_dir,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="published YouTube video URL")
    parser.add_argument("--expected-channel-id", required=True,
                        help="exact Omoshiro Movies YouTube channel ID")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--state-dir", default="state")
    parser.add_argument("--out-json", default="queue_shorts/youtube_source.json")
    args = parser.parse_args()

    try:
        data = inspect_url(args.url, args.expected_channel_id, args.config, args.state_dir)
        output = Path(args.out_json)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with Path(github_output).open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(f"kick_video_url={data['kick_video_url']}\n")
    except SourceValidationError as exc:
        sys.exit(f"error: {exc}")
    except OSError:
        sys.exit("error: could not write source metadata JSON")

    print(f"validated YouTube source: {data['video_id']} ({data['channel_id']})")


if __name__ == "__main__":
    main()
