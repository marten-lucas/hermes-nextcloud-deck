from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _uid_from_assignee(value: Any) -> str:
    if isinstance(value, dict):
        participant = value.get("participant")
        if isinstance(participant, dict):
            user = participant.get("user")
            if isinstance(user, dict) and user.get("uid"):
                return str(user["uid"]).strip()
        for key in ("uid", "user", "userId", "primaryKey"):
            if value.get(key):
                return str(value[key]).strip()
    return str(value).strip() if value else ""


class DeckIdentityResolver:
    """Resolve the actor used for Hermes' execution context."""

    def __init__(self, bot_user_id: str):
        self.bot_user_id = str(bot_user_id or "").strip()

    def assigned_uids(self, card_data: Dict[str, Any]) -> List[str]:
        raw = card_data.get("assignedUsers") or card_data.get("assignees") or []
        if not isinstance(raw, list):
            return []
        return [uid for uid in (_uid_from_assignee(v) for v in raw) if uid]

    def resolve_card_actor(
        self,
        card_data: Dict[str, Any],
        comment_author: Optional[str] = None,
    ) -> Tuple[str, List[str]]:
        assigned = self.assigned_uids(card_data)
        if self.bot_user_id and self.bot_user_id in assigned:
            return self.bot_user_id, []

        fallback = (
            os.getenv("MCP_IDENTITY_FALLBACK_USER", "").strip()
            or os.getenv("NEXTCLOUD_DECK_USERNAME", "").strip()
            or "system"
        )
        return (str(comment_author).strip() if comment_author else fallback), []

    @staticmethod
    def set_contextvars_identity(user_id: str, groups: Iterable[str] = ()) -> None:
        try:
            from hermes_x_on_behalf.plugin import set_identity_context  # type: ignore
        except ImportError:
            return
        set_identity_context(user_id, ",".join(str(g) for g in groups if str(g).strip()))
