"""Optional speech-to-text for the first seconds of a reel (``pip install .[asr]``).

Claude sees frames but cannot hear audio; a local Whisper model recovers the
spoken hook. Without it the scout falls back to the caption and on-screen text.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


class Transcriber:
    def __init__(self, model_size: str | None = None, *, device: str = "cpu", compute_type: str = "int8"):
        from faster_whisper import WhisperModel  # optional dependency

        self.model = WhisperModel(model_size or os.getenv("HOOKBOT_WHISPER_MODEL", "small"),
                                  device=device, compute_type=compute_type)

    def first_seconds(self, video_path: str, seconds: float = 4.0) -> str:
        """Text spoken in the first ``seconds`` of the video ('' when silent)."""
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "hook.wav"
            proc = subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-t", f"{seconds}", "-i", video_path, "-vn", "-ac", "1",
                 "-ar", "16000", str(wav)],
                capture_output=True, timeout=60, check=False,
            )
            if proc.returncode != 0 or not wav.exists():
                return ""
            segments, _ = self.model.transcribe(str(wav), vad_filter=True)
            return " ".join(seg.text.strip() for seg in segments if seg.start < seconds).strip()


def make_transcriber() -> Transcriber | None:
    if not available():
        return None
    try:
        return Transcriber()
    except Exception as exc:  # noqa: BLE001 - model download/load problems must not stop the scout
        log.warning("Speech-to-text disabled: %s", exc)
        return None
