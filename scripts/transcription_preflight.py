"""Small decode-only preflight; no model download or transcription inference."""
import sys
import tempfile
import wave
from importlib.metadata import version
from pathlib import Path


def check_decode(path):
    from faster_whisper.audio import decode_audio
    audio = decode_audio(str(path), sampling_rate=16000)
    if not audio.size:
        raise RuntimeError("decode produced no samples")
    return audio


def check_input_decode(path):
    # Inputs here are ffmpeg PCM WAVs. Probe only 100ms to keep long VODs bounded.
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "input-probe.wav"
        with wave.open(str(path), "rb") as src, wave.open(str(probe), "wb") as dst:
            dst.setparams(src.getparams())
            dst.writeframes(src.readframes(max(1, src.getframerate() // 10)))
        check_decode(probe)


def preflight():
    versions = {name: version(name) for name in ("faster-whisper", "av", "ctranslate2")}
    print(f"transcription dependencies: {versions}; Python {sys.version}", flush=True)
    if versions["faster-whisper"] == "1.2.1" and int(versions["av"].split(".")[0]) >= 19:
        raise RuntimeError("faster-whisper 1.2.1 / PyAV >=19: metadata_errors incompatibility")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x00\x00" * 1600)
        audio = check_decode(path)
        if audio.size != 1600:
            raise RuntimeError(f"unexpected preflight samples: {audio.size}")
    print("decode preflight passed (synthetic PCM WAV only)", flush=True)


if __name__ == "__main__":
    preflight()
