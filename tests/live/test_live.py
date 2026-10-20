"""Live tests against the real Claude API and Instagram Graph API.

They run only when credentials are present (and the network allows the hosts):
    ANTHROPIC_API_KEY                       — Claude tests
    IG_ACCESS_TOKEN (IG_USER_ID optional)  — Instagram tests
    HOOKBOT_LIVE_IG_USERNAME                — a public business/creator account to discover
Run: pytest -m live -v
Every test spends real API credits (a few cents each).
"""

import os
from pathlib import Path

import pytest

from hookbot.core import instagram, video
from hookbot.core.memory import MemoryStore
from hookbot.engine import HookEngine, HookInput

pytestmark = pytest.mark.live
needs_claude = pytest.mark.skipif(not os.getenv("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set")
needs_ig = pytest.mark.skipif(not os.getenv("IG_ACCESS_TOKEN"), reason="Instagram token not set")
REAL_VIDEOS = Path(os.getenv("HOOKBOT_REAL_VIDEOS", "/home/user/intel-iot-devkit/sample-videos"))


@pytest.fixture
def live_engine(tmp_path):
    return HookEngine(MemoryStore(tmp_path / "live.db"))


@needs_claude
async def test_claude_evaluates_a_real_hook_and_caches_the_system_prompt(live_engine):
    hook = HookInput("Я потратил 47 000 ₽ на таргет, чтобы вам не пришлось", on_screen_text="−47 000 ₽",
                     visual="Автор бросает пачку чеков в камеру")
    first = await live_engine.evaluate("live", hook)
    assert 0 <= first.score <= 100 and len(first.evaluation.improved) == 3
    assert all(0 <= v <= 10 for v in first.criteria.values())
    second = await live_engine.evaluate("live", HookInput("Хотите больше клиентов?"))
    assert second.score < first.score, "a generic question should score below a specific, visual hook"
    assert (live_engine.last_usage.cache_read_input_tokens or 0) > 0, "system prompt should be served from cache"


@needs_claude
@pytest.mark.skipif(not (REAL_VIDEOS / "head-pose-face-detection-female.mp4").exists(), reason="real videos missing")
async def test_claude_sees_a_face_in_real_footage(live_engine):
    probe = video.probe(str(REAL_VIDEOS / "head-pose-face-detection-female.mp4"))
    analysis = await live_engine.study_reel(probe.frames, facts_text=probe.facts_text())
    assert analysis.face_first_second is True
    assert analysis.format in ("talking_head", "other", "vlog")


@needs_claude
async def test_claude_rater_reliability(live_engine):
    ids = [live_engine.store.add_hook("live", t) for t in
           ("Хотите больше клиентов?", "Стоп! 3 ошибки, из-за которых ты теряешь деньги на рекламе")]
    result = await live_engine.reliability_study("live", ids, raters=2)
    assert result["score_icc"] is not None


@needs_claude
async def test_claude_finds_real_fresh_papers(live_engine):
    scan = await live_engine.scan_literature("2025-01-01")
    assert scan.papers, "expected at least one study since 2025"
    assert all(p.link.startswith("http") for p in scan.papers)


@needs_ig
def test_instagram_connection_is_ready():
    steps = instagram.check_connection(instagram.InstagramGraph.from_env())
    for s in steps:
        print(s)
    assert all(s["ok"] is not False for s in steps), [s for s in steps if s["ok"] is False]


@needs_ig
def test_instagram_business_discovery():
    ig = instagram.InstagramGraph.from_env()
    username = os.getenv("HOOKBOT_LIVE_IG_USERNAME", "instagram")
    profile, media = ig.discover(username, limit=10)
    records = instagram.records_from_discovery(profile, media)
    assert profile["username"] and records, "expected public reels"
    assert any(r.views for r in records), "view_count should be present for reels"


@needs_ig
def test_instagram_own_reels_have_skip_rate(tmp_path):
    ig = instagram.InstagramGraph.from_env()
    records = instagram.own_records(ig, workspace="live", limit=10)
    assert records, "the connected account has no reels"
    assert any(r.skip_rate is not None for r in records), "reels_skip_rate missing (too few views?)"
    with_video = next((r for r in records if r.video_url), None)
    if with_video:
        path = instagram.download(with_video.video_url, tmp_path / "own.mp4")
        assert video.probe(str(path)).frames
