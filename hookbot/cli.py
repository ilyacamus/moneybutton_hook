"""Command-line interface: ``python -m hookbot <command>``.

Handy for local testing and for creators who prefer the terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from . import render
from .core import audience, corpus, instagram, learning, literature, target, validation, video
from .core.memory import METRIC_FIELDS, MemoryStore
from .engine import EngineError, HookEngine, HookInput
from .scout import Scout, ScoutConfig

DEFAULT_DB = os.getenv("HOOKBOT_DB", "data/hookbot.db")


def _read(value: str | None) -> str:
    if not value:
        return ""
    if value == "-":
        return sys.stdin.read()
    path = Path(value)
    return path.read_text(encoding="utf-8") if path.is_file() else value


def _hook(args: argparse.Namespace) -> HookInput:
    return HookInput(
        text=args.text,
        on_screen_text=args.screen or "",
        visual=args.visual or "",
        niche=args.niche or "",
        script=_read(getattr(args, "script", None)),
    )


async def run(args: argparse.Namespace) -> int:
    store = MemoryStore(args.db)
    ws = args.workspace
    f = render.Plain

    if args.command == "ig-check":
        ig = instagram.InstagramGraph.from_env()
        if ig is None:
            print("IG_ACCESS_TOKEN не задан — как его получить: docs/INSTAGRAM_SETUP.md", file=sys.stderr)
            return 1
        steps = await asyncio.to_thread(instagram.check_connection, ig, discover_username=args.username)
        print(render.render_connection(steps, f))
        return 0 if all(s["ok"] is not False for s in steps) else 1
    if args.command == "profile":
        fields = {k: v for k, v in (("niche", args.niche), ("audience", args.audience), ("language", args.language)) if v}
        if args.share is not None:
            fields["share_data"] = args.share == "on"
        print(store.update_profile(ws, **fields))
        return 0
    if args.command == "stats":
        print(render.render_stats(learning.build_context(store, ws), f))
        return 0
    if args.command == "playbook":
        insight = store.latest_insight(ws)
        print(render.render_playbook(insight["content"] if insight else None, f))
        return 0
    if args.command == "science":
        niche = args.niche or corpus.match_niche(store, store.get_profile(ws)["niche"])
        print(render.render_science(corpus.latest_report(store, niche), f))
        return 0
    if args.command == "audience":
        print(render.render_audience(audience.latest_report(store, ws), f))
        return 0
    if args.command == "gap":
        print(render.render_audience_profile(target.latest_profile(store, ws), f))
        print("\n" + render.render_gap(target.latest_gap(store, ws), f))
        return 0
    if args.command == "validate":
        print(render.render_validation(validation.report(store, ws), f))
        return 0
    if args.command == "papers":
        if args.approve:
            chosen = literature.approve(store, [int(x) for x in args.approve.split(",")])
            print(f"Одобрено: {len(chosen)}")
        print(render.render_papers(literature.pending(store), f))
        return 0
    if args.command == "scout" and args.action in ("watch", "unwatch", "list", "import"):
        c = corpus.Corpus(store)
        if args.action == "watch":
            c.watch(ws, args.target, niche=args.niche or store.get_profile(ws)["niche"])
        elif args.action == "unwatch":
            c.unwatch(ws, args.target)
        elif args.action == "import":
            records = instagram.import_jsonl(args.target, default_niche=args.niche or "")
            for rec in records:
                c.upsert(rec)
            print(f"Импортировано: {len(records)}, размечено: {c.label_all()}")
        for entry in c.watchlist():
            print(f"@{entry['username']}  ниша: {entry['niche'] or '—'}  ({entry['workspace']})")
        print(c.counts())
        return 0

    engine = HookEngine(store)
    if args.command == "scout":
        scout = Scout(store, engine, ig=instagram.InstagramGraph.from_env(), config=ScoutConfig.from_env())
        if args.action == "once":
            summary = await scout.run_once()
            print(summary)
            for niche in summary.reports:
                print("\n" + render.render_science(corpus.latest_report(store, niche), f))
        else:
            await scout.run_forever()
        return 0
    if args.command == "audit":
        records = None
        if args.csv:
            records = instagram.import_insights_csv(args.csv, workspace=ws, niche=store.get_profile(ws)["niche"],
                                                    videos_dir=args.videos)
            missing = sum(1 for r in records if not r.local_path)
            print(f"Из выгрузки: {len(records)} роликов, без видеофайла: {missing}")
        scout = Scout(store, engine, ig=instagram.InstagramGraph.from_env(), config=ScoutConfig.from_env())

        async def progress(text: str) -> None:
            print(text)

        report = await scout.audit(ws, records=records, limit=args.limit, max_study=args.max_study, progress=progress)
        print(render.render_audience(report, f))
        return 0
    if args.command == "target":
        scout = Scout(store, engine, ig=instagram.InstagramGraph.from_env(), config=ScoutConfig.from_env())

        async def say(text: str) -> None:
            print(text)

        result = await scout.target_audience(ws, study_budget=args.budget, progress=say)
        print(render.render_audience_profile(target.latest_profile(store, ws), f))
        print("\n" + render.render_gap(result["gap"], f))
        return 0
    if args.command == "ref":
        scout = Scout(store, engine, config=ScoutConfig.from_env())
        reel, data = await scout.study_file(
            args.video, workspace=ws, niche=args.niche or store.get_profile(ws)["niche"], views=args.views,
            typical_views=args.typical, caption=_read(args.caption),
        )
        print(render.render_reel_analysis(data, reel, f))
        return 0
    if args.command == "reliability":
        ids = [h["id"] for h in store.recent_hooks(ws, limit=args.sample) if h["source"] in learning.SCORED_SOURCES]
        print(render.render_validation({"reliability": await engine.reliability_study(ws, ids, raters=args.raters)}, f))
        return 0
    if args.command == "score":
        frames, facts, facts_text = [], None, ""
        if args.video:
            probe = await asyncio.to_thread(video.probe, args.video)
            frames, facts, facts_text = probe.frames, probe.facts(), probe.facts_text()
        report = await engine.evaluate(ws, _hook(args), frames=frames, video_facts=facts_text, facts=facts,
                                       source="video" if args.video else "evaluated")
        print(render.render_evaluation(report, f))
    elif args.command == "generate":
        script = _read(args.script)
        if not script.strip():
            print("Пустой сценарий", file=sys.stderr)
            return 2
        print(render.render_variants(await engine.generate(ws, script, count=args.count), f))
    elif args.command == "rewrite":
        print(render.render_variants(await engine.rewrite(ws, _hook(args)), f))
    elif args.command == "actions":
        print(render.render_actions(await engine.plan_actions(ws, _hook(args)), f))
    elif args.command == "result":
        metrics = {k: getattr(args, k) for k in METRIC_FIELDS if getattr(args, k) is not None}
        outcome = await engine.record_result(ws, args.hook_id, notes=args.notes or "", **metrics)
        print(render.render_result(outcome, f))
        if outcome.reflection_due:
            playbook = await engine.reflect(ws)
            print("\n" + render.render_playbook(playbook.model_dump() if playbook else None, f))
    elif args.command == "learn":
        playbook = await engine.reflect(ws)
        print(render.render_playbook(playbook.model_dump() if playbook else None, f))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hookbot", description="Оценщик хуков для Reels/TikTok/Shorts")
    p.add_argument("--db", default=DEFAULT_DB, help="путь к SQLite (по умолчанию $HOOKBOT_DB или data/hookbot.db)")
    p.add_argument("--workspace", default=os.getenv("HOOKBOT_WORKSPACE", "cli"))
    sub = p.add_subparsers(dest="command", required=True)

    def hook_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("text", help="хук: что говорится в первые 3 секунды")
        sp.add_argument("--screen", help="текст на экране")
        sp.add_argument("--visual", help="что происходит в кадре")
        sp.add_argument("--niche")

    sp = sub.add_parser("score", help="оценить хук")
    hook_args(sp)
    sp.add_argument("--video", help="путь к видео: анализ кадров и звука первых 3 секунд")

    sp = sub.add_parser("generate", help="хуки по сценарию")
    sp.add_argument("script", help="текст сценария, путь к файлу или - для stdin")
    sp.add_argument("--count", type=int, default=8)

    sp = sub.add_parser("rewrite", help="переписать хук")
    hook_args(sp)
    sp.add_argument("--script", help="сценарий ролика (текст или файл)")

    sp = sub.add_parser("actions", help="звук, действие, текст для первых 3 секунд")
    hook_args(sp)
    sp.add_argument("--script", help="сценарий ролика (текст или файл)")

    sp = sub.add_parser("result", help="внести реальные метрики опубликованного ролика")
    sp.add_argument("hook_id", type=int)
    sp.add_argument("--skip", dest="skip_rate", type=float, help="skip rate, %%")
    sp.add_argument("--hold", dest="hold_3s", type=float, help="удержание 3 с, %%")
    sp.add_argument("--views", type=int)
    sp.add_argument("--avg-watch", dest="avg_watch_sec", type=float)
    sp.add_argument("--length", dest="video_length_sec", type=float)
    for name in ("likes", "comments", "shares", "saves", "follows"):
        sp.add_argument(f"--{name}", type=int)
    sp.add_argument("--notes")

    sub.add_parser("learn", help="обновить плейбук по накопленным результатам")
    sub.add_parser("stats", help="что модель выучила")
    sub.add_parser("playbook", help="показать текущий плейбук")

    sp = sub.add_parser("ig-check", help="проверить подключение Instagram: токен, разрешения, skip rate, поиск авторов")
    sp.add_argument("--username", help="чей аккаунт искать через Business Discovery (по умолчанию ваш)")

    sp = sub.add_parser("audit", help="аудит вашего канала: что удерживает именно вашу аудиторию")
    sp.add_argument("--csv", help="выгрузка статистики Reels (CSV из Meta Business Suite / панели профессионала)")
    sp.add_argument("--videos", help="папка с вашими роликами (имя файла содержит id публикации или код из ссылки)")
    sp.add_argument("--limit", type=int, default=100, help="сколько последних роликов взять через API")
    sp.add_argument("--max-study", type=int, default=100, help="сколько роликов разобрать за запуск (каждый — запрос к Claude)")
    sub.add_parser("audience", help="последний аудит канала")

    sp = sub.add_parser("target", help="целевая аудитория канала → авторы для неё → сравнение хуков")
    sp.add_argument("--budget", type=int, default=40, help="сколько чужих роликов разобрать (каждый — запрос к Claude)")
    sub.add_parser("gap", help="последний профиль аудитории и сравнение с её залетевшим контентом")

    sp = sub.add_parser("scout", help="робот-разведчик: собирает Reels, находит залетевшие, учится")
    sp.add_argument("action", choices=("once", "run", "watch", "unwatch", "list", "import"))
    sp.add_argument("target", nargs="?", help="аккаунт для watch/unwatch или файл JSONL для import")
    sp.add_argument("--niche")

    sp = sub.add_parser("ref", help="разобрать залетевший ролик (видеофайл) и добавить в корпус")
    sp.add_argument("video")
    sp.add_argument("--views", type=int, help="просмотры этого ролика")
    sp.add_argument("--typical", type=float, help="обычные просмотры автора")
    sp.add_argument("--caption", help="подпись или файл с ней")
    sp.add_argument("--niche")

    sp = sub.add_parser("science", help="что реально работает: корпус залетевших + литература")
    sp.add_argument("--niche")
    sub.add_parser("validate", help="насколько можно доверять оценщику")
    sp = sub.add_parser("reliability", help="повторная оценка хуков: надёжность (ICC)")
    sp.add_argument("--sample", type=int, default=10)
    sp.add_argument("--raters", type=int, default=3)
    sp = sub.add_parser("papers", help="свежие исследования, найденные роботом")
    sp.add_argument("--approve", help="номера через запятую")

    sp = sub.add_parser("profile", help="ниша, аудитория, язык")
    sp.add_argument("--niche")
    sp.add_argument("--audience")
    sp.add_argument("--language")
    sp.add_argument("--share", choices=("on", "off"), help="делиться анонимными данными для общей модели")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except (EngineError, video.VideoError, instagram.InstagramError, LookupError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
