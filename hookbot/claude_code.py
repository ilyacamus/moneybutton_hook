"""Claude through the Claude Code CLI and the owner's claude.ai subscription — no API key needed.

For a personal bot: every request runs as ``claude -p`` (print mode) signed in with the owner's own
Claude subscription (``claude auth login``, or a one-year ``claude setup-token`` token in
``CLAUDE_CODE_OAUTH_TOKEN`` for unattended runs) and counts against that plan's usage limits.
Anthropic's terms allow the subscription for the subscriber's own, individual use only: a bot open to
other people must use an API key (``ANTHROPIC_API_KEY``) instead — the bot refuses to start otherwise.

``ClaudeCodeClient`` stands in for the part of ``anthropic.AsyncAnthropic`` the engine uses
(``beta.messages.create``): the system prompt goes to a file, the user message (text and frames) goes
in as stream-json, the answer is forced into the engine's JSON schema with ``--json-schema``, and no
tools are available to the model.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

BINARY = os.getenv("CLAUDE_CODE_BIN") or "claude"
TIMEOUT_S = float(os.getenv("CLAUDE_CODE_TIMEOUT", "300"))
MAX_TURNS = 8  # no tools: one answer, plus the structured-output step
QUIET_SETTINGS = json.dumps({"disableAllHooks": True})  # the owner's own Claude Code hooks must not run per request
FALLBACK_MODEL = os.getenv("CLAUDE_CODE_FALLBACK_MODEL", "sonnet")

Runner = Callable[[list[str], bytes, str], Awaitable[tuple[int, bytes, bytes]]]


class ClaudeCodeError(Exception):
    """The CLI failed or returned an error result; the message is safe to show to the user."""


def available(binary: str = BINARY) -> bool:
    return shutil.which(binary) is not None


async def _run(args: list[str], stdin: bytes, cwd: str) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ClaudeCodeError("Claude не ответил вовремя. Попробуйте ещё раз.") from None
    return proc.returncode or 0, out, err


def _friendly(message: str) -> str:
    text = message.lower()
    if "limit" in text or "usage" in text and "reset" in text:
        return "Лимит подписки Claude на сейчас исчерпан. Попробуйте позже — лимит обновится сам."
    if "login" in text or "auth" in text or "credential" in text or "not logged" in text or "expired" in text:
        return ("Вход Claude Code истёк или не выполнен: на компьютере с ботом запустите ./scripts/setup_bot.sh "
                "(или `claude setup-token` и впишите токен в .env)")
    return "Не удалось получить ответ от Claude. Попробуйте ещё раз."


def _strip_fences(text: str) -> str:
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    return (match.group(1) if match else text).strip()


class ClaudeCodeClient:
    """``beta.messages.create(...)`` answered by ``claude -p`` with the owner's subscription."""

    def __init__(self, binary: str = BINARY, *, run: Runner | None = None):
        self.binary = binary
        self._run = run or _run
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        self.messages = self.beta.messages

    def command(self, *, model: str, prompt_file: str, effort: str | None, schema: dict | None,
                web: bool) -> list[str]:
        args = [self.binary, "-p", "--verbose", "--input-format", "stream-json", "--output-format", "stream-json",
                "--model", model, "--fallback-model", FALLBACK_MODEL, "--system-prompt-file", prompt_file,
                "--tools", "WebSearch" if web else "", "--strict-mcp-config", "--no-session-persistence",
                "--disable-slash-commands", "--settings", QUIET_SETTINGS, "--max-turns", str(MAX_TURNS)]
        if effort:
            args += ["--effort", effort]
        if schema is not None:
            args += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
        return args

    async def create(self, *, model: str, system: Any, messages: list[dict[str, Any]],
                     output_config: dict[str, Any] | None = None, tools: list | None = None, **_: Any) -> Any:
        system_text = "\n\n".join(b["text"] for b in system) if isinstance(system, list) else str(system or "")
        output_config = output_config or {}
        schema = (output_config.get("format") or {}).get("schema")
        user = next(m for m in reversed(messages) if m["role"] == "user")
        stdin = json.dumps({"type": "user", "message": {"role": "user", "content": user["content"]}},
                           ensure_ascii=False).encode() + b"\n"
        with tempfile.TemporaryDirectory(prefix="hookbot-claude-") as tmp:
            prompt_file = Path(tmp) / "system.md"
            prompt_file.write_text(system_text, encoding="utf-8")
            args = self.command(model=model, prompt_file=str(prompt_file), effort=output_config.get("effort"),
                                schema=schema, web=bool(tools))
            code, out, err = await self._run(args, stdin, tmp)
        return self._response(code, out, err, structured=schema is not None)

    @staticmethod
    def _response(code: int, out: bytes, err: bytes, *, structured: bool) -> Any:
        result = None
        for line in out.decode("utf-8", "replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                result = event
        if result is None or result.get("is_error") or result.get("subtype") not in (None, "success"):
            detail = (result or {}).get("result") or err.decode("utf-8", "replace") or f"exit code {code}"
            raise ClaudeCodeError(_friendly(str(detail)))
        if structured and result.get("structured_output") is not None:
            text = json.dumps(result["structured_output"], ensure_ascii=False)
        else:
            text = _strip_fences(str(result.get("result") or ""))
        usage = result.get("usage") or {}
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=usage.get("input_tokens", 0),
                                  output_tokens=usage.get("output_tokens", 0),
                                  cache_read_input_tokens=usage.get("cache_read_input_tokens", 0)),
            cost_usd=result.get("total_cost_usd"),
        )


async def logged_in(binary: str = BINARY) -> bool:
    """``claude auth status``: True when the CLI is signed in (subscription or key)."""
    if not available(binary):
        return False
    proc = await asyncio.create_subprocess_exec(binary, "auth", "status", stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    return proc.returncode == 0 and b"not logged" not in out.lower()
