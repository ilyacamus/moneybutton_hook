import copy
import random
import re
import subprocess

import pytest
from fakes import CANNED

from hookbot.core import audience, instagram, learning, video
from hookbot.core.corpus import Corpus
from hookbot.engine import HookEngine
from hookbot.scout import Scout, ScoutConfig
from test_scout import FakeGraph

GOOD = "3 ошибки, из-за которых ты теряешь деньги"
BAD = "Привет! Сегодня немного про бюджет"


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    if not video.ffmpeg_available():
        pytest.skip("ffmpeg not installed")
    path = tmp_path_factory.mktemp("own") / "Cabc123.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=180x320:d=3.5",
                    "-f", "lavfi", "-i", "sine=d=3.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(path)], check=True)
    return path


def blind_responder(name, params):
    """Scores a reel from what the model would see: here, its caption."""
    if name != "ReelAnalysis":
        return None
    text = params["messages"][0]["content"][-1]["text"]
    caption = re.search(r"<caption>\n(.*?)\n</caption>", text, re.S).group(1)
    good = caption.startswith("3 ошибки")
    data = copy.deepcopy(CANNED["ReelAnalysis"])
    data["spoken_hook"] = caption
    for cid in data["scores"]:
        data["scores"][cid]["score"] = 5
    data["scores"]["curiosity"]["score"] = 8 if good else 3
    data["trigger_ids"] = ["curiosity_gap", "loss_aversion"] if good else ["question_hook"]
    return data


def write_export(tmp_path, clip, n=12, seed=3):
    rng = random.Random(seed)
    lines = ["Идентификатор публикации;Постоянная ссылка;Описание;Время публикации;Просмотры;Сохранения;Процент пропусков"]
    for i in range(n):
        good = i % 2 == 0
        skip = rng.uniform(22, 32) if good else rng.uniform(52, 64)
        views = int(rng.uniform(30_000, 60_000) if good and i < 4 else rng.uniform(8_000, 12_000))
        caption = GOOD if good else BAD
        views_text = f"{views:,}".replace(",", "\u00a0")  # "12 345" with a no-break space, as in exports
        skip_text = f"{skip:.1f}".replace(".", ",") + "%"  # decimal comma
        lines.append(f"{1000 + i};https://www.instagram.com/reel/C{i}x/;{caption};2026-0{1 + i % 9}-10T10:00:00;"
                     f"{views_text};{i};{skip_text}")
    path = tmp_path / "insights.csv"
    path.write_text("\n".join(lines), encoding="utf-8-sig")
    videos = tmp_path / "videos"
    videos.mkdir()
    for i in range(n):
        (videos / f"my reel C{i}x.mp4").write_bytes(clip.read_bytes())
    return path, videos


def test_csv_import_matches_columns_numbers_and_videos(tmp_path, clip):
    path, videos = write_export(tmp_path, clip, n=4)
    records = instagram.import_insights_csv(path, workspace="me", niche="финансы", videos_dir=videos)
    first = records[0]
    assert first.external_id == "1000" and first.caption == GOOD and first.source == "own_csv"
    assert 22 <= first.skip_rate <= 32 and first.views >= 30_000
    assert first.local_path.endswith("my reel C0x.mp4") and first.creator == "own:me"


async def test_audit_finds_what_holds_this_audience(store, client, tmp_path, clip):
    client.responder = blind_responder
    store.update_profile("me", niche="финансы")
    path, videos = write_export(tmp_path, clip)
    records = instagram.import_insights_csv(path, workspace="me", videos_dir=videos)
    scout = Scout(store, HookEngine(store, client), config=ScoutConfig(literature_every_days=0))
    report = await scout.audit("me", records=records)

    assert report["n_reels"] == 12 and report["n_studied"] == 12 and report["synced"] == 12
    hold = {e["variable"]: e for e in report["hold_effects"]}
    assert hold["criterion:curiosity"]["rho"] > 0.7 and hold["criterion:curiosity"]["q"] < 0.1
    assert hold["feature:greeting_start"]["rho"] < -0.7 and hold["feature:greeting_start"]["verdict"].startswith("подтверждено")
    assert hold["trigger:curiosity_gap"]["hold_with"] > hold["trigger:curiosity_gap"]["hold_without"] + 20
    assert "criterion:specificity" not in hold or hold["criterion:specificity"]["verdict"] == "не подтверждено"
    statuses = {s["id"]: s["status"] for s in report["hold_synthesis"]}
    assert statuses["slow_intro"] == "доказано: наука + данные"

    # The channel is now training data: calibration on 12 own reels, playbook written.
    assert learning.calibrate(store, "me").scope == "workspace"
    assert store.latest_insight("me") is not None
    text = learning.build_context(store, "me").to_prompt()
    assert "Аудит канала" in text and "Любопытство" in text

    # Re-running does not duplicate anything.
    again = await scout.audit("me", records=records)
    assert again["studied_now"] == 0 and again["synced"] == 0 and len(store.training_rows("me")) == 12


async def test_audit_through_instagram_api(store, client, clip):
    scout = Scout(store, HookEngine(store, client), ig=instagram.InstagramGraph("t", "1", getter=FakeGraph(clip)),
                  config=ScoutConfig(literature_every_days=0))
    report = await scout.audit("me")
    assert report["n_reels"] == 1 and report["n_with_skip_rate"] == 1 and report["synced"] == 1


async def test_audit_without_any_source_explains_what_is_missing(store, client):
    with pytest.raises(instagram.InstagramError, match="IG_ACCESS_TOKEN"):
        await Scout(store, HookEngine(store, client)).audit("me")


def test_own_media_follows_pagination():
    pages = {None: {"data": [{"id": "1"}, {"id": "2"}], "paging": {"cursors": {"after": "x"}, "next": "u"}},
             "x": {"data": [{"id": "3"}], "paging": {"cursors": {"after": "y"}}}}
    ig = instagram.InstagramGraph("t", "1", getter=lambda path, params: pages[params.get("after")])
    assert [m["id"] for m in ig.own_media(limit=10)] == ["1", "2", "3"]
