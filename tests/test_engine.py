import json
from types import SimpleNamespace

import pytest
from fakes import CANNED, evaluation

from hookbot import prompts
from hookbot.core import knowledge
from hookbot.engine import EngineError, HookEngine, HookInput, RefusalError



async def test_evaluate_request_shape(engine, client):
    await engine.evaluate("w", HookInput("Хотите больше клиентов?", visual="говорящая голова"))
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["system"][0]["text"] == prompts.SYSTEM_PROMPT
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["output_config"]["effort"] == "medium"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["betas"] == ["server-side-fallback-2026-07-01"] and call["fallbacks"] == "default"
    assert "thinking" not in call  # adaptive by default on this model
    user_text = call["messages"][0]["content"][-1]["text"]
    assert "<hook>" in user_text and "Хотите больше клиентов?" in user_text


async def test_fallbacks_can_be_disabled(store, client):
    await HookEngine(store, client, fallbacks=False).plan_actions("w", HookInput("x"))
    assert "fallbacks" not in client.calls[0] and "betas" not in client.calls[0]


async def test_evaluate_scores_and_stores(engine, store):
    report = await engine.evaluate("w", HookInput("Хук", on_screen_text="Текст"))
    assert report.score == 60.0
    assert report.predicted_hold is None  # no results yet
    hook = store.get_hook(report.hook_id, "w")
    assert hook["criteria"] == {c: 6 for c in knowledge.criterion_ids()}
    assert hook["triggers"] == ["curiosity_gap", "specificity"]
    assert len(report.improved_ids) == 3
    assert all(store.get_hook(i)["parent_id"] == report.hook_id for i in report.improved_ids)


async def test_scores_are_clamped(engine, client):
    bad = evaluation()
    bad["scores"]["curiosity"]["score"] = 42
    client.overrides["Evaluation"] = bad
    report = await engine.evaluate("w", HookInput("x"))
    assert report.criteria["curiosity"] == 10


async def test_video_frames_become_images(engine, client):
    from hookbot.core.video import Frame

    await engine.evaluate("w", HookInput("x"), frames=[Frame(0.0, b"jpeg")], video_facts="Звук с 0.0 с", source="video")
    content = client.calls[0]["messages"][0]["content"]
    assert content[1]["type"] == "image" and content[1]["source"]["media_type"] == "image/jpeg"
    assert "<video_facts>" in content[-1]["text"]


async def test_refusal(engine, client):
    client.stop_reason = "refusal"
    with pytest.raises(RefusalError):
        await engine.evaluate("w", HookInput("x"))


async def test_truncation(engine, client):
    client.stop_reason = "max_tokens"
    with pytest.raises(EngineError):
        await engine.generate("w", "script")


async def test_invalid_output(engine, client):
    client.overrides["Playbook"] = {"summary": "only"}
    client.overrides["ActionPlan"] = {"nope": 1}
    with pytest.raises(EngineError):
        await engine.plan_actions("w", HookInput("x"))


async def test_answer_after_server_side_fallback(engine, client):
    async def create(**params):
        client.calls.append(params)
        return SimpleNamespace(
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0),
            content=[
                SimpleNamespace(type="text", text='{"partial'),
                SimpleNamespace(type="fallback"),
                SimpleNamespace(type="text", text=json.dumps(CANNED["ActionPlan"])),
            ],
        )

    client.beta.messages.create = create
    plan = await engine.plan_actions("w", HookInput("x"))
    assert plan.sound[0].action_id == "s_impact"


async def test_generate_and_rewrite_store_variants(engine, store):
    gen = await engine.generate("w", "Сценарий про таргет")
    assert len(gen.hook_ids) == 8 and store.get_hook(gen.hook_ids[0])["source"] == "generated"
    rw = await engine.rewrite("w", HookInput("Старый хук"))
    parent = store.get_hook(rw.hook_ids[0])["parent_id"]
    assert store.get_hook(parent)["source"] == "original"


async def test_record_result_scores_unscored_hook_blind(engine, client, store):
    gen = await engine.generate("w", "Сценарий")
    hook_id = gen.hook_ids[0]
    outcome = await engine.record_result("w", hook_id, skip_rate=25, views=5000)
    assert outcome.metrics["hold_3s"] == 75
    assert store.get_hook(hook_id)["criteria"]  # now part of the training set
    last_prompt = client.calls[-1]["messages"][0]["content"][-1]["text"]
    assert "75" not in last_prompt and "5000" not in last_prompt  # the result is not leaked
    assert outcome.predicted_hold is None and outcome.prediction_error is None


async def test_prediction_error_and_reflection(engine, store, client):
    for i in range(5):
        report = await engine.evaluate("w", HookInput(f"Хук {i}"))
        outcome = await engine.record_result("w", report.hook_id, skip_rate=30 + i)
    assert outcome.reflection_due
    assert outcome.calibration.scope == "workspace"
    # A hook scored now gets a forecast, and the error is reported when its result arrives.
    report = await engine.evaluate("w", HookInput("Новый"))
    assert report.predicted_hold is not None
    outcome = await engine.record_result("w", report.hook_id, hold_3s=60)
    assert outcome.prediction_error == round(60 - report.predicted_hold, 1)

    playbook = await engine.reflect("w")
    assert playbook.summary and store.latest_insight("w")["n_results"] == 6
    assert "<results>" in client.calls[-1]["messages"][0]["content"][-1]["text"]


async def test_reflect_needs_data(engine):
    assert await engine.reflect("empty") is None


async def test_unknown_hook(engine):
    with pytest.raises(EngineError):
        await engine.record_result("w", 999, skip_rate=10)
