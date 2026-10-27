from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)
_UNSET = object()


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
    # Inhalt des neuesten Kommentars — damit ein EDIT eines bestehenden
    # Kommentars (gleiche ID, geänderter Text) als Trigger erkannt wird.
    # Deck liefert pro Kommentar kein lastModified-Feld, daher ist der
    # Message-Text der einzige verlässliche Änderungsindikator.
    last_comment_message: Optional[str] = None
    due_date: Optional[str] = None
    done: object = None

    def _hash_payload(self, *, include_comment: bool) -> str:
        payload = {
            "board_id": self.board_id,
            "stack_id": self.stack_id,
            "card_id": self.card_id,
            "title": self.title,
            "description": self.description,
            "assigned_users": sorted(self.assigned_users),
            "labels": sorted(self.labels),
            "due_date": self.due_date,
            "done": self.done,
        }
        if include_comment:
            payload.update({
                "last_comment_id": self.last_comment_id,
                "last_author": self.last_author,
                "last_comment_message": self.last_comment_message,
            })
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()

    def fingerprint(self) -> str:
        return self._hash_payload(include_comment=True)

    def content_fingerprint(self) -> str:
        """Fingerprint all trigger fields except the latest comment metadata."""
        return self._hash_payload(include_comment=False)

    def user_fingerprint(self) -> str:
        """Fingerprint card data other than workflow labels/comments."""
        payload = {
            "board_id": self.board_id,
            "stack_id": self.stack_id,
            "card_id": self.card_id,
            "title": self.title,
            "description": self.description,
            "assigned_users": sorted(self.assigned_users),
            "due_date": self.due_date,
            "done": self.done,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()

    def revision_id(self) -> str:
        """Stabile Hermes-Request-ID für genau diese Kartenrevision."""
        return self.fingerprint()


class DeckStateManager:
    """In-memory deduplication for one adapter process.

    Der Fingerprint umfasst auch die Labels, damit ein menschenseitiger
    Label-Wechsel (z. B. ``hermes/approval:approved``) als echtes Event
    erkannt wird und den Agenten erneut triggert — auch ohne Spaltenwechsel.

    ``mark_dispatched`` hält eine Revision während des Hintergrund-Turns
    dedupliziert, ohne sie als abgeschlossen zu markieren. Erst der echte
    Processing-Completion-Hook setzt ``mark_processed``. Der separate
    Kommentar-Watermark bleibt auf dem Input begrenzt.
    """

    def __init__(self, state_file: Optional[str] = None) -> None:
        self._fingerprints: Dict[str, str] = {}
        # Letzter bekannter Workflow-Label-Zustand pro Karte (kanonische Keys),
        # um menschenseitige Label-Änderungen als Trigger zu erkennen — auch
        # wenn der letzte Kommentar vom Agenten selbst stammt.
        self._last_labels: Dict[str, List[str]] = {}
        # Letzte bereits verarbeitete Kommentar-ID pro Karte. Dient dazu, beim
        # nächsten Agent-Lauf nur die NEUEN Kommentare (id > Baseline) als
        # Kontext zu übergeben, statt nur den allerletzten.
        self._last_comment_ids: Dict[str, Optional[int]] = {}
        # Prompt watermark for a dispatched but not yet completed turn. This
        # survives a gateway restart so resumed runs do not consume comments
        # that were never part of the original prompt.
        self._in_flight_comment_ids: Dict[str, Optional[int]] = {}
        self._pending_fingerprints: Dict[str, str] = {}
        self._in_flight_fingerprints: Dict[str, str] = {}
        self._content_fingerprints: Dict[str, str] = {}

        # Persistenz: Der State wird in einer JSON-Datei gespeichert, damit er
        # nach einem Gateway-Restart erhalten bleibt. Sonst gilt nach jedem
        # Restart jede Karte in einer aktiven Spalte als "neu" (Fingerprint-
        # Cache leer) und blockiert das WIP-Limit (siehe Karte 123 blockiert
        # Karte 120 nach Restart).
        self._state_file = state_file
        if state_file:
            self._load()

    def _key(self, board_id: str, card_id: str) -> str:
        return f"{board_id}:{card_id}"

    def _load(self) -> None:
        """Lädt den State aus der JSON-Datei (best-effort)."""
        try:
            if not self._state_file or not os.path.isfile(self._state_file):
                return
            with open(self._state_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict) or data.get("version", 1) not in {1, 2, 3, 4}:
                raise ValueError("unsupported Deck state format")
            self._fingerprints = dict(data.get("fingerprints") or {})
            self._last_labels = {
                k: list(v) for k, v in (data.get("last_labels") or {}).items()
            }
            self._last_comment_ids = {
                k: (int(v) if v is not None else None)
                for k, v in (data.get("last_comment_ids") or {}).items()
            }
            self._in_flight_comment_ids = {
                k: (int(v) if v is not None else None)
                for k, v in (data.get("in_flight_comment_ids") or {}).items()
            }
            self._pending_fingerprints = dict(data.get("pending_fingerprints") or {})
            self._in_flight_fingerprints = dict(data.get("in_flight_fingerprints") or {})
            self._content_fingerprints = dict(data.get("content_fingerprints") or {})

        except Exception as exc:
            # State loss may cause previously consumed review approvals to be
            # treated as new. Keep the adapter available, but make recovery
            # visible instead of silently failing open.
            logger.warning("Deck state could not be loaded from %s: %s", self._state_file, exc)
            self._fingerprints = {}
            self._last_labels = {}
            self._last_comment_ids = {}
            self._in_flight_comment_ids = {}
            self._pending_fingerprints = {}
            self._in_flight_fingerprints = {}
            self._content_fingerprints = {}

    def save(self) -> None:
        """Persistiert den State in die JSON-Datei (best-effort)."""
        if not self._state_file:
            return
        try:
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            data = {
                "version": 4,
                "fingerprints": self._fingerprints,
                "last_labels": self._last_labels,
                "last_comment_ids": self._last_comment_ids,
                "in_flight_comment_ids": self._in_flight_comment_ids,
                "pending_fingerprints": self._pending_fingerprints,
                "in_flight_fingerprints": self._in_flight_fingerprints,
                "content_fingerprints": self._content_fingerprints,
            }
            tmp = self._state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._state_file)
        except Exception as exc:
            logger.warning("Deck state could not be saved to %s: %s", self._state_file, exc)

    def last_processed_comment_id(self, board_id: str, card_id: str) -> Optional[int]:
        """Liefert die letzte Kommentar-ID, die bereits verarbeitet wurde.

        None = noch nie verarbeitet (erster Lauf). Kommentare mit höherer ID
        gelten als "neu" und werden dem Agenten als Kontext übergeben.
        """
        return self._last_comment_ids.get(self._key(board_id, card_id))

    def should_process(self, snapshot: DeckCardSnapshot) -> bool:
        key = f"{snapshot.board_id}:{snapshot.card_id}"
        fingerprint = snapshot.fingerprint()
        if self._pending_fingerprints.get(key) == fingerprint:
            return False
        if self._fingerprints.get(key) == fingerprint:
            baseline_comment = self._last_comment_ids.get(key)
            try:
                current_comment = int(str(snapshot.last_comment_id)) if snapshot.last_comment_id is not None else None
            except (ValueError, TypeError):
                current_comment = None
            if current_comment is None or current_comment <= (baseline_comment or 0):
                return False
        return True

    def content_changed_since_baseline(self, snapshot: DeckCardSnapshot) -> bool:
        key = self._key(snapshot.board_id, snapshot.card_id)
        baseline = self._content_fingerprints.get(key)
        # Unknown baseline is not evidence that the latest mutation was a bot
        # comment; fail open so first-run human edits are not dropped.
        return baseline is None or baseline != snapshot.content_fingerprint()

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

    def mark_dispatched(self, snapshot: DeckCardSnapshot) -> None:
        """Persist the input revision and its prompt comment watermark."""
        key = self._key(snapshot.board_id, snapshot.card_id)
        fingerprint = snapshot.fingerprint()
        self._pending_fingerprints[key] = fingerprint
        self._content_fingerprints[key] = snapshot.content_fingerprint()
        self._in_flight_fingerprints.setdefault(key, fingerprint)
        if key not in self._in_flight_comment_ids:
            self._in_flight_comment_ids[key] = self._last_comment_ids.get(key)
            try:
                self._in_flight_comment_ids[key] = (
                    int(str(snapshot.last_comment_id)) if snapshot.last_comment_id is not None else None
                )
            except (ValueError, TypeError):
                self._in_flight_comment_ids[key] = self._last_comment_ids.get(key)
        self.save()

    def in_flight_comment_id(self, board_id: str, card_id: str) -> Optional[int]:
        return self._in_flight_comment_ids.get(self._key(board_id, card_id))

    def pending_fingerprint(self, board_id: str, card_id: str) -> Optional[str]:
        return self._pending_fingerprints.get(self._key(board_id, card_id))

    def in_flight_fingerprint(self, board_id: str, card_id: str) -> Optional[str]:
        return self._in_flight_fingerprints.get(self._key(board_id, card_id))

    def pending_cards(self) -> List[tuple[str, str]]:
        cards = []
        for key in self._pending_fingerprints:
            board_id, separator, card_id = key.partition(":")
            if separator and board_id and card_id:
                cards.append((board_id, card_id))
        return cards

    def clear_pending(self, board_id: str, card_id: str) -> None:
        key = self._key(board_id, card_id)
        self._pending_fingerprints.pop(key, None)
        self._in_flight_comment_ids.pop(key, None)
        self._in_flight_fingerprints.pop(key, None)
        self.save()

    def cancel_dispatch(self, snapshot: DeckCardSnapshot) -> None:
        """Roll back a dispatch reservation when the Gateway rejected handoff."""
        key = self._key(snapshot.board_id, snapshot.card_id)
        if self._pending_fingerprints.get(key) == snapshot.fingerprint():
            self._pending_fingerprints.pop(key, None)
        if self._in_flight_fingerprints.get(key) == snapshot.fingerprint():
            self._in_flight_fingerprints.pop(key, None)
            self._in_flight_comment_ids.pop(key, None)
        self.save()

    def mark_processed(
        self,
        snapshot: DeckCardSnapshot,
        *,
        comment_id_limit: object = _UNSET,
        dispatch_fingerprint: Optional[str] = None,
    ) -> None:
        """Document a dispatched/processed revision and persist it atomically.

        ``comment_id_limit`` is the highest comment actually included in the
        prompt. A completion-time card refresh must not consume comments that
        arrived while the agent was working.
        """
        key = self._key(snapshot.board_id, snapshot.card_id)
        self._fingerprints[key] = snapshot.fingerprint()
        self._content_fingerprints[key] = snapshot.content_fingerprint()
        self._last_labels[key] = list(snapshot.labels or [])
        candidate = snapshot.last_comment_id
        try:
            comment_id = int(str(candidate)) if candidate is not None else None
        except (ValueError, TypeError):
            comment_id = None
        if comment_id_limit is not _UNSET:
            comment_id = int(comment_id_limit) if comment_id_limit is not None else None
        if comment_id is not None:
            previous = self._last_comment_ids.get(key)
            self._last_comment_ids[key] = max(previous or 0, comment_id)
        if dispatch_fingerprint is None or self._pending_fingerprints.get(key) == dispatch_fingerprint:
            self._pending_fingerprints.pop(key, None)
        if dispatch_fingerprint is None or self._in_flight_fingerprints.get(key) == dispatch_fingerprint:
            self._in_flight_fingerprints.pop(key, None)
            self._in_flight_comment_ids.pop(key, None)
        self.save()


    def mark_seen(self, snapshot: DeckCardSnapshot) -> None:
        """Seed an observed card revision without consuming its comments.

        Used to establish the initial baseline for existing Review approvals;
        WIP-waiting turns use ``mark_dispatched`` and remain pending.
        """
        key = self._key(snapshot.board_id, snapshot.card_id)
        self._fingerprints[key] = snapshot.fingerprint()
        self._content_fingerprints[key] = snapshot.content_fingerprint()
        self._last_labels[key] = list(snapshot.labels or [])
        self.save()
