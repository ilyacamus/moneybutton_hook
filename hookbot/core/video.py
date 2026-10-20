"""Objective facts about a video's first seconds, extracted with ffmpeg.

Claude cannot hear audio, so we measure what we can: frames to look at, when
the first sound starts, how many cuts happen in the first 3 seconds.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field

FRAME_TIMES = (0.0, 0.5, 1.0, 1.5, 2.0, 3.0)
HOOK_WINDOW = 3.0


class VideoError(RuntimeError):
    pass


@dataclass
class Frame:
    t: float
    jpeg: bytes


@dataclass
class VideoProbe:
    duration: float
    has_audio: bool
    first_sound_at: float | None
    cuts: list[float] = field(default_factory=list)
    frames: list[Frame] = field(default_factory=list)

    def facts(self) -> dict:
        return {
            "duration_sec": round(self.duration, 2),
            "has_audio": self.has_audio,
            "first_sound_at_sec": self.first_sound_at,
            "cuts_in_first_3s": [round(c, 2) for c in self.cuts],
        }

    def facts_text(self) -> str:
        lines = [f"Длительность ролика: {self.duration:.1f} с"]
        if not self.has_audio:
            lines.append("Звуковой дорожки нет")
        elif self.first_sound_at is None:
            lines.append("В первые 3 секунды звук не обнаружен (тишина)")
        elif self.first_sound_at <= 0.1:
            lines.append("Звук начинается сразу, с 0.0 с")
        else:
            lines.append(f"Первый звук начинается на {self.first_sound_at:.2f} с (до этого тишина)")
        if self.cuts:
            lines.append("Смены плана в первые 3 с: " + ", ".join(f"{c:.2f} с" for c in self.cuts))
        else:
            lines.append("Смен плана в первые 3 с нет (один непрерывный план)")
        return "\n".join(lines)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VideoError(f"ffmpeg failed: {exc}") from exc


def _duration(path: str) -> float:
    proc = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path])
    try:
        return float(proc.stdout.decode().strip())
    except ValueError as exc:
        raise VideoError("Не удалось прочитать видео") from exc


def _has_audio(path: str) -> bool:
    proc = _run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", path])
    return bool(proc.stdout.strip())


def _frame(path: str, t: float, width: int) -> bytes | None:
    proc = _run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
         "-vf", f"scale={width}:-2", "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    )
    return proc.stdout or None


def _first_sound(path: str, window: float) -> float | None:
    """Seconds until the audio rises above -35 dB, ``None`` if silent for the whole window."""
    proc = _run(["ffmpeg", "-v", "info", "-t", f"{window}", "-i", path,
                 "-af", "silencedetect=noise=-35dB:d=0.15", "-f", "null", "-"])
    log = proc.stderr.decode(errors="ignore")
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]
    if not starts or starts[0] > 0.05:
        return 0.0
    # ffmpeg also reports a silence_end when the clipped window ends while still
    # silent; that is not the start of a sound.
    if not ends or ends[0] >= window - 0.05:
        return None
    return round(ends[0], 2)


def _cuts(path: str, window: float, threshold: float = 0.3) -> list[float]:
    proc = _run(["ffmpeg", "-v", "info", "-t", f"{window}", "-i", path,
                 "-vf", f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-"])
    log = proc.stderr.decode(errors="ignore")
    return [float(x) for x in re.findall(r"pts_time:([\d.]+)", log)]


def probe(path: str, *, times: tuple[float, ...] = FRAME_TIMES, width: int = 540) -> VideoProbe:
    """Extract frames and timing facts from the first seconds of ``path``."""
    if not ffmpeg_available():
        raise VideoError("ffmpeg не установлен")
    duration = _duration(path)
    has_audio = _has_audio(path)
    window = min(HOOK_WINDOW, duration)
    frames = []
    for t in times:
        if t >= duration:
            continue
        jpeg = _frame(path, t, width)
        if jpeg:
            frames.append(Frame(t=t, jpeg=jpeg))
    if not frames:
        raise VideoError("Не удалось извлечь кадры из видео")
    return VideoProbe(
        duration=duration,
        has_audio=has_audio,
        first_sound_at=_first_sound(path, window) if has_audio else None,
        cuts=_cuts(path, window),
        frames=frames,
    )
