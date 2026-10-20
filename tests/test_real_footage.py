"""Perception checks on real camera footage (not synthetic test patterns).

Uses the public intel-iot-devkit/sample-videos repository:
    GIT_LFS_SKIP_SMUDGE=1 git clone --depth 1 https://github.com/intel-iot-devkit/sample-videos
and HOOKBOT_REAL_VIDEOS=<path to the clone>. Skipped when absent.
"""

import os
import subprocess
from pathlib import Path

import pytest

from hookbot.core import features, video

ROOT = Path(os.getenv("HOOKBOT_REAL_VIDEOS", "/home/user/intel-iot-devkit/sample-videos"))
pytestmark = pytest.mark.skipif(not (ROOT / "classroom.mp4").exists() or not video.ffmpeg_available(),
                                reason="real sample videos not available")


def test_real_silent_track_reads_as_no_sound():
    probe = video.probe(str(ROOT / "head-pose-face-detection-female.mp4"))
    assert probe.has_audio and probe.first_sound_at is None and probe.cuts == []
    assert features.video_features(probe.facts())["first_sound_at"] == 3.0


def test_real_edit_cuts_are_found_where_they_are(tmp_path):
    out = tmp_path / "edited.mp4"
    chain = ",setpts=PTS-STARTPTS,scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,fps=30"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-i", str(ROOT / "head-pose-face-detection-female.mp4"), "-i", str(ROOT / "classroom.mp4"),
        "-i", str(ROOT / "fruit-and-vegetable-detection.mp4"),
        "-filter_complex", f"[0:v]trim=0:1.5{chain}[a];[1:v]trim=0:1.0{chain}[b];[2:v]trim=0:2{chain}[c];"
                           "[a][b][c]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out),
    ], check=True)
    probe = video.probe(str(out))
    assert probe.cuts == pytest.approx([1.5, 2.5], abs=0.05)
    assert not probe.has_audio and len(probe.frames) == 6
