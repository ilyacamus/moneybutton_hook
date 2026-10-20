"""Keep the Claude plugin in sync with the core.

The plugin must be self-contained (Claude Code copies it into its plugin
cache), so it ships its own copy of ``hookbot/core`` as ``hookcore`` and the
knowledge base rendered to Markdown.

    python -m hookbot.plugin_sync          # regenerate
    python -m hookbot.plugin_sync --check  # fail if the plugin is stale (CI)
"""

from __future__ import annotations

import sys
from pathlib import Path

from .core import knowledge

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "hookbot" / "core"
PLUGIN = ROOT / "plugins" / "hook-master"
TARGET_CORE = PLUGIN / "scripts" / "hookcore"
TARGET_KNOWLEDGE = PLUGIN / "knowledge"
GENERATED_NOTE = "<!-- Generated from hookbot/core/data by `python -m hookbot.plugin_sync`. Do not edit. -->\n\n"


def expected_files() -> dict[Path, bytes]:
    files: dict[Path, bytes] = {}
    for src in sorted(CORE.rglob("*")):
        if src.is_file() and "__pycache__" not in src.parts and src.suffix in {".py", ".json", ".md"}:
            files[TARGET_CORE / src.relative_to(CORE)] = src.read_bytes()
    for name, text in knowledge.render_all().items():
        files[TARGET_KNOWLEDGE / name] = (GENERATED_NOTE + text).encode("utf-8")
    return files


def _existing() -> set[Path]:
    return {
        p
        for target in (TARGET_CORE, TARGET_KNOWLEDGE)
        if target.exists()
        for p in target.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }


def check() -> list[str]:
    expected = expected_files()
    problems = [f"stale: {p.relative_to(ROOT)}" for p, data in expected.items() if not p.exists() or p.read_bytes() != data]
    problems += [f"extra: {p.relative_to(ROOT)}" for p in _existing() - set(expected)]
    return sorted(problems)


def sync() -> list[Path]:
    expected = expected_files()
    changed = []
    for path, data in expected.items():
        if not path.exists() or path.read_bytes() != data:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            changed.append(path)
    for path in _existing() - set(expected):
        path.unlink()
        changed.append(path)
    return changed


def main(argv: list[str]) -> int:
    if "--check" in argv:
        problems = check()
        for p in problems:
            print(p)
        if problems:
            print("Plugin is out of sync: run `python -m hookbot.plugin_sync`", file=sys.stderr)
        return 1 if problems else 0
    for path in sync():
        print(f"updated {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
