from __future__ import annotations

from typing import Any


class DeckReminderScheduler:
    """Placeholder kept intentionally side-effect free.

    Talk/reminder delivery is not part of the platform adapter MVP. Keeping the
    object here avoids pretending that scheduling is implemented when it is not.
    """

    def __init__(self, client: Any):
        self.client = client

    async def schedule_reminder(self, *args: Any, **kwargs: Any) -> bool:
        return False
