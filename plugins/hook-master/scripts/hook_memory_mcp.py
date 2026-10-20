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

from hookcore import knowledge, learning  # noqa: E402
from hookcore.memory import METRIC_FIELDS, MemoryStore  # noqa: E402

SERVER_NAME = "hook-memory"
SERVER_VERSION = "0.1.0"
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
        except (KeyError, ValueError, LookupError, TypeError) as exc:
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
