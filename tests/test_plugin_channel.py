"""The plugin's channel audit and target-audience tools, called the way Claude calls them (JSON-RPC ``tools/call``).

Claude's part — looking at the frames and scoring blind — is played by ``blind_analysis``,
which sees only what ``next_reels`` hands out.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from hookbot import plugin_sync
from hookbot.core import knowledge
from hookbot.core.instagram import InstagramError
from test_audience import GOOD, write_export
from test_target import WINNER, AudienceGraph

SERVER = plugin_sync.PLUGIN / "scripts" / "hook_memory_mcp.py"


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("HOOKBOT_DB", str(tmp_path / "plugin" / "hooks.db"))
    monkeypatch.delenv("HOOKBOT_WORKSPACE", raising=False)
    for var in ("IG_ACCESS_TOKEN", "IG_USER_ID"):
        monkeypatch.setenv(var, "${user_config.unset}")  # how Claude Code passes an empty userConfig value
    spec = importlib.util.spec_from_file_location(f"hook_memory_mcp_{tmp_path.name}", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    module.STORE.close()


def call(server, name, **arguments):
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})
    result = response["result"]
    text = result["content"][0]["text"]
    assert not result["isError"], text
    return json.loads(text)


def blind_analysis(reel, *, face):
    """What Claude would save after looking at the frames: judged from the caption, never from the numbers."""
    caption = reel["caption"]
    good = caption in (GOOD, WINNER)
    criteria = {cid: 5 for cid in knowledge.criterion_ids()}
    criteria["curiosity"] = 8 if good else 3
    return {"reel_id": reel["reel_id"], "spoken_hook": caption, "spoken_source": "caption", "on_screen_text": "",
            "visual": "говорящая голова" if face(good) else "предметный план", "format": "talking_head",
            "face_first_second": face(good), "text_first_second": False, "motion_first_second": True,
            "criteria": criteria, "trigger_ids": ["curiosity_gap", "loss_aversion"] if good else ["question_hook"]}


def study_all(server, scope, face):
    studied = 0
    while True:
        batch = call(server, "next_reels", scope=scope, limit=5)
        assert batch["errors"] == []
        if not batch["reels"]:
            return studied
        for reel in batch["reels"]:
            assert not {"views", "skip_rate", "label", "outlier_score"} & set(reel), "the study must be blind"
            assert len(reel["frames"]) == 6 and all(Path(f).stat().st_size > 1000 for f in reel["frames"])
            assert "звук" in reel["video_facts"].lower()
            saved = call(server, "save_reel_analysis", **blind_analysis(reel, face=face))
            assert saved["features"]["face_first_second"] == float(face(reel["caption"] in (GOOD, WINNER)))
            studied += 1


def connect_instagram(server, clip):
    graph = AudienceGraph(clip)

    def getter(path, params):
        try:
            return graph(path, params)
        except InstagramError as exc:  # the plugin carries its own copy of the core
            raise server.instagram.InstagramError(str(exc)) from exc

    server._ig = lambda: server.instagram.InstagramGraph("t", "1", getter=getter)


def test_audit_from_csv_export(server, tmp_path, clip):
    assert server._ig() is None  # unresolved ${user_config.…} placeholders mean "not connected"
    path, videos = write_export(tmp_path, clip)
    collected = call(server, "audit_collect", csv_path=str(path), videos_dir=str(videos))
    assert collected == {"collected": 12, "own_reels": 12, "to_study": 12, "without_video": 0, "with_skip_rate": 12,
                         "table_matched": None}

    assert study_all(server, "own", face=lambda good: False) == 12
    report = call(server, "audit_report")
    assert report["n_studied"] == 12 and report["n_with_skip_rate"] == 12
    hold = {e["variable"]: e for e in report["hold_effects"]}
    assert hold["criterion:curiosity"]["rho"] > 0.7 and hold["feature:greeting_start"]["rho"] < -0.7
    assert any(s["id"] == "slow_intro" and s["status"] == "доказано: наука + данные" for s in report["science"])

    # The own reels became training data: the next evaluations are calibrated on them.
    context = call(server, "learning_context")
    assert context["calibration"]["n"] == 12 and "Аудит канала" in context["prompt"]
    assert call(server, "audit_collect", csv_path=str(path), videos_dir=str(videos))["to_study"] == 0


def test_audit_without_instagram_or_csv_says_what_is_missing(server):
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "audit_collect", "arguments": {}}})
    assert response["result"]["isError"] and "csv_path" in response["result"]["content"][0]["text"]
    checked = call(server, "verify_and_watch", usernames=["coach_a"])
    assert checked["results"][0]["verified"] is None


def test_target_audience_flow(server, tmp_path, clip):
    path, videos = write_export(tmp_path, clip)
    call(server, "audit_collect", csv_path=str(path), videos_dir=str(videos))
    study_all(server, "own", face=lambda good: False)  # the channel never shows a face in the first second
    call(server, "audit_report")
    connect_instagram(server, clip)

    signals = call(server, "audience_signals")
    assert signals["n_reels"] == 12 and signals["best_by_hold"][0]["hook"] == GOOD
    assert "25-34" in json.dumps(signals["demographics"]) and signals["comments_sample"]

    call(server, "save_audience_profile", profile={"summary": "Предприниматели 25–44", "confidence": "medium"},
         creators=[{"username": "@Coach_A", "why": "та же боль"}])
    checked = {r["username"]: r for r in call(server, "verify_and_watch",
                                              usernames=["@Coach_A", "coach_b", "coach_c", "invented_account"])["results"]}
    assert checked["coach_a"]["verified"] and not checked["invented_account"]["verified"]
    creators = server.target.latest_profile(server.STORE, "local")["creators"]
    assert {c["username"] for c in creators} == {"coach_a", "coach_b", "coach_c", "invented_account"}
    assert next(c for c in creators if c["username"] == "coach_a")["why"] == "та же боль"

    collected = call(server, "collect_audience")
    assert collected["collected"] == 24 and collected["errors"] == []
    # Batches of 5 study each viral reel with a matched control from the same creator, plus one more
    # control per creator — not every typical reel: 6 + 6 + 3. The winners show a face.
    assert study_all(server, "audience", face=lambda good: good) == 15

    gap = call(server, "gap_report")
    assert gap["n_creators"] == 3 and gap["n_viral"] == 6 and gap["n_typical"] == 9 and gap["n_own"] == 12
    face = {r["variable"]: r for r in gap["recommendations"]}["feature:face_first_second"]
    assert face["do"] == "больше" and face["audience_viral"] == 100 and face["own"] == 0
    assert "доказано" in face["literature"]
    assert any(ex["hook"] == WINNER for ex in gap["exemplars"])
    assert "Контент для этой же аудитории" in call(server, "learning_context")["prompt"]


def test_audit_through_apify_with_the_stats_table(server, tmp_path, clip):
    import hookcore.apify as plugin_apify  # the plugin's own copy of the core, importable once the server is loaded
    from test_apify import FakeApify

    fake = FakeApify(clip)
    server._ig = lambda: plugin_apify.ApifyInstagram("tok", "me_reels", client=plugin_apify.ApifyClient("tok", requester=fake))
    table = tmp_path / "stats.csv"
    table.write_text("Ссылка;Процент пропусков;Сохранения\nhttps://www.instagram.com/reel/Me00x/;31%;40\nMe01x;58%;2\n",
                     encoding="utf-8")
    collected = call(server, "audit_collect", csv_path=str(table))
    assert collected["collected"] == 12 and collected["table_matched"] == 2 and collected["with_skip_rate"] == 2
    checked = call(server, "instagram_check")
    assert checked["ready"] and any(s["step"] == "Ваши Reels" for s in checked["steps"])
