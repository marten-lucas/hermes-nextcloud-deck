from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class DeckCardSnapshot:
    board_id: str
    stack_id: str
    card_id: str
    title: str
    description: str
    assigned_users: List[str] = field(default_factory=list)
    last_comment_id: Optional[str] = None
    last_author: Optional[str] = None

    def has_changed(self, other: DeckCardSnapshot) -> bool:
        return (
            self.title != other.title
            or self.description != other.description
            or set(self.assigned_users) != set(other.assigned_users)
            or self.last_comment_id != other.last_comment_id
            or self.stack_id != other.stack_id
        )


class DeckStateManager:
    """Verhindert doppelte Ausführungen und Endlosschleifen im Polling-Loop."""

    def __init__(self) -> None:
        self._snapshots: Dict[str, DeckCardSnapshot] = {}

    def should_process(self, snapshot: DeckCardSnapshot) -> bool:
        key = f"{snapshot.board_id}:{snapshot.card_id}"
        previous = self._snapshots.get(key)

        if previous is None:
            self._snapshots[key] = snapshot
            return True

        if previous.has_changed(snapshot):
            self._snapshots[key] = snapshot
            return True

        return False