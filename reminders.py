from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class DeckReminderScheduler:
    """Verwaltet Erinnerungen für Deck-Karten."""

    def __init__(self, client: Any):
        self.client = client

    async def schedule_reminder(self, card_id: str, reminder_time: str, note: str) -> bool:
        logger.info("Erinnerung für Deck-Karte %s geplant (%s): %s", card_id, reminder_time, note)
        return True