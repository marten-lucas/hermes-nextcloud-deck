"""Ingestion-Kern (aus adapter.py extrahiert, Refactoring §3).

Polling, Dedupe/Rebaseline und Trigger-Basis:

  - ``poll_once``               : ein Poll-Zyklus über alle konfigurierten Boards.
  - ``polling_loop``            : dauerhafter Polling-Loop (Health-Log, Session-Reset,
                                  Auto-Resume-Pass nach jedem Zyklus).
  - ``rebaseline_card``         : Dedup-Baseline auf den aktuellen Karten-Zustand setzen.
  - ``new_comments_since_baseline`` : Kommentare > letzter verarbeiteter ID (aufsteigend).
  - ``card_label_titles``       : alle Label-Titel einer Karte (deterministisch sortiert).
  - ``last_comment_author``     : Absender eines Kommentars robust auflösen.
  - ``comment_id``              : Kommentar-ID als int (oder None).

Auto-Resume (R1–R7) — verhindert idle Warteschleifen nach Gateway-Restart
und während Leerlauf (s. ``auto_resume_pass`` am Dateiende).

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
    from .state import DeckCardSnapshot
    from .wip import card_is_triggered, in_flight_count
    from .workflow import (
        APPROVAL_APPROVED,
        is_backlog_stack,
        is_terminal_stack,
        workflow_label_keys,
    )
except ImportError:  # direct test/import
    from state import DeckCardSnapshot
    from wip import card_is_triggered, in_flight_count
    from workflow import (
        APPROVAL_APPROVED,
        is_backlog_stack,
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
            # Auto-Resume-Pass (R1–R7): Hat der Poll nichts gestartet
            # (unterbrochene Turns nach Gateway-Restart, neu freigegebene
            # Review-Karten), die Karten jetzt starten — statt idle auf einen
            # manuellen Trigger zu warten. Der ERSTE Zyklus nach connect()
            # ist damit automatisch der Startup-Pass.
            try:
                await auto_resume_pass(adapter)
            except Exception:
                logger.exception("Deck: Auto-Resume-Pass fehlgeschlagen")
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
# Auto-Resume (R1–R7)
# ────────────────────────────────────────────────────────────────
#
# Verhindert, dass das System nach Gateway-Restart und während Leerlauf
# idle wartet, obwohl Arbeit fertig liegt:
#
#   - NUR wenn aktuell KEIN Turn läuft (``in_flight_count == 0``) —
#     laufende Turns werden nicht parallel überholt (wichtig: unabhängig
#     vom WIP-Limit, auch bei ``max_in_progress=0``).
#   - Kandidaten Priorität 1 (active_work): Karten in aktiven Spalten
#     (Todo/Ready/Running/…), die dem Bot zugewiesen sind und keinen
#     frischen Trigger mehr tragen — typisch: unterbrochene Turns nach
#     einem Gateway-Restart (Fingerprint unverändert, ``should_process``
#     würde sie überspringen).
#   - Kandidaten Priorität 2 (review_approved): Karten in Review mit NEUER
#     menschlicher Freigabe (``approval:approved`` seit dem letzten Lauf).
#     Die normale Verarbeitung ignoriert End-Spalten — ohne diesen
#     Kandidaten würden erteilte Freigaben in Review idle bleiben.
#   - Budget: ``max_auto_resumes_per_card`` Auto-Resume-Läufe pro
#     Karten-Zyklus (persistiert in state.py; Fingerprint-Wechsel = neuer
#     Zyklus = frisches Budget). Erschöpftes Budget = klare Log +
#     manueller Trigger nötig (Loop-Schutz).
#   - WIP-Limit (``max_in_progress``) wird respektiert (Batch-Größe).

_APPROVED_KEY = f"approval:{APPROVAL_APPROVED}"

# Kandidaten-Arten (Priorität: active_work zuerst, dann review_approved).
KIND_ACTIVE_WORK = "active_work"
KIND_REVIEW_APPROVED = "review_approved"


def _is_review_stack(stack: Dict[str, Any], board_config: Optional[Dict[str, Any]]) -> bool:
    """Prüft, ob der Stack die Review-Spalte ist (Terminal UND Review).

    Nur Review-Karten mit Freigabe sind Auto-Resume-Kandidaten — Blocked
    wartet bewusst auf den Menschen, Done ist beendet.
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
    """True, wenn die Karte JETZT ``approval:approved`` trägt, das im
    Baseline-Zustand (letzter Lauf) NICHT stand — also eine NEUE
    menschliche Freigabe vorliegt. Bereits verarbeitete Freigaben zählen
    nicht mehr mit, selbst wenn das Label noch auf der Karte klebt
    (Konsum-Logik; das Budget ist der zweite Loop-Schutz)."""
    if _APPROVED_KEY not in workflow_label_keys(card_label_titles(card)):
        return False
    baseline = adapter.state._last_labels.get(f"{board_id}:{card_id}")
    if baseline is None:
        # Keine Baseline = Karte wurde nie (von uns) verarbeitet →
        # Freigabe ist per Definition neu.
        return True
    return _APPROVED_KEY not in workflow_label_keys(baseline)


def find_resume_candidates(
    adapter,
    boards: List[Dict[str, Any]],
    stacks_by_board: Dict[str, List[Dict[str, Any]]],
) -> List[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], str]]:
    """Sammelt Auto-Resume-Kandidaten über alle konfigurierten Boards.

    Rückgabe: Liste von ``(board, stack, card, kind)``, Priorität 1
    (``active_work``) vor Priorität 2 (``review_approved``), deterministisch
    in Board-/Stack-/Karten-Reihenfolge.
    """
    active_work: List[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], str]] = []
    review_approved: List[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], str]] = []
    for board in boards:
        board_id = str(board.get("id") or "").strip()
        if not board_id:
            continue
        board_config = adapter._configured_board(board_id)
        if board_config is None:
            continue
        for stack in stacks_by_board.get(board_id) or []:
            stack_id = str(stack.get("id") or "").strip()
            if not stack_id:
                continue
            for card in stack.get("cards") or []:
                card_id = str(card.get("id") or "").strip()
                if not card_id or card_id in adapter._active_turns:
                    continue  # läuft gerade — kein Kandidat
                if is_backlog_stack(stack, board_config):
                    continue
                if is_terminal_stack(stack, board_config):
                    # Nur Review + neue Freigabe (Priorität 2).
                    if _is_review_stack(stack, board_config) and _new_approval_since_baseline(
                        adapter, board_id, card_id, card
                    ):
                        review_approved.append((board, stack, card, KIND_REVIEW_APPROVED))
                    continue
                # Aktive Spalte (Todo/Ready/Running/Triage/…): zugewiesene
                # Karte = unterbrochene oder wartende Arbeit (Priorität 1).
                if card_is_triggered(adapter, card, []):
                    active_work.append((board, stack, card, KIND_ACTIVE_WORK))
    return active_work + review_approved


async def auto_resume_pass(adapter) -> int:
    """Ein Auto-Resume-Pass: Kandidaten sammeln und (sofern idle) starten.

    Wird nach jedem ``poll_once``-Zyklus aufgerufen (``polling_loop``) —
    der erste Zyklus nach ``connect()`` ist damit automatisch der
    Startup-Pass. Rückgabe: Anzahl gestarteter Karten.
    """
    if not adapter.runtime.auto_resume:
        return 0
    active = in_flight_count(adapter)
    if active > 0:
        return 0  # läuft bereits — nichts überholen

    max_in_progress = adapter.runtime.max_in_progress
    # Batch-Größe: WIP-Limit respektieren; bei „unbegrenzt“ (0) startet der
    # Pass pro Zyklus genau EINE Karte — der nächste Poll-Zyklus (nach dem
    # Turn) nimmt die nächste. Schützt Ollama vor Parallel-Overload.
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
        logger.debug("Deck: Auto-Resume übersprungen (Boards nicht ladbar): %s", exc)
        return 0

    candidates = find_resume_candidates(adapter, boards, stacks_by_board)
    if not candidates:
        return 0

    cap = adapter.runtime.max_auto_resumes_per_card
    started = 0
    for board, stack, card, kind in candidates:
        if started >= slots:
            break
        board_id = str(board.get("id") or "").strip()
        card_id = str(card.get("id") or "").strip()
        if not board_id or not card_id:
            continue

        # Budget prüfen + buchen (Fingerprint als Zyklus-Identifikator —
        # ein neuer Zyklus (neue Freigabe/Edit/Move) hat frisches Budget).
        comments: List[Dict[str, Any]] = []
        try:
            comments = await adapter.client.get_card_comments(card_id)
        except Exception:
            comments = []
        last = comments[-1] if comments else {}
        snapshot = DeckCardSnapshot(
            board_id=board_id,
            stack_id=str(stack.get("id") or ""),
            card_id=card_id,
            title=str(card.get("title") or ""),
            description=str(card.get("description") or ""),
            assigned_users=adapter.identity.assigned_uids(card),
            labels=card_label_titles(card),
            last_comment_id=str(last.get("id")) if last.get("id") else None,
            last_author=last_comment_author(last) if last else None,
            last_comment_message=str(last.get("message") or "") if last else None,
            due_date=str(card.get("duedate")) if card.get("duedate") else None,
            done=card.get("done"),
        )
        fingerprint = snapshot.fingerprint()
        if adapter.state.auto_resume_budget_left(board_id, card_id, cap, fingerprint) <= 0:
            logger.info(
                "Deck: Auto-Resume-Budget für Karte %s ('%s') erschöpft (%d Läufe) — "
                "bitte manuell triggern (Kommentar/Zuweisung) oder State-Budget zurücksetzen.",
                card_id, snapshot.title, cap,
            )
            continue
        adapter.state.record_auto_resume(board_id, card_id, fingerprint)
        reason = (
            "unterbrochene Arbeit in aktiver Spalte"
            if kind == KIND_ACTIVE_WORK
            else "neue Freigabe in Review"
        )
        logger.info(
            "Deck: Auto-Resume startet Karte %s ('%s') — %s.",
            card_id, snapshot.title, reason,
        )
        try:
            await adapter._process_card(board, stack, card, force_resume=True)
            started += 1
        except Exception as exc:
            # Ein fehlgeschlagener Start zählt trotzdem ins Budget
            # (Loop-Schutz); die Exception wird abgefangen, damit der
            # Polling-Loop lebt.
            logger.warning("Deck: Auto-Resume für Karte %s fehlgeschlagen: %s", card_id, exc)
    if started:
        logger.info("Deck: Auto-Resume: %d Karte(n) gestartet.", started)
    return started
