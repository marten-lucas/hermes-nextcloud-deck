"""WIP-Limit-Kern (aus adapter.py extrahiert, Refactoring §3).

Kapselt das In-Flight-Turn-Tracking und die Trigger-Erkennung, die das
globale WIP-Limit (``max_in_progress``) tragen:

  - ``active_turn``       : async-Context-Manager, registriert einen Turn als
                            in-flight und entfernt ihn beim Verlassen (auch bei
                            Exception). UNABHÄNGIG vom Speed-Feature (FUNC-002).
  - ``count_active_cards``: Zähl-Basis des WIP-Guards (in-flight Turns).
  - ``card_is_triggered`` : erkennt, ob eine Karte den Bot triggert.

Design: Die Adapter-Instanz wird als erstes Argument übergeben — dieses Modul
importiert bewusst NICHT aus ``adapter`` (Import-Richtung bleibt strikt,
zyklenfrei). ``adapter`` importiert die Funktionen hier und stellt sie als
dünne Delegatoren bereit.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@asynccontextmanager
async def active_turn(adapter, card_id: str):
    """WIP-Tracking: registriert den Turn dieser Karte als in-flight und
    entfernt ihn beim Verlassen des Blocks — auch bei Exception (kein Leak).

    UNABHÄNGIG vom Speed-Feature: ``send_typing()`` early-returnt bei
    ``speed_enabled=False`` und hätte ``_active_turns`` nie gefüllt; dadurch
    war das WIP-Limit (``max_in_progress``) unter der Default-Konfiguration
    faktisch wirkungslos (FUNC-002). Der Turn-Lifecycle (add/discard) gehört
    an die Stelle, wo der Turn tatsächlich startet/endet, nicht an das
    Speed-Feature.
    """
    adapter._active_turns.add(card_id)
    try:
        yield
    finally:
        adapter._active_turns.discard(card_id)


def in_flight_count(adapter, exclude_card_id: Optional[str] = None) -> int:
    """Anzahl der tatsächlich laufenden (in-flight) Turns — UNABHÄNGIG vom
    WIP-Limit. Im Gegensatz zu ``count_active_cards``, das bei
    ``max_in_progress=0`` (unbegrenzt) per Definition 0 liefert, zählt diese
    Funktion ``_active_turns`` immer — sie ist die Basis der Idle-Prüfung im
    Auto-Resume-Pass (sonst würde bei Default-Konfiguration parallel zu einem
    laufenden Turn gestartet)."""
    total = 0
    for cid in adapter._active_turns:
        if exclude_card_id and cid == exclude_card_id:
            continue
        total += 1
    return total


async def count_active_cards(adapter, exclude_card_id: Optional[str] = None) -> int:
    """Zählt Karten, die TATSÄCHLICH einen Turn laufen (in-flight).

    Wird für das GLOBALE WIP-LIMIT genutzt. Nur Karten, deren Turn gerade
    aktiv läuft (``_active_turns``), zählen gegen das Limit. Karten, die nur
    in einer aktiven Spalte (todo/ready/running) liegen, aber KEINEN Turn
    laufen, zählen NICHT — sonst blockiert eine fertige Karte in Ready das
    WIP-Limit, obwohl sie gar nicht arbeitet.

    Bei ``max_in_progress=0`` (unbegrenzt) gibt es kein Limit zu prüfen —
    daher 0 (WIP-Semantik). Für die Idle-Prüfung (Auto-Resume) ist
    ``in_flight_count`` die richtige Basis.
    """
    if not adapter.runtime.max_in_progress:
        return 0
    return in_flight_count(adapter, exclude_card_id)


def card_is_triggered(adapter, card: Dict[str, Any], comments: List[Dict[str, Any]]) -> bool:
    """Erkennt, ob eine Karte den Bot triggert (Zuweisung, @-Nennung in
    Beschreibung oder Kommentar, Bot-Aliases)."""
    assigned = set(adapter.identity.assigned_uids(card))
    if adapter.runtime.hermes_user_id in assigned:
        return True

    needles = {
        adapter.runtime.hermes_user_id.lower(),
        adapter.runtime.username.lower(),
        *adapter.runtime.bot_aliases,
    }
    description = str(card.get("description") or "").lower()
    if any(needle and needle in description for needle in needles):
        return True

    for comment in comments:
        message = str(comment.get("message") or "").lower()
        if any(needle and needle in message for needle in needles):
            return True
    return False
