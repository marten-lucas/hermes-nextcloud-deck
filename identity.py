from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class DeckIdentityResolver:
    """Ermittelt die Ausführungsidentität basierend auf Kartenzuweisung und Kommentator.

    - Karte dem Bot zugewiesen: Hermes agiert unter eigener Bot-Identität.
    - Kommentar-Trigger (ohne Zuweisung): Hermes agiert im Namen des Kommentators.
    """

    def __init__(self, bot_user_id: str):
        self.bot_user_id = str(bot_user_id or "").strip()

    def resolve_card_actor(
        self, card_data: Dict[str, Any], comment_author: Optional[str] = None
    ) -> Tuple[str, Optional[str]]:
        assignees = card_data.get("assignedUsers") or card_data.get("assignees") or []
        assigned_uids = []
        if isinstance(assignees, list):
            for a in assignees:
                if isinstance(a, dict):
                    uid = (
                        a.get("participant", {}).get("user", {}).get("uid")
                        or a.get("uid")
                        or a.get("user")
                    )
                else:
                    uid = str(a)
                if uid:
                    assigned_uids.append(str(uid).strip())

        is_assigned_to_bot = self.bot_user_id in assigned_uids if self.bot_user_id else False

        if is_assigned_to_bot:
            actor_id = self.bot_user_id or "system"
        else:
            fallback = os.getenv("MCP_IDENTITY_FALLBACK_USER", "").strip() or "system"
            actor_id = str(comment_author).strip() if comment_author else fallback

        return actor_id, None

    def set_contextvars_identity(self, user_id: str, groups: Optional[str] = None) -> None:
        try:
            from hermes_x_on_behalf.plugin import set_identity_context  # type: ignore
            set_identity_context(user_id, groups)
        except ImportError:
            pass