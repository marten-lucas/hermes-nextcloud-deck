from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


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
            "last_comment_message": self.last_comment_message,
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
        # Auto-Resume-Budget pro Karte (key → {"count": int, "fp": str}).
        # "count" = verbrauchte Auto-Resume-Läufe im aktuellen Karten-Zyklus;
        # "fp" identifiziert den Zyklus (Karten-Fingerprint). Ein Fingerprint-
        # Wechsel (neue Freigabe, Edit, Move) bedeutet einen neuen Zyklus und
        # setzt das Budget automatisch zurück (siehe auto_resume_budget_left).
        self._auto_resume: Dict[str, Dict[str, Any]] = {}
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
            self._fingerprints = dict(data.get("fingerprints") or {})
            self._last_labels = {
                k: list(v) for k, v in (data.get("last_labels") or {}).items()
            }
            self._last_comment_ids = {
                k: (int(v) if v is not None else None)
                for k, v in (data.get("last_comment_ids") or {}).items()
            }
            self._auto_resume = {
                k: dict(v) for k, v in (data.get("auto_resume") or {}).items()
                if isinstance(v, dict)
            }
        except Exception:
            # Best-effort: Ein korrupter State darf den Adapter nicht blockieren.
            self._fingerprints = {}
            self._last_labels = {}
            self._last_comment_ids = {}

    def save(self) -> None:
        """Persistiert den State in die JSON-Datei (best-effort)."""
        if not self._state_file:
            return
        try:
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            data = {
                "fingerprints": self._fingerprints,
                "last_labels": self._last_labels,
                "last_comment_ids": self._last_comment_ids,
                "auto_resume": self._auto_resume,
            }
            tmp = self._state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._state_file)
        except Exception:
            pass

    def last_processed_comment_id(self, board_id: str, card_id: str) -> Optional[int]:
        """Liefert die letzte Kommentar-ID, die bereits verarbeitet wurde.

        None = noch nie verarbeitet (erster Lauf). Kommentare mit höherer ID
        gelten als "neu" und werden dem Agenten als Kontext übergeben.
        """
        return self._last_comment_ids.get(self._key(board_id, card_id))

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
        """Vollständige Verarbeitung dokumentieren (Agent lief wirklich)."""
        self.mark_seen(snapshot)
        if snapshot.last_comment_id is not None:
            try:
                self._last_comment_ids[self._key(snapshot.board_id, snapshot.card_id)] = int(str(snapshot.last_comment_id))
            except (ValueError, TypeError):
                pass
        self.save()

    def auto_resume_budget_left(
        self, board_id: str, card_id: str, cap: int, fingerprint: str
    ) -> int:
        """Verbleibendes Auto-Resume-Budget für eine Karte (0 = erschöpft).

        Ein Fingerprint-Wechsel (neue Freigabe, Edit, Move) bedeutet einen
        neuen Bearbeitungszyklus und liefert das VOLLE Budget zurück —
        so bekommt jede menschliche Freigabe ein frisches Budget, während
        wiederholte (auto-)Resume-Läufe im selben Zyklus gegen das Budget
        anrechnen (Loop-Schutz).
        """
        if cap <= 0:
            return 0
        entry = self._auto_resume.get(self._key(board_id, card_id)) or {}
        if str(entry.get("fp")) != str(fingerprint):
            return cap
        return max(cap - int(entry.get("count") or 0), 0)

    def record_auto_resume(self, board_id: str, card_id: str, fingerprint: str) -> int:
        """Buchung eines Auto-Resume-Laufs. Rückgabe: verbrauchtes Budget."""
        key = self._key(board_id, card_id)
        entry = self._auto_resume.get(key) or {}
        if str(entry.get("fp")) != str(fingerprint):
            entry = {"count": 0, "fp": str(fingerprint)}
        entry["count"] = int(entry.get("count") or 0) + 1
        self._auto_resume[key] = entry
        self.save()
        return int(entry["count"])

    def mark_seen(self, snapshot: DeckCardSnapshot) -> None:
        """Nur Dedupe-Baseline setzen (Fingerprint + Labels), ohne die
        Kommentar-Baseline fortzuschreiben.

        Wird z. B. vom WIP-Guard genutzt: Die Karte wurde "gesehen" (kein
        Re-Trigger-Spam), aber der Agent hat sie NICHT verarbeitet. Die neu
        aufgelaufenen Kommentare bleiben daher als "neu" erhalten, bis der
        Agent beim nächsten freien WIP-Slot wirklich läuft.
        """
        key = self._key(snapshot.board_id, snapshot.card_id)
        self._fingerprints[key] = snapshot.fingerprint()
        self._last_labels[key] = list(snapshot.labels or [])
        self.save()
