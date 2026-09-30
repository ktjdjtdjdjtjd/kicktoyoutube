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
LAUGH_RE = re.compile(r"[wｗ]{2,}|笑{2,}", re.IGNORECASE)


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
        if LAUGH_RE.search(text):
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


def segment_entry(pack, board, index):
    """Use the finalized pack, including any dropped post-video tail comments."""
    scores = sorted((item["score"] for item in board["cands"]), reverse=True)
    score = round(sum(scores) / len(scores) + sum(scores[:3]) / min(3, len(scores)), 3)
    name = f"seg_{index:03d}"
    return {
        "kickId": pack["kickId"],
        "index": index,
        "artifactName": f"seg-{index}",
        "videoFile": name + ".mp4",
        "commentsFile": name + "-comments.json",
        "candidatesFile": name + "-candidates.json",
        "sourceStartSeconds": pack["sourceStartSeconds"],
        "durationSeconds": pack["durationSeconds"],
        "score": score,
        "commentCount": len(pack["messages"]),
        "wwwCount": sum(bool(LAUGH_RE.search(item["text"])) for item in pack["messages"]),
    }


def write_pack(meta_path, chat_path, index, video_path, out_path, board_path, entry_path=None):
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
    entry_out = Path(entry_path) if entry_path else None
    if out.exists() or board_out.exists() or (entry_out and entry_out.exists()):
        raise FileExistsError("BridgeClip JSON output already exists; refusing to overwrite")
    try:
        _atomic_json(out, pack)
        _atomic_json(board_out, board)
        _validate_pair(out, board_out, pack, board)
        if entry_out:
            entry = segment_entry(pack, board, index)
            _atomic_json(entry_out, entry)
            if json.loads(entry_out.read_text(encoding="utf-8")) != entry:
                raise ValueError("BridgeClip entry does not match finalized pair")
    except Exception:
        out.unlink(missing_ok=True)
        board_out.unlink(missing_ok=True)
        if entry_out:
            entry_out.unlink(missing_ok=True)
        raise
    return len(pack["messages"])


def build_index(meta, entries, run_id, require_all=False):
    """Small ranking manifest from the burned video pair's verified receipts."""
    if meta.get("slug") != "zingisukan2525" or not meta.get("segments"):
        raise ValueError("index requires a planned zingisukan2525 archive")
    if not str(run_id).isdigit() or not meta.get("uuid") or not meta.get("title"):
        raise ValueError("index requires run id, Kick id and title")
    planned = {item["idx"]: item for item in meta["segments"]}
    if len(planned) != len(meta["segments"]):
        raise ValueError("duplicate planned segment index")
    hours = []
    seen = set()
    for entry in entries:
        index = entry.get("index")
        if not isinstance(index, int) or index not in planned or index in seen:
            raise ValueError("unexpected or duplicate segment receipt")
        seen.add(index)
        span = planned[index]
        name = f"seg_{index:03d}"
        duration = entry.get("durationSeconds")
        if (entry.get("kickId") != meta["uuid"] or entry.get("artifactName") != f"seg-{index}"
                or entry.get("videoFile") != name + ".mp4"
                or entry.get("commentsFile") != name + "-comments.json"
                or entry.get("candidatesFile") != name + "-candidates.json"
                or entry.get("sourceStartSeconds") != span["start"]
                or not isinstance(duration, (int, float)) or not math.isfinite(duration)
                or duration <= 0
                or abs(duration - (span["end"] - span["start"])) > 2
                or not isinstance(entry.get("score"), (int, float))
                or not math.isfinite(entry["score"])
                or not isinstance(entry.get("commentCount"), int) or entry["commentCount"] < 0
                or not isinstance(entry.get("wwwCount"), int)
                or not 0 <= entry["wwwCount"] <= entry["commentCount"]):
            raise ValueError("segment receipt does not match planned archive")
        hours.append({key: value for key, value in entry.items() if key != "kickId"})
    if not hours or (require_all and len(hours) != len(planned)):
        raise ValueError("required BridgeClip segment receipts are missing")
    # A tiny trailing remainder remains selectable but must not outrank a full hour.
    hours.sort(key=lambda hour: (planned[hour["index"]]["end"] - planned[hour["index"]]["start"] >= 3600,
                                 hour["score"],
                                 -hour["sourceStartSeconds"]), reverse=True)
    return {
        "schemaVersion": 1,
        "kickId": meta["uuid"],
        "title": meta["title"],
        "sourceUrl": f'https://kick.com/zingisukan2525/videos/{meta["uuid"]}',
        "runId": int(run_id),
        "segments": hours,
    }


def write_index(meta_path, entry_dir, run_id, out_path, require_all=False):
    directory = Path(entry_dir)
    entries = [json.loads(path.read_text(encoding="utf-8"))
               for path in directory.glob("bridgeclip-hour-*/seg_*-index.json")]
    index = build_index(json.loads(Path(meta_path).read_text(encoding="utf-8")),
                        entries, run_id, require_all)
    dest = Path(out_path)
    if dest.exists():
        raise FileExistsError("BridgeClip index already exists; refusing to overwrite")
    try:
        _atomic_json(dest, index)
        if json.loads(dest.read_text(encoding="utf-8")) != index:
            raise ValueError("BridgeClip index does not match source")
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return len(index["segments"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--meta", required=True)
    parser.add_argument("--chat")
    parser.add_argument("--index", type=int)
    parser.add_argument("--video")
    parser.add_argument("--out")
    parser.add_argument("--board-out")
    parser.add_argument("--index-out")
    parser.add_argument("--run-id")
    parser.add_argument("--entry-out")
    parser.add_argument("--entry-dir")
    parser.add_argument("--require-all", choices=("true", "false"), default="false")
    args = parser.parse_args()
    if args.index_out:
        if (args.index is not None or args.video or args.out or args.board_out
                or args.chat or args.entry_out or not args.run_id or not args.entry_dir):
            parser.error("index mode requires --run-id/--entry-dir and excludes segment arguments")
        count = write_index(args.meta, args.entry_dir, args.run_id, args.index_out,
                            args.require_all == "true")
        print(f"BridgeClip hourly index: {count} -> {args.index_out}")
        return
    if (args.index is None or not args.chat or not args.video or not args.out
            or not args.board_out or args.run_id or args.entry_dir):
        parser.error("segment mode requires --chat, --index, --video, --out and --board-out")
    count = write_pack(args.meta, args.chat, args.index, args.video, args.out,
                       args.board_out, args.entry_out)
    print(f"BridgeClip comments: {count} -> {args.out}, {args.board_out}")


if __name__ == "__main__":
    main()
