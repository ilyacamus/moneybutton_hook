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
from .core import video
from .core import learning
from .core.memory import METRIC_FIELDS, MemoryStore
from .engine import EngineError, HookEngine, HookInput

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

    engine = HookEngine(store)
    if args.command == "score":
        frames, facts = [], ""
        if args.video:
            probe = await asyncio.to_thread(video.probe, args.video)
            frames, facts = probe.frames, probe.facts_text()
        report = await engine.evaluate(ws, _hook(args), frames=frames, video_facts=facts,
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
    except (EngineError, video.VideoError, LookupError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
