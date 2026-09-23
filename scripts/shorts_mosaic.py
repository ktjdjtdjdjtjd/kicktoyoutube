"""TikTok用縦型動画に、最大の人物以外の検出枠を追従モザイクする。

OpenCV Zoo YOLOX ONNXをOpenCV DNNで実行する。最大人物を配信者本人と
仮定して残す方式であり、人物の本人確認は行わない。
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np


MODEL_SIZE = 640
MODEL_STRIDES = (8, 16, 32)
MODEL_CLASS_COUNT = 80


def mosaic(img, x, y, w, h, block):
    height, width = img.shape[:2]
    x1, y1 = max(0, int(x)), max(0, int(y))
    x2, y2 = min(width, int(x + w)), min(height, int(y + h))
    if x2 - x1 < 2 or y2 - y1 < 2:
        return
    roi = img[y1:y2, x1:x2]
    small = cv2.resize(roi, (max(1, roi.shape[1] // block),
                             max(1, roi.shape[0] // block)),
                       interpolation=cv2.INTER_LINEAR)
    img[y1:y2, x1:x2] = cv2.resize(
        small, (roi.shape[1], roi.shape[0]), interpolation=cv2.INTER_NEAREST)


def letterbox(frame):
    """OpenCV Zoo YOLOX demoと同じ左上寄せの640x640 letterboxを作る。"""
    height, width = frame.shape[:2]
    scale = min(MODEL_SIZE / height, MODEL_SIZE / width)
    resized = cv2.resize(
        cv2.cvtColor(frame, cv2.COLOR_BGR2RGB),
        (int(width * scale), int(height * scale)),
        interpolation=cv2.INTER_LINEAR,
    ).astype(np.float32)
    padded = np.full((MODEL_SIZE, MODEL_SIZE, 3), 114.0, dtype=np.float32)
    padded[:resized.shape[0], :resized.shape[1]] = resized
    return padded, scale


def make_grids():
    grids = []
    strides = []
    for stride in MODEL_STRIDES:
        size = MODEL_SIZE // stride
        x_grid, y_grid = np.meshgrid(np.arange(size), np.arange(size))
        grids.append(np.stack((x_grid, y_grid), axis=2).reshape(1, -1, 2))
        strides.append(np.full((1, size * size, 1), stride, dtype=np.float32))
    return np.concatenate(grids, axis=1).astype(np.float32), np.concatenate(strides, axis=1)


GRID, EXPANDED_STRIDES = make_grids()


def detect_people(net, frame, conf_threshold, nms_threshold):
    """YOLOX person(class 0)検出結果を元フレーム座標のxyxyで返す。"""
    padded, scale = letterbox(frame)
    net.setInput(np.transpose(padded, (2, 0, 1))[np.newaxis, :, :, :])
    outputs = net.forward(net.getUnconnectedOutLayersNames())
    if not outputs:
        raise RuntimeError("YOLOX returned no output tensors")
    predictions = np.asarray(outputs[0], dtype=np.float32).squeeze()
    expected_columns = 5 + MODEL_CLASS_COUNT
    if predictions.ndim != 2 or predictions.shape[1] != expected_columns:
        raise RuntimeError(
            f"unexpected YOLOX output shape: {predictions.shape}; "
            f"expected (N, {expected_columns})"
        )
    if predictions.shape[0] != GRID.shape[1]:
        raise RuntimeError(
            f"unexpected YOLOX box count: {predictions.shape[0]}; "
            f"expected {GRID.shape[1]}"
        )

    decoded = predictions.copy()
    decoded[:, :2] = (decoded[:, :2] + GRID[0]) * EXPANDED_STRIDES[0]
    decoded[:, 2:4] = np.exp(np.clip(decoded[:, 2:4], -20.0, 20.0)) * EXPANDED_STRIDES[0]
    class_ids = np.argmax(decoded[:, 5:], axis=1)
    person_scores = decoded[:, 4] * decoded[:, 5]
    person_indices = np.flatnonzero(
        (class_ids == 0) & (person_scores >= conf_threshold)
    )
    if not len(person_indices):
        return []

    centers = decoded[person_indices, :2]
    sizes = decoded[person_indices, 2:4]
    boxes = np.column_stack((centers - sizes / 2.0, sizes))
    scores = person_scores[person_indices]
    kept = cv2.dnn.NMSBoxes(
        boxes.tolist(), scores.tolist(), conf_threshold, nms_threshold
    )
    if kept is None or len(kept) == 0:
        return []

    detections = []
    for kept_index in np.asarray(kept).reshape(-1):
        x, y, box_width, box_height = boxes[int(kept_index)] / scale
        detections.append((x, y, x + box_width, y + box_height))
    return detections


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--model", required=True,
                        help="SHA256検証済みのOpenCV Zoo YOLOX ONNXファイル")
    parser.add_argument("--block", type=int, default=10)
    parser.add_argument("--keep-min", type=float, default=0.06,
                        help="配信者と推定する最大人物の最小画面占有率")
    parser.add_argument("--conf", type=float, default=0.30)
    parser.add_argument("--nms", type=float, default=0.50)
    args = parser.parse_args()
    inp, out, model_path = Path(args.input), Path(args.output), Path(args.model)
    if not inp.is_file():
        sys.exit("error: input video is missing")
    if not model_path.is_file():
        sys.exit("error: verified YOLOX model is missing")

    net = cv2.dnn.readNet(str(model_path))
    net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
    net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
    cap = cv2.VideoCapture(str(inp))
    if not cap.isOpened():
        sys.exit("error: could not open input video")
    fps = float(cap.get(cv2.CAP_PROP_FPS))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if fps <= 0 or width <= 0 or height <= 0:
        cap.release()
        sys.exit("error: invalid video properties")

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-video_size", f"{width}x{height}",
        "-framerate", f"{fps:.6f}", "-i", "pipe:0", "-i", str(inp),
        "-map", "0:v:0", "-map", "1:a?", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "22", "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(out),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    boxes = []
    keep = -1
    frame_no = 0
    mosaics = 0
    processing_error = ""
    started = time.time()
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            boxes = detect_people(net, frame, args.conf, args.nms)
            if not boxes:
                processing_error = f"no person detected at frame {frame_no}"
                break
            areas = [(b[2] - b[0]) * (b[3] - b[1]) for b in boxes]
            keep = int(np.argmax(areas))
            if areas[keep] < args.keep_min * width * height:
                keep = -1

            for index, (x1, y1, x2, y2) in enumerate(boxes):
                if index == keep:
                    continue
                bw, bh = x2 - x1, y2 - y1
                pad = 0.10
                mosaic(frame, x1 - bw * pad, y1 - bh * pad,
                       bw * (1 + 2 * pad), bh * (1 + 2 * pad), args.block)
                mosaics += 1
            proc.stdin.write(frame.tobytes())
            frame_no += 1
            if frame_no % 1200 == 0:
                print(f"mosaic: {frame_no} frames", flush=True)
    except BrokenPipeError:
        processing_error = "ffmpeg encoder pipe failed"
    except Exception as exc:
        processing_error = f"mosaic processing failed: {type(exc).__name__}"
    finally:
        cap.release()
        if proc.stdin and not proc.stdin.closed:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                if not processing_error:
                    processing_error = "ffmpeg encoder pipe failed"
    return_code = proc.wait()
    if (processing_error or return_code != 0 or frame_no == 0
            or not out.is_file() or out.stat().st_size == 0):
        out.unlink(missing_ok=True)
        if processing_error:
            sys.exit(f"error: {processing_error}")
        sys.exit("error: mosaic encoding failed")
    print(f"mosaic complete: {frame_no} frames, {mosaics} mosaic boxes, "
          f"{time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
