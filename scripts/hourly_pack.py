"""焼き込み済みのジンギスカン1時間セグメント用にコメントJSONを作る。

動画はburn.pyのseg_XXX.mp4を再利用し、ここでは再エンコードしない。
既存chat.jsonlはrel秒とcontentのみを保存しているため、userは空文字、
idは元ファイルの行番号に基づく安定した代替IDになる。
"""
import argparse
import json
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path

# Same marker syntax as chat_fetch.py; this offline packer must not load Kick API deps.
EMOTE_RE = re.compile(r"\[emote:\d+:([^\]]+)\]")


def build_pack(meta, chat_rows, index):
    if meta.get("slug") != "zingisukan2525":
        raise ValueError("BridgeClip pack is limited to zingisukan2525")
    segments = meta.get("segments")
    if not isinstance(segments, list) or index < 0 or index >= len(segments):
        raise ValueError("segment index is not in the plan")
    segment = segments[index]
    start, end = float(segment["start"]), float(segment["end"])
    if (segment.get("idx") != index or not math.isfinite(start) or not math.isfinite(end)
            or start < 0 or end <= start or end - start > 3600.001
            or abs(start - index * 3600) > 0.001):
        raise ValueError("invalid hourly segment boundary")
    messages = []
    for line_number, row in enumerate(chat_rows, 1):
        rel = float(row["rel"])
        if not math.isfinite(rel) or not (start <= rel < end):
            continue
        raw = row["content"]
        if not isinstance(raw, str):
            raise ValueError(f"invalid comment text on line {line_number}")
        text = EMOTE_RE.sub(lambda match: match.group(1), raw)
        local_seconds = round(rel - start, 3)
        # 3599.9998などが丸めで3600.000になって次窓の範囲へ出ないようにする。
        if local_seconds >= end - start:
            local_seconds = max(0, math.floor((end - start - 1e-9) * 1000) / 1000)
        messages.append({
            "id": f"chat-line-{line_number}",
            "seconds": local_seconds,
            "user": "",
            "text": text,
        })
    messages.sort(key=lambda row: row["seconds"])
    return {
        "kickId": meta["uuid"],
        "sourceStartSeconds": start,
        "durationSeconds": end - start,
        "messages": messages,
    }


def candidate_board(pack):
    """BridgeClipの5分bin採点形式（src/main/zingisukan-import.ts）に合わせる。"""
    duration = pack["durationSeconds"]
    bins = [{"count": 0, "ww": 0, "peaks": {}}
            for _ in range(math.ceil(duration / 300))]
    for message in pack["messages"]:
        second = message["seconds"]
        index = math.floor(second / 300)
        if index >= len(bins):
            continue
        entry = bins[index]
        entry["count"] += 1
        text = message["text"]
        if re.search(r"[wｗ]{2,}|笑{2,}", text, re.IGNORECASE):
            entry["ww"] += 1
        sub = math.floor(second / 30)
        entry["peaks"][sub] = entry["peaks"].get(sub, 0) + 1
    cands = []
    for index, entry in enumerate(bins):
        start = index * 300
        end = min(duration, start + 300)
        minutes = (end - start) / 60
        peak = max(entry["peaks"].values(), default=0)
        cands.append({"start": start, "end": end,
                      "score": round((entry["count"] + entry["ww"] * 1.5 + peak * 0.75) / minutes, 3),
                      "rel": round(entry["count"] / minutes, 3)})
    return {"cands": cands}


def _atomic_json(dest, value):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=dest.parent,
                                         prefix=dest.name + ".", suffix=".tmp",
                                         delete=False) as stream:
            temp = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp, dest)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def probe_video_duration(video_path):
    video = Path(video_path)
    if not video.is_file() or video.stat().st_size == 0:
        raise ValueError(f"burned segment is missing or empty: {video}")
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True, check=True, timeout=30)
    duration = float(result.stdout.strip())
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"invalid burned segment duration: {duration}")
    return duration


def _validate_pair(out_path, board_path, expected_pack, expected_board):
    out = Path(out_path)
    board = Path(board_path)
    if not out.is_file() or not board.is_file():
        raise ValueError("BridgeClip JSON pair is incomplete")
    actual_pack = json.loads(out.read_text(encoding="utf-8"))
    actual_board = json.loads(board.read_text(encoding="utf-8"))
    # Equality to the validated in-memory shapes also catches truncation, wrong
    # keys, wrong types and a stale file from a different segment.
    if (actual_pack != expected_pack or actual_board != expected_board
            or set(actual_pack) != {"kickId", "sourceStartSeconds", "durationSeconds", "messages"}
            or set(actual_board) != {"cands"}):
        raise ValueError("BridgeClip JSON pair does not match its schema/source")


def write_pack(meta_path, chat_path, index, video_path, out_path, board_path):
    meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    with open(chat_path, encoding="utf-8") as stream:
        # 全配信のchatpackが大きくても、読み込みは1行ずつに留める。
        pack = build_pack(meta, (json.loads(line) for line in stream if line.strip()), index)
    actual_duration = probe_video_duration(video_path)
    planned_duration = pack["durationSeconds"]
    # Encode/concat may shift a boundary by a few frames. Over 2s is not
    # explainable by frame rounding, so never distribute mismatched comments.
    if abs(actual_duration - planned_duration) > 2:
        raise ValueError(f"segment duration mismatch: planned={planned_duration:.3f}s "
                         f"actual={actual_duration:.3f}s")
    pack["durationSeconds"] = round(actual_duration, 3)
    pack["messages"] = [message for message in pack["messages"]
                        if message["seconds"] < pack["durationSeconds"]]
    board = candidate_board(pack)
    out = Path(out_path)
    board_out = Path(board_path)
    if out.exists() or board_out.exists():
        raise FileExistsError("BridgeClip JSON output already exists; refusing to overwrite")
    try:
        _atomic_json(out, pack)
        _atomic_json(board_out, board)
        _validate_pair(out, board_out, pack, board)
    except Exception:
        out.unlink(missing_ok=True)
        board_out.unlink(missing_ok=True)
        raise
    return len(pack["messages"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--meta", required=True)
    parser.add_argument("--chat", required=True)
    parser.add_argument("--index", required=True, type=int)
    parser.add_argument("--video", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--board-out", required=True)
    args = parser.parse_args()
    count = write_pack(args.meta, args.chat, args.index, args.video, args.out, args.board_out)
    print(f"BridgeClip comments: {count} -> {args.out}, {args.board_out}")


if __name__ == "__main__":
    main()
