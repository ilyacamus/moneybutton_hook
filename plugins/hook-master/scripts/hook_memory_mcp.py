#!/usr/bin/env python3
"""Hook memory MCP server (stdio, standard library only).

Gives Claude the evaluator's long-term memory and self-learning: stored hooks,
real Instagram results, the calibrated rubric weights and the learned playbook.
Claude does the reasoning itself; this server keeps the numbers consistent
with the Telegram bot (same ``hookcore`` package, same math).
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hookcore import audience, corpus, instagram, knowledge, learning, target, validation, video  # noqa: E402
from hookcore.memory import METRIC_FIELDS, MemoryStore  # noqa: E402

SERVER_NAME = "hook-memory"
SERVER_VERSION = "0.3.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
WORKSPACE = os.environ.get("HOOKBOT_WORKSPACE") or "local"


def _db_path() -> str:
    explicit = os.environ.get("HOOKBOT_DB")
    if explicit and "${" not in explicit:
        return explicit
    data_dir = os.environ.get("CLAUDE_PLUGIN_DATA")
    base = Path(data_dir) if data_dir else Path.home() / ".hookmaster"
    return str(base / "hooks.db")


STORE = MemoryStore(_db_path())

CRITERIA_SCHEMA = {
    "type": "object",
    "description": "Оценки 0–10 по каждому критерию рубрики",
    "properties": {
        c["id"]: {"type": "integer", "minimum": 0, "maximum": 10, "description": c["name"]}
        for c in knowledge.rubric()
    },
    "required": knowledge.criterion_ids(),
    "additionalProperties": False,
}
TRIGGERS_SCHEMA = {"type": "array", "items": {"type": "string", "enum": knowledge.trigger_ids()}}
ANTI_PATTERNS_SCHEMA = {"type": "array", "items": {"type": "string", "enum": knowledge.anti_pattern_ids()}}
VARIANT_SCHEMA = {
    "type": "object",
    "properties": {
        "spoken": {"type": "string", "description": "Что произносится в первые 3 секунды"},
        "on_screen_text": {"type": "string"},
        "visual": {"type": "string"},
        "formula_id": {"type": "string"},
        "trigger_ids": TRIGGERS_SCHEMA,
    },
    "required": ["spoken"],
}
RULE_SCHEMA = {
    "type": "object",
    "properties": {
        "rule": {"type": "string"},
        "evidence": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["rule", "evidence", "confidence"],
}


def _calibration_summary(cal: learning.Calibration) -> dict[str, Any]:
    return {
        "scope": cal.scope,
        "n": cal.n,
        "mae": cal.mae,
        "mean_hold": cal.y_mean,
        "weights": cal.weights,
        "can_predict_hold": cal.can_predict,
        "results_needed_for_prediction": max(0, learning.MIN_RESULTS - cal.n) if not cal.can_predict else 0,
    }


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


def tool_learning_context(args: dict[str, Any]) -> dict[str, Any]:
    ctx = learning.build_context(STORE, WORKSPACE)
    return {
        "prompt": ctx.to_prompt(),
        "calibration": _calibration_summary(ctx.calibration),
        "profile": ctx.profile,
        "hooks_awaiting_results": ctx.pending,
        "knowledge_dir": str(Path(__file__).resolve().parent.parent / "knowledge"),
    }


def tool_save_evaluation(args: dict[str, Any]) -> dict[str, Any]:
    criteria = args["criteria"]
    analysis = learning.analyze(STORE, WORKSPACE, criteria)
    hook_id = STORE.add_hook(
        WORKSPACE,
        args["text"],
        source=args.get("source", "evaluated"),
        on_screen_text=args.get("on_screen_text", ""),
        visual=args.get("visual", ""),
        niche=args.get("niche") or STORE.get_profile(WORKSPACE)["niche"],
        triggers=args.get("trigger_ids", []),
        criteria=criteria,
        score=analysis["score"],
        predicted_hold=analysis["predicted_hold"],
        meta={"anti_patterns": args.get("anti_pattern_ids", [])},
    )
    return {"hook_id": hook_id, **analysis}


def tool_save_variants(args: dict[str, Any]) -> dict[str, Any]:
    parent_id = args.get("parent_id")
    if parent_id is not None and STORE.get_hook(parent_id, WORKSPACE) is None:
        raise ValueError(f"Хук #{parent_id} не найден")
    niche = args.get("niche") or STORE.get_profile(WORKSPACE)["niche"]
    ids = [
        STORE.add_hook(
            WORKSPACE,
            v["spoken"],
            source=args.get("source", "generated"),
            on_screen_text=v.get("on_screen_text", ""),
            visual=v.get("visual", ""),
            niche=niche,
            triggers=v.get("trigger_ids", []),
            parent_id=parent_id,
            meta={"formula_id": v.get("formula_id", "")},
        )
        for v in args["variants"]
    ]
    return {"hook_ids": ids}


def tool_record_result(args: dict[str, Any]) -> dict[str, Any]:
    hook_id = args["hook_id"]
    hook = STORE.get_hook(hook_id, WORKSPACE)
    if hook is None:
        raise ValueError(f"Хук #{hook_id} не найден")
    metrics = {k: args[k] for k in METRIC_FIELDS if args.get(k) is not None}
    clean = STORE.add_result(hook_id, WORKSPACE, notes=args.get("notes", ""), **metrics)
    error = None
    if hook["predicted_hold"] is not None and clean.get("hold_3s") is not None:
        error = round(clean["hold_3s"] - hook["predicted_hold"], 1)
    return {
        "hook_id": hook_id,
        "metrics": clean,
        "predicted_hold_before": hook["predicted_hold"],
        "prediction_error": error,
        "needs_scoring": not hook["criteria"],
        "results_total": STORE.count_results(WORKSPACE),
        "calibration": _calibration_summary(learning.calibrate(STORE, WORKSPACE)),
        "reflection_due": STORE.reflection_due(WORKSPACE, every=5),
    }


def tool_score_hook(args: dict[str, Any]) -> dict[str, Any]:
    hook = STORE.get_hook(args["hook_id"], WORKSPACE)
    if hook is None:
        raise ValueError(f"Хук #{args['hook_id']} не найден")
    analysis = learning.analyze(STORE, WORKSPACE, args["criteria"])
    STORE.update_hook_analysis(
        hook["id"],
        criteria=args["criteria"],
        triggers=sorted(set(hook["triggers"]) | set(args.get("trigger_ids", []))),
        score=analysis["score"],
        predicted_hold=hook["predicted_hold"],  # never back-fill a forecast after the fact
    )
    return {"hook_id": hook["id"], "score": analysis["score"],
            "calibration": _calibration_summary(learning.calibrate(STORE, WORKSPACE))}


def tool_training_data(args: dict[str, Any]) -> dict[str, Any]:
    rows = STORE.training_rows(WORKSPACE)
    ctx = learning.build_context(STORE, WORKSPACE)
    keep = ("id", "text", "on_screen_text", "visual", "triggers", "criteria", "hold_3s", "views",
            "avg_watch_sec", "shares", "saves")
    return {
        "results": [{k: r.get(k) for k in keep} for r in sorted(rows, key=lambda r: r["hold_3s"], reverse=True)],
        "calibration": _calibration_summary(ctx.calibration),
        "trigger_lift": ctx.lifts,
        "current_playbook": ctx.playbook,
    }


def tool_save_playbook(args: dict[str, Any]) -> dict[str, Any]:
    content = {k: args.get(k, [] if k != "summary" else "") for k in ("summary", "do_more", "do_less", "experiments")}
    insight_id = STORE.save_insight(WORKSPACE, content, n_results=STORE.count_results(WORKSPACE))
    return {"insight_id": insight_id}


def tool_set_profile(args: dict[str, Any]) -> dict[str, Any]:
    fields = {k: args[k] for k in ("niche", "audience", "language") if args.get(k)}
    return STORE.update_profile(WORKSPACE, **fields)


def tool_list_hooks(args: dict[str, Any]) -> dict[str, Any]:
    limit = int(args.get("limit", 10))
    if args.get("without_results"):
        hooks = STORE.hooks_without_results(WORKSPACE, limit=limit)
    else:
        hooks = STORE.recent_hooks(WORKSPACE, limit=limit)
    keep = ("id", "created_at", "source", "text", "on_screen_text", "score", "predicted_hold")
    return {"hooks": [{k: h[k] for k in keep} for h in hooks]}


# --------------------------------------------------------------------------- #
# Channel audit, target audience and blind reel study
# --------------------------------------------------------------------------- #

FACTS: dict[int, dict[str, Any]] = {}  # reel id -> measured video facts, kept between next_reels and save
FRAMES_DIR = Path(os.environ.get("HOOKBOT_FRAMES_DIR") or Path(_db_path()).parent / "frames")
CORPUS = corpus.Corpus(STORE)


def _ig() -> Any:
    return instagram.from_env()  # Graph API token, else Apify; unset plugin options are ignored


def tool_audit_collect(args: dict[str, Any]) -> dict[str, Any]:
    niche = STORE.get_profile(WORKSPACE)["niche"]
    table = None
    if args.get("csv_path"):
        table = instagram.import_insights_csv(args["csv_path"], workspace=WORKSPACE, niche=niche,
                                              videos_dir=args.get("videos_dir"))
    ig, matched = _ig(), None
    if ig is not None:
        records = instagram.own_records(ig, workspace=WORKSPACE, niche=niche, limit=int(args.get("limit", 100)))
        if table:
            matched = instagram.merge_own_stats(records, table)  # skip rate, saves and video files from the table
    elif table is not None:
        records = table
    else:
        raise ValueError("Instagram не подключён (ig_access_token, или apify_token и ig_username в настройках плагина) — "
                         "или передайте csv_path с таблицей статистики и videos_dir с роликами")
    for rec in records:
        rec.workspace = WORKSPACE
        CORPUS.upsert(rec)
    CORPUS.label_all()
    own = audience.own_reels(STORE, WORKSPACE)
    return {"collected": len(records), "own_reels": len(own),
            "to_study": sum(1 for r in own if r["status"] != "studied"),
            "without_video": sum(1 for r in records if not (r.local_path or r.video_url or r.thumbnail_url)),
            "with_skip_rate": sum(1 for r in records if r.skip_rate is not None), "table_matched": matched}


def _reel_frames(reel: dict[str, Any]) -> dict[str, Any]:
    folder = FRAMES_DIR / f"reel_{reel['id']}"
    folder.mkdir(parents=True, exist_ok=True)
    path = reel["local_path"] if reel["local_path"] and Path(reel["local_path"]).exists() else None
    if path is None and reel["video_url"]:
        path = str(instagram.download(reel["video_url"], folder / "reel.mp4"))
    if path:
        probe = video.probe(path)
        FACTS[reel["id"]] = probe.facts()
        frames = []
        for frame in probe.frames:
            out = folder / f"frame_{frame.t:.1f}s.jpg"
            out.write_bytes(frame.jpeg)
            frames.append(str(out))
        return {"frames": frames, "video_facts": probe.facts_text()}
    if reel["thumbnail_url"]:
        thumb = instagram.download(reel["thumbnail_url"], folder / "cover.jpg", max_bytes=5 * 1024 * 1024)
        return {"frames": [str(thumb)], "video_facts": "Доступна только обложка: видео скачать нельзя."}
    raise video.VideoError("Нет ни видео, ни обложки")


def tool_next_reels(args: dict[str, Any]) -> dict[str, Any]:
    scope, limit = args.get("scope", "own"), int(args.get("limit", 5))
    if scope == "own":
        queue = [r for r in audience.own_reels(STORE, WORKSPACE) if r["status"] in ("new", "failed")][:limit]
    else:
        queue = CORPUS.queue(limit, niche=target.tag(WORKSPACE) if scope == "audience" else None)
    out, errors = [], []
    for reel in queue:
        try:
            media = _reel_frames(reel)
        except (video.VideoError, instagram.InstagramError) as exc:
            CORPUS.mark_failed(reel["id"], str(exc))
            errors.append({"reel_id": reel["id"], "error": str(exc)})
            continue
        # Blind: no views, no skip rate, no label — only what a viewer sees.
        out.append({"reel_id": reel["id"], "caption": (reel["caption"] or "")[:1500], **media})
    return {"reels": out, "errors": errors,
            "instruction": "Посмотри кадры (Read), оцени хук вслепую по рубрике и вызови save_reel_analysis для каждого ролика."}


def tool_save_reel_analysis(args: dict[str, Any]) -> dict[str, Any]:
    reel_id = int(args["reel_id"])
    if CORPUS.get(reel_id) is None:
        raise ValueError(f"Ролик {reel_id} не найден")
    data = {k: v for k, v in args.items() if k != "reel_id"}
    feats = corpus.store_analysis(CORPUS, reel_id, data, FACTS.pop(reel_id, None))
    synced = audience.sync_own_results(STORE)
    return {"reel_id": reel_id, "features": feats, "own_results_synced": synced}


def tool_audit_report(args: dict[str, Any]) -> dict[str, Any]:
    report = audience.audience_report(STORE, WORKSPACE)
    audience.save_report(STORE, report)
    keep = lambda effects: [e for e in effects if e["verdict"] != "не подтверждено"][:10]  # noqa: E731
    return {**{k: report[k] for k in ("n_reels", "n_studied", "n_with_skip_rate", "median_views", "median_skip_rate",
                                       "breakouts", "best_hold", "worst_hold", "min_reels")},
            "median_engagement": report["median_engagement"],
            "hold_effects": keep(report["hold_effects"]), "reach_effects": keep(report["reach_effects"]),
            "views_effects": keep(report["views_effects"]), "engagement_effects": keep(report["engagement_effects"]),
            "saves_effects": keep(report["saves_effects"]),
            "science": [s for s in report["hold_synthesis"] + report["views_synthesis"] + report["reach_synthesis"]
                        if s["status"] not in ("нет данных", "гипотеза")]}


def tool_audience_signals(args: dict[str, Any]) -> dict[str, Any]:
    ig, demographics, comments = _ig(), {}, []
    if ig is not None:
        demographics = {"followers": ig.demographics("follower_demographics"),
                        "engaged": ig.demographics("engaged_audience_demographics", "this_month")}
        own = [r for r in audience.own_reels(STORE, WORKSPACE)
               if r["source"] == "instagram_own" or str(r["external_id"]).isdigit()]
        for reel in sorted(own, key=lambda r: -(r["views"] or 0))[:5]:
            comments += ig.comments(reel["external_id"], 30)
    return target.channel_signals(STORE, WORKSPACE, demographics=demographics, comments=comments)


def tool_save_audience_profile(args: dict[str, Any]) -> dict[str, Any]:
    target.save_profile(STORE, WORKSPACE, args["profile"], args.get("creators", []))
    return {"saved": True}


def tool_verify_and_watch(args: dict[str, Any]) -> dict[str, Any]:
    ig = _ig()
    results = []
    for username in args["usernames"]:
        if ig is None:
            results.append({"username": username, "verified": None, "reason": "Instagram не подключён"})
            continue
        check = instagram.verify_creator(ig, username)
        if check["ok"]:
            CORPUS.watch(WORKSPACE, check["username"], niche=target.tag(WORKSPACE))
        results.append({**check, "verified": check["ok"]})
    saved = target.latest_profile(STORE, WORKSPACE)
    if saved:
        handle = lambda name: str(name).lstrip("@").lower()  # noqa: E731
        known = {handle(c["username"]): c for c in saved.get("creators", []) if c.get("username")}
        for r in results:
            known[handle(r["username"])] = {**known.get(handle(r["username"]), {}), **r}
        target.save_profile(STORE, WORKSPACE, saved["profile"], list(known.values()))
    return {"results": results}


def tool_collect_audience(args: dict[str, Any]) -> dict[str, Any]:
    ig = _ig()
    if ig is None:
        raise ValueError("Instagram не подключён")
    tag, n, errors = target.tag(WORKSPACE), 0, []
    for entry in CORPUS.watchlist(WORKSPACE):
        if entry["niche"] != tag:
            continue
        try:
            profile, media = ig.discover(entry["username"], int(args.get("per_creator", 25)))
        except instagram.InstagramError as exc:
            errors.append(f"@{entry['username']}: {exc}")
            continue
        for rec in instagram.records_from_discovery(profile, media, niche=tag):
            CORPUS.upsert(rec)
            n += 1
    labeled = CORPUS.label_all()
    return {"collected": n, "labeled": labeled, "queued": len(CORPUS.queue(1000, niche=tag)), "errors": errors}


def tool_search_accounts(args: dict[str, Any]) -> dict[str, Any]:
    ig = _ig()
    if ig is None or not hasattr(ig, "search_accounts"):
        raise ValueError("Поиск аккаунтов в Instagram доступен через Apify (apify_token и ig_username); иначе ищите веб-поиском")
    found = ig.search_accounts(args["query"], int(args.get("limit", 10)))
    return {"accounts": [a for a in found if not a["is_private"]], "skipped_private": sum(a["is_private"] for a in found)}


def tool_gap_report(args: dict[str, Any]) -> dict[str, Any]:
    tag = target.tag(WORKSPACE)
    report = corpus.build_report(CORPUS, tag)
    if report["effects"]:
        corpus.save_report(STORE, report)
    gap = target.gap_report(STORE, WORKSPACE)
    target.save_gap(STORE, gap)
    return {k: gap[k] for k in ("n_creators", "n_viral", "n_typical", "n_own", "recommendations", "exemplars")} | {
        "gaps": [g for g in gap["gaps"] if g["verdict"] != "нет разрыва"][:10]}


def tool_instagram_check(args: dict[str, Any]) -> dict[str, Any]:
    ig = _ig()
    if ig is None:
        raise ValueError("Instagram не подключён: ig_access_token, или apify_token и ig_username в настройках плагина "
                         "(как получить — docs/INSTAGRAM_SETUP.md в репозитории плагина)")
    steps = instagram.check_connection(ig, discover_username=args.get("username"))
    return {"steps": steps, "ready": all(s["ok"] is not False for s in steps)}


def tool_validation(args: dict[str, Any]) -> dict[str, Any]:
    return validation.report(STORE, WORKSPACE)


REEL_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "reel_id": {"type": "integer"},
        "spoken_hook": {"type": "string", "description": "Что говорится в первые 3 с (из подписи/текста/кадров), пусто если неизвестно"},
        "spoken_source": {"type": "string", "enum": ["transcript", "caption", "on_screen", "unknown"]},
        "on_screen_text": {"type": "string", "description": "Текст на экране в первых кадрах дословно"},
        "visual": {"type": "string"},
        "format": {"type": "string"},
        "face_first_second": {"type": "boolean"},
        "text_first_second": {"type": "boolean"},
        "motion_first_second": {"type": "boolean"},
        "criteria": CRITERIA_SCHEMA,
        "trigger_ids": TRIGGERS_SCHEMA,
        "anti_pattern_ids": ANTI_PATTERNS_SCHEMA,
        "formula_id": {"type": "string"},
        "hypothesis": {"type": "string"},
    },
    "required": ["reel_id", "visual", "face_first_second", "text_first_second", "motion_first_second", "criteria", "trigger_ids"],
}


Tool = tuple[str, str, dict[str, Any], Callable[[dict[str, Any]], dict[str, Any]]]

TOOLS: list[Tool] = [
    (
        "learning_context",
        "Вызывай ПЕРВЫМ перед оценкой, генерацией или переписыванием хуков. Возвращает, чему оценщик "
        "научился на реальных результатах автора: откалиброванные веса критериев, влияние триггеров на "
        "удержание, лучшие и худшие хуки, плейбук и хуки, по которым ещё ждём статистику.",
        {"type": "object", "properties": {}},
        tool_learning_context,
    ),
    (
        "save_evaluation",
        "Сохранить оценку хука. Возвращает hook_id, балл 0–100 по текущим (выученным) весам и прогноз "
        "удержания 3 с, если данных достаточно. Используй балл и прогноз из ответа, а не свои.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Что произносится в первые 3 секунды"},
                "on_screen_text": {"type": "string"},
                "visual": {"type": "string"},
                "niche": {"type": "string"},
                "criteria": CRITERIA_SCHEMA,
                "trigger_ids": TRIGGERS_SCHEMA,
                "anti_pattern_ids": ANTI_PATTERNS_SCHEMA,
                "source": {"type": "string", "enum": ["evaluated", "video"]},
            },
            "required": ["text", "criteria"],
        },
        tool_save_evaluation,
    ),
    (
        "save_variants",
        "Сохранить предложенные хуки (сгенерированные, переписанные или улучшенные), чтобы автор мог "
        "потом внести по ним статистику. Возвращает hook_ids в том же порядке.",
        {
            "type": "object",
            "properties": {
                "variants": {"type": "array", "items": VARIANT_SCHEMA},
                "source": {"type": "string", "enum": ["generated", "rewritten", "improved", "original"]},
                "parent_id": {"type": "integer", "description": "hook_id исходного хука, если есть"},
                "niche": {"type": "string"},
            },
            "required": ["variants", "source"],
        },
        tool_save_variants,
    ),
    (
        "record_result",
        "Внести реальную статистику опубликованного ролика из Instagram Insights. Главное — skip_rate "
        "(или hold_3s = 100 − skip_rate). Если в ответе needs_scoring=true — оцени хук по рубрике и вызови "
        "score_hook. Если reflection_due=true — предложи обновить плейбук.",
        {
            "type": "object",
            "properties": {
                "hook_id": {"type": "integer"},
                "skip_rate": {"type": "number", "description": "% пролиставших в первые 3 секунды"},
                "hold_3s": {"type": "number", "description": "% досмотревших 3 секунды"},
                "views": {"type": "integer"},
                "avg_watch_sec": {"type": "number"},
                "video_length_sec": {"type": "number"},
                "likes": {"type": "integer"},
                "comments": {"type": "integer"},
                "shares": {"type": "integer"},
                "saves": {"type": "integer"},
                "follows": {"type": "integer"},
                "notes": {"type": "string"},
            },
            "required": ["hook_id"],
        },
        tool_record_result,
    ),
    (
        "score_hook",
        "Добавить оценки рубрики к уже сохранённому хуку без оценки (например, сгенерированному), чтобы "
        "он участвовал в обучении. Оценивай хук, не глядя на его статистику.",
        {
            "type": "object",
            "properties": {"hook_id": {"type": "integer"}, "criteria": CRITERIA_SCHEMA, "trigger_ids": TRIGGERS_SCHEMA},
            "required": ["hook_id", "criteria"],
        },
        tool_score_hook,
    ),
    (
        "training_data",
        "Все хуки автора с фактическим удержанием 3 с, калибровка и влияние триггеров — для обновления плейбука.",
        {"type": "object", "properties": {}},
        tool_training_data,
    ),
    (
        "save_playbook",
        "Сохранить обновлённый плейбук автора: что делать больше, что меньше (с доказательствами в цифрах) "
        "и какие A/B-тесты провести. Плейбук подмешивается во все следующие оценки.",
        {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "do_more": {"type": "array", "items": RULE_SCHEMA},
                "do_less": {"type": "array", "items": RULE_SCHEMA},
                "experiments": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary", "do_more", "do_less"],
        },
        tool_save_playbook,
    ),
    (
        "set_profile",
        "Сохранить нишу, целевую аудиторию и язык автора.",
        {
            "type": "object",
            "properties": {"niche": {"type": "string"}, "audience": {"type": "string"}, "language": {"type": "string"}},
        },
        tool_set_profile,
    ),
    (
        "list_hooks",
        "Последние сохранённые хуки (или только те, по которым ещё нет статистики) с их номерами.",
        {
            "type": "object",
            "properties": {"limit": {"type": "integer"}, "without_results": {"type": "boolean"}},
        },
        tool_list_hooks,
    ),
]
TOOLS += [
    (
        "instagram_check",
        "Проверить подключение Instagram: токен и срок, разрешения, аккаунт, свои Reels, skip rate, комментарии, "
        "демография, поиск авторов. Для каждой ошибки — что исправить. Вызывай перед первым аудитом.",
        {"type": "object", "properties": {"username": {"type": "string", "description": "чей аккаунт проверить через "
                                                       "Business Discovery (по умолчанию ваш)"}}},
        tool_instagram_check,
    ),
    (
        "audit_collect",
        "Аудит канала, шаг 1: забрать свои Reels со статистикой (skip rate, просмотры) через Instagram API "
        "или из выгрузки CSV (csv_path) с папкой видео (videos_dir).",
        {"type": "object", "properties": {"csv_path": {"type": "string"}, "videos_dir": {"type": "string"},
                                          "limit": {"type": "integer"}}},
        tool_audit_collect,
    ),
    (
        "next_reels",
        "Следующие ролики для слепого разбора: кадры первых 3 с (пути к JPEG), измеренные факты о звуке и склейках, "
        "подпись. Без просмотров и меток — оценивай вслепую. scope: own — свои ролики, audience — авторы вашей "
        "аудитории, corpus — все отслеживаемые.",
        {"type": "object", "properties": {"scope": {"type": "string", "enum": ["own", "audience", "corpus"]},
                                          "limit": {"type": "integer"}}},
        tool_next_reels,
    ),
    (
        "save_reel_analysis",
        "Сохранить слепой разбор ролика: хук, текст на экране, визуал, лицо/текст/движение в первую секунду, "
        "оценки рубрики, триггеры. Свои ролики со skip rate автоматически становятся обучающими данными.",
        REEL_ANALYSIS_SCHEMA,
        tool_save_reel_analysis,
    ),
    (
        "audit_report",
        "Аудит канала, итог: что связано с удержанием первых 3 секунд у вашей аудитории (корреляции с поправкой "
        "на множественные проверки), что отличает прорывные ролики, сверка с научными данными.",
        {"type": "object", "properties": {}},
        tool_audit_report,
    ),
    (
        "audience_signals",
        "Данные для определения целевой аудитории: демография подписчиков и вовлечённых (если подключён Instagram), "
        "комментарии, лучшие и худшие хуки, выводы аудита.",
        {"type": "object", "properties": {}},
        tool_audience_signals,
    ),
    (
        "save_audience_profile",
        "Сохранить профиль целевой аудитории (summary, segments, interests, pains, desires, language_style, "
        "search_keywords, confidence, data_gaps) и, если есть, кандидатов-авторов.",
        {"type": "object", "properties": {"profile": {"type": "object"}, "creators": {"type": "array", "items": {"type": "object"}}},
         "required": ["profile"]},
        tool_save_audience_profile,
    ),
    (
        "verify_and_watch",
        "Проверить через Instagram API, что аккаунты существуют, бизнес/авторские и публикуют Reels; подходящие "
        "добавить в отслеживаемые для вашей аудитории. Несуществующие отклоняются.",
        {"type": "object", "properties": {"usernames": {"type": "array", "items": {"type": "string"}}},
         "required": ["usernames"]},
        tool_verify_and_watch,
    ),
    (
        "search_accounts",
        "Найти аккаунты поиском самого Instagram (через Apify) по ключевому запросу аудитории; закрытые отбрасываются. "
        "Найденных проверь и добавь через verify_and_watch.",
        {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]},
        tool_search_accounts,
    ),
    (
        "collect_audience",
        "Собрать Reels отслеживаемых авторов вашей аудитории и разметить залетевшие (≥3× медианы автора) и обычные.",
        {"type": "object", "properties": {"per_creator": {"type": "integer"}}},
        tool_collect_audience,
    ),
    (
        "gap_report",
        "Что работает у вашей аудитории у других авторов, чем ваши ролики отличаются от залетевших, и согласуется ли "
        "это с вашим аудитом и наукой. Обновляет выводы, которые учитываются во всех оценках.",
        {"type": "object", "properties": {}},
        tool_gap_report,
    ),
    (
        "validation_report",
        "Насколько можно доверять оценщику: ошибка прогноза против наивного, AUC на корпусе, надёжность (ICC).",
        {"type": "object", "properties": {}},
        tool_validation,
    ),
]
HANDLERS = {name: handler for name, _, _, handler in TOOLS}


# --------------------------------------------------------------------------- #
# JSON-RPC over stdio
# --------------------------------------------------------------------------- #


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}
    if msg_id is None:  # notification (e.g. notifications/initialized)
        return None

    def ok(result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    if method == "initialize":
        requested = params.get("protocolVersion")
        return ok(
            {
                "protocolVersion": requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": "Долговременная память и самообучение оценщика хуков. "
                "Перед любой работой с хуками вызывай learning_context.",
            }
        )
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s, _ in TOOLS]})
    if method == "tools/call":
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
        try:
            result = handler(params.get("arguments") or {})
            return ok({"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=1)}],
                       "isError": False})
        except (KeyError, ValueError, LookupError, TypeError, instagram.InstagramError, video.VideoError) as exc:
            return ok({"content": [{"type": "text", "text": f"Ошибка: {exc}"}], "isError": True})
        except Exception:  # keep the server alive, report to the client
            traceback.print_exc(file=sys.stderr)
            return ok({"content": [{"type": "text", "text": "Внутренняя ошибка сервера памяти"}], "isError": True})
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            continue
        batch = message if isinstance(message, list) else [message]
        responses = [r for r in (handle(m) for m in batch if isinstance(m, dict)) if r is not None]
        for response in responses:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
