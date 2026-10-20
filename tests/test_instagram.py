import time

import pytest

from hookbot import cli, render
from hookbot.core import instagram
from hookbot.core.instagram import InstagramError, InstagramGraph
from hookbot.engine import HookEngine
from hookbot.scout import CycleSummary, Scout, ScoutConfig

ALL_SCOPES = ["instagram_basic", "pages_read_engagement", "instagram_manage_insights", "instagram_manage_comments",
              "pages_show_list"]


class Meta:
    """The Graph API as a freshly set-up account sees it; tweak attributes to break one thing at a time."""

    def __init__(self):
        self.scopes = list(ALL_SCOPES)
        self.valid = True
        self.days_left = 59
        self.pages = [{"name": "Моя страница", "instagram_business_account": {"id": "1784", "username": "me_reels"}}]
        self.skip_rate = True
        self.followers = 900
        self.discovery = True

    def __call__(self, path, params):
        if path == "debug_token":
            data = {"is_valid": self.valid, "scopes": self.scopes,
                    "expires_at": int(time.time() + self.days_left * 86400) if self.days_left is not None else 0}
            return {"data": data if self.valid else {**data, "error": {"message": "Session has expired"}}}
        if path == "me/accounts":
            if "pages_show_list" not in self.scopes:
                raise InstagramError("(#10) Requires pages_show_list permission")
            return {"data": self.pages}
        fields = params.get("fields", "")
        if "business_discovery" in fields:
            if not self.discovery:
                raise InstagramError("(#10) Application does not have permission for this action")
            return {"business_discovery": {"username": "me_reels", "media": {"data": [
                {"id": "r1", "media_product_type": "REELS", "view_count": 1200}]}}}
        if path == "1784":
            return {"username": "me_reels", "followers_count": self.followers}
        if path == "1784/media":
            return {"data": [{"id": "r1", "media_product_type": "REELS"}, {"id": "p1", "media_type": "IMAGE"}]}
        if path == "1784/insights":
            if self.followers < 100:
                raise InstagramError("(#100) Not enough followers")
            return {"data": [{"name": "follower_demographics", "total_value": {"breakdowns": [{"results": [
                {"dimension_values": ["25-34"], "value": 70}, {"dimension_values": ["35-44"], "value": 30}]}]}}]}
        if path == "r1/insights":
            values = {"views": 1500, "reels_skip_rate": 41.0}
            names = params["metric"].split(",")
            return {"data": [{"name": n, "values": [{"value": values[n]}]} for n in names
                             if n in values and (n != "reels_skip_rate" or self.skip_rate)]}
        if path == "r1/comments":
            if "instagram_manage_comments" not in self.scopes:
                raise InstagramError("(#10) Requires instagram_manage_comments permission")
            return {"data": []}
        raise AssertionError(f"unexpected call {path} {params}")


def statuses(meta, **kwargs):
    return {s["step"]: s for s in instagram.check_connection(InstagramGraph("t", getter=meta), **kwargs)}


def test_account_id_is_found_from_the_token():
    meta = Meta()
    ig = InstagramGraph("t", getter=meta)
    assert ig.user_id == "1784" and ig.own_profile()["username"] == "me_reels"
    assert InstagramGraph("t", "999", getter=meta).user_id == "999"  # an explicit id wins

    meta.pages.append({"name": "Вторая", "instagram_business_account": {"id": "1785", "username": "second"}})
    with pytest.raises(InstagramError, match="@second"):
        InstagramGraph("t", getter=meta).user_id
    meta.pages = [{"name": "Без Instagram"}]
    with pytest.raises(InstagramError, match="страниц"):
        InstagramGraph("t", getter=meta).user_id


def test_token_info_reports_days_left():
    info = InstagramGraph("t", getter=Meta()).token_info()
    assert info["valid"] and 58 <= info["days_left"] <= 59 and "instagram_basic" in info["scopes"]
    meta = Meta()
    meta.days_left = None
    assert InstagramGraph("t", getter=meta).token_info()["days_left"] is None  # never expires


def test_check_connection_all_green():
    steps = statuses(Meta())
    assert all(s["ok"] is True for s in steps.values()), steps
    assert "IG_USER_ID=1784" in steps["Аккаунт Instagram"]["detail"]
    assert "25-34" in steps["Демография подписчиков"]["detail"]
    assert "Можно запускать" in render.render_connection(list(steps.values()), render.Plain)


def test_check_connection_explains_each_problem():
    meta = Meta()
    meta.scopes.remove("instagram_manage_insights")
    meta.scopes.remove("instagram_manage_comments")
    meta.skip_rate, meta.followers, meta.days_left = False, 40, 3
    steps = statuses(meta)
    assert steps["Разрешения"]["ok"] is False and "instagram_manage_insights" in steps["Разрешения"]["detail"]
    assert steps["Токен"]["ok"] is None and "Extend" in steps["Токен"]["fix"]
    assert steps["Skip rate"]["ok"] is None and steps["Комментарии"]["ok"] is None
    assert steps["Демография подписчиков"]["ok"] is None and "100" in steps["Демография подписчиков"]["fix"]

    meta = Meta()
    meta.discovery = False
    assert statuses(meta)["Поиск авторов (Business Discovery)"]["ok"] is False

    meta = Meta()
    meta.valid = False
    steps = statuses(meta)
    assert list(steps) == ["Токен"] and "expired" in steps["Токен"]["detail"]


def test_ig_check_command(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("IG_ACCESS_TOKEN", raising=False)
    assert cli.main(["--db", str(tmp_path / "db"), "ig-check"]) == 1
    assert "INSTAGRAM_SETUP" in capsys.readouterr().err

    monkeypatch.setenv("IG_ACCESS_TOKEN", "t")
    monkeypatch.delenv("IG_USER_ID", raising=False)
    meta = Meta()
    monkeypatch.setattr(instagram.InstagramGraph, "_http_get", lambda self, path, params: meta(path, params))
    assert cli.main(["--db", str(tmp_path / "db"), "ig-check"]) == 0
    assert "✅ Аккаунт Instagram: @me_reels" in capsys.readouterr().out
    meta.discovery = False
    assert cli.main(["--db", str(tmp_path / "db"), "ig-check"]) == 1


async def test_scout_warns_before_the_token_expires(store, client):
    meta = Meta()
    meta.days_left = 5
    scout = Scout(store, HookEngine(store, client), ig=InstagramGraph("t", getter=meta),
                  config=ScoutConfig(literature_every_days=0))
    summary = CycleSummary()
    await scout.check_token(summary)
    assert any("истекает через 5 дн." in a for a in summary.alerts)
    again = CycleSummary()
    await scout.check_token(again)  # checked at most once a day
    assert again.alerts == []
