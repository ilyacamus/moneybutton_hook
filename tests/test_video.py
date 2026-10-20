import subprocess

import pytest

from hookbot.core import video

pytestmark = pytest.mark.skipif(not video.ffmpeg_available(), reason="ffmpeg not installed")


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """4 s clip: red 1.2 s then blue (one cut), silence until 0.8 s then a tone."""
    path = tmp_path_factory.mktemp("video") / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=red:s=320x568:d=1.2",
            "-f", "lavfi", "-i", "color=c=blue:s=320x568:d=2.8",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=0.8",
            "-f", "lavfi", "-i", "sine=frequency=440:d=3.2:r=44100",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v];[2:a][3:a]concat=n=2:v=0:a=1[a]",
            "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
        ],
        check=True,
    )
    return str(path)


def test_probe_measures_first_seconds(clip):
    probe = video.probe(clip)
    assert probe.duration == pytest.approx(4.0, abs=0.1)
    assert probe.has_audio
    assert probe.first_sound_at == pytest.approx(0.8, abs=0.1)
    assert probe.cuts and probe.cuts[0] == pytest.approx(1.2, abs=0.1)
    assert [f.t for f in probe.frames] == list(video.FRAME_TIMES)
    assert all(f.jpeg.startswith(b"\xff\xd8") for f in probe.frames)
    assert "0.80 с" in probe.facts_text()


def test_probe_rejects_garbage(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(video.VideoError):
        video.probe(str(bad))
