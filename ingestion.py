"""Ingestion-Kern (aus adapter.py extrahiert, Refactoring §3).

Polling, Dedupe/Rebaseline und Trigger-Basis:

  - ``poll_once``               : ein Poll-Zyklus über alle konfigurierten Boards.
  - ``polling_loop``            : dauerhafter Polling-Loop (Health-Log, Session-Reset,
                                  neuer Review-Freigabe-Dispatch nach jedem Zyklus).
  - ``rebaseline_card``         : Dedup-Baseline auf den aktuellen Karten-Zustand setzen.
  - ``new_comments_since_baseline`` : Kommentare > letzter verarbeiteter ID (aufsteigend).
  - ``card_label_titles``       : alle Label-Titel einer Karte (deterministisch sortiert).
  - ``last_comment_author``     : Absender eines Kommentars robust auflösen.
  - ``comment_id``              : Kommentar-ID als int (oder None).

Gateway-Restart-resume wird von Hermes anhand des durable ``resume_pending``-
Sessionmarkers durchgeführt. Dieses Modul dispatcht separat neue menschliche
Freigaben in Review (s. ``review_approval_pass`` am Dateiende).

Design: Die Adapter-Instanz wird als erstes Argument übergeben — dieses Modul
importiert bewusst NICHT aus ``adapter`` (Import-Richtung bleibt strikt,
zyklenfrei). ``adapter`` importiert die Funktionen hier und stellt sie als
dünne Delegatoren bereit.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

try:
    from .platform_wip import active_platform_turns
    from .state import DeckCardSnapshot
    from .wip import in_flight_count
    from .workflow import (
        APPROVAL_APPROVED,
        is_terminal_stack,
        workflow_label_keys,
    )
except ImportError:  # direct test/import
    from platform_wip import active_platform_turns
    from state import DeckCardSnapshot
    from wip import in_flight_count
    from workflow import (
        APPROVAL_APPROVED,
        is_terminal_stack,
        workflow_label_keys,
    )

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
            # Hermes setzt gateway-seitig markierte restart_pending-Sessions
            # mit ihrem exakten Session-/Transcript-Kontext fort. Der Deck-Pass
            # hier ist ausschließlich für neue menschliche Review-Freigaben.
            try:
                await review_approval_pass(adapter)
            except Exception:
                logger.exception("Deck: Review-Freigabe-Dispatch fehlgeschlagen")
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


# ────────────────────────────────────────────────────────────────
# Review-Freigaben (getrennt von Gateway-Restart-resume)
# ────────────────────────────────────────────────────────────────
# Hermes setzt gateway-seitig markierte ``resume_pending``-Sessions fort und
# bewahrt dabei genau den unterbrochenen Conversation-/Transcript-Kontext.
# Der Deck-Poll darf deshalb keine beliebigen zugewiesenen Karten neu starten.
# Diese separate Dispatch-Logik gilt nur für eine NEUE menschliche Freigabe in
# Review; Loop-Schutz bleibt Dedupe über die verarbeitete Kartenrevision.

_APPROVED_KEY = f"approval:{APPROVAL_APPROVED}"
KIND_REVIEW_APPROVED = "review_approved"


def _is_review_stack(stack: Dict[str, Any], board_config: Optional[Dict[str, Any]]) -> bool:
    """Prüft, ob der Stack die Review-Spalte ist (Terminal UND Review).

    Nur Review-Karten mit Freigabe werden separat dispatcht — Blocked wartet
    bewusst auf den Menschen, Done ist beendet.
    """
    if not is_terminal_stack(stack, board_config):
        return False
    stack_title = str(stack.get("title") or "").strip().lower()
    stack_id = str(stack.get("id") or "").strip()
    if board_config:
        mapping = board_config.get("status_mapping") or board_config.get("stack_mapping") or {}
        configured = mapping.get("review")
        if configured and (
            str(configured).strip() == stack_id
            or str(configured).strip().lower() == stack_title
        ):
            return True
    return stack_title in {"review", "pruefung", "prüfung", "abnahme"}


def _new_approval_since_baseline(adapter, board_id: str, card_id: str, card: Dict[str, Any]) -> bool:
    """True, wenn ``approval:approved`` seit der letzten verarbeiteten
    Kartenrevision neu gesetzt wurde. Die persistierte Label-Baseline konsumiert
    die Freigabe und verhindert, dass Hermes seine eigenen Änderungen triggert."""
    if _APPROVED_KEY not in workflow_label_keys(card_label_titles(card)):
        return False
    baseline = adapter.state._last_labels.get(f"{board_id}:{card_id}")
    if baseline is None:
        # Keine Baseline = Karte wurde nie (von uns) verarbeitet →
        # Freigabe ist per Definition neu.
        return True
    return _APPROVED_KEY not in workflow_label_keys(baseline)


def find_review_approval_candidates(
    adapter,
    boards: List[Dict[str, Any]],
    stacks_by_board: Dict[str, List[Dict[str, Any]]],
) -> List[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], str]]:
    """Sammelt ausschließlich neue menschliche Freigaben in Review.

    Aktive Karten werden hier bewusst NICHT als Restart-Kandidaten behandelt:
    Hermes setzt nur Sessions mit einem echten gateway-seitigen
    ``resume_pending``-Marker fort.
    """
    candidates: List[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], str]] = []
    for board in boards:
        board_id = str(board.get("id") or "").strip()
        if not board_id:
            continue
        board_config = adapter._configured_board(board_id)
        if board_config is None:
            continue
        for stack in stacks_by_board.get(board_id) or []:
            if not _is_review_stack(stack, board_config):
                continue
            for card in stack.get("cards") or []:
                card_id = str(card.get("id") or "").strip()
                if not card_id or card_id in adapter._active_turns:
                    continue
                if _new_approval_since_baseline(adapter, board_id, card_id, card):
                    candidates.append((board, stack, card, KIND_REVIEW_APPROVED))
    return candidates


async def review_approval_pass(adapter) -> int:
    """Dispatch neue Review-Freigaben, unabhängig von Gateway-Restart-resume.

    Gateway-interrupted sessions werden von Hermes selbst fortgesetzt. Dieser
    Poll-Pass verarbeitet nur neue ``approval:approved``-Ereignisse, die der
    normale Kartenpfad ignoriert, weil Review eine End-Spalte ist.
    """
    if not adapter.runtime.auto_start_approved_reviews:
        return 0
    active = in_flight_count(adapter) + active_platform_turns(exclude_platform="deck")
    if active > 0:
        return 0

    max_in_progress = adapter.runtime.max_in_progress
    slots = max_in_progress - active if max_in_progress > 0 else 1

    boards: List[Dict[str, Any]] = []
    stacks_by_board: Dict[str, List[Dict[str, Any]]] = {}
    try:
        loaded = await adapter.client.get_boards()
        for board in loaded if isinstance(loaded, list) else []:
            board_id = str(board.get("id") or "").strip()
            if not board_id or adapter._configured_board(board_id) is None:
                continue
            boards.append(board)
            stacks_by_board[board_id] = await adapter.client.get_stacks(board_id)
    except Exception as exc:
        logger.debug("Deck: Review-Freigabe-Dispatch übersprungen (Boards nicht ladbar): %s", exc)
        return 0

    candidates = find_review_approval_candidates(adapter, boards, stacks_by_board)
    started = 0
    for board, stack, card, _kind in candidates:
        if started >= slots:
            break
        logger.info(
            "Deck: Starte neue menschliche Freigabe für Karte %s ('%s') in Review.",
            card.get("id"), card.get("title"),
        )
        try:
            await adapter._process_card(board, stack, card, force_process=True)
            started += 1
        except Exception as exc:
            # Kein persistenter Attempt-Counter: bis zur erfolgreichen Verarbeitung
            # bleibt die menschliche Freigabe offen und kann beim nächsten Poll
            # erneut versucht werden.
            logger.warning(
                "Deck: Review-Freigabe für Karte %s fehlgeschlagen: %s",
                card.get("id"), exc,
            )
    if started:
        logger.info("Deck: %d neue Review-Freigabe(n) gestartet.", started)
    return started
