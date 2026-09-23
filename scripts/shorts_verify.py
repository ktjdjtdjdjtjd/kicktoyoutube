"""Verify TikTok-ready MP4 metadata and decode every video frame."""
import argparse
import json
import math
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path


class VerifyError(Exception):
    pass


MAX_TIKTOK_BYTES = 1_000_000_000
TIKTOK_ASPECT_RATIO = 9 / 16
TIKTOK_ASPECT_RELATIVE_TOLERANCE = 0.005


def run_probe(path):
    command = [
        "ffprobe", "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=30, check=False,
        )
    except subprocess.TimeoutExpired:
        raise VerifyError("metadata read timed out") from None
    except OSError:
        raise VerifyError("ffprobe is unavailable") from None
    if result.returncode != 0:
        raise VerifyError("metadata read failed")
    try:
        return json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VerifyError("metadata response is invalid") from None


def number(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def frame_rate(stream):
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = stream.get(key)
        if not value or value == "0/0":
            continue
        try:
            rate = float(Fraction(value))
        except (ValueError, ZeroDivisionError):
            continue
        if math.isfinite(rate) and rate > 0:
            return rate
    return None


def verify_metadata(path, metadata):
    if path.stat().st_size > MAX_TIKTOK_BYTES:
        raise VerifyError("video exceeds TikTok's 1 GB file size limit")
    fmt = metadata.get("format") or {}
    formats = set(str(fmt.get("format_name") or "").split(","))
    if path.suffix.lower() != ".mp4" or "mp4" not in formats:
        raise VerifyError("container is not MP4")

    video_streams = [stream for stream in metadata.get("streams", [])
                     if stream.get("codec_type") == "video"]
    if not video_streams:
        raise VerifyError("video stream is missing")
    audio_streams = [stream for stream in metadata.get("streams", [])
                     if stream.get("codec_type") == "audio"]
    if not audio_streams:
        raise VerifyError("audio stream is missing")
    stream = video_streams[0]
    if stream.get("codec_name") != "h264":
        raise VerifyError("video codec is not H.264")
    if stream.get("pix_fmt") != "yuv420p":
        raise VerifyError("pixel format is not yuv420p")

    width = stream.get("width")
    height = stream.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or min(width, height) < 360:
        raise VerifyError("video dimensions are below 360 pixels")
    relative_aspect_error = abs((width / height) / TIKTOK_ASPECT_RATIO - 1.0)
    if relative_aspect_error > TIKTOK_ASPECT_RELATIVE_TOLERANCE:
        raise VerifyError("video aspect ratio must be 9:16 within 0.5%")

    duration = number(stream.get("duration"))
    if duration is None:
        duration = number(fmt.get("duration"))
    if duration is None or not 3 <= duration <= 600:
        raise VerifyError("video duration must be 3 to 600 seconds")

    fps = frame_rate(stream)
    if fps is None or not 23 <= fps <= 60:
        raise VerifyError("frame rate must be 23 to 60 fps")
    return width, height, duration, fps


def verify_decode(path, duration):
    command = [
        "ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path),
        "-map", "0:v", "-map", "0:a", "-progress", "pipe:1", "-nostats",
        "-f", "null", "-",
    ]
    timeout = max(60, min(1200, int(duration * 3) + 30))
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise VerifyError("full video decode timed out") from None
    except OSError:
        raise VerifyError("ffmpeg is unavailable") from None

    progress = result.stdout.decode("ascii", errors="ignore")
    frames = [int(match.group(1)) for match in
              re.finditer(r"(?m)^frame=\s*(\d+)\s*$", progress)]
    if result.returncode != 0 or not frames or max(frames) < 1 or "progress=end" not in progress:
        raise VerifyError("full video decode failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", help="MP4 file to verify")
    args = parser.parse_args()
    try:
        path = Path(args.video).resolve(strict=True)
        if not path.is_file():
            raise VerifyError("video file is missing")
        metadata = run_probe(path)
        width, height, duration, fps = verify_metadata(path, metadata)
        verify_decode(path, duration)
    except VerifyError as exc:
        print(f"video verification failed: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("video verification failed: video file is unavailable", file=sys.stderr)
        return 1
    except Exception:
        print("video verification failed: unexpected verification error", file=sys.stderr)
        return 1

    print(f"verified: mp4 h264 yuv420p {width}x{height} "
          f"{duration:.2f}s {fps:.2f}fps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
