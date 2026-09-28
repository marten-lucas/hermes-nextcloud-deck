from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class DeckPresenceManager:
    """Steuert Presence- und Custom-Status des Deck-Bot-Users (wie Talk).

    Opota für das Deck-Plugin nachgebildet — der Bot-User bekommt einen
    Live-Status (online/busy/offline) plus eine Custom-Status-Nachricht mit
    Icon (z. B. ✍️ Antwortet, 🛠️ Fuehrt Werkzeuge aus). Da Talk und Deck das
    Modell nie gleichzeitig nutzen, ist der User-Status global eindeutig.

    API: OCS `apps/user_status/api/v1/user_status/...`.
    """

    def __init__(self, client: Any):
        self.client = client
        self._current_presence_state: Optional[str] = None
        self._current_custom_status: Optional[tuple[Optional[str], str]] = None
        # Referenzzähler für aktive Turns — Presence busy <-> online.
        self._busy_refs: int = 0

    async def set_presence_status(self, state: str) -> None:
        normalized = str(state or "").strip().lower()
        if normalized == self._current_presence_state:
            return
        await self.client.ocs_put_core(
            "apps/user_status/api/v1/user_status/status",
            {"statusType": normalized},
        )
        self._current_presence_state = normalized

    async def set_busy(self) -> None:
        """Markiert einen aktiven Turn (Referenzgezählt)."""
        self._busy_refs += 1
        await self.set_presence_status("busy")

    async def clear_busy(self) -> None:
        """Turn beendet — Referenz freigeben."""
        self._busy_refs = max(0, self._busy_refs - 1)
        if self._busy_refs == 0:
            await self.set_presence_status("online")

    @property
    def is_busy(self) -> bool:
        return self._busy_refs > 0

    async def set_custom_status_message(self, message: str, status_icon: Optional[str] = None) -> None:
        normalized_message = " ".join(str(message or "").split()).strip()
        new_state = (status_icon, normalized_message)
        if not normalized_message or new_state == self._current_custom_status:
            return

        payload: Dict[str, Any] = {"message": normalized_message[:140]}
        if status_icon:
            payload["statusIcon"] = status_icon

        await self.client.ocs_put_core(
            "apps/user_status/api/v1/user_status/message/custom",
            payload,
        )
        self._current_custom_status = new_state

    async def clear_custom_status_message(self, *, force: bool = False) -> None:
        if self._current_custom_status is None and not force:
            return
        await self.client.ocs_delete_core("apps/user_status/api/v1/user_status/message")
        self._current_custom_status = None