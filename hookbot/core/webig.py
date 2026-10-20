"""Own Instagram collector: public web data without login and without third parties.

Mode A runs on the creator's own computer (a home IP is what Instagram
tolerates for anonymous reads); mode B is the same code in the cloud behind a
residential proxy (``IG_WEB_PROXY``). It exposes the ``InstagramGraph``
methods the scout uses, so the rest of the system does not care where data
comes from:

* ``discover`` — the profile and its latest posts (the public profile
  endpoint returns about 12; polling every day accumulates the history);
* ``related`` — accounts Instagram itself suggests next to a profile, the
  scout's way to grow the niche on its own;
* ``search_accounts`` — Instagram's public account search.

It is polite on purpose: one request every ``IG_WEB_MIN_INTERVAL`` seconds
with jitter, and a cooldown as soon as Instagram asks to slow down. Every
answer is read through aliases, because Instagram changes field names.
Collecting without Instagram's permission is against its terms of use: no
account is logged in and only public data of open accounts is read.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from .instagram import InstagramError

BASE = "https://www.instagram.com"
APP_ID = "936619743392459"  # the public web app id the site itself sends
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/129.0 Safari/537.36")

Fetcher = Callable[[str, dict[str, str]], tuple[int, bytes]]


class Blocked(InstagramError):
    """Instagram asked to slow down or wants a login: stop for a while."""


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _count(node: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, dict):
            value = value.get("count")
        if value is not None:
            return _int(value)
    return None


def media_from_node(node: dict[str, Any]) -> dict[str, Any] | None:
    """A post of the public profile endpoint in the shape of a Graph API media object."""
    code = node.get("shortcode") or node.get("code")
    if not code:
        return None
    is_video = bool(node.get("is_video")) or node.get("__typename") in ("GraphVideo", "XDTGraphVideo") \
        or node.get("media_type") == 2
    product = str(node.get("product_type") or "").lower()
    caption_edges = ((node.get("edge_media_to_caption") or {}).get("edges") or [])
    caption = caption_edges[0]["node"].get("text", "") if caption_edges else ((node.get("caption") or {}).get("text", "")
                                                                           if isinstance(node.get("caption"), dict) else "")
    taken = node.get("taken_at_timestamp") or node.get("taken_at")
    return {
        "id": str(code),
        "media_product_type": "REELS" if product == "clips" or is_video else "FEED",
        "media_type": "VIDEO" if is_video else "IMAGE",
        "caption": caption or "",
        "permalink": f"{BASE}/reel/{code}/",
        "timestamp": datetime.fromtimestamp(int(taken), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000") if taken else None,
        "view_count": _count(node, "video_play_count", "play_count", "video_view_count", "view_count"),
        "like_count": _count(node, "edge_liked_by", "edge_media_preview_like", "like_count"),
        "comments_count": _count(node, "edge_media_to_comment", "comment_count"),
        "media_url": node.get("video_url") or "",
        "thumbnail_url": node.get("display_url") or node.get("thumbnail_src") or "",
        "duration": node.get("video_duration"),
    }


class WebInstagram:
    """Anonymous public reads of instagram.com with polite pacing (``InstagramGraph``-compatible)."""

    kind = "web"

    def __init__(self, *, fetch: Fetcher | None = None, min_interval: float | None = None,
                 proxy: str | None = None, cooldown_s: float = 30 * 60, timeout: float = 30):
        self.min_interval = float(os.getenv("IG_WEB_MIN_INTERVAL", "20")) if min_interval is None else min_interval
        self.cooldown_s = cooldown_s
        self.timeout = timeout
        self.blocked_until = 0.0
        self.requests = 0
        self._last = 0.0
        self._lock = threading.Lock()
        handlers: list[Any] = [urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())]
        proxy = proxy if proxy is not None else os.getenv("IG_WEB_PROXY", "")
        if proxy:  # mode B: residential proxy in the cloud
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        self._opener = urllib.request.build_opener(*handlers)
        self._fetch = fetch or self._http
        self._profiles: dict[str, tuple[float, dict[str, Any]]] = {}

    @property
    def user_id(self) -> str:
        raise InstagramError("Свой сборщик не читает закрытую статистику аккаунта: только публичные данные")

    def _http(self, url: str, headers: dict[str, str]) -> tuple[int, bytes]:
        request = urllib.request.Request(url, headers=headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read() or b""
        except (urllib.error.URLError, TimeoutError) as exc:
            raise InstagramError(f"Instagram недоступен: {exc}") from exc

    def _get(self, path: str, params: dict[str, str], referer: str = "") -> dict[str, Any]:
        if time.time() < self.blocked_until:
            wait = int(self.blocked_until - time.time())
            raise Blocked(f"Instagram попросил паузу: следующая попытка через {wait // 60} мин")
        with self._lock:  # one request at a time, spaced like a person browsing
            pause = self._last + self.min_interval * random.uniform(0.8, 1.4) - time.time()
            if pause > 0:
                time.sleep(pause)
            self._last = time.time()
            self.requests += 1
        url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
        status, body = self._fetch(url, {
            "User-Agent": USER_AGENT, "X-IG-App-ID": APP_ID, "Accept": "*/*", "Accept-Language": "ru,en;q=0.9",
            "X-Requested-With": "XMLHttpRequest", "Referer": referer or f"{BASE}/",
        })
        text = body.decode("utf-8", "replace")
        if status in (401, 429) or "Please wait a few minutes" in text or '"require_login":true' in text:
            self.blocked_until = time.time() + self.cooldown_s
            raise Blocked(f"Instagram ограничил запросы (HTTP {status}): пауза {int(self.cooldown_s // 60)} мин")
        if status == 404:
            raise InstagramError("Аккаунт не найден")
        if status >= 400:
            raise InstagramError(f"Instagram ответил HTTP {status}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise Blocked("Instagram вернул страницу вместо данных (вероятно, просит вход): пауза") from exc

    def _user(self, username: str) -> dict[str, Any]:
        username = username.strip().lstrip("@").lower()
        hit = self._profiles.get(username)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        data = self._get("api/v1/users/web_profile_info/", {"username": username}, f"{BASE}/{username}/")
        user = (data.get("data") or {}).get("user") or data.get("user")
        if not user:
            raise InstagramError(f"@{username}: профиль не найден")
        self._profiles[username] = (time.time(), user)
        return user

    # --- the InstagramGraph interface ----------------------------------------

    def profile(self, username: str) -> dict[str, Any]:
        user = self._user(username)
        return {
            "username": str(user.get("username") or username).lower(),
            "full_name": user.get("full_name") or "",
            "biography": user.get("biography") or "",
            "followers_count": _count(user, "edge_followed_by", "follower_count"),
            "media_count": _count(user, "edge_owner_to_timeline_media", "media_count"),
            "is_private": bool(user.get("is_private")),
            "is_business": bool(user.get("is_business_account") or user.get("is_professional_account")),
            "category": user.get("category_name") or "",
        }

    def discover(self, username: str, limit: int = 25) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        user = self._user(username)
        if user.get("is_private"):
            raise InstagramError(f"@{username.lstrip('@').lower()}: закрытый аккаунт")
        timeline = (user.get("edge_owner_to_timeline_media") or {}).get("edges") or []
        clips = (user.get("edge_felix_video_timeline") or {}).get("edges") or []
        seen, media = set(), []
        for edge in timeline + clips:
            m = media_from_node(edge.get("node") or edge)
            if m and m["id"] not in seen:
                seen.add(m["id"])
                media.append(m)
        if not any(m["media_product_type"] == "REELS" for m in media):
            raise InstagramError(f"@{username.lstrip('@').lower()}: нет публичных Reels в последних публикациях")
        profile = self.profile(username)
        return {"username": profile["username"], "followers_count": profile["followers_count"],
                "media_count": profile["media_count"]}, media[:limit]

    def related(self, username: str) -> list[str]:
        """Accounts Instagram shows next to this profile (empty when it shows none)."""
        user = self._user(username)
        edges = (user.get("edge_related_profiles") or {}).get("edges") or []
        return [str((e.get("node") or e).get("username")).lower() for e in edges
                if (e.get("node") or e).get("username") and not (e.get("node") or e).get("is_private")]

    def search_accounts(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        try:
            data = self._get("web/search/topsearch/", {"context": "user", "query": query})
        except InstagramError:
            return []
        out = []
        for u in data.get("users") or []:
            user = u.get("user") or u
            if user.get("username"):
                out.append({"username": str(user["username"]).lower(), "full_name": user.get("full_name") or "",
                            "followers_count": _int(user.get("follower_count")), "is_private": bool(user.get("is_private")),
                            "biography": "", "media_count": None, "is_business": False, "category": ""})
        return out[:limit]

    def comments(self, media_id: str, limit: int = 50) -> list[str]:
        return []  # comments need a login; the audience profile then relies on hooks and captions

    def demographics(self, metric: str = "follower_demographics", timeframe: str | None = None) -> dict[str, dict[str, float]]:
        return {}

    def token_info(self) -> dict[str, Any]:
        blocked = time.time() < self.blocked_until
        return {"valid": True, "scopes": [], "days_left": None, "error": "", "blocked": blocked}


def check_connection(ig: WebInstagram, *, discover_username: str | None = None) -> list[dict[str, Any]]:
    """Can this computer read public Instagram data without a login (see ``instagram.check_connection``)."""
    steps: list[dict[str, Any]] = []

    def step(name: str, ok: bool | None, detail: str, fix: str = "") -> None:
        steps.append({"step": name, "ok": ok, "detail": detail, "fix": fix})

    name = (discover_username or "instagram").lstrip("@")
    try:
        profile, media = ig.discover(name, 12)
    except Blocked as exc:
        step("Доступ к Instagram", False, str(exc), "Instagram ограничил этот IP. С домашнего интернета обычно "
             "проходит через 30–60 минут; в облаке нужен прокси (IG_WEB_PROXY)")
        return steps
    except InstagramError as exc:
        step("Доступ к Instagram", False, str(exc), "Проверьте интернет и что аккаунт открыт и публикует Reels")
        return steps
    reels = [m for m in media if m["media_product_type"] == "REELS"]
    step("Доступ к Instagram", True, f"@{profile['username']}: {len(media)} публикаций, Reels: {len(reels)}")
    with_views = sum(1 for m in reels if m["view_count"] is not None)
    with_video = [m for m in reels if m["media_url"]]
    step("Просмотры", True if with_views else None, f"есть у {with_views} из {len(reels)} Reels",
         "" if with_views else "Instagram не отдал просмотры: разметка «залетел/обычный» будет недоступна")
    step("Видео", True if with_video else None, f"ссылка на видео у {len(with_video)} из {len(reels)} Reels",
         "" if with_video else "Разбор пойдёт по обложкам")
    related = ig.related(name)
    step("Похожие аккаунты", True if related else None, f"{len(related)}: " + ", ".join(f"@{u}" for u in related[:5]),
         "" if related else "Instagram не показал похожих; новые авторы будут искаться поиском")
    return steps
