"""The bot on the owner's Claude subscription: requests go through `claude -p` (Claude Code), no API key."""
import json

import anthropic
import pytest

from fakes import evaluation
from hookbot import claude_code, prompts
from hookbot.claude_code import ClaudeCodeClient
from hookbot.core.video import Frame
from hookbot.engine import EngineError, HookEngine, HookInput, default_client


class FakeCLI:
    """`claude -p --input-format stream-json --output-format stream-json`: records the call, answers one result."""

    def __init__(self, structured=None, *, text=None, error=None):
        self.structured, self.text, self.error = structured, text, error
        self.calls = []

    async def __call__(self, args, stdin, cwd):
        system = open(args[args.index("--system-prompt-file") + 1], encoding="utf-8").read()
        self.calls.append({"args": args, "stdin": json.loads(stdin), "system": system, "cwd": cwd})
        result = {"type": "result", "subtype": "success", "is_error": bool(self.error),
                  "result": self.error or self.text or "", "usage": {"input_tokens": 1200, "output_tokens": 300},
                  "total_cost_usd": 0.02}
        if self.structured is not None:
            result["structured_output"] = self.structured
        events = [{"type": "system", "subtype": "init"}, {"type": "assistant", "message": {"content": []}}, result]
        return 0, "\n".join(json.dumps(e, ensure_ascii=False) for e in events).encode(), b""


async def test_hook_scored_through_claude_code(store):
    cli = FakeCLI(evaluation(7))
    engine = HookEngine(store, ClaudeCodeClient(run=cli))
    report = await engine.evaluate("w", HookInput(text="Торгуешь по новостям — сливаешь депозит"),
                                   frames=[Frame(0.0, b"\xff\xd8jpeg")])
    assert report.score == 70.0 and report.hook_id == 1
    call = cli.calls[0]
    args = call["args"]
    assert args[:2] == ["claude", "-p"] and args[args.index("--model") + 1] == engine.model
    assert args[args.index("--tools") + 1] == ""  # the model gets no tools: it only answers
    assert json.loads(args[args.index("--settings") + 1]) == {"disableAllHooks": True}
    schema = json.loads(args[args.index("--json-schema") + 1])
    assert "scores" in schema["properties"] and args[args.index("--effort") + 1] == engine.effort
    assert call["system"] == prompts.SYSTEM_PROMPT
    content = call["stdin"]["message"]["content"]
    assert call["stdin"]["type"] == "user" and content[1]["type"] == "image"  # frames go in as images
    assert "Торгуешь по новостям" in content[-1]["text"]
    assert engine.last_usage.input_tokens == 1200


async def test_plain_text_answer_and_errors(store):
    client = ClaudeCodeClient(run=FakeCLI(text="```json\n" + json.dumps(evaluation(5)) + "\n```"))
    report = await HookEngine(store, client).evaluate("w", HookInput(text="Хук"))
    assert report.score == 50.0  # an answer in a code fence still parses
    limited = ClaudeCodeClient(run=FakeCLI(error="Claude usage limit reached · resets 7pm"))
    with pytest.raises(EngineError, match="Лимит подписки"):
        await HookEngine(store, limited).evaluate("w", HookInput(text="Хук"))
    signed_out = ClaudeCodeClient(run=FakeCLI(error="Login expired · Please run /login"))
    with pytest.raises(EngineError, match="setup-token"):
        await HookEngine(store, signed_out).evaluate("w", HookInput(text="Хук"))


def test_backend_choice(monkeypatch):
    monkeypatch.setattr(claude_code, "available", lambda binary="claude": True)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("HOOKBOT_BACKEND", "auto")
    assert isinstance(default_client(), ClaudeCodeClient)  # no key: the owner's subscription
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert isinstance(default_client(), anthropic.AsyncAnthropic)  # a key wins
    monkeypatch.setenv("HOOKBOT_BACKEND", "claude-code")
    assert isinstance(default_client(), ClaudeCodeClient)
    monkeypatch.setenv("HOOKBOT_BACKEND", "api")
    assert isinstance(default_client(), anthropic.AsyncAnthropic)
