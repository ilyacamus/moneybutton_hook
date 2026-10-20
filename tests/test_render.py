import pytest

from hookbot import render
from hookbot.engine import HookInput



async def test_html_escapes_user_text(engine):
    report = await engine.evaluate("w", HookInput("<b>evil</b> & co"))
    report.evaluation.verdict = "<script>alert(1)</script>"
    out = render.render_evaluation(report, render.HTML)
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert f"<code>#{report.hook_id}</code>" in out


async def test_plain_has_no_tags(engine):
    out = render.render_variants(await engine.generate("w", "s"), render.Plain)
    assert "<b>" not in out and "<i>" not in out and "⭐" in out
    out = render.render_actions(await engine.plan_actions("w", HookInput("x")), render.Plain)
    assert "Удар (hit/boom)" in out and "0.0–0.5 с" in out


def test_verdict_labels():
    assert render.verdict_label(80).startswith("🔥")
    assert render.verdict_label(30).startswith("🛑")
    assert render.bar(50) == "▰▰▰▰▰▱▱▱▱▱"


def test_playbook_without_data():
    assert "3 ролика" in render.render_playbook(None)
