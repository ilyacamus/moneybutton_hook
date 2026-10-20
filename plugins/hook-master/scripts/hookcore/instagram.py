"""Instagram Graph API source (standard library only).

* ``discover`` — public reels of any business/creator account via Business
  Discovery: caption, likes, comments and ``view_count`` (the only way to get
  other accounts' reel views through the official API).
* ``own_media`` + ``media_insights`` — the connected account's own reels with
  ``reels_skip_rate`` (share of views skipped in the first 3 seconds), so the
  learning loop can close itself without manual input.

``media_url`` is omitted for reels with licensed audio or with downloads
disabled by the owner; callers then fall back to ``thumbnail_url``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .corpus import ReelRecord

GRAPH_URL = "https://graph.facebook.com"
DEFAULT_VERSION = os.getenv("IG_GRAPH_VERSION", "v25.0")
MEDIA_FIELDS = ("id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp,"
                "like_count,comments_count")
INSIGHT_METRICS = ("views", "reach", "saved", "shares", "ig_reels_avg_watch_time", "reels_skip_rate")
MAX_DOWNLOAD_BYTES = 60 * 1024 * 1024


class InstagramError(RuntimeError):
    pass


Getter = Callable[[str, dict[str, Any]], dict[str, Any]]


class InstagramGraph:
    def __init__(self, access_token: str, ig_user_id: str, *, version: str = DEFAULT_VERSION,
                 getter: Getter | None = None, timeout: float = 30):
        self.token = access_token
        self.user_id = ig_user_id
        self.version = version
        self.timeout = timeout
        self._get = getter or self._http_get

    @classmethod
    def from_env(cls) -> "InstagramGraph | None":
        token, user = os.getenv("IG_ACCESS_TOKEN"), os.getenv("IG_USER_ID")
        return cls(token, user) if token and user else None

    def _http_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = urllib.parse.urlencode({**params, "access_token": self.token})
        url = f"{GRAPH_URL}/{self.version}/{path.lstrip('/')}?{query}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read().decode())["error"]["message"]
            except Exception:  # noqa: BLE001 - any unreadable body
                message = str(exc)
            raise InstagramError(message) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise InstagramError(str(exc)) from exc

    def discover(self, username: str, limit: int = 25) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        username = username.lstrip("@")
        fields = (f"business_discovery.username({username})"
                  f"{{username,followers_count,media_count,media.limit({limit}){{{MEDIA_FIELDS},view_count}}}}")
        data = self._get(self.user_id, {"fields": fields}).get("business_discovery")
        if not data:
            raise InstagramError(f"Аккаунт {username} не найден или не является бизнес/авторским")
        media = (data.get("media") or {}).get("data", [])
        return {k: data.get(k) for k in ("username", "followers_count", "media_count")}, media

    def own_profile(self) -> dict[str, Any]:
        return self._get(self.user_id, {"fields": "username,followers_count,media_count"})

    def own_media(self, limit: int = 25) -> list[dict[str, Any]]:
        return self._get(f"{self.user_id}/media", {"fields": MEDIA_FIELDS, "limit": limit}).get("data", [])

    def media_insights(self, media_id: str) -> dict[str, float]:
        """Lifetime insights of one own reel; unsupported metrics are skipped."""
        try:
            return _parse_insights(self._get(f"{media_id}/insights", {"metric": ",".join(INSIGHT_METRICS)}))
        except InstagramError:
            out: dict[str, float] = {}
            for metric in INSIGHT_METRICS:  # one bad metric fails the whole call: retry one by one
                try:
                    out.update(_parse_insights(self._get(f"{media_id}/insights", {"metric": metric})))
                except InstagramError:
                    continue
            return out


def _parse_insights(payload: dict[str, Any]) -> dict[str, float]:
    out = {}
    for item in payload.get("data", []):
        value = None
        if item.get("values"):
            value = item["values"][0].get("value")
        elif item.get("total_value"):
            value = item["total_value"].get("value")
        if isinstance(value, (int, float)):
            out[item["name"]] = float(value)
    return out


def _is_reel(media: dict[str, Any]) -> bool:
    return media.get("media_product_type") == "REELS" or media.get("media_type") == "VIDEO"


def records_from_discovery(profile: dict[str, Any], media: list[dict[str, Any]], *, niche: str = "",
                           workspace: str = "") -> list[ReelRecord]:
    creator = f"ig:{profile.get('username', '')}"
    return [
        ReelRecord(
            source="instagram", external_id=m["id"], url=m.get("permalink", ""), creator=creator, niche=niche,
            caption=m.get("caption") or "", published_at=m.get("timestamp"), views=m.get("view_count"),
            likes=m.get("like_count"), comments=m.get("comments_count"), followers=profile.get("followers_count"),
            video_url=m.get("media_url") or "", thumbnail_url=m.get("thumbnail_url") or "", workspace=workspace,
        )
        for m in media if _is_reel(m)
    ]


def own_records(ig: InstagramGraph, *, workspace: str, niche: str = "", limit: int = 25) -> list[ReelRecord]:
    """The connected account's reels with insights (views, saves, shares, skip rate)."""
    profile = ig.own_profile()
    records = []
    for m in ig.own_media(limit):
        if not _is_reel(m):
            continue
        insights = ig.media_insights(m["id"])
        skip = insights.get("reels_skip_rate")
        records.append(ReelRecord(
            source="instagram_own", external_id=m["id"], url=m.get("permalink", ""),
            creator=f"ig:{profile.get('username', '')}", niche=niche, caption=m.get("caption") or "",
            published_at=m.get("timestamp"), views=int(insights["views"]) if "views" in insights else None,
            likes=m.get("like_count"), comments=m.get("comments_count"),
            shares=int(insights["shares"]) if "shares" in insights else None,
            saves=int(insights["saved"]) if "saved" in insights else None,
            skip_rate=skip if skip is None or skip > 1 else skip * 100,  # accept both 0–1 and 0–100
            followers=profile.get("followers_count"), video_url=m.get("media_url") or "",
            thumbnail_url=m.get("thumbnail_url") or "", workspace=workspace,
        ))
    return records


def download(url: str, dest: Path, *, max_bytes: int = MAX_DOWNLOAD_BYTES, timeout: float = 60) -> Path:
    """Stream ``url`` to ``dest`` with a size cap."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp, open(dest, "wb") as out:
            total = 0
            while chunk := resp.read(1 << 16):
                total += len(chunk)
                if total > max_bytes:
                    raise InstagramError("Файл слишком большой")
                out.write(chunk)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise InstagramError(f"Не удалось скачать: {exc}") from exc
    return dest


def import_jsonl(path: str | Path, *, default_niche: str = "", default_source: str = "jsonl") -> list[ReelRecord]:
    """Provider-agnostic import: one JSON object per line with ``ReelRecord`` fields
    (e.g. an export from your own analytics or a licensed data provider)."""
    allowed = set(ReelRecord.__dataclass_fields__)
    records = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            data = {k: v for k, v in json.loads(line).items() if k in allowed}
            data.setdefault("source", default_source)
            data.setdefault("niche", default_niche)
            if not data.get("external_id"):
                data["external_id"] = data.get("url") or f"{Path(path).name}:{n}"
            records.append(ReelRecord(**data))
    return records
