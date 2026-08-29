from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


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