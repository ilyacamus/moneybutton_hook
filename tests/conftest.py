import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeClient  # noqa: E402

from hookbot.core import knowledge, video  # noqa: E402
from hookbot.core.memory import MemoryStore  # noqa: E402
from hookbot.engine import HookEngine  # noqa: E402


@pytest.fixture
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """A real 3.5 s H.264 reel with sound (modules may override it with their own footage)."""
    if not video.ffmpeg_available():
        pytest.skip("ffmpeg not installed")
    path = tmp_path_factory.mktemp("own") / "Cabc123.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=180x320:d=3.5",
                    "-f", "lavfi", "-i", "sine=d=3.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(path)], check=True)
    return path


@pytest.fixture
def engine(store, client):
    return HookEngine(store, client, model="claude-opus-5-5", effort="medium", fallbacks=True)


def criteria(value=5, **overrides):
    out = {cid: value for cid in knowledge.criterion_ids()}
    out.update(overrides)
    return out
