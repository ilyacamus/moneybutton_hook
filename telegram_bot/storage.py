"""Bot-only tables (users, credits, payments, follow-ups) next to the hook memory."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from hookbot.core.memory import MemoryStore, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT NOT NULL DEFAULT '',
    first_name  TEXT NOT NULL DEFAULT '',
    credits     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    pack_id     TEXT NOT NULL,
    stars       INTEGER NOT NULL,
    credits     INTEGER NOT NULL,
    charge_id   TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    refunded    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    op          TEXT NOT NULL,
    cost        INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS followups (
    hook_id     INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    due_at      TEXT NOT NULL,
    sent        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS result_bonuses (
    hook_id     INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL
);
"""


def workspace_of(user_id: int) -> str:
    return f"tg:{user_id}"


class BotStorage:
    def __init__(self, memory: MemoryStore):
        self.memory = memory
        self.conn = memory.conn
        self.lock = memory.lock
        with self.lock:
            self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ #
    # Users and credits
    # ------------------------------------------------------------------ #

    def touch_user(self, user_id: int, username: str = "", first_name: str = "", free_credits: int = 0) -> tuple[dict, bool]:
        """Create or refresh a user. Returns (user, created)."""
        now = now_iso()
        with self.lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO users(user_id, username, first_name, credits, created_at, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, username or "", first_name or "", free_credits, now, now),
            )
            created = cur.rowcount == 1
            if not created:
                self.conn.execute(
                    "UPDATE users SET username = ?, first_name = ?, last_seen = ? WHERE user_id = ?",
                    (username or "", first_name or "", now, user_id),
                )
        return self.get_user(user_id), created

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def apply_owner_niche(self, user_ids: frozenset[int] | set[int], niche: str) -> list[int]:
        """Give the owners' workspaces their niche unless they already chose one. Returns who got it."""
        changed = []
        for user_id in sorted(user_ids):
            workspace = workspace_of(user_id)
            if niche and not self.memory.get_profile(workspace)["niche"]:
                self.memory.update_profile(workspace, niche=niche)
                changed.append(user_id)
        return changed

    def credits(self, user_id: int) -> int:
        user = self.get_user(user_id)
        return user["credits"] if user else 0

    def charge(self, user_id: int, cost: int, op: str, *, unlimited: bool = False) -> bool:
        """Atomically take ``cost`` credits. False when the balance is too low."""
        with self.lock, self.conn:
            if not unlimited and cost > 0:
                cur = self.conn.execute(
                    "UPDATE users SET credits = credits - ? WHERE user_id = ? AND credits >= ?",
                    (cost, user_id, cost),
                )
                if cur.rowcount == 0:
                    return False
            self.conn.execute(
                "INSERT INTO usage(user_id, op, cost, created_at) VALUES (?, ?, ?, ?)",
                (user_id, op, 0 if unlimited else cost, now_iso()),
            )
        return True

    def add_credits(self, user_id: int, amount: int) -> int:
        with self.lock, self.conn:
            self.conn.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (amount, user_id))
        return self.credits(user_id)

    def refund_charge(self, user_id: int, cost: int, op: str, *, unlimited: bool = False) -> None:
        """Undo ``charge`` after a failed operation."""
        if not unlimited:
            self.add_credits(user_id, cost)
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO usage(user_id, op, cost, created_at) VALUES (?, ?, ?, ?)",
                (user_id, f"refund:{op}", -cost if not unlimited else 0, now_iso()),
            )

    def grant_result_bonus(self, user_id: int, hook_id: int, amount: int) -> bool:
        """Reward the first result reported for a hook, once."""
        if amount <= 0:
            return False
        with self.lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO result_bonuses(hook_id, user_id) VALUES (?, ?)", (hook_id, user_id)
            )
            if cur.rowcount == 0:
                return False
            self.conn.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (amount, user_id))
        return True

    # ------------------------------------------------------------------ #
    # Payments (Telegram Stars)
    # ------------------------------------------------------------------ #

    def record_payment(self, user_id: int, pack_id: str, stars: int, credits: int, charge_id: str) -> bool:
        """Credit a successful payment once. False if this charge was already processed."""
        with self.lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO payments(user_id, pack_id, stars, credits, charge_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, pack_id, stars, credits, charge_id, now_iso()),
            )
            if cur.rowcount == 0:
                return False
            self.conn.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (credits, user_id))
        return True

    def get_payment(self, charge_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM payments WHERE charge_id = ?", (charge_id,)).fetchone()
        return dict(row) if row else None

    def mark_refunded(self, charge_id: str) -> dict[str, Any] | None:
        """Mark a payment refunded and take back its unused credits (never below zero)."""
        payment = self.get_payment(charge_id)
        if payment is None or payment["refunded"]:
            return None
        with self.lock, self.conn:
            self.conn.execute("UPDATE payments SET refunded = 1 WHERE charge_id = ?", (charge_id,))
            self.conn.execute(
                "UPDATE users SET credits = MAX(0, credits - ?) WHERE user_id = ?",
                (payment["credits"], payment["user_id"]),
            )
        return payment

    # ------------------------------------------------------------------ #
    # Follow-ups: ask for real metrics after the video had time to perform
    # ------------------------------------------------------------------ #

    def schedule_followup(self, hook_id: int, user_id: int, hours: float) -> None:
        due = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO followups(hook_id, user_id, due_at, sent) VALUES (?, ?, ?, 0)",
                (hook_id, user_id, due),
            )

    def due_followups(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM followups WHERE sent = 0 AND due_at <= ? ORDER BY due_at LIMIT ?",
            (now_iso(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def close_followup(self, hook_id: int) -> None:
        with self.lock, self.conn:
            self.conn.execute("UPDATE followups SET sent = 1 WHERE hook_id = ?", (hook_id,))

    # ------------------------------------------------------------------ #
    # Admin
    # ------------------------------------------------------------------ #

    def admin_stats(self) -> dict[str, Any]:
        one = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "users": one("SELECT COUNT(*) FROM users"),
            "active_7d": one(
                "SELECT COUNT(*) FROM users WHERE last_seen >= "
                f"'{(datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec='seconds')}'"
            ),
            "operations": one("SELECT COUNT(*) FROM usage WHERE op NOT LIKE 'refund:%'"),
            "paid_stars": one("SELECT COALESCE(SUM(stars), 0) FROM payments WHERE refunded = 0"),
            "payments": one("SELECT COUNT(*) FROM payments WHERE refunded = 0"),
            "results": one("SELECT COUNT(*) FROM results"),
        }
