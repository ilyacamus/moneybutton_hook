"""Public Instagram data through Apify scrapers (standard library only).

For accounts without Facebook, where the official Graph API is out of reach.
``ApifyInstagram`` exposes the same methods as ``InstagramGraph``, so the
scout, the channel audit and the target-audience search work unchanged:

* ``discover`` — public reels of any open account: caption, plays, likes,
  comments, video and cover URLs (``apify/instagram-reel-scraper``);
* ``own_profile`` / ``own_media`` — the same for the creator's own account
  (``IG_USERNAME``); skip rate, saves and demographics are private, so they
  stay empty unless the creator adds a stats table;
* ``comments`` — viewers' comments on a reel (``apify/instagram-comment-scraper``);
* ``search_accounts`` — accounts found by Instagram's own search
  (``apify/instagram-scraper``).

Field names differ between actor versions, so items are read through
aliases; every actor is configurable through the environment. Each run is
paid per result, so results are cached for ``APIFY_CACHE_HOURS``.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from .instagram import InstagramError

API_URL = "https://api.apify.com/v2"
ACTORS = {
    "reels": os.getenv("APIFY_REEL_ACTOR", "apify/instagram-reel-scraper"),
    "profile": os.getenv("APIFY_PROFILE_ACTOR", "apify/instagram-profile-scraper"),
    "comments": os.getenv("APIFY_COMMENT_ACTOR", "apify/instagram-comment-scraper"),
    "search": os.getenv("APIFY_SEARCH_ACTOR", "apify/instagram-scraper"),
}
SYNC_LIMIT_S = 300  # run-sync endpoints answer 408 after this

Requester = Callable[[str, str, dict[str, Any] | None], Any]


class ApifyError(InstagramError):
    pass


class ApifyClient:
    def __init__(self, token: str, *, requester: Requester | None = None, timeout: float = SYNC_LIMIT_S + 30):
        self.token = token
        self.timeout = timeout
        self._request = requester or self._http

    def _http(self, method: str, path: str, body: dict[str, Any] | None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{API_URL}/{path}", data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "null")
        except urllib.error.HTTPError as exc:
            if exc.code == 408:
                raise ApifyError(f"Apify: запуск не уложился в {SYNC_LIMIT_S} с — уменьшите число роликов") from exc
            try:
                message = json.loads(exc.read().decode())["error"]["message"]
            except Exception:  # noqa: BLE001 - any unreadable body
                message = str(exc)
            raise ApifyError(f"Apify: {message}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ApifyError(f"Apify недоступен: {exc} (домен api.apify.com должен быть разрешён)") from exc

    def run(self, actor: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Run an actor and return its dataset items (synchronous call, up to 5 minutes)."""
        path = f"acts/{urllib.parse.quote(actor.replace('/', '~'))}/run-sync-get-dataset-items?timeout={SYNC_LIMIT_S}"
        items = self._request("POST", path, payload)
        if not isinstance(items, list):
            raise ApifyError(f"Apify: неожиданный ответ {actor}")
        return [i for i in items if isinstance(i, dict)]

    def me(self) -> dict[str, Any]:
        return (self._request("GET", "users/me", None) or {}).get("data") or {}


def _first(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if item.get(key) not in (None, ""):
            return item[key]
    return None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
    return str(value) if value else None


def _errors(items: list[dict[str, Any]]) -> str:
    return "; ".join(str(_first(i, "errorDescription", "error")) for i in items if i.get("error"))


def media_from_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """An Apify reel/post item in the shape of a Graph API media object."""
    code = _first(item, "shortCode", "shortcode", "code")
    if not code or item.get("error"):
        return None
    product = str(_first(item, "productType", "product_type", "type") or "").lower()
    is_video = bool(_first(item, "videoUrl", "videoPlayCount", "videoViewCount", "playCount")) or product in ("clips", "video", "reel")
    return {
        "id": str(code),
        "media_product_type": "REELS" if is_video else "FEED",
        "media_type": "VIDEO" if is_video else "IMAGE",
        "caption": _first(item, "caption", "text") or "",
        "permalink": _first(item, "url", "permalink") or f"https://www.instagram.com/reel/{code}/",
        "timestamp": _iso(_first(item, "timestamp", "takenAt", "taken_at")),
        # Instagram shows reel plays as "views"; older items only have videoViewCount.
        "view_count": _int(_first(item, "videoPlayCount", "playCount", "videoViewCount", "viewCount")),
        "like_count": _int(_first(item, "likesCount", "likeCount")),
        "comments_count": _int(_first(item, "commentsCount", "commentCount")),
        "media_url": _first(item, "videoUrl") or "",
        "thumbnail_url": _first(item, "displayUrl", "thumbnailUrl", "thumbnail_url") or "",
        "owner": str(_first(item, "ownerUsername", "username") or "").lower(),
        "latest_comments": [c.get("text") for c in item.get("latestComments") or [] if isinstance(c, dict) and c.get("text")],
    }


def profile_from_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "username": str(_first(item, "username", "ownerUsername") or "").lower(),
        "full_name": _first(item, "fullName", "full_name") or "",
        "biography": _first(item, "biography", "bio") or "",
        "followers_count": _int(_first(item, "followersCount", "followers")),
        "media_count": _int(_first(item, "postsCount", "mediaCount")),
        "is_private": bool(item.get("private") or item.get("isPrivate")),
        "is_business": bool(item.get("isBusinessAccount") or item.get("isBusiness")),
        "category": _first(item, "businessCategoryName", "category") or "",
    }


class ApifyInstagram:
    """``InstagramGraph``-compatible access to public Instagram data through Apify."""

    kind = "apify"

    def __init__(self, token: str, username: str = "", *, client: ApifyClient | None = None,
                 cache_hours: float | None = None):
        self.client = client or ApifyClient(token)
        self.username = username.strip().lstrip("@").lower()
        hours = cache_hours if cache_hours is not None else float(os.getenv("APIFY_CACHE_HOURS", "12"))
        self.cache_s = hours * 3600
        self._cache: dict[tuple, tuple[float, Any]] = {}

    @property
    def user_id(self) -> str:
        if not self.username:
            raise InstagramError("Укажите свой Instagram в IG_USERNAME (без @)")
        return self.username

    def _cached(self, key: tuple, fetch: Callable[[], Any]) -> Any:
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.cache_s:
            return hit[1]
        value = fetch()
        self._cache[key] = (time.time(), value)
        return value

    # --- the InstagramGraph interface ----------------------------------------

    def profile(self, username: str) -> dict[str, Any]:
        username = username.strip().lstrip("@").lower()

        def fetch() -> dict[str, Any]:
            items = self.client.run(ACTORS["profile"], {"usernames": [username]})
            found = [i for i in items if not i.get("error")]
            if not found:
                raise InstagramError(f"Аккаунт {username} не найден" + (f": {_errors(items)}" if _errors(items) else ""))
            return profile_from_item(found[0])

        return self._cached(("profile", username), fetch)

    def discover(self, username: str, limit: int = 25) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        username = username.strip().lstrip("@").lower()

        def fetch() -> list[dict[str, Any]]:
            items = self.client.run(ACTORS["reels"], {"username": [username], "resultsLimit": limit})
            media = [m for m in (media_from_item(i) for i in items) if m]
            if not media:
                reason = _errors(items) or "нет публичных Reels: аккаунт закрыт, не существует или пуст"
                raise InstagramError(f"@{username}: {reason}")
            return media

        hit = self._cache.get(("reels", username))
        if hit and hit[1][0] < limit:
            self._cache.pop(("reels", username))  # a fresh run is needed for more reels than were asked before
        _, media = self._cached(("reels", username), lambda: (limit, fetch()))
        return {"username": username, "followers_count": None, "media_count": None}, media[:limit]

    def own_profile(self) -> dict[str, Any]:
        return self.profile(self.user_id)

    def own_media(self, limit: int = 25) -> list[dict[str, Any]]:
        return self.discover(self.user_id, limit)[1]

    def media_insights(self, media_id: str) -> dict[str, float]:
        """Only public numbers exist here: plays. Skip rate, saves and shares are private."""
        for _, (_, media) in (v for k, v in self._cache.items() if k[0] == "reels"):
            for m in media:
                if m["id"] == media_id and m["view_count"] is not None:
                    return {"views": float(m["view_count"])}
        return {}

    def demographics(self, metric: str = "follower_demographics", timeframe: str | None = None) -> dict[str, dict[str, float]]:
        return {}  # private: only the account owner sees it in the app

    def fetch_comments(self, media_id: str, limit: int = 50) -> list[str]:
        url = media_id if media_id.startswith("http") else f"https://www.instagram.com/reel/{media_id}/"
        items = self.client.run(ACTORS["comments"], {"directUrls": [url], "resultsLimit": limit})
        return [str(i["text"]) for i in items if i.get("text") and not i.get("error")]

    def comments(self, media_id: str, limit: int = 50) -> list[str]:
        try:
            return self._cached(("comments", media_id, limit), lambda: self.fetch_comments(media_id, limit))
        except InstagramError:
            return []

    def search_accounts(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Accounts found by Instagram's own search for ``query`` (best effort: [] on failure)."""
        def fetch() -> list[dict[str, Any]]:
            items = self.client.run(ACTORS["search"], {"search": query, "searchType": "user", "searchLimit": limit,
                                                       "resultsType": "details", "resultsLimit": 1})
            seen, out = set(), []
            for item in items:
                p = profile_from_item(item)
                if p["username"] and p["username"] not in seen and not item.get("error"):
                    seen.add(p["username"])
                    out.append(p)
            return out[:limit]

        try:
            return self._cached(("search", query, limit), fetch)
        except InstagramError:
            return []

    def token_info(self) -> dict[str, Any]:
        """Apify tokens do not expire; this only checks that the token works."""
        try:
            me = self.client.me()
        except InstagramError as exc:
            return {"valid": False, "scopes": [], "days_left": None, "error": str(exc)}
        return {"valid": bool(me), "scopes": [], "days_left": None, "error": "", "account": me.get("username", "")}


def check_connection(ig: ApifyInstagram, *, discover_username: str | None = None, probe_video: bool = True) -> list[dict[str, Any]]:
    """Every Apify call the robot relies on, with what to fix when one fails (see ``instagram.check_connection``)."""
    steps: list[dict[str, Any]] = []

    def step(name: str, ok: bool | None, detail: str, fix: str = "") -> None:
        steps.append({"step": name, "ok": ok, "detail": detail, "fix": fix})

    info = ig.token_info()
    if not info["valid"]:
        step("Токен Apify", False, info["error"] or "токен не принят",
             "Apify Console → Settings → API & Integrations: скопируйте токен в APIFY_TOKEN; домен api.apify.com должен быть разрешён")
        return steps
    step("Токен Apify", True, f"аккаунт Apify {info.get('account') or 'подключён'}")

    try:
        profile = ig.own_profile()
    except InstagramError as exc:
        step("Ваш Instagram", False, str(exc), "Укажите свой ник в IG_USERNAME (без @)")
        return steps
    if profile["is_private"]:
        step("Ваш Instagram", False, f"@{profile['username']} — закрытый аккаунт", "Публичные данные есть только у открытого аккаунта")
        return steps
    step("Ваш Instagram", True, f"@{profile['username']}, подписчиков: {profile['followers_count']}"
         + (f", «{profile['biography'][:60]}»" if profile["biography"] else ""))

    try:
        media = ig.own_media(12)
    except InstagramError as exc:
        step("Ваши Reels", False, str(exc), "Нужны опубликованные Reels в открытом аккаунте")
        return steps
    reels = [m for m in media if m["media_product_type"] == "REELS"]
    with_views = sum(1 for m in reels if m["view_count"] is not None)
    with_video = [m for m in reels if m["media_url"]]
    step("Ваши Reels", True if reels and with_views else None,
         f"{len(reels)} Reels, с просмотрами: {with_views}, с видеофайлом: {len(with_video)}",
         "" if with_views else "Парсер не вернул просмотры — проверьте актёр APIFY_REEL_ACTOR")

    if probe_video and with_video:
        try:
            request = urllib.request.Request(with_video[0]["media_url"], headers={"Range": "bytes=0-1023"})
            with urllib.request.urlopen(request, timeout=30) as resp:
                resp.read(1024)
            step("Скачивание видео", True, "кадры первых секунд будут извлекаться из видео")
        except (urllib.error.URLError, TimeoutError) as exc:
            step("Скачивание видео", None, f"не удалось: {exc}", "Разрешите домены *.cdninstagram.com и *.fbcdn.net; "
                 "иначе разбор пойдёт только по обложкам")

    if reels:
        try:
            got = ig.fetch_comments(reels[0]["id"], 5)
            step("Комментарии", True, f"доступны ({len(got)} у последнего ролика)")
        except InstagramError as exc:
            step("Комментарии", None, str(exc), "Без комментариев профиль аудитории строится по хукам и подписям")

    step("Демография", None, "закрыта: Instagram показывает её только владельцу в приложении",
         "Можно переписать возраст, пол и города из «Профессиональной панели» в описание аудитории")

    if discover_username:
        try:
            _, found = ig.discover(discover_username, 5)
            step("Чужие Reels", True, f"@{discover_username.lstrip('@').lower()}: {len(found)} публикаций")
        except InstagramError as exc:
            step("Чужие Reels", False, str(exc), "Нужен открытый аккаунт с Reels")
    return steps
