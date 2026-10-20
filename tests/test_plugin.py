import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from hookbot import plugin_sync
from hookbot.core import knowledge

PLUGIN = plugin_sync.PLUGIN
ROOT = plugin_sync.ROOT


def test_plugin_is_in_sync_with_core():
    assert plugin_sync.check() == [], "run `python -m hookbot.plugin_sync`"


def test_manifests():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "hook-master"
    assert "hook-memory" in manifest["mcpServers"]
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    entry = market["plugins"][0]
    assert entry["name"] == manifest["name"] and (ROOT / entry["source"]).resolve() == PLUGIN.resolve()


@pytest.mark.parametrize("skill", sorted(p.parent.name for p in (PLUGIN / "skills").glob("*/SKILL.md")))
def test_skill_frontmatter_and_references(skill):
    text = (PLUGIN / "skills" / skill / "SKILL.md").read_text()
    front = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert front, "missing frontmatter"
    assert re.search(rf"^name: {skill}$", front.group(1), re.MULTILINE)
    assert re.search(r"^description: .{40,}", front.group(1), re.MULTILINE)
    for ref in re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]+)", text):
        assert (PLUGIN / ref).exists(), ref


def test_all_skills_present():
    names = {p.parent.name for p in (PLUGIN / "skills").glob("*/SKILL.md")}
    assert names == {"hook-score", "hook-generate", "hook-rewrite", "hook-actions", "hook-result", "hook-learn",
                     "hook-audit", "hook-target"}


class MCP:
    def __init__(self, db: Path):
        env = dict(os.environ, HOOKBOT_DB=str(db))
        self.proc = subprocess.Popen(
            [sys.executable, str(PLUGIN / "scripts" / "hook_memory_mcp.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        self.next_id = 0

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.next_id += 1
            msg["id"] = self.next_id
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        return None if notify else json.loads(self.proc.stdout.readline())

    def call(self, name, **arguments):
        result = self.send("tools/call", {"name": name, "arguments": arguments})["result"]
        text = result["content"][0]["text"]
        return result["isError"], (text if result["isError"] else json.loads(text))

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


@pytest.fixture
def mcp(tmp_path):
    server = MCP(tmp_path / "hooks.db")
    init = server.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    server.send("notifications/initialized", notify=True)
    yield server
    server.close()


def test_mcp_tools_list(mcp):
    tools = {t["name"]: t for t in mcp.send("tools/list")["result"]["tools"]}
    assert {"learning_context", "save_evaluation", "record_result", "score_hook", "save_playbook"} <= set(tools)
    crit = tools["save_evaluation"]["inputSchema"]["properties"]["criteria"]
    assert crit["required"] == knowledge.criterion_ids()


def test_mcp_learning_loop(mcp):
    crit = {c: 6 for c in knowledge.criterion_ids()}
    ids = []
    for i in range(5):
        err, saved = mcp.call("save_evaluation", text=f"Хук {i}", criteria=crit, trigger_ids=["specificity"])
        assert not err and saved["score"] == 60.0
        ids.append(saved["hook_id"])
        err, res = mcp.call("record_result", hook_id=saved["hook_id"], skip_rate=40 - i)
        assert not err and res["metrics"]["hold_3s"] == 60 + i

    assert res["reflection_due"] and res["calibration"]["can_predict_hold"]
    err, saved = mcp.call("save_evaluation", text="Новый", criteria=crit)
    assert saved["predicted_hold"] is not None and saved["calibration_scope"] == "workspace"

    err, variants = mcp.call("save_variants", variants=[{"spoken": "Вариант"}], source="generated")
    err, res = mcp.call("record_result", hook_id=variants["hook_ids"][0], hold_3s=70)
    assert res["needs_scoring"]
    err, scored = mcp.call("score_hook", hook_id=variants["hook_ids"][0], criteria=crit)
    assert not err and scored["calibration"]["n"] == 6

    err, data = mcp.call("training_data")
    assert len(data["results"]) == 6
    err, _ = mcp.call("save_playbook", summary="Цифры работают",
                      do_more=[{"rule": "Сумма в кадре", "evidence": "+5 п.п.", "confidence": "low"}], do_less=[])
    err, ctx = mcp.call("learning_context")
    assert "Цифры работают" in ctx["prompt"]


def test_mcp_errors_do_not_kill_server(mcp):
    err, text = mcp.call("record_result", hook_id=404, skip_rate=10)
    assert err and "404" in text
    assert mcp.send("tools/call", {"name": "nope", "arguments": {}})["error"]["code"] == -32602
    assert mcp.send("ping")["result"] == {}
