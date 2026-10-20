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
import hashlib
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .core import audience, corpus, features, instagram, knowledge, learning, literature, target, video
from .core.corpus import Corpus
from .core.memory import MemoryStore, now_iso
from .core.video import Frame
from .engine import EngineError, HookEngine

log = logging.getLogger(__name__)


@dataclass
class ScoutConfig:
    media_per_account: int = 25
    study_per_cycle: int = 10
    daily_budget: int = 60
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

    async def study_file(
        self,
        path: str,
        *,
        workspace: str = "",
        niche: str = "",
        views: int | None = None,
        typical_views: float | None = None,
        caption: str = "",
        url: str = "",
        own: bool = False,
        skip_rate: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Study a reel sent in as a file: a competitor's viral reel, or (``own=True``)
        one of the creator's own reels with its real skip rate."""
        digest = hashlib.sha1(Path(path).read_bytes()).hexdigest()[:16]
        reel_id = self.corpus.upsert(corpus.ReelRecord(
            source="own_file" if own else "manual", external_id=url or digest, url=url,
            creator=f"own:{workspace}" if own else f"manual:{workspace}:{digest}",
            niche=niche, caption=caption, views=views, baseline_views=typical_views, local_path=path,
            workspace=workspace, skip_rate=skip_rate,
        ))
        if views and typical_views:
            score = round(views / typical_views, 3)
            self.corpus.set_label(reel_id, corpus.label_for(score), score)
        reel = self.corpus.get(reel_id)
        data = reel["analysis"] if reel["status"] == "studied" else await self.study_one(reel)
        if own:
            self.corpus.label_all()
            self.sync_own_results()
            audience.save_report(self.store, audience.audience_report(self.store, workspace))
        return self.corpus.get(reel_id), data

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
            f"SELECT id FROM reels WHERE source IN ({', '.join('?' * len(audience.OWN_SOURCES))}) "
            "AND status = 'studied' AND skip_rate IS NOT NULL",
            audience.OWN_SOURCES,
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

    # audit of the creator's own channel -------------------------------------

    async def audit(
        self,
        workspace: str,
        *,
        records: list[corpus.ReelRecord] | None = None,
        limit: int = 100,
        max_study: int = 100,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Study every own reel, turn it into training data and report what holds this audience.

        ``records`` come from an insights export; without them the connected
        Instagram account is read through the API.
        """
        niche = self.store.get_profile(workspace)["niche"]
        if records is None:
            if self.ig is None:
                raise instagram.InstagramError("Instagram не подключён: задайте IG_ACCESS_TOKEN и IG_USER_ID "
                                               "или загрузите выгрузку статистики")
            records = await asyncio.to_thread(instagram.own_records, self.ig, workspace=workspace, niche=niche, limit=limit)
        for rec in records:
            rec.workspace = workspace
            self.corpus.upsert(rec)
        self.corpus.label_all()
        todo = [r for r in audience.own_reels(self.store, workspace) if r["status"] in ("new", "failed")][:max_study]
        done = failed = 0
        for n, reel in enumerate(todo, 1):
            try:
                await self.study_one(reel)
                done += 1
            except (EngineError, video.VideoError, instagram.InstagramError) as exc:
                self.corpus.mark_failed(reel["id"], str(exc))
                failed += 1
            if progress and n % 5 == 0:
                await progress(f"Разобрано {n} из {len(todo)}…")
        synced = self.sync_own_results()
        report = audience.audience_report(self.store, workspace)
        report.update(collected=len(records), studied_now=done, failed_now=failed, synced=synced)
        audience.save_report(self.store, report)
        if len(self.store.training_rows(workspace)) >= 3:
            try:
                await self.engine.reflect(workspace)
            except EngineError as exc:
                log.warning("Playbook update failed: %s", exc)
        return report

    # target audience: who else makes content for the same people ----------

    async def target_audience(
        self,
        workspace: str,
        *,
        own_account: bool = True,
        study_budget: int = 40,
        media_per_creator: int = 25,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Profile the channel's audience, find and verify creators serving it,
        study their viral and typical reels, and compare with the channel."""

        async def say(text: str) -> None:
            if progress:
                await progress(text)

        demographics, comments = {}, []
        if self.ig is not None and own_account:
            demographics = {
                "followers": await asyncio.to_thread(self.ig.demographics, "follower_demographics"),
                "engaged": await asyncio.to_thread(self.ig.demographics, "engaged_audience_demographics", "this_month"),
            }
            # Reels from the API, or from an export whose post id is the Instagram media id.
            with_id = (r for r in audience.own_reels(self.store, workspace)
                       if r["source"] == "instagram_own" or str(r["external_id"]).isdigit())
            top = sorted(with_id, key=lambda r: -(r["views"] or 0))[:5]
            for reel in top:
                comments += await asyncio.to_thread(self.ig.comments, reel["external_id"], 30)
        signals = target.channel_signals(self.store, workspace, demographics=demographics, comments=comments)
        await say("Определяю целевую аудиторию по данным канала…")
        profile = await self.engine.profile_audience(signals)

        known = [w["username"] for w in self.corpus.watchlist(workspace)]
        await say("Ищу авторов, которые делают контент для этой же аудитории…")
        search = await self.engine.discover_creators(profile, known)
        creators = []
        tag = target.tag(workspace)
        for cand in search.candidates:
            entry = {"username": cand.username.lstrip("@").lower(), "segment": cand.segment, "why": cand.why,
                     "source_url": cand.source_url, "verified": None}
            if self.ig is not None:
                check = await asyncio.to_thread(instagram.verify_creator, self.ig, cand.username)
                entry.update(verified=check["ok"], reason=check["reason"], followers=check.get("followers"),
                             median_views=check.get("median_views"))
                if check["ok"]:
                    self.corpus.watch(workspace, entry["username"], niche=tag)
            creators.append(entry)
        target.save_profile(self.store, workspace, profile.model_dump(), creators)
        verified = [c for c in creators if c["verified"]]
        if self.ig is None:
            await say("Instagram не подключён: кандидаты найдены, но не проверены и не собраны.")
            return {"profile": profile.model_dump(), "creators": creators, "studied": 0, "gap": None}

        await say(f"Проверено через Instagram: {len(verified)} из {len(creators)}. Собираю их Reels…")
        summary = CycleSummary()
        for entry in self.corpus.watchlist(workspace):
            if entry["niche"] != tag:
                continue
            try:
                profile_ig, media = await asyncio.to_thread(self.ig.discover, entry["username"], media_per_creator)
            except instagram.InstagramError as exc:
                summary.errors.append(f"@{entry['username']}: {exc}")
                continue
            for rec in instagram.records_from_discovery(profile_ig, media, niche=tag):
                self.corpus.upsert(rec)
        self.corpus.label_all()
        studied = 0
        queue = self.corpus.queue(study_budget, niche=tag)
        await say(f"Разбираю вслепую {len(queue)} роликов: залетевшие и обычные у тех же авторов…")
        for reel in queue:
            try:
                await self.study_one(reel)
                studied += 1
            except (EngineError, video.VideoError, instagram.InstagramError) as exc:
                self.corpus.mark_failed(reel["id"], str(exc))
        report = corpus.build_report(self.corpus, tag)
        if report["effects"]:
            corpus.save_report(self.store, report)
        gap = target.gap_report(self.store, workspace)
        target.save_gap(self.store, gap)
        return {"profile": profile.model_dump(), "creators": creators, "studied": studied, "gap": gap,
                "errors": summary.errors}

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
            if niche.startswith(target.TAG_PREFIX):  # keep each creator's audience comparison fresh
                target.save_gap(self.store, target.gap_report(self.store, niche[len(target.TAG_PREFIX):]))

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
