"""Own collector: public instagram.com data without login, pacing, cooldowns and the niche growing on its own."""
import json
import time

import pytest

from hookbot import cli
from hookbot.core import instagram, webig
from hookbot.core.corpus import Corpus
from hookbot.core.webig import Blocked, WebInstagram
from hookbot.engine import HookEngine
from hookbot.scout import CycleSummary, Scout, ScoutConfig

DAY = 86400


def node(code, views, caption, *, days, video=True):
    return {"node": {"__typename": "GraphVideo" if video else "GraphImage", "shortcode": code, "is_video": video,
                     "product_type": "clips" if video else "feed", "video_view_count": views if video else None,
                     "video_url": f"https://cdn.example/{code}.mp4" if video else None,
                     "display_url": f"https://cdn.example/{code}.jpg", "taken_at_timestamp": int(time.time() - days * DAY),
                     "edge_liked_by": {"count": 10}, "edge_media_to_comment": {"count": 2},
                     "edge_media_to_caption": {"edges": [{"node": {"text": caption}}]}}}


class FakeSite:
    """instagram.com's public profile endpoint for a handful of accounts."""

    def __init__(self):
        self.calls = []
        self.status = 200
        self.users = {
            "coach_a": self.user("coach_a", "Трейдинг и крипта", ["coach_b", "chef_c", "closed"], "Биткоин: где вход"),
            "coach_b": self.user("coach_b", "", [], "Как торговать фьючерсы без слива депозита"),
            "chef_c": self.user("chef_c", "Рецепты за 5 минут", [], "Паста с грибами"),
            "closed": {**self.user("closed", "", [], "x"), "is_private": True},
        }

    @staticmethod
    def user(name, bio, related, caption):
        return {"username": name, "full_name": name.title(), "biography": bio, "is_private": False,
                "edge_followed_by": {"count": 5000},
                "edge_owner_to_timeline_media": {"count": 40, "edges": [
                    node(f"{name}{i}", 50_000 if i == 1 else 9_000 + 100 * i, caption, days=5 + 2 * i) for i in range(8)
                ] + [node(f"{name}p", None, caption, days=2, video=False)]},
                "edge_related_profiles": {"edges": [{"node": {"username": r, "is_private": r == "closed"}} for r in related]}}

    def __call__(self, url, headers):
        self.calls.append(url)
        assert headers["X-IG-App-ID"] == webig.APP_ID and "Mozilla" in headers["User-Agent"]
        if self.status != 200:
            return self.status, b'{"message": "Please wait a few minutes before you try again."}'
        if "web_profile_info" in url:
            name = url.split("username=")[1]
            if name not in self.users:
                return 404, b"{}"
            return 200, json.dumps({"data": {"user": self.users[name]}, "status": "ok"}).encode()
        if "topsearch" in url:
            return 200, json.dumps({"users": [{"user": {"username": "coach_b", "follower_count": 900}}]}).encode()
        return 404, b""


@pytest.fixture
def site():
    return FakeSite()


@pytest.fixture
def web(site):
    return WebInstagram(fetch=site, min_interval=0)


def test_profile_and_reels(web, site):
    profile, media = web.discover("@Coach_A")
    reels = [m for m in media if m["media_product_type"] == "REELS"]
    assert profile == {"username": "coach_a", "followers_count": 5000, "media_count": 40}
    assert len(reels) == 8 and reels[1]["view_count"] == 50_000 and reels[0]["media_url"].endswith(".mp4")
    records = instagram.records_from_discovery(profile, media, niche="крипта")
    assert len(records) == 8 and records[0].creator == "ig:coach_a" and records[0].published_at.endswith("+0000")
    assert web.related("coach_a") == ["coach_b", "chef_c"]  # private accounts are dropped
    assert len(site.calls) == 1  # the profile was read once and reused
    assert web.search_accounts("трейдинг")[0]["username"] == "coach_b"
    with pytest.raises(instagram.InstagramError, match="закрытый"):
        web.discover("closed")


def test_blocks_trigger_a_cooldown(web, site):
    site.status = 429
    with pytest.raises(Blocked, match="пауза"):
        web.discover("coach_a")
    site.status = 200
    with pytest.raises(Blocked, match="через"):
        web.discover("coach_b")  # still cooling down: no request is sent
    assert len(site.calls) == 1


def test_requests_are_spaced(site):
    web = WebInstagram(fetch=site, min_interval=0.2)
    start = time.time()
    web.discover("coach_a")
    web.discover("coach_b")
    assert time.time() - start >= 0.15


def test_connection_check_and_source_selection(web):
    steps = {s["step"]: s for s in instagram.check_connection(web, discover_username="coach_a")}
    assert steps["Доступ к Instagram"]["ok"] and steps["Просмотры"]["ok"] and "@coach_b" in steps["Похожие аккаунты"]["detail"]
    assert instagram.from_env({"IG_SOURCE": "web"}).kind == "web"
    assert instagram.from_env({"IG_SOURCE": "web", "IG_ACCESS_TOKEN": "t"}).kind == "web"  # explicit choice wins


async def test_scout_grows_the_niche_on_its_own(store, client, web):
    corpus = Corpus(store)
    corpus.watch("cli", "coach_a", niche="крипта")
    scout = Scout(store, HookEngine(store, client), ig=web,
                  config=ScoutConfig(literature_every_days=0, study_per_cycle=0, expand_checks_per_cycle=5))
    summary = await scout.run_once()
    assert summary.collected == 16 and summary.discovered == ["coach_b"]  # the cooking account is off the niche
    assert {e["username"] for e in corpus.watchlist()} == {"coach_a", "coach_b"}
    assert any("@coach_b" in a for a in summary.alerts)
    again = CycleSummary()
    await scout.expand(again)
    assert again.discovered == []  # candidates are checked once


async def test_collection_rotates_through_the_watchlist(store, client, web):
    corpus = Corpus(store)
    for name in ("coach_a", "coach_b", "chef_c"):
        corpus.watch("cli", name)
    scout = Scout(store, HookEngine(store, client), ig=web, config=ScoutConfig(collect_per_cycle=2, expand_per_cycle=0))
    first = [e["username"] for e in scout._due(corpus.watchlist())]
    second = [e["username"] for e in scout._due(corpus.watchlist())]
    assert len(first) == 2 and len(second) == 2 and set(first) | set(second) == {"coach_a", "coach_b", "chef_c"}


def test_seed_command(tmp_path, capsys):
    names = tmp_path / "creators.txt"
    names.write_text("@Kirillmedved77\nevertrader_vlad  # trader\n\nkirillmedved77\n", encoding="utf-8")
    db = str(tmp_path / "db")
    assert cli.main(["--db", db, "scout", "seed", str(names), "--niche", "крипта"]) == 0
    assert "добавлено: 2" in capsys.readouterr().out
