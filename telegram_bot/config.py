"""Bot settings from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Pack:
    id: str
    credits: int
    stars: int

    @property
    def title(self) -> str:
        return f"{self.credits} анализов — {self.stars} ⭐"

    @property
    def payload(self) -> str:
        return f"pack:{self.id}:{self.credits}:{self.stars}"

    @classmethod
    def from_payload(cls, payload: str) -> "Pack | None":
        parts = payload.split(":")
        if len(parts) != 4 or parts[0] != "pack":
            return None
        try:
            return cls(parts[1], int(parts[2]), int(parts[3]))
        except ValueError:
            return None


# Credits per operation. Reporting results is free and rewarded: it is the training data.
COSTS = {"score": 1, "video": 2, "generate": 1, "rewrite": 1, "actions": 1}


def parse_packs(raw: str) -> tuple[Pack, ...]:
    """``"start:15:200,pro:50:600"`` -> packs (id:credits:stars)."""
    packs = []
    for item in filter(None, (p.strip() for p in raw.split(","))):
        pack_id, credits, stars = item.split(":")
        packs.append(Pack(pack_id, int(credits), int(stars)))
    return tuple(packs)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    db_path: str = "data/hookbot.db"
    admin_ids: frozenset[int] = frozenset()
    free_credits: int = 5
    result_bonus: int = 1
    followup_hours: float = 48.0
    reflect_every: int = 5
    max_concurrent: int = 4
    packs: tuple[Pack, ...] = parse_packs("start:15:200,pro:50:600,studio:150:1500")
    support_contact: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
        admins = frozenset(int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x)
        kwargs = dict(
            bot_token=token,
            db_path=os.getenv("HOOKBOT_DB", cls.db_path),
            admin_ids=admins,
            free_credits=int(os.getenv("FREE_CREDITS", cls.free_credits)),
            result_bonus=int(os.getenv("RESULT_BONUS", cls.result_bonus)),
            followup_hours=float(os.getenv("FOLLOWUP_HOURS", cls.followup_hours)),
            reflect_every=int(os.getenv("REFLECT_EVERY", cls.reflect_every)),
            max_concurrent=int(os.getenv("MAX_CONCURRENT", cls.max_concurrent)),
            support_contact=os.getenv("SUPPORT_CONTACT", ""),
        )
        if os.getenv("HOOKBOT_PACKS"):
            kwargs["packs"] = parse_packs(os.environ["HOOKBOT_PACKS"])
        return cls(**kwargs)

    def pack(self, pack_id: str) -> Pack | None:
        return next((p for p in self.packs if p.id == pack_id), None)

    def pack_from_payload(self, payload: str) -> Pack | None:
        """The pack an invoice refers to, only if it still matches the current offer."""
        pack = Pack.from_payload(payload)
        return pack if pack is not None and pack in self.packs else None
