"""SQLite memory: every hook the bot has seen and the real results it got.

This is what makes the evaluator self-learning: hooks are stored with their
rubric scores, users report the real 3-second hold (skip rate) after
publishing, and ``learning`` fits the scoring weights to those outcomes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    workspace   TEXT PRIMARY KEY,
    niche       TEXT NOT NULL DEFAULT '',
    audience    TEXT NOT NULL DEFAULT '',
    language    TEXT NOT NULL DEFAULT 'ru',
    share_data  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hooks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    source          TEXT NOT NULL,
    text            TEXT NOT NULL,
    on_screen_text  TEXT NOT NULL DEFAULT '',
    visual          TEXT NOT NULL DEFAULT '',
    niche           TEXT NOT NULL DEFAULT '',
    triggers        TEXT NOT NULL DEFAULT '[]',
    criteria        TEXT NOT NULL DEFAULT '{}',
    score           REAL,
    predicted_hold  REAL,
    parent_id       INTEGER REFERENCES hooks(id),
    meta            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS hooks_workspace ON hooks(workspace, created_at);

CREATE TABLE IF NOT EXISTS results (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    hook_id           INTEGER NOT NULL REFERENCES hooks(id),
    workspace         TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    skip_rate         REAL,
    hold_3s           REAL,
    views             INTEGER,
    avg_watch_sec     REAL,
    video_length_sec  REAL,
    likes             INTEGER,
    comments          INTEGER,
    shares            INTEGER,
    saves             INTEGER,
    follows           INTEGER,
    notes             TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS results_hook ON results(hook_id);
CREATE INDEX IF NOT EXISTS results_workspace ON results(workspace);

CREATE TABLE IF NOT EXISTS insights (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    n_results   INTEGER NOT NULL,
    content     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS insights_workspace ON insights(workspace, id);

-- Reference corpus: other creators' (and the user's own) published reels.
CREATE TABLE IF NOT EXISTS reels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    workspace       TEXT NOT NULL DEFAULT '',
    url             TEXT NOT NULL DEFAULT '',
    creator         TEXT NOT NULL DEFAULT '',
    niche           TEXT NOT NULL DEFAULT '',
    caption         TEXT NOT NULL DEFAULT '',
    published_at    TEXT,
    collected_at    TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    views           INTEGER,
    likes           INTEGER,
    comments        INTEGER,
    shares          INTEGER,
    saves           INTEGER,
    skip_rate       REAL,
    followers       INTEGER,
    baseline_views  REAL,
    outlier_score   REAL,
    label           TEXT,
    video_url       TEXT NOT NULL DEFAULT '',
    thumbnail_url   TEXT NOT NULL DEFAULT '',
    local_path      TEXT NOT NULL DEFAULT '',
    transcript      TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    feed            INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'new',
    analysis        TEXT,
    features        TEXT,
    error           TEXT,
    UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS reels_creator ON reels(creator);
CREATE INDEX IF NOT EXISTS reels_status ON reels(status, label);

CREATE TABLE IF NOT EXISTS watchlist (
    workspace   TEXT NOT NULL,
    platform    TEXT NOT NULL,
    username    TEXT NOT NULL,
    niche       TEXT NOT NULL DEFAULT '',
    added_at    TEXT NOT NULL,
    PRIMARY KEY (workspace, platform, username)
);
"""

PERCENT_FIELDS = ("skip_rate", "hold_3s")
FLOAT_FIELDS = ("avg_watch_sec", "video_length_sec")
INT_FIELDS = ("views", "likes", "comments", "shares", "saves", "follows")
METRIC_FIELDS = PERCENT_FIELDS + INT_FIELDS + FLOAT_FIELDS
PROFILE_FIELDS = ("niche", "audience", "language", "share_data")
REEL_MIGRATIONS = {
    "transcript": "TEXT NOT NULL DEFAULT ''",
    "description": "TEXT NOT NULL DEFAULT ''",
    "feed": "INTEGER NOT NULL DEFAULT 1",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clamp_criteria(criteria: dict[str, Any] | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, value in (criteria or {}).items():
        try:
            out[str(key)] = max(0, min(10, int(round(float(value)))))
        except (TypeError, ValueError):
            continue
    return out


class MemoryStore:
    """Thin, synchronous SQLite wrapper. One instance per process is enough."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            if self.path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(reels)")}
            for column, ddl in REEL_MIGRATIONS.items():  # databases created by older versions
                if column not in columns:
                    self.conn.execute(f"ALTER TABLE reels ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        self.conn.close()

    def backup(self, path: str | Path) -> Path:
        """A consistent copy of the database while it is in use (SQLite online backup)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(path))
        try:
            with self.lock:
                self.conn.backup(target)
        finally:
            target.close()
        return path

    # ------------------------------------------------------------------ #
    # Workspaces (a Telegram user, a CLI user, a plugin install)
    # ------------------------------------------------------------------ #

    def ensure_workspace(self, workspace: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO workspaces(workspace, created_at) VALUES (?, ?)",
                (workspace, now_iso()),
            )

    def get_profile(self, workspace: str) -> dict[str, Any]:
        self.ensure_workspace(workspace)
        row = self.conn.execute("SELECT * FROM workspaces WHERE workspace = ?", (workspace,)).fetchone()
        profile = dict(row)
        profile["share_data"] = bool(profile["share_data"])
        return profile

    def update_profile(self, workspace: str, **fields: Any) -> dict[str, Any]:
        unknown = set(fields) - set(PROFILE_FIELDS)
        if unknown:
            raise ValueError(f"Unknown profile fields: {sorted(unknown)}")
        self.ensure_workspace(workspace)
        if fields:
            values = {k: int(bool(v)) if k == "share_data" else str(v) for k, v in fields.items()}
            assignments = ", ".join(f"{k} = ?" for k in values)
            with self.lock, self.conn:
                self.conn.execute(
                    f"UPDATE workspaces SET {assignments} WHERE workspace = ?",
                    (*values.values(), workspace),
                )
        return self.get_profile(workspace)

    def delete_workspace(self, workspace: str) -> None:
        """Forget everything about a workspace (user-requested data deletion)."""
        with self.lock, self.conn:
            for table in ("results", "insights", "hooks", "workspaces"):
                self.conn.execute(f"DELETE FROM {table} WHERE workspace = ?", (workspace,))

    # ------------------------------------------------------------------ #
    # Hooks
    # ------------------------------------------------------------------ #

    def add_hook(
        self,
        workspace: str,
        text: str,
        *,
        source: str = "evaluated",
        on_screen_text: str = "",
        visual: str = "",
        niche: str = "",
        triggers: Iterable[str] = (),
        criteria: dict[str, Any] | None = None,
        score: float | None = None,
        predicted_hold: float | None = None,
        parent_id: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> int:
        if not text or not text.strip():
            raise ValueError("Hook text is empty")
        self.ensure_workspace(workspace)
        with self.lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO hooks(workspace, created_at, source, text, on_screen_text, visual, niche,
                                     triggers, criteria, score, predicted_hold, parent_id, meta)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    workspace,
                    now_iso(),
                    source,
                    text.strip(),
                    on_screen_text.strip(),
                    visual.strip(),
                    niche.strip(),
                    json.dumps(sorted(set(triggers)), ensure_ascii=False),
                    json.dumps(_clamp_criteria(criteria), ensure_ascii=False, sort_keys=True),
                    score,
                    predicted_hold,
                    parent_id,
                    json.dumps(meta or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
            return int(cur.lastrowid)

    def update_hook_analysis(
        self,
        hook_id: int,
        *,
        criteria: dict[str, Any],
        triggers: Iterable[str],
        score: float | None,
        predicted_hold: float | None,
    ) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE hooks SET criteria = ?, triggers = ?, score = ?, predicted_hold = ? WHERE id = ?",
                (
                    json.dumps(_clamp_criteria(criteria), ensure_ascii=False, sort_keys=True),
                    json.dumps(sorted(set(triggers)), ensure_ascii=False),
                    score,
                    predicted_hold,
                    hook_id,
                ),
            )

    @staticmethod
    def _hook_from_row(row: sqlite3.Row) -> dict[str, Any]:
        hook = dict(row)
        hook["triggers"] = json.loads(hook["triggers"])
        hook["criteria"] = json.loads(hook["criteria"])
        hook["meta"] = json.loads(hook["meta"])
        return hook

    def get_hook(self, hook_id: int, workspace: str | None = None) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM hooks WHERE id = ?", (hook_id,)).fetchone()
        if row is None or (workspace is not None and row["workspace"] != workspace):
            return None
        return self._hook_from_row(row)

    def recent_hooks(self, workspace: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM hooks WHERE workspace = ? ORDER BY id DESC LIMIT ?", (workspace, limit)
        ).fetchall()
        return [self._hook_from_row(r) for r in rows]

    def hooks_without_results(
        self,
        workspace: str,
        *,
        older_than_hours: float = 0,
        sources: Iterable[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=older_than_hours)).isoformat(timespec="seconds")
        params: list[Any] = [workspace, cutoff]
        source_filter = ""
        if sources is not None:
            sources = list(sources)
            source_filter = f"AND h.source IN ({', '.join('?' * len(sources))})"
            params += sources
        rows = self.conn.execute(
            f"""SELECT h.* FROM hooks h
                WHERE h.workspace = ? AND h.created_at <= ? {source_filter}
                  AND NOT EXISTS (SELECT 1 FROM results r WHERE r.hook_id = h.id)
                ORDER BY h.id DESC LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return [self._hook_from_row(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Results (ground truth from Instagram Insights)
    # ------------------------------------------------------------------ #

    @staticmethod
    def normalize_metrics(**metrics: Any) -> dict[str, Any]:
        """Validate metrics and derive hold_3s / skip_rate from each other."""
        unknown = set(metrics) - set(METRIC_FIELDS)
        if unknown:
            raise ValueError(f"Unknown metrics: {sorted(unknown)}")
        clean: dict[str, Any] = {}
        for key, value in metrics.items():
            if value is None or value == "":
                continue
            number = float(value)
            if number < 0:
                raise ValueError(f"{key} must not be negative")
            if key in PERCENT_FIELDS and number > 100:
                raise ValueError(f"{key} must be a percentage between 0 and 100")
            clean[key] = int(round(number)) if key in INT_FIELDS else round(number, 2)
        if not clean:
            raise ValueError("No metrics given")
        if "skip_rate" in clean and "hold_3s" not in clean:
            clean["hold_3s"] = round(100 - clean["skip_rate"], 2)
        elif "hold_3s" in clean and "skip_rate" not in clean:
            clean["skip_rate"] = round(100 - clean["hold_3s"], 2)
        return clean

    def add_result(self, hook_id: int, workspace: str, notes: str = "", **metrics: Any) -> dict[str, Any]:
        """Store real metrics for a published hook. Returns the normalized metrics."""
        clean = self.normalize_metrics(**metrics)
        if self.get_hook(hook_id, workspace) is None:
            raise LookupError(f"Hook #{hook_id} not found")
        columns = ["hook_id", "workspace", "created_at", "notes", *clean]
        with self.lock, self.conn:
            self.conn.execute(
                f"INSERT INTO results({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                (hook_id, workspace, now_iso(), notes, *clean.values()),
            )
        return clean

    def training_rows(self, workspace: str | None = None, *, shared_only: bool = False) -> list[dict[str, Any]]:
        """Hooks joined with their latest result that has a 3-second hold value.

        ``workspace=None`` returns rows across workspaces; with ``shared_only``
        only from workspaces that opted in to sharing anonymized data.
        """
        where = ["r.hold_3s IS NOT NULL"]
        params: list[Any] = []
        if workspace is not None:
            where.append("h.workspace = ?")
            params.append(workspace)
        if shared_only:
            where.append("h.workspace IN (SELECT workspace FROM workspaces WHERE share_data = 1)")
        rows = self.conn.execute(
            f"""SELECT h.*, r.skip_rate, r.hold_3s, r.views, r.avg_watch_sec, r.video_length_sec,
                       r.likes, r.comments, r.shares, r.saves, r.follows, r.created_at AS result_at
                FROM hooks h
                JOIN results r ON r.id = (
                    SELECT r2.id FROM results r2
                    WHERE r2.hook_id = h.id AND r2.hold_3s IS NOT NULL
                    ORDER BY r2.id DESC LIMIT 1)
                WHERE {' AND '.join(where)}
                ORDER BY h.id""",
            params,
        ).fetchall()
        return [self._hook_from_row(r) for r in rows]

    def has_result(self, hook_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM results WHERE hook_id = ? LIMIT 1", (hook_id,)).fetchone() is not None

    def count_results(self, workspace: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT hook_id) FROM results WHERE workspace = ? AND hold_3s IS NOT NULL",
            (workspace,),
        ).fetchone()
        return int(row[0])

    # ------------------------------------------------------------------ #
    # Insights (the learned playbook)
    # ------------------------------------------------------------------ #

    def save_insight(self, workspace: str, content: dict[str, Any], n_results: int) -> int:
        with self.lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO insights(workspace, created_at, n_results, content) VALUES (?, ?, ?, ?)",
                (workspace, now_iso(), n_results, json.dumps(content, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def latest_insight(self, workspace: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM insights WHERE workspace = ? ORDER BY id DESC LIMIT 1", (workspace,)
        ).fetchone()
        if row is None:
            return None
        insight = dict(row)
        insight["content"] = json.loads(insight["content"])
        return insight

    def reflection_due(self, workspace: str, every: int) -> bool:
        """True when ``every`` new results arrived since the last playbook update."""
        if every <= 0:
            return False
        latest = self.latest_insight(workspace)
        seen = latest["n_results"] if latest else 0
        return self.count_results(workspace) - seen >= every
