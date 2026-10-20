import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hookbot.core import instagram, literature, video
from hookbot.core.corpus import Corpus
from hookbot.core.instagram import InstagramError, InstagramGraph
from hookbot.engine import HookEngine
from hookbot.scout import Scout, ScoutConfig


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    if not video.ffmpeg_available():
        pytest.skip("ffmpeg not installed")
    path = tmp_path_factory.mktemp("v") / "reel.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x568:d=4",
                    "-f", "lavfi", "-i", "sine=frequency=300:d=4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+0000")


class FakeGraph:
    """Answers Graph API paths like the real service would."""

    def __init__(self, clip):
        self.clip = clip
        self.calls = []

    def __call__(self, path, params):
        self.calls.append((path, params))
        if "business_discovery" in params.get("fields", ""):
            media = [{"id": f"a{i}", "media_product_type": "REELS", "media_type": "VIDEO", "caption": f"Хук {i}",
                      "permalink": f"https://instagram.com/reel/a{i}", "timestamp": ago(10 + i),
                      "view_count": 60_000 if i == 0 else 10_000 + 100 * i, "like_count": 10, "comments_count": 1,
                      "media_url": self.clip.as_uri()} for i in range(6)]
            media.append({"id": "photo", "media_type": "IMAGE", "timestamp": ago(3)})
            return {"business_discovery": {"username": "fit_anna", "followers_count": 5000, "media": {"data": media}}}
        if path.endswith("/media"):
            return {"data": [{"id": "own1", "media_product_type": "REELS", "caption": "Мой хук", "timestamp": ago(5),
                              "permalink": "https://instagram.com/reel/own1", "media_url": self.clip.as_uri()}]}
        if path.endswith("/insights"):
            if "reels_skip_rate" in params["metric"] and "," in params["metric"]:
                raise InstagramError("(#100) metric not supported")  # exercise the one-by-one fallback
            name = params["metric"]
            values = {"views": 1500, "reels_skip_rate": 37.5, "saved": 12, "shares": 4}
            return {"data": [{"name": name, "values": [{"value": values[name]}]}]} if name in values else {"data": []}
        return {"username": "me", "followers_count": 900}


def test_graph_client_parses_discovery(clip):
    ig = InstagramGraph("t", "1", getter=FakeGraph(clip))
    profile, media = ig.discover("@fit_anna")
    records = instagram.records_from_discovery(profile, media, niche="фитнес")
    assert len(records) == 6 and records[0].views == 60_000 and records[0].creator == "ig:fit_anna"
    assert records[0].followers == 5000 and records[0].video_url.startswith("file://")


def test_insights_fall_back_metric_by_metric(clip):
    ig = InstagramGraph("t", "1", getter=FakeGraph(clip))
    assert ig.media_insights("own1") == {"views": 1500, "reels_skip_rate": 37.5, "saved": 12, "shares": 4}


def test_import_jsonl(tmp_path):
    f = tmp_path / "export.jsonl"
    f.write_text(json.dumps({"url": "https://x/1", "creator": "c", "views": 10, "unknown": 1}) + "\n\n", encoding="utf-8")
    (rec,) = instagram.import_jsonl(f, default_niche="кофе")
    assert rec.external_id == "https://x/1" and rec.niche == "кофе" and rec.source == "jsonl"


async def test_scout_cycle_end_to_end(store, client, clip):
    engine = HookEngine(store, client)
    store.update_profile("tg:1", niche="фитнес")
    Corpus(store).watch("tg:1", "fit_anna", niche="фитнес")
    alerts = []

    async def notify(text):
        alerts.append(text)

    scout = Scout(store, engine, ig=InstagramGraph("t", "1", getter=FakeGraph(clip)), notify=notify,
                  config=ScoutConfig(study_per_cycle=3, literature_every_days=7, own_workspace="tg:1"))
    summary = await scout.run_once()

    assert summary.collected == 7  # 6 discovered reels + 1 own reel
    assert summary.studied == 3 and summary.failed == 0
    studied = [r for r in Corpus(store).studied() if r["source"] == "instagram"]
    assert studied[0]["label"] == "viral" and {r["label"] for r in studied[1:]} == {"typical"}
    reel = studied[0]
    assert reel["features"]["first_sound_at"] == 0.0 and reel["features"]["face_first_second"] == 1.0
    assert reel["analysis"]["criteria"]["curiosity"] == 7

    # Blind: the analysis request never contains the reel's views.
    study_prompts = [c for c in client.calls if c["output_config"]["format"]["schema"]["title"] == "ReelAnalysis"]
    assert study_prompts and all("60000" not in json.dumps(c["messages"], ensure_ascii=False) for c in study_prompts)

    # Literature scan used web search and is waiting for approval.
    lit_call = next(c for c in client.calls if c["output_config"]["format"]["schema"]["title"] == "LiteratureScan")
    assert lit_call["tools"][0]["type"] == "web_search_20260209"
    assert summary.papers == 1 and len(literature.pending(store)) == 1
    assert any("исследований" in a for a in alerts)
    literature.approve(store, [1])
    assert "Свежие исследования" in engine.context("tg:1").to_prompt()


async def test_own_reels_become_training_data(store, client, clip):
    engine = HookEngine(store, client)
    scout = Scout(store, engine, ig=InstagramGraph("t", "1", getter=FakeGraph(clip)),
                  config=ScoutConfig(study_per_cycle=10, literature_every_days=0, own_workspace="tg:1"))
    await scout.collect(summary := __import__("hookbot.scout", fromlist=["CycleSummary"]).CycleSummary())
    own = Corpus(store).find("instagram_own", "own1")
    assert own["skip_rate"] == 37.5 and own["views"] == 1500
    Corpus(store).set_label(own["id"], "typical", 1.0)
    await scout.study_one(own)
    assert scout.sync_own_results() == 1
    rows = store.training_rows("tg:1")
    assert len(rows) == 1 and rows[0]["hold_3s"] == 62.5 and rows[0]["source"] == "own_reel"
    assert scout.sync_own_results() == 0  # idempotent


async def test_daily_budget(store, client, clip):
    scout = Scout(store, HookEngine(store, client), ig=InstagramGraph("t", "1", getter=FakeGraph(clip)),
                  config=ScoutConfig(study_per_cycle=10, daily_budget=2, literature_every_days=0))
    Corpus(store).watch("w", "fit_anna")
    summary = await scout.run_once()
    assert summary.studied == 2
    assert (await scout.run_once()).studied == 0


async def test_pause_turn_is_continued(store, client):
    client.pause_first = True
    scan = await HookEngine(store, client).scan_literature("2026-01-01")
    assert len(client.calls) == 2 and client.calls[1]["messages"][-1]["role"] == "assistant"
    assert scan.papers[0].year == 2026
