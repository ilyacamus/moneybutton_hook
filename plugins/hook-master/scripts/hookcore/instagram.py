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

import csv
import json
import os
import re
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
        username = username.strip().lstrip("@").lower()  # Instagram usernames are lowercase; web pages are not
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
        """Up to ``limit`` most recent own media, following pagination cursors."""
        media: list[dict[str, Any]] = []
        params: dict[str, Any] = {"fields": MEDIA_FIELDS, "limit": min(limit, 50)}
        while len(media) < limit:
            page = self._get(f"{self.user_id}/media", params)
            media += page.get("data", [])
            after = ((page.get("paging") or {}).get("cursors") or {}).get("after")
            if not after or not page.get("data") or not (page.get("paging") or {}).get("next"):
                break
            params = {**params, "after": after}
        return media[:limit]

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


    def demographics(self, metric: str = "follower_demographics", timeframe: str | None = None) -> dict[str, dict[str, float]]:
        """Audience breakdowns by age, gender, city and country, as shares in percent.

        ``follower_demographics`` describes followers; ``engaged_audience_demographics``
        describes the accounts that interacted with the content. Needs ≥100 followers.
        """
        out: dict[str, dict[str, float]] = {}
        for breakdown in ("age", "gender", "city", "country"):
            params = {"metric": metric, "period": "lifetime", "metric_type": "total_value", "breakdown": breakdown}
            if timeframe:
                params["timeframe"] = timeframe
            try:
                payload = self._get(f"{self.user_id}/insights", params)
            except InstagramError:
                continue
            counts: dict[str, float] = {}
            for item in payload.get("data", []):
                for b in (item.get("total_value") or {}).get("breakdowns", []):
                    for r in b.get("results", []):
                        key = " / ".join(r.get("dimension_values") or [])
                        counts[key] = counts.get(key, 0) + float(r.get("value") or 0)
            total = sum(counts.values())
            if total:
                top = sorted(counts.items(), key=lambda kv: -kv[1])[:10]
                out[breakdown] = {k: round(100 * v / total, 1) for k, v in top}
        return out

    def comments(self, media_id: str, limit: int = 50) -> list[str]:
        """Top-level comment texts of an own media (newest first)."""
        try:
            payload = self._get(f"{media_id}/comments", {"fields": "text,like_count,timestamp", "limit": min(limit, 50)})
        except InstagramError:
            return []
        return [c["text"] for c in payload.get("data", []) if c.get("text")]


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


def verify_creator(ig: InstagramGraph, username: str, limit: int = 12) -> dict[str, Any]:
    """Does this account exist, is it a business/creator account, and does it post reels?"""
    try:
        profile, media = ig.discover(username, limit=limit)
    except InstagramError as exc:
        return {"username": username.lstrip("@").lower(), "ok": False, "reason": str(exc)}
    reels = [m for m in media if _is_reel(m)]
    return {
        "username": profile.get("username") or username.lstrip("@").lower(),
        "ok": bool(reels),
        "reason": "" if reels else "нет Reels",
        "followers": profile.get("followers_count"),
        "reels_sampled": len(reels),
        "median_views": sorted(m.get("view_count") or 0 for m in reels)[len(reels) // 2] if reels else None,
    }


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


# --------------------------------------------------------------------------- #
# Own channel from an insights export (Meta Business Suite / Professional dashboard)
# --------------------------------------------------------------------------- #

CSV_COLUMNS: dict[str, tuple[str, ...]] = {
    "external_id": ("post id", "media id", "id", "идентификатор публикации", "id публикации"),
    "url": ("permalink", "link", "url", "постоянная ссылка", "ссылка"),
    "caption": ("description", "caption", "title", "описание", "подпись", "текст"),
    "published_at": ("publish time", "published", "date", "время публикации", "дата публикации", "дата"),
    "views": ("views", "plays", "просмотры", "воспроизведения"),
    "likes": ("likes", "отметки «нравится»", "отметки \"нравится\"", "лайки"),
    "comments": ("comments", "комментарии"),
    "shares": ("shares", "репосты", "поделились"),
    "saves": ("saves", "сохранения"),
    "skip_rate": ("skip rate", "reels skip rate", "процент пропусков", "пропуски", "skip"),
    "hold_3s": ("hold 3s", "3-second hold", "удержание 3 с", "удержание 3с"),
    "file": ("file", "video", "файл", "видео"),
}
_SHORTCODE = re.compile(r"/(?:reel|reels|p)/([A-Za-z0-9_-]+)")


def _num(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip().lower().replace("\u00a0", "").replace(" ", "").replace("%", "")
    if not text or text in ("-", "—", "n/a"):
        return None
    mult = 1.0
    if text[-1] in "kк":
        mult, text = 1_000.0, text[:-1]
    elif text[-1] in "mм":
        mult, text = 1_000_000.0, text[:-1]
    try:
        return float(text.replace(",", ".")) * mult
    except ValueError:
        return None


def _match_video(row: dict[str, str], videos: list[Path]) -> str:
    keys = [row.get("file") or "", row.get("external_id") or ""]
    m = _SHORTCODE.search(row.get("url") or "")
    if m:
        keys.append(m.group(1))
    for key in filter(None, keys):
        for path in videos:
            if key == path.name or key in path.stem:
                return str(path)
    return ""


def import_insights_csv(path: str | Path, *, workspace: str, niche: str = "",
                        videos_dir: str | Path | None = None) -> list[ReelRecord]:
    """Own reels from an exported insights table (+ a folder with the videos).

    Column names are matched loosely in English and Russian. Videos are
    matched by a ``file`` column, the post id or the reel shortcode in the
    file name.
    """
    with open(path, encoding="utf-8-sig", newline="") as f:
        header = f.readline()
        f.seek(0)
        # The header decides: captions often contain commas, so sniffing whole rows misleads.
        delimiter = max((";", "\t", ","), key=header.count)
        table = list(csv.DictReader(f, delimiter=delimiter))
    videos = sorted(p for p in Path(videos_dir).iterdir() if p.is_file()) if videos_dir else []
    records = []
    for n, raw in enumerate(table, 1):
        lowered = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        row = {field: next((lowered[a] for a in aliases if lowered.get(a)), "") for field, aliases in CSV_COLUMNS.items()}
        skip = _num(row["skip_rate"])
        hold = _num(row["hold_3s"])
        if skip is not None and skip <= 1:
            skip *= 100
        if skip is None and hold is not None:
            skip = 100 - (hold * 100 if hold <= 1 else hold)
        views = _num(row["views"])
        records.append(ReelRecord(
            source="own_csv", external_id=row["external_id"] or row["url"] or f"{Path(path).name}:{n}",
            url=row["url"], creator=f"own:{workspace}", niche=niche, caption=row["caption"],
            published_at=row["published_at"] or None, views=int(views) if views is not None else None,
            likes=_int(row["likes"]), comments=_int(row["comments"]), shares=_int(row["shares"]),
            saves=_int(row["saves"]), skip_rate=skip, local_path=_match_video(row, videos), workspace=workspace,
        ))
    return records


def _int(value: str) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None
