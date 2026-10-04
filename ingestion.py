"""Ingestion-Kern (aus adapter.py extrahiert, Refactoring §3).

Polling, Dedupe/Rebaseline und Trigger-Basis:

  - ``poll_once``               : ein Poll-Zyklus über alle konfigurierten Boards.
  - ``polling_loop``            : dauerhafter Polling-Loop (Health-Log, Session-Reset).
  - ``rebaseline_card``         : Dedup-Baseline auf den aktuellen Karten-Zustand setzen.
  - ``new_comments_since_baseline`` : Kommentare > letzter verarbeiteter ID (aufsteigend).
  - ``card_label_titles``       : alle Label-Titel einer Karte (deterministisch sortiert).
  - ``last_comment_author``     : Absender eines Kommentars robust auflösen.
  - ``comment_id``              : Kommentar-ID als int (oder None).

Design: Die Adapter-Instanz wird als erstes Argument übergeben — dieses Modul
importiert bewusst NICHT aus ``adapter`` (Import-Richtung bleibt strikt,
zyklenfrei). ``adapter`` importiert die Funktionen hier und stellt sie als
dünne Delegatoren bereit.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

try:
    from .state import DeckCardSnapshot
except ImportError:  # direct test/import
    from state import DeckCardSnapshot

logger = logging.getLogger(__name__)


def card_label_titles(card: Dict[str, Any]) -> List[str]:
    """Alle Label-Titel einer Karte (sortiert deterministisch)."""
    titles = [
        str(lbl.get("title") or "")
        for lbl in (card.get("labels") or [])
        if isinstance(lbl, dict) and lbl.get("title")
    ]
    return sorted(titles)


def last_comment_author(comment: Dict[str, Any]) -> Optional[str]:
    """Löst den Absender eines Kommentars robust aus (mehrere Key-Fall)."""
    for key in ("actorId", "actor", "author", "userId"):
        value = comment.get(key)
        if isinstance(value, dict):
            value = value.get("uid") or value.get("id") or value.get("primaryKey")
        if value:
            return str(value).strip()
    return None


def comment_id(comment: Dict[str, Any]) -> Optional[int]:
    """Kommentar-ID als int, oder None wenn nicht zahlbar."""
    try:
        return int(str(comment.get("id") or ""))
    except (ValueError, TypeError):
        return None


def new_comments_since_baseline(
    adapter, board_id: str, card_id: str, comments: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Liefert Kommentare mit ID > letzter verarbeiteter ID (aufsteigend).

    Während eine Karte auf Waiting/Freigabe wartet, sammeln sich mehrere
    menschliche Kommentare an. Diese sollen dem Agenten beim nächsten Lauf
    ALLE als Kontext übergeben werden — nicht nur der allerletzte.
    """
    baseline = adapter.state.last_processed_comment_id(board_id, card_id)
    result: List[Dict[str, Any]] = []
    for c in comments:
        cid = comment_id(c)
        if cid is None:
            continue
        if baseline is not None and cid <= baseline:
            continue
        result.append(c)
    return result


async def rebaseline_card(adapter, board_id: str, stack_id: str, card_id: str) -> None:
    """Setzt die Dedup-Baseline auf den aktuellen Karten-Zustand neu.

    WICHTIG: ``stack_id`` kann nach einem Move der AEHRE Stack sein (z. B. die
    Karte wurde gerade von Triage nach Review verschoben). Deshalb wird die
    aktuelle Position zuerst per ``_locate_card`` neu ermittelt — sonst
    liefert ``get_card`` mit dem alten Stack None, die Baseline wird NICHT
    gesetzt, und die Karte erscheint beim nächsten Poll als "verändert"
    (Re-Trigger-Schleife nach einem Review-Move).
    """
    # Aktuelle Position (board_id + stack_id) neu auflösen, falls vorhanden.
    location = await adapter._locate_card(card_id)
    if location is not None:
        board_id, stack_id = location
    try:
        current = await adapter.client.get_card(board_id, stack_id, card_id)
    except Exception:
        return
    if not current:
        return
    comments = await adapter.client.get_card_comments(card_id)
    comments = sorted(
        comments,
        key=lambda c: int(str(c.get("id") or 0) or 0) if str(c.get("id") or 0).isdigit() else 0,
    )
    last = comments[-1] if comments else {}
    fresh = DeckCardSnapshot(
        board_id=board_id,
        stack_id=stack_id,
        card_id=card_id,
        title=str(current.get("title") or ""),
        description=str(current.get("description") or ""),
        assigned_users=adapter.identity.assigned_uids(current),
        labels=card_label_titles(current),
        last_comment_id=str(last.get("id")) if last.get("id") else None,
        last_author=last_comment_author(last) if last else None,
        last_comment_message=str(last.get("message") or "") if last else None,
        due_date=str(current.get("duedate")) if current.get("duedate") else None,
        done=current.get("done"),
    )
    adapter.state.mark_processed(fresh)


async def poll_once(adapter) -> int:
    """Ein Poll-Zyklus: Boards laden, pro Board Backlog-Template sichern und
    jede Karte durch ``_process_card`` jagen. Rückgabe: verarbeitete Karten."""
    boards = await adapter.client.get_boards()
    processed = 0
    for board in boards:
        board_id = str(board.get("id") or "").strip()
        if not board_id:
            continue
        config = adapter._configured_board(board_id)
        if config is None:
            continue
        stacks = await adapter.client.get_stacks(board_id)
        # Vorlagen-/Format-Sicherung im Backlog (pro Board, pro Poll-Zyklus)
        await adapter._ensure_backlog_template(board_id, board, stacks)
        for stack in stacks:
            for card in stack.get("cards") or []:
                await adapter._process_card(board, stack, card)
                processed += 1
    return processed


async def polling_loop(adapter) -> None:
    """Dauerhafter Polling-Loop: ``poll_once`` im ``poll_interval_seconds``-Rhythmus.

    Fix 3: Health-Log alle 20 Zyklen, damit ein stiller Loop-Tod sichtbar
    bleibt. Fix 2: Session nach Fehler zurücksetzen, damit ein einzelner
    Timeout/Connection-Error den Loop nicht dauerhaft lähmt.
    """
    poll_count = 0
    while not adapter._stop_event.is_set():
        try:
            await poll_once(adapter)
            poll_count += 1
            # Health-Log alle 20 Zyklen (~10 min bei 30s-Intervall), damit
            # ein lebender Loop beobachtbar bleibt.
            if poll_count % 20 == 0:
                logger.info("Deck: Polling-Loop aktiv (%d Zyklen).", poll_count)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Nextcloud Deck polling failed")
            # Fix 2: Session nach Fehler zurücksetzen.
            try:
                await adapter.client.reset_session()
            except Exception:
                pass
        try:
            await asyncio.wait_for(
                adapter._stop_event.wait(),
                timeout=adapter.runtime.poll_interval_seconds,
            )
        except asyncio.TimeoutError:
            pass
