"""The scout: watches reels all day, finds what went viral, studies the hooks
and re-learns — because what works keeps changing.

One cycle (every ``interval_minutes``):

1. **collect** — fresh reels of watched accounts (Instagram Business Discovery)
   and, if connected, the user's own reels with ``reels_skip_rate``.
2. **label** — within-creator outlier scores once reels are mature.
3. **study** — blind analysis of viral reels and matched controls from the
   same creators: frames of the first 3 s, when the sound starts, cuts,
   caption and (optionally) the spoken words. Bounded by a daily budget.
4. **own results** — own reels with a skip rate become training data for the
   account, closing the learning loop without manual input.
5. **learn** — per-niche reports: effects with FDR control, evidence
   synthesis against the literature, trends (last 30 days vs. before) and
   recency-weighted rubric weights that become every account's prior.
6. **literature** — a periodic web scan for fresh studies, held for human approval.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .core import corpus, features, instagram, knowledge, learning, literature, video
from .core.corpus import Corpus
from .core.memory import MemoryStore, now_iso
from .core.video import Frame
from .engine import EngineError, HookEngine

log = logging.getLogger(__name__)


@dataclass
class ScoutConfig:
    media_per_account: int = 25
    study_per_cycle: int = 10
    daily_budget: int = 150
    interval_minutes: float = 60
    literature_every_days: float = 7
    own_workspace: str | None = None  # workspace whose Instagram account the token belongs to

    @classmethod
    def from_env(cls) -> "ScoutConfig":
        return cls(
            media_per_account=int(os.getenv("SCOUT_MEDIA_PER_ACCOUNT", cls.media_per_account)),
            study_per_cycle=int(os.getenv("SCOUT_STUDY_PER_CYCLE", cls.study_per_cycle)),
            daily_budget=int(os.getenv("SCOUT_DAILY_BUDGET", cls.daily_budget)),
            interval_minutes=float(os.getenv("SCOUT_INTERVAL_MINUTES", cls.interval_minutes)),
            literature_every_days=float(os.getenv("SCOUT_LITERATURE_DAYS", cls.literature_every_days)),
            own_workspace=os.getenv("SCOUT_OWN_WORKSPACE") or None,
        )


@dataclass
class CycleSummary:
    collected: int = 0
    labeled: int = 0
    studied: int = 0
    failed: int = 0
    own_results: int = 0
    reports: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    papers: int = 0
    errors: list[str] = field(default_factory=list)


class Scout:
    def __init__(
        self,
        store: MemoryStore,
        engine: HookEngine,
        *,
        ig: instagram.InstagramGraph | None = None,
        transcriber: Any = None,
        config: ScoutConfig | None = None,
        notify: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.store = store
        self.engine = engine
        self.corpus = Corpus(store)
        self.ig = ig
        self.transcriber = transcriber
        self.config = config or ScoutConfig()
        self.notify = notify

    # 1. collect ------------------------------------------------------------

    async def collect(self, summary: CycleSummary) -> int:
        if self.ig is None:
            return 0
        n = 0
        for entry in self.corpus.watchlist():
            try:
                profile, media = await asyncio.to_thread(self.ig.discover, entry["username"], self.config.media_per_account)
            except instagram.InstagramError as exc:
                summary.errors.append(f"@{entry['username']}: {exc}")
                continue
            for rec in instagram.records_from_discovery(profile, media, niche=entry["niche"]):
                self.corpus.upsert(rec)
                n += 1
        if self.config.own_workspace:
            ws = self.config.own_workspace
            niche = self.store.get_profile(ws)["niche"]
            try:
                records = await asyncio.to_thread(instagram.own_records, self.ig, workspace=ws, niche=niche,
                                                  limit=self.config.media_per_account)
            except instagram.InstagramError as exc:
                summary.errors.append(f"own account: {exc}")
                records = []
            for rec in records:
                self.corpus.upsert(rec)
                n += 1
        return n

    # 3. study --------------------------------------------------------------

    def studied_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        return self.store.conn.execute(
            "SELECT COUNT(*) FROM reels WHERE status IN ('studied', 'failed') AND updated_at >= ?", (today,)
        ).fetchone()[0]

    async def _frames(self, reel: dict[str, Any], tmp: Path) -> tuple[list[Frame], dict | None, str, str]:
        """Frames, measured facts, facts text and transcript for one reel."""
        path = reel["local_path"] if reel["local_path"] and Path(reel["local_path"]).exists() else None
        if path is None and reel["video_url"]:
            path = str(await asyncio.to_thread(instagram.download, reel["video_url"], tmp / "reel.mp4"))
        if path:
            probe = await asyncio.to_thread(video.probe, path)
            transcript = ""
            if self.transcriber is not None:
                try:
                    transcript = await asyncio.to_thread(self.transcriber.first_seconds, path)
                except Exception as exc:  # noqa: BLE001 - speech is optional
                    log.warning("Transcription failed for reel %s: %s", reel["id"], exc)
            return probe.frames, probe.facts(), probe.facts_text(), transcript
        if reel["thumbnail_url"]:
            thumb = await asyncio.to_thread(instagram.download, reel["thumbnail_url"], tmp / "thumb.jpg",
                                            max_bytes=5 * 1024 * 1024)
            return [Frame(0.0, thumb.read_bytes())], None, "Доступна только обложка: видео нельзя скачать (лицензионный звук или запрет автора).", ""
        raise video.VideoError("Нет ни видео, ни обложки")

    async def study_one(self, reel: dict[str, Any]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            frames, facts, facts_text, transcript = await self._frames(reel, Path(tmp))
        analysis = await self.engine.study_reel(frames, facts_text=facts_text, caption=reel["caption"],
                                                transcript=transcript)
        data = analysis.model_dump()
        scores = data.pop("scores")
        data["criteria"] = {cid: max(0, min(10, int(scores[cid]["score"]))) for cid in knowledge.criterion_ids()}
        data["transcript"] = transcript
        data["video_only_thumbnail"] = facts is None
        text = " ".join(filter(None, [analysis.spoken_hook, analysis.on_screen_text]))
        feats = features.extract(text, facts, data)
        self.corpus.save_analysis(reel["id"], data, feats)
        return data

    async def study(self, summary: CycleSummary) -> None:
        left = min(self.config.study_per_cycle, self.config.daily_budget - self.studied_today())
        if left <= 0:
            return
        for reel in self.corpus.queue(left):
            try:
                await self.study_one(reel)
                summary.studied += 1
            except (EngineError, video.VideoError, instagram.InstagramError) as exc:
                self.corpus.mark_failed(reel["id"], str(exc))
                summary.failed += 1

    # 4. own results ----------------------------------------------------------

    def sync_own_results(self) -> int:
        """Own studied reels with a skip rate → hooks with results in the account's memory."""
        rows = self.store.conn.execute(
            "SELECT id FROM reels WHERE source = 'instagram_own' AND status = 'studied' AND skip_rate IS NOT NULL"
        ).fetchall()
        n = 0
        for (reel_id,) in rows:
            reel = self.corpus.get(reel_id)
            data = reel["analysis"]
            if not reel["workspace"] or data.get("hook_id"):
                continue
            text = data.get("spoken_hook") or data.get("on_screen_text") or reel["caption"][:120] or f"Reel {reel['external_id']}"
            analysis = learning.analyze(self.store, reel["workspace"], data["criteria"])
            hook_id = self.store.add_hook(
                reel["workspace"], text, source="own_reel", on_screen_text=data.get("on_screen_text", ""),
                visual=data.get("visual", ""), niche=reel["niche"], triggers=data.get("trigger_ids", []),
                criteria=data["criteria"], score=analysis["score"], meta={"reel_id": reel_id, "url": reel["url"]},
            )
            metrics = {k: reel[k] for k in ("skip_rate", "views", "shares", "saves", "likes", "comments") if reel[k] is not None}
            self.store.add_result(hook_id, reel["workspace"], notes="auto: Instagram insights", **metrics)
            data["hook_id"] = hook_id
            self.corpus.save_analysis(reel_id, data, reel["features"] or {})
            n += 1
        return n

    # 5. learn ---------------------------------------------------------------

    def learn(self, summary: CycleSummary) -> None:
        for niche in [corpus.ALL_NICHES, *self.corpus.niches()]:
            previous = self.store.latest_insight(corpus.REPORT_WS + niche)
            report = corpus.build_report(self.corpus, niche)
            if not report["effects"]:
                continue
            corpus.save_report(self.store, report)
            summary.reports.append(niche)
            summary.alerts += self._alerts(niche, previous["content"] if previous else None, report)

    @staticmethod
    def _alerts(niche: str, before: dict | None, after: dict) -> list[str]:
        where = "все ниши" if niche == corpus.ALL_NICHES else f"ниша «{niche}»"
        alerts = []
        old = {s["id"]: s["status"] for s in (before or {}).get("synthesis", [])}
        for s in after["synthesis"]:
            if before and s["status"] != old.get(s["id"]) and s["status"] not in ("нет данных", "гипотеза"):
                alerts.append(f"{where}: «{s['name']}» — {s['status']}")
        trends = after.get("trends") or {}
        for item in trends.get("rising", [])[:3]:
            alerts.append(f"{where}: у залетевших чаще «{item['label']}» ({item['share_before']}% → {item['share_now']}%)")
        for item in trends.get("shifts", [])[:3]:
            if item["flipped"]:
                alerts.append(f"{where}: эффект «{item['label']}» сменил знак — тренд меняется")
        return alerts

    # 6. literature ----------------------------------------------------------

    async def maybe_scan_literature(self, summary: CycleSummary) -> None:
        if self.config.literature_every_days <= 0:
            return
        last = self.store.latest_insight(literature.SCAN_WS)
        now = datetime.now(timezone.utc)
        if last and datetime.fromisoformat(last["created_at"]) > now - timedelta(days=self.config.literature_every_days):
            return
        since = (datetime.fromisoformat(last["created_at"]) if last else now - timedelta(days=365)).date().isoformat()
        try:
            scan = await self.engine.scan_literature(since)
            summary.papers = len(scan.papers)
            if scan.papers:
                summary.alerts.append(f"Найдено свежих исследований: {len(scan.papers)} — ждут проверки (/papers)")
        except EngineError as exc:
            summary.errors.append(f"literature: {exc}")

    # cycle --------------------------------------------------------------------

    async def run_once(self) -> CycleSummary:
        summary = CycleSummary()
        summary.collected = await self.collect(summary)
        summary.labeled = self.corpus.label_all()
        await self.study(summary)
        summary.own_results = self.sync_own_results()
        if summary.studied or summary.own_results or summary.labeled:
            self.learn(summary)
        await self.maybe_scan_literature(summary)
        self.store.save_insight("__scout__", {"at": now_iso(), **summary.__dict__}, n_results=summary.studied)
        if self.notify and summary.alerts:
            await self.notify("\n".join(summary.alerts))
        return summary

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                summary = await self.run_once()
                log.info("Scout cycle: %s", summary)
            except Exception:
                log.exception("Scout cycle failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.config.interval_minutes * 60)
            except asyncio.TimeoutError:
                pass
