"""Stdlib-only core of the hook evaluator.

Shared by the Telegram bot, the CLI and the Claude plugin (which ships a copy of
this package as ``hookcore``), so it must not import anything outside the
standard library and must only use relative imports.
"""

from . import knowledge, learning, memory, video

__all__ = ["knowledge", "learning", "memory", "video"]
