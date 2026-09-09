from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class DeckCardSnapshot:
    board_id: str
    stack_id: str
    card_id: str
    title: str
    description: str
    assigned_users: List[str] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)
    last_comment_id: Optional[str] = None
    last_author: Optional[str] = None
    due_date: Optional[str] = None
    done: object = None

    def fingerprint(self) -> str:
        payload = {
            "board_id": self.board_id,
            "stack_id": self.stack_id,
            "card_id": self.card_id,
            "title": self.title,
            "description": self.description,
            "assigned_users": sorted(self.assigned_users),
            "labels": sorted(self.labels),
            "last_comment_id": self.last_comment_id,
            "last_author": self.last_author,
            "due_date": self.due_date,
            "done": self.done,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()


class DeckStateManager:
    """In-memory deduplication for one adapter process.

    Der Fingerprint umfasst auch die Labels, damit ein menschenseitiger
    Label-Wechsel (z. B. ``hermes/approval:approved``) als echtes Event
    erkannt wird und den Agenten erneut triggert — auch ohne Spaltenwechsel.

    Loop-Prävention gegen eigene Label-Set: ``mark_processed`` legt die
    Baseline direkt nach einem erfolgreichen Event neu an, sodass die vom
    Adapter/Agenten selbst gesetzten Labels (approval:required, etc.) nicht
    als *neuer* Trigger wirken.
    """

    def __init__(self) -> None:
        self._fingerprints: Dict[str, str] = {}
        # Letzter bekannter Workflow-Label-Zustand pro Karte (kanonische Keys),
        # um menschenseitige Label-Änderungen als Trigger zu erkennen — auch
        # wenn der letzte Kommentar vom Agenten selbst stammt.
        self._last_labels: Dict[str, List[str]] = {}

    def should_process(self, snapshot: DeckCardSnapshot) -> bool:
        key = f"{snapshot.board_id}:{snapshot.card_id}"
        fingerprint = snapshot.fingerprint()
        if self._fingerprints.get(key) == fingerprint:
            return False
        return True

    def label_changed_since_baseline(self, snapshot: DeckCardSnapshot) -> bool:
        """True, wenn sich die Labels seit der letzten Baseline geändert haben.

        Wird VOR dem Eigen-Kommentar-Filter geprüft: Eine menschliche
        Label-Änderung (z. B. Freigabe erteilen) soll den Agenten triggern,
        auch wenn der Agent selbst den letzten Kommentar geschrieben hat.
        """
        key = f"{snapshot.board_id}:{snapshot.card_id}"
        baseline = self._last_labels.get(key)
        if baseline is None:
            return False  # keine Baseline → kein Vergleich möglich
        return sorted(baseline) != sorted(snapshot.labels or [])

    def mark_processed(self, snapshot: DeckCardSnapshot) -> None:
        key = f"{snapshot.board_id}:{snapshot.card_id}"
        self._fingerprints[key] = snapshot.fingerprint()
        self._last_labels[key] = list(snapshot.labels or [])

    def forget(self, board_id: str, card_id: str) -> None:
        self._fingerprints.pop(f"{board_id}:{card_id}", None)
        self._last_labels.pop(f"{board_id}:{card_id}", None)
