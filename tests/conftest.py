import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeClient  # noqa: E402

from hookbot.core import knowledge  # noqa: E402
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


@pytest.fixture
def engine(store, client):
    return HookEngine(store, client, model="claude-opus-5-5", effort="medium", fallbacks=True)


def criteria(value=5, **overrides):
    out = {cid: value for cid in knowledge.criterion_ids()}
    out.update(overrides)
    return out
