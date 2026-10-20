import copy
import re
from datetime import datetime, timedelta, timezone

import pytest
from fakes import CANNED

from hookbot import render
from hookbot.core import instagram, learning, target
from hookbot.core.corpus import Corpus
from hookbot.core.instagram import InstagramError
from hookbot.engine import HookEngine
from hookbot.scout import Scout, ScoutConfig
from test_audience import clip, write_export  # noqa: F401 - fixture reuse

WINNER = "3 ошибки, из-за которых ты теряешь деньги"
PLAIN = "Привет! Сегодня немного про бюджет"


def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+0000")


class AudienceGraph:
    """Instagram for the target-audience flow: demographics, comments, 3 real creators, 1 invented."""

    creators = ("coach_a", "coach_b", "coach_c")

    def __init__(self, clip):
        self.clip = clip

    def __call__(self, path, params):
        fields = params.get("fields", "")
        if "business_discovery" in fields:
            username = re.search(r"username\(([^)]+)\)", fields).group(1)
            if username not in self.creators:
                raise InstagramError("Invalid user id")
            media = []
            for i in range(8):
                viral = i in (1, 5)
                media.append({"id": f"{username}-{i}", "media_product_type": "REELS", "timestamp": ago(5 + 3 * i),
                              "caption": WINNER if viral else PLAIN, "permalink": f"https://instagram.com/reel/{username}{i}",
                              "view_count": 50_000 + i if viral else 9_000 + 100 * i, "media_url": self.clip.as_uri()})
            return {"business_discovery": {"username": username, "followers_count": 20_000, "media": {"data": media}}}
        if path.endswith("/insights"):
            return {"data": [{"name": params["metric"], "total_value": {"breakdowns": [{"results": [
                {"dimension_values": ["25-34"], "value": 600}, {"dimension_values": ["35-44"], "value": 400}]}]}}]}
        if path.endswith("/comments"):
            return {"data": [{"text": "Где взять деньги на рекламу?"}, {"text": "Спасибо, про бюджет полезно"}]}
        return {"data": []}


def responder(name, params):
    text = params["messages"][0]["content"][-1]["text"]
    if name == "ReelAnalysis":
        caption = re.search(r"<caption>\n(.*?)\n</caption>", text, re.S).group(1)
        good = caption.startswith("3 ошибки")
        data = copy.deepcopy(CANNED["ReelAnalysis"])
        data["spoken_hook"] = caption
        for cid in data["scores"]:
            data["scores"][cid]["score"] = 5
        data["scores"]["curiosity"]["score"] = 8 if good else 3
        data["face_first_second"] = good  # winners show a face in the first second
        data["trigger_ids"] = ["curiosity_gap", "face_emotion"] if good else ["question_hook"]
        return data
    if name == "AudienceProfile":
        assert "25-34" in text and "Где взять деньги" in text  # the model sees demographics and comments
        return {"summary": "Предприниматели 25–44, считающие рекламный бюджет", "demographics": "25–34: 60%",
                "segments": [{"name": "Малый бизнес", "description": "Сами ведут рекламу", "weight": "основной",
                              "evidence": "комментарии про бюджет"}],
                "interests": ["таргет"], "pains": ["слив бюджета"], "desires": ["окупаемая реклама"],
                "language_style": "на «ты», просто", "resonating_content": ["разборы ошибок"],
                "search_keywords": ["таргет для малого бизнеса"], "hashtags": ["#таргет"],
                "creator_types": ["таргетологи"], "confidence": "medium", "data_gaps": ["мало комментариев"]}
    if name == "CreatorSearch":
        return {"queries_used": ["таргетолог instagram"], "candidates": [
            {"username": u, "segment": "Малый бизнес", "why": "та же боль", "source_url": "https://example.com/list"}
            for u in ("coach_a", "@coach_b", "coach_c", "invented_account")]}
    return None


async def test_target_audience_end_to_end(store, client, tmp_path, clip):  # noqa: F811
    client.responder = responder
    store.update_profile("me", niche="таргет")
    # The channel: hooks without a face in the first second.
    path, videos = write_export(tmp_path, clip)
    ig = instagram.InstagramGraph("t", "1", getter=AudienceGraph(clip))
    scout = Scout(store, HookEngine(store, client), ig=ig, config=ScoutConfig(literature_every_days=0))
    original = copy.deepcopy(responder)

    def own_responder(name, params):
        data = original(name, params)
        if name == "ReelAnalysis":
            data["face_first_second"] = False
        return data

    client.responder = own_responder
    await scout.audit("me", records=instagram.import_insights_csv(path, workspace="me", videos_dir=videos))
    client.responder = responder

    result = await scout.target_audience("me", own_account=True)

    creators = {c["username"]: c for c in result["creators"]}
    assert creators["coach_b"]["verified"] and not creators["invented_account"]["verified"]
    assert {w["username"] for w in Corpus(store).watchlist("me")} == {"coach_a", "coach_b", "coach_c"}
    assert result["studied"] == 24

    gap = result["gap"]
    assert gap["n_creators"] == 3 and gap["n_viral"] == 6 and gap["n_own"] == 12
    recs = {r["variable"]: r for r in gap["recommendations"]}
    face = recs["feature:face_first_second"]
    assert face["do"] == "больше" and face["audience_viral"] == 100 and face["own"] == 0
    assert "доказано" in face["literature"]
    assert any(ex["hook"] == WINNER for ex in gap["exemplars"])

    saved = target.latest_profile(store, "me")
    assert saved["profile"]["segments"][0]["name"] == "Малый бизнес"
    assert "Лицо в первую секунду" in render.render_gap(gap, render.Plain)
    assert "Контент для этой же аудитории" in learning.build_context(store, "me").to_prompt()


async def test_target_without_instagram_lists_unverified_candidates(store, client):
    client.responder = responder
    store.update_profile("me", niche="таргет")
    original = responder

    def no_demographics(name, params):
        if name == "AudienceProfile":
            data = copy.deepcopy(original.__wrapped__(name, params)) if hasattr(original, "__wrapped__") else None
            return data or {**CANNED_PROFILE}
        return original(name, params)

    client.responder = no_demographics
    result = await Scout(store, HookEngine(store, client), config=ScoutConfig(literature_every_days=0)).target_audience("me")
    assert result["gap"] is None and all(c["verified"] is None for c in result["creators"])
    assert Corpus(store).watchlist("me") == []
    assert "нужна проверка" in render.render_audience_profile(target.latest_profile(store, "me"), render.Plain)


CANNED_PROFILE = {"summary": "—", "demographics": "нет данных", "segments": [], "interests": [], "pains": [],
                  "desires": [], "language_style": "—", "resonating_content": [], "search_keywords": [],
                  "hashtags": [], "creator_types": [], "confidence": "low", "data_gaps": ["нет данных Instagram"]}
