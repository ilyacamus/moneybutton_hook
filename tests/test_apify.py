"""Public Instagram data through Apify, end to end with a fake Apify API that answers like the real one."""

import random
import re
from datetime import datetime, timedelta, timezone

import pytest

from hookbot import render
from hookbot.core import apify, audience, instagram, target
from hookbot.core.apify import ApifyClient, ApifyError, ApifyInstagram
from hookbot.core.corpus import Corpus
from hookbot.engine import HookEngine
from hookbot.scout import Scout, ScoutConfig
from test_audience import BAD, GOOD, blind_responder
from test_target import PLAIN, WINNER, responder


def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", ".000Z")


class FakeApify:
    """Answers run-sync-get-dataset-items like the Apify actors do (field names of apify/instagram-* actors)."""

    def __init__(self, clip, *, own_reels=12, seed=5):
        self.clip = clip
        self.runs = []
        self.datasets, self.run_state = {}, {}
        self.used_usd = 0.0
        self.token_ok = True
        rng = random.Random(seed)
        self.accounts = {"me_reels": [], "coach_a": [], "coach_b": [], "coach_c": [], "coach_d": []}
        for i in range(own_reels):
            good = i % 2 == 0
            views = int(rng.uniform(30_000, 60_000) if good else rng.uniform(8_000, 12_000))
            self.accounts["me_reels"].append(self._reel(f"Me{i:02d}x", GOOD if good else BAD, views,
                                                        likes=int(views * (0.06 if good else 0.02)), days=5 + 3 * i))
        for name in ("coach_a", "coach_b", "coach_c", "coach_d"):
            for i in range(8):
                viral = i in (1, 5)
                self.accounts[name].append(self._reel(f"{name[-1]}{i}Code", WINNER if viral else PLAIN,
                                                      50_000 + i if viral else 9_000 + 100 * i, likes=300, days=5 + 3 * i))

    def _reel(self, code, caption, views, *, likes, days):
        return {"id": str(abs(hash(code)) % 10**12), "shortCode": code, "caption": caption,
                "url": f"https://www.instagram.com/reel/{code}/", "timestamp": ago(days), "productType": "clips",
                "videoPlayCount": views, "videoViewCount": views // 2, "likesCount": likes, "commentsCount": 7,
                "videoUrl": self.clip.as_uri(), "displayUrl": "", "ownerUsername": "?",
                "latestComments": [{"text": "Где взять деньги на рекламу?"}]}

    def __call__(self, method, path, body):
        """Apify API v2: start a run, poll it, read its dataset; account limits; who am I."""
        if path == "users/me":
            if not self.token_ok:
                raise ApifyError("Apify: User was not found or authentication token is not valid")
            return {"data": {"username": "apify_user"}}
        if path == "users/me/limits":
            return {"data": {"limits": {"maxMonthlyUsageUsd": 5}, "current": {"monthlyUsageUsd": self.used_usd}}}
        if path.startswith("actor-runs/"):  # the poll: the run finished meanwhile
            run_id = path.split("/")[1].split("?")[0]
            return {"data": {**self.run_state[run_id], "status": "SUCCEEDED"}}
        if path.startswith("datasets/"):
            return self.datasets[path.split("/")[1]]
        actor = re.match(r"acts/([^/]+)/runs", path).group(1)
        self.runs.append((actor, body))
        run_id = f"run{len(self.runs)}"
        self.datasets[f"ds{run_id}"] = self.items(actor, body)
        # Every other run is still running after the first wait, to exercise polling.
        self.run_state[run_id] = {"id": run_id, "defaultDatasetId": f"ds{run_id}", "usageTotalUsd": 0.01,
                                  "status": "RUNNING" if len(self.runs) % 2 else "SUCCEEDED"}
        return {"data": self.run_state[run_id]}

    def items(self, actor, body):
        if actor == "apify~instagram-reel-scraper":
            name = body["username"][0]
            if name not in self.accounts:
                return [{"error": "not_found", "errorDescription": "Profile does not exist"}]
            return [{**r, "ownerUsername": name} for r in self.accounts[name][: body["resultsLimit"]]]
        if actor == "apify~instagram-profile-scraper":
            name = body["usernames"][0]
            if name not in self.accounts:
                return [{"error": "not_found"}]
            return [{"username": name, "fullName": "Me", "biography": "Таргет для малого бизнеса",
                     "followersCount": 2400, "postsCount": 40, "private": False}]
        if actor == "apify~instagram-comment-scraper":
            return [{"text": "Спасибо, про бюджет полезно", "ownerUsername": "fan"}][: body["resultsLimit"]]
        if actor == "apify~instagram-scraper":
            return [{"username": "coach_d", "followersCount": 9000, "private": False},
                    {"username": "closed_one", "private": True}]
        raise AssertionError(f"unexpected actor {actor}")


@pytest.fixture
def fake(clip):
    return FakeApify(clip)


@pytest.fixture
def ig(fake):
    return ApifyInstagram("tok", "@Me_Reels", client=ApifyClient("tok", requester=fake), cache_hours=12)


def test_items_are_read_through_aliases():
    media = apify.media_from_item({"shortCode": "Abc123", "videoViewCount": 900, "likesCount": 5, "takenAt": 1_700_000_000,
                                   "videoUrl": "https://x/v.mp4"})
    assert media["view_count"] == 900 and media["timestamp"].startswith("2023-11-14") and media["media_product_type"] == "REELS"
    assert media["permalink"] == "https://www.instagram.com/reel/Abc123/"
    assert apify.media_from_item({"error": "not_found"}) is None
    p = apify.profile_from_item({"username": "Coach_A", "followersCount": "1200", "private": True})
    assert p == {**p, "username": "coach_a", "followers_count": 1200, "is_private": True}


def test_runs_are_cached_and_widened_on_demand(ig, fake):
    profile, media = ig.discover("coach_a", 5)
    assert len(media) == 5 and media[0]["view_count"] == 9_000 and profile["username"] == "coach_a"
    ig.discover("@COACH_A", 5)
    ig.discover("coach_a", 3)
    assert len(fake.runs) == 1  # one paid run
    assert len(ig.discover("coach_a", 8)[1]) == 8 and len(fake.runs) == 2  # more reels → a new run
    with pytest.raises(instagram.InstagramError, match="does not exist"):
        ig.discover("nobody_here")


def test_own_channel_without_skip_rate(ig):
    records = instagram.own_records(ig, workspace="me", limit=50)
    assert len(records) == 12 and all(r.source == "instagram_own" and r.creator == "ig:me_reels" for r in records)
    assert records[0].views >= 30_000 and records[0].skip_rate is None and records[0].external_id == "Me00x"
    assert records[0].followers == 2400 and records[0].video_url.startswith("file://")


def test_stats_table_adds_private_numbers(ig, tmp_path):
    records = instagram.own_records(ig, workspace="me", limit=50)
    table = tmp_path / "stats.csv"
    table.write_text("Ссылка;Просмотры;Процент пропусков;Сохранения\n"
                     "https://www.instagram.com/reel/Me00x/;41 000;28,5%;120\nMe01x;9 900;61%;3\nunknown;1;1;1\n",
                     encoding="utf-8")
    rows = instagram.import_insights_csv(table, workspace="me")
    assert instagram.merge_own_stats(records, rows) == 2
    first, second = records[0], records[1]
    assert (first.skip_rate, first.saves, first.views) == (28.5, 120, 41_000) and second.skip_rate == 61
    assert records[2].skip_rate is None


def test_from_env_prefers_the_official_api(monkeypatch):
    assert instagram.from_env({"APIFY_TOKEN": "a", "IG_USERNAME": "me"}).kind == "apify"
    assert isinstance(instagram.from_env({"APIFY_TOKEN": "a", "IG_ACCESS_TOKEN": "g"}), instagram.InstagramGraph)
    assert instagram.from_env({"APIFY_TOKEN": "${user_config.apify_token}"}) is None
    assert instagram.from_env({}) is None


def test_connection_check(ig, fake):
    steps = {s["step"]: s for s in instagram.check_connection(ig, discover_username="coach_b")}
    assert steps["Токен Apify"]["ok"] and "@me_reels" in steps["Ваш Instagram"]["detail"]
    assert "с просмотрами: 12" in steps["Ваши Reels"]["detail"] and steps["Скачивание видео"]["ok"]
    assert steps["Комментарии"]["ok"] and steps["Демография"]["ok"] is None and steps["Чужие Reels"]["ok"]
    assert "Можно запускать" in render.render_connection(list(steps.values()), render.Plain)

    fake.token_ok = False
    steps = instagram.check_connection(ApifyInstagram("bad", "me_reels", client=ApifyClient("bad", requester=fake)))
    assert len(steps) == 1 and steps[0]["ok"] is False and "APIFY_TOKEN" in steps[0]["fix"]
    no_user = instagram.check_connection(ApifyInstagram("tok", "", client=ApifyClient("tok", requester=FakeApify(fake.clip))))
    assert no_user[-1]["step"] == "Ваш Instagram" and "IG_USERNAME" in no_user[-1]["fix"]


def test_http_errors_become_readable(monkeypatch):
    import io
    import urllib.error

    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("u", 408, "timeout", {}, io.BytesIO(b""))

    monkeypatch.setattr(apify.urllib.request, "urlopen", fail)
    with pytest.raises(ApifyError, match="300"):
        ApifyClient("t").run("apify/instagram-reel-scraper", {})


def test_budget_guard_and_transcripts(fake, monkeypatch):
    client = ApifyClient("tok", requester=fake)
    fake.used_usd = 4.9
    with pytest.raises(ApifyError, match="осталось \\$0.10"):
        ApifyInstagram("tok", "me_reels", client=client).discover("coach_a", 50)  # 50 × $0.003 > $0.10
    assert fake.runs == []  # nothing was started
    fake.used_usd = 0
    _, media = ApifyInstagram("tok", "me_reels", client=client).discover("coach_a", 5)
    assert "includeTranscript" not in fake.runs[-1][1] and client.last_cost_usd == 0.01
    monkeypatch.setattr(apify, "TRANSCRIPTS", True)
    ApifyInstagram("tok", "me_reels", client=client).discover("coach_b", 5)
    assert fake.runs[-1][1]["includeTranscript"] is True


def test_source_transcript_reaches_the_record(fake):
    fake.accounts["coach_a"][0]["transcript"] = "Смотри на эту линию на графике, за ней следят все фонды и частные трейдеры, поэтому"
    _, media = ApifyInstagram("tok", "me_reels", client=ApifyClient("tok", requester=fake)).discover("coach_a", 3)
    rec = instagram.records_from_discovery({"username": "coach_a"}, media)[0]
    assert rec.transcript.startswith("Смотри на эту линию")
    from hookbot.scout import opening_words
    assert opening_words(rec.transcript, 5) == "Смотри на эту линию на …"


def test_proxy_injected_token_sends_no_header(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"data": {"username": "u"}}'

    def urlopen(request, timeout):
        seen["auth"] = request.get_header("Authorization")
        return Resp()

    monkeypatch.setattr(apify.urllib.request, "urlopen", urlopen)
    ApifyClient(apify.PROXY_AUTH).me()
    assert seen["auth"] is None
    ApifyClient("tok").me()
    assert seen["auth"] == "Bearer tok"


async def test_audit_on_public_numbers(store, client, ig):
    client.responder = blind_responder
    scout = Scout(store, HookEngine(store, client), ig=ig, config=ScoutConfig(literature_every_days=0))
    report = await scout.audit("me")
    assert report["n_reels"] == 12 and report["n_studied"] == 12 and report["n_with_skip_rate"] == 0
    views = {e["variable"]: e for e in report["views_effects"]}
    engagement = {e["variable"]: e for e in report["engagement_effects"]}
    assert views["criterion:curiosity"]["rho"] > 0.7 and views["criterion:curiosity"]["q"] < 0.1
    assert engagement["feature:greeting_start"]["rho"] < -0.7
    assert report["hold_effects"] == [] and report["median_engagement"] > 0
    text = render.render_audience(report, render.Plain)
    assert "просмотрами" in text and "Skip rate нет" in text
    assert "просмотры выше" in audience.prompt_block(report)


async def test_target_audience_through_apify(store, client, tmp_path, ig, fake):
    store.update_profile("me", niche="таргет")
    client.responder = lambda name, params: (
        {**blind_responder(name, params), "face_first_second": False} if name == "ReelAnalysis" else None)
    scout = Scout(store, HookEngine(store, client), ig=ig, config=ScoutConfig(literature_every_days=0))
    await scout.audit("me")

    client.responder = responder_without_demographics
    runs_before = len(fake.runs)
    result = await scout.target_audience("me", study_budget=40)

    creators = {c["username"]: c for c in result["creators"]}
    assert creators["coach_d"]["why"].startswith("поиск Instagram")  # found by Instagram's own search
    assert "closed_one" not in creators and not creators["invented_account"]["verified"]
    assert {w["username"] for w in Corpus(store).watchlist("me")} == {"coach_a", "coach_b", "coach_c", "coach_d"}
    reel_runs = [b["username"][0] for a, b in fake.runs[runs_before:] if a == "apify~instagram-reel-scraper"]
    assert sorted(reel_runs) == ["coach_a", "coach_b", "coach_c", "coach_d", "invented_account"]  # one run per creator

    gap = result["gap"]
    assert gap["n_creators"] == 4 and gap["n_viral"] == 8 and gap["n_own"] == 12
    face = {r["variable"]: r for r in gap["recommendations"]}["feature:face_first_second"]
    assert face["do"] == "больше" and face["own"] == 0
    assert "Контент для этой же аудитории" in target.prompt_block(gap)


def responder_without_demographics(name, params):
    if name == "AudienceProfile":
        text = params["messages"][0]["content"][-1]["text"]
        assert "Где взять деньги" in text or "про бюджет" in text  # viewers' comments reached the model
        return {"summary": "Предприниматели, считающие рекламный бюджет", "demographics": "нет данных",
                "segments": [{"name": "Малый бизнес", "description": "Сами ведут рекламу", "weight": "основной",
                              "evidence": "комментарии"}],
                "interests": ["таргет"], "pains": ["слив бюджета"], "desires": ["окупаемая реклама"],
                "language_style": "на «ты»", "resonating_content": ["разборы ошибок"],
                "search_keywords": ["таргет малый бизнес"], "hashtags": ["#таргет"], "creator_types": ["таргетологи"],
                "confidence": "low", "data_gaps": ["нет демографии"]}
    return responder(name, params)
