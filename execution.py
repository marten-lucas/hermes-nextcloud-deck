"""Execution-Kern (aus adapter.py extrahiert, Refactoring §3).

deck_card_action-Handler-Basis, Prompt-/Gate-Logik und Intent-Erkennung:

  - ``update_card_description`` : Agent-Workspace-Teil der Description aktualisieren.
  - ``move_card_to_status``     : Karte in Ziel-Stack verschieben (mit Gate-Prüfung).
  - ``xor_group_of`` / ``remove_xor_siblings`` : XOR-Label-Invariante (Konzept 2).
  - ``normalize_label_conflicts`` : Poll-seitige idempotente Label-Konflikt-Bereinigung.
  - ``apply_label_to_card`` / ``remove_label_from_card`` : Label-Zu/-Entfernung (mit Gate).
  - ``assign_user_to_card`` / ``unassign_user_from_card`` : Assignee-Handhabung.
  - ``locate_card`` / ``resolve_target_stack_id`` : Kartenspaltung / Stack-Auflösung.
  - ``run_has_failure_signal`` / ``run_has_review_intent`` / ``run_has_block_intent`` :
    Intent-Erkennung des Run-Ergebnisses (ehemals @classmethod).
  - ``auto_block_card``         : deterministischer Block nach Fehl-Lauf.

Design: Die Adapter-Instanz wird als erstes Argument übergeben — dieses Modul
importiert bewusst NICHT aus ``adapter`` (Import-Richtung bleibt strikt,
zyklenfrei). Klassenattribute wie ``_LABEL_XOR_GROUPS`` bzw. die Marker-Tupels
bleiben in der Adapter-Klasse und werden über die Instanz gelesen
(``adapter._RUN_FAILURE_MARKERS``). ``adapter`` stellt die Funktionen als
dünne Delegatoren bereit.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

try:
    from .client import NextcloudDeckError
    from .workflow import (
        AGENT_WORKSPACE_MARKER,
        APPROVAL_APPROVED,
        APPROVAL_REQUIRED,
        FRIENDLY_LABELS,
        LABEL_PREFIX_PHASE,
        PHASE_EXECUTE,
        PHASE_PLAN,
        STATUS_BLOCKED,
        STATUS_REVIEW,
        canonical_label_key,
        check_agent_label_gate,
        check_agent_status_gate,
        extract_hermes_labels,
        friendly_label_title,
        split_agent_workspace,
    )
except ImportError:  # direct test/import
    from client import NextcloudDeckError
    from workflow import (
        AGENT_WORKSPACE_MARKER,
        APPROVAL_APPROVED,
        APPROVAL_REQUIRED,
        FRIENDLY_LABELS,
        LABEL_PREFIX_PHASE,
        PHASE_EXECUTE,
        PHASE_PLAN,
        STATUS_BLOCKED,
        STATUS_REVIEW,
        canonical_label_key,
        check_agent_label_gate,
        check_agent_status_gate,
        extract_hermes_labels,
        friendly_label_title,
        split_agent_workspace,
    )

logger = logging.getLogger(__name__)


async def update_card_description(adapter, card_id: str, description: str) -> bool:
    """Aktualisiert den AGENT-WORKSPACE-Teil der Karten-Beschreibung.

    Der Agent sendet nur den Inhalt seiner Agent-Sektionen (die ``##``-Blöcke
    unterhalb des ``# Agent Workspace``-Markers). Dieser Handler ersetzt
    ausschließlich den Teil AB dem Marker — der Mensch-Teil bleibt byte-genau
    erhalten. Ist noch kein Marker vorhanden (z. B. alte Karte), wird er
    zusammen mit dem Agent-Inhalt angehängt.

    Truncation-Absicherung: Ist der neue Agent-Teil ein bloßes Präfix des
    bestehenden Agent-Teils (Abruch mitten im Schreiben), wird er verworfen.
    """
    location = await adapter._locate_card(card_id)
    if location is None:
        return False
    board_id, stack_id = location

    new_agent = (description or "").strip()

    try:
        current_card = await adapter.client.get_card(board_id, stack_id, card_id)
        existing = str((current_card or {}).get("description") or "")
    except NextcloudDeckError:
        existing = ""

    human_part, old_agent = split_agent_workspace(existing)

    # Truncation-Verdacht: neuer Agent-Teil ist ein Präfix des bisherigen.
    if old_agent and new_agent:
        old_agent_stripped = old_agent.strip()
        head = old_agent_stripped[: max(1, len(new_agent))]
        if len(new_agent) < len(old_agent_stripped) and head == new_agent:
            logger.warning(
                "Deck: Agent-Workspace-Update für Karte %s verworfen — neuer Text ist ein abgeschnittenes Präfix (Truncation-Verdacht).",
                card_id,
            )
            return False

    # Zusammenbauen: Mensch-Teil + Marker + neuer Agent-Teil.
    marker_line = f"# {AGENT_WORKSPACE_MARKER}"
    if old_agent is None and human_part:
        # Kein Agent-Workspace-Marker vorhanden (Alt-Karte im alten Template).
        new_description = f"{human_part.rstrip()}\n\n{marker_line}\n\n{new_agent}".rstrip()
    elif human_part:
        # human_part enthält bereits die Marker-Zeile (aus split_agent_workspace).
        new_description = f"{human_part.rstrip()}\n\n{new_agent}".rstrip()
    else:
        new_description = f"{marker_line}\n\n{new_agent}".rstrip()

    if not new_description:
        return False

    try:
        result = await adapter.client.update_card(board_id, stack_id, card_id, description=new_description)
        return result is not None
    except NextcloudDeckError as exc:
        logger.warning("Deck description update failed for card %s: %s", card_id, exc)
        return False


async def move_card_to_status(adapter, card_id: str, status_key: str) -> tuple:
    """Verschiebt die Karte in den Ziel-Stack mit Berücksichtigung von Workflow-Gates."""
    location = await adapter._locate_card(card_id)
    if location is None:
        return False, f"Card {card_id} could not be located"
    board_id, stack_id = location

    # Karte laden, um aktuelle Phase / Approval-Status zu prüfen
    current_card = await adapter.client.get_card(board_id, stack_id, card_id)
    if current_card:
        hermes_labels = extract_hermes_labels(current_card)
        phase = hermes_labels.get("phase") or PHASE_PLAN
        approval = hermes_labels.get("approval")
        task_type = hermes_labels.get("type")
        risk = hermes_labels.get("risk")
        allowed, reason = check_agent_status_gate(status_key, phase, approval, task_type, risk)
        if not allowed:
            logger.warning("Deck: Gate-Sperre für Karte %s (Move nach '%s'): %s", card_id, status_key, reason)
            return False, reason

    # Ziel-Stack ermitteln: Board-Konfiguration (status_mapping) oder Stack-Titel-Match
    target_stack_id = await adapter._resolve_target_stack_id(board_id, status_key)
    if not target_stack_id:
        logger.warning("Deck: kein Ziel-Stack für Status '%s' in Board %s konfiguriert", status_key, board_id)
        return False, f"No target stack found for status '{status_key}'"

    try:
        result = await adapter.client.move_card(board_id, stack_id, card_id, target_stack_id)
        if result is None:
            return False, f"Move of card {card_id} to stack {target_stack_id} failed (empty response)"
        # Verifikation: Die Deck-API kann Moves still fehlschlagen lassen
        # (HTTP 200, aber keine Änderung). Den tatsächlichen Stack prüfen.
        verify_location = await adapter._locate_card(card_id)
        if verify_location is not None:
            _, actual_stack_id = verify_location
            if str(actual_stack_id) != str(target_stack_id):
                logger.warning(
                    "Deck: Move-Verifikation fehlgeschlagen für Karte %s: erwartet Stack %s, tatsächlich %s",
                    card_id, target_stack_id, actual_stack_id,
                )
                return False, f"Card {card_id} did not move to stack {target_stack_id} (silent API failure)"
        # Konzept 2 (I3): Ein Move nach 'review' bedeutet "wartet auf (erneute)
        # Freigabe". Den Alt-Zustand deterministisch bereinigen.
        status_norm = str(status_key or "").strip().lower()
        if status_norm == STATUS_REVIEW:
            try:
                await adapter._remove_label_from_card(card_id, f"{LABEL_PREFIX_PHASE}{PHASE_EXECUTE}")
                await adapter._apply_label_to_card(card_id, f"approval:{APPROVAL_REQUIRED}")
            except Exception as exc:
                logger.debug("Deck: Review-Normalisierung für Karte %s fehlgeschlagen: %s", card_id, exc)

        logger.info(
            "Deck: Karte %s nach Stack %s ('%s') verschoben und verifiziert.",
            card_id, target_stack_id, status_key,
        )
        return True, None
    except NextcloudDeckError as exc:
        logger.warning("Deck card move failed for card %s: %s", card_id, exc)
        return False, str(exc)


def xor_group_of(adapter, canonical_key: Optional[str]) -> Optional[tuple]:
    """Liefert die XOR-Gruppe (Paar kanonischer Keys), zu der ``canonical_key``
    gehört, oder None, wenn der Key keiner Gruppe angehört."""
    if not canonical_key:
        return None
    for group in adapter._LABEL_XOR_GROUPS:
        if canonical_key in group:
            return group
    return None


async def remove_xor_siblings(
    adapter,
    card_id: str,
    canonical_key: str,
    except_sibling: Optional[str] = None,
) -> None:
    """Entfernt die Schwester-Labels derselben XOR-Gruppe von einer Karte.

    Setzt man z. B. ``phase:execute``, wird ``phase:plan`` entfernt; setzt man
    ``approval:required``, wird ``approval:approved`` entfernt. Dadurch kann
    auf einer Karte nie mehr als EIN Label pro Gruppe liegen.
    Best-effort: Fehler werden geloggt, niemals geworfen.
    """
    group = xor_group_of(adapter, canonical_key)
    if not group:
        return
    for sibling in group:
        if sibling == canonical_key:
            continue
        if except_sibling and sibling == except_sibling:
            continue
        try:
            removed = await adapter._remove_label_from_card(card_id, sibling)
            if removed:
                logger.info(
                    "Deck: XOR-Invariante — '%s' entfernt (Konflikt mit '%s') auf Karte %s.",
                    sibling, canonical_key, card_id,
                )
        except Exception as exc:
            logger.debug("Deck: XOR-Entfernung '%s' auf Karte %s fehlgeschlagen: %s", sibling, card_id, exc)


async def normalize_label_conflicts(adapter, card_id: str) -> None:
    """Poll-seitige Normalisierung (Konzept 2, Punkt 3): erkennt und bereinigt
    widersprüchliche Label-Paare auf einer Karte IDEMPOTENT, ohne auf einen
    Agent-Lauf warten zu müssen.

    Regel (Best-effort, deterministisch): Der aktuelle/menschliche Zustand
    gewinnt. Phase-Gruppe: ``execute`` gewinnt. Approval-Gruppe: ``required``
    gewinnt.
    """
    location = await adapter._locate_card(card_id)
    if location is None:
        return
    board_id, stack_id = location
    try:
        current_card = await adapter.client.get_card(board_id, stack_id, card_id)
    except NextcloudDeckError:
        return
    if not current_card:
        return
    label_keys = set()
    for lbl in (current_card.get("labels") or []):
        key = canonical_label_key(str(lbl.get("title") or ""))
        if key:
            label_keys.add(key)
    # Phase: execute gewinnt gegen plan
    phase_execute = f"{LABEL_PREFIX_PHASE}{PHASE_EXECUTE}"
    phase_plan = f"{LABEL_PREFIX_PHASE}{PHASE_PLAN}"
    if phase_execute in label_keys and phase_plan in label_keys:
        await adapter._remove_label_from_card(card_id, phase_plan)
        logger.info("Deck: Label-Konflikt bereinigt — 'phase:plan' entfernt (execute gewinnt) auf Karte %s.", card_id)
    # Approval: required gewinnt gegen approved
    approval_required = f"approval:{APPROVAL_REQUIRED}"
    approval_approved = f"approval:{APPROVAL_APPROVED}"
    if approval_required in label_keys and approval_approved in label_keys:
        await adapter._remove_label_from_card(card_id, approval_approved)
        logger.info("Deck: Label-Konflikt bereinigt — 'approval:approved' entfernt (required gewinnt) auf Karte %s.", card_id)


async def apply_label_to_card(adapter, card_id: str, label_title: str) -> tuple:
    """Weist der Karte ein Label zu (erstellt das Label bei Bedarf auf dem Board).

    Akzeptiert Friendly-Titel ("⌛ Freigabe nötig"), kanonische Keys
    ("approval:required") sowie die alte technische Form ("hermes/approval:required").
    Im Board wird das Label immer im Friendly-Format mit Mapping-Farbe angelegt.
    """
    # Kanonischen Key auflösen; unbekannte Labels unverändert durchreichen
    canonical_key = canonical_label_key(label_title)
    board_title = friendly_label_title(canonical_key) if canonical_key else label_title

    location = await adapter._locate_card(card_id)
    if location is None:
        return False, "Card could not be located"
    board_id, stack_id = location

    current_card = await adapter.client.get_card(board_id, stack_id, card_id)
    if current_card:
        # Idempotenz: Ist das Label (in irgendeiner Schreibweise) bereits auf
        # der Karte, ist die Zuweisung ein No-Op.
        existing_keys = {
            canonical_label_key(str(lbl.get("title") or "")) or str(lbl.get("title") or "").strip().lower()
            for lbl in (current_card.get("labels") or [])
            if isinstance(lbl, dict)
        }
        target_key = canonical_key or board_title.strip().lower()
        if target_key in existing_keys:
            logger.debug("Deck: Label '%s' ist bereits auf Karte %s — übersprungen.", board_title, card_id)
            return True, None

        hermes_labels = extract_hermes_labels(current_card)
        phase = hermes_labels.get("phase") or PHASE_PLAN
        approval = hermes_labels.get("approval")
        task_type = hermes_labels.get("type")
        risk = hermes_labels.get("risk")
        allowed, reason = check_agent_label_gate(label_title, phase, approval, task_type, risk)
        if not allowed:
            logger.warning("Deck: Label-Gate-Sperre für Karte %s: %s", card_id, reason)
            return False, reason

    board_labels = await adapter.client.get_board_labels(board_id)
    target_label_id = None
    for lbl in board_labels:
        lbl_key = canonical_label_key(str(lbl.get("title") or ""))
        if lbl_key and lbl_key == canonical_key:
            target_label_id = lbl.get("id")
            break
        if not lbl_key and str(lbl.get("title") or "").strip().lower() == board_title.strip().lower():
            target_label_id = lbl.get("id")
            break

    # Wenn Label noch nicht auf dem Board existiert: anlegen (Friendly-Titel + Mapping-Farbe)
    if target_label_id is None:
        color = "317CCC"
        if canonical_key and canonical_key in FRIENDLY_LABELS:
            color = FRIENDLY_LABELS[canonical_key][1]
        try:
            new_lbl = await adapter.client.create_board_label(board_id, board_title, color=color)
        except NextcloudDeckError as exc:
            # Z. B. HTTP 403: Der Bot-User darf keine Board-Labels anlegen.
            # Das darf den Poll-Zyklus NICHT crashen — sauber als Fehlschlag melden.
            logger.warning(
                "Deck: Konnte Label '%s' auf Board %s nicht anlegen (%s) — Label-Zuweisung übersprungen.",
                board_title, board_id, exc,
            )
            return False, str(exc)
        if new_lbl and new_lbl.get("id"):
            target_label_id = new_lbl["id"]

    if target_label_id is None:
        return False, f"Could not create/find label '{label_title}' on board {board_id}"

    try:
        res = await adapter.client.assign_label(board_id, stack_id, card_id, int(target_label_id))
    except NextcloudDeckError as exc:
        # Wenn bereits zugewiesen, als Erfolg werten
        if "already assigned" in str(exc).lower():
            return True, None
        logger.warning("Deck assign_label failed for card %s: %s", card_id, exc)
        return False, str(exc)

    # XOR-Invariante (Konzept 2): Schwester-Labels derselben Gruppe entfernen.
    if canonical_key:
        await remove_xor_siblings(adapter, card_id, canonical_key)

    return res is not None, None


async def remove_label_from_card(adapter, card_id: str, label_title: str) -> bool:
    """Entfernt ein Label von der Karte (Friendly-Titel oder kanonischer Key)."""
    location = await adapter._locate_card(card_id)
    if location is None:
        return False
    board_id, stack_id = location

    canonical_key = canonical_label_key(label_title)
    board_labels = await adapter.client.get_board_labels(board_id)
    target_label_id = None
    for lbl in board_labels:
        lbl_key = canonical_label_key(str(lbl.get("title") or ""))
        if canonical_key and lbl_key == canonical_key:
            target_label_id = lbl.get("id")
            break
        if str(lbl.get("title") or "").strip().lower() == label_title.strip().lower():
            target_label_id = lbl.get("id")
            break

    if target_label_id is None:
        return False

    try:
        res = await adapter.client.remove_label(board_id, stack_id, card_id, int(target_label_id))
        return res is not None
    except NextcloudDeckError as exc:
        logger.warning("Deck remove_label failed for card %s: %s", card_id, exc)
        return False


async def assign_user_to_card(adapter, card_id: str, user_id: str) -> bool:
    """Weist einen Benutzer der Karte zu (löst Username -> UID auf)."""
    location = await adapter._locate_card(card_id)
    if location is None:
        return False
    board_id, stack_id = location

    # Username/Display-Name -> Nextcloud-UID auflösen (Handoff an Menschen).
    resolved = await adapter.identity.resolve_user_uid(user_id)
    if resolved:
        user_id = resolved

    try:
        res = await adapter.client.assign_user(board_id, stack_id, card_id, user_id)
        return res is not None
    except NextcloudDeckError as exc:
        if "already assigned" in str(exc).lower():
            return True
        logger.warning("Deck assign_user failed for card %s: %s", card_id, exc)
        return False


async def unassign_user_from_card(adapter, card_id: str, user_id: str) -> bool:
    """Entfernt einen Benutzer von der Karte."""
    location = await adapter._locate_card(card_id)
    if location is None:
        return False
    board_id, stack_id = location
    try:
        res = await adapter.client.unassign_user(board_id, stack_id, card_id, user_id)
        return res is not None
    except NextcloudDeckError as exc:
        logger.warning("Deck unassign_user failed for card %s: %s", card_id, exc)
        return False


async def locate_card(adapter, card_id: str) -> Optional[tuple]:
    """Findet (board_id, stack_id) einer Karte über die konfigurierten Boards."""
    try:
        boards = await adapter.client.get_boards()
    except NextcloudDeckError as exc:
        logger.warning("Deck: Boards konnten nicht geladen werden: %s", exc)
        return None
    for board in boards if isinstance(boards, list) else []:
        board_id = str(board.get("id") or "").strip()
        if not board_id or (adapter.runtime.boards and board_id not in adapter.runtime.boards):
            continue
        try:
            stacks = await adapter.client.get_stacks(board_id)
        except NextcloudDeckError:
            continue
        for stack in stacks if isinstance(stacks, list) else []:
            stack_id = str(stack.get("id") or "").strip()
            for card in stack.get("cards") or []:
                if str(card.get("id") or "") == card_id:
                    return board_id, stack_id
    return None


async def resolve_target_stack_id(adapter, board_id: str, status_key: str) -> Optional[str]:
    """Löst einen Status-Schlüssel zu einer Stack-ID auf (Board-Config 'status_mapping' oder Stack-Titel)."""
    config = adapter._configured_board(board_id) or {}
    mapping = config.get("status_mapping") or config.get("stack_mapping") or {}
    if isinstance(mapping, dict):
        target = mapping.get(status_key) or mapping.get(status_key.lower())
        if target:
            return str(target).strip()

    # Fallback: Stack-Titel-Match (case-insensitive)
    try:
        stacks = await adapter.client.get_stacks(board_id)
    except NextcloudDeckError:
        return None
    for stack in stacks if isinstance(stacks, list) else []:
        title = str(stack.get("title") or "").strip().casefold()
        if title == status_key.strip().casefold():
            return str(stack.get("id") or "").strip() or None
    return None


def run_has_failure_signal(adapter, result: Any) -> bool:
    """Erkennt Fehlschlag-Signale im Run-Ergebnis.

    Prüft den Text des Ergebnisses auf bekannte Token-Limit-/Fehler-Marker
    (z. B. „No visible answer was produced ... output-token limit").
    """
    text = ""
    if isinstance(result, str):
        text = result
    elif isinstance(result, dict):
        text = str(result.get("text") or result.get("content") or result.get("error") or "")
    else:
        text = str(result or "")
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in adapter._RUN_FAILURE_MARKERS)


def run_has_review_intent(adapter, result: Any) -> bool:
    """Erkennt ein Review-Intent-Signal des Agenten (Fix 1).

    Prüft auf semantische Muster „ich pausiere und warte auf eine menschliche
    Entscheidung/Freigabe".
    """
    text = ""
    if isinstance(result, str):
        text = result
    elif isinstance(result, dict):
        text = str(result.get("text") or result.get("content") or result.get("error") or "")
    else:
        text = str(result or "")
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in adapter._REVIEW_INTENT_MARKERS)


def run_has_block_intent(adapter, result: Any) -> bool:
    """Erkennt ein inhaltliches Block-Intent-Signal des Agenten (Konzept 1).

    Anders als ``run_has_failure_signal`` (technischer Token-/Fehler-Marker)
    prüft dies auf ein semantisches „ich kann das nicht lösen / Plan-Change
    nötig"-Signal.
    """
    text = ""
    if isinstance(result, str):
        text = result
    elif isinstance(result, dict):
        text = str(result.get("text") or result.get("content") or result.get("error") or "")
    else:
        text = str(result or "")
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in adapter._BLOCK_INTENT_MARKERS)


async def auto_block_card(adapter, card_id: str) -> None:
    """Verschiebt eine fehlgeschlagene Karte nach 'blocked' + Kommentar.

    Wird vom Adapter (nicht vom Agenten) ausgelöst, wenn ein Run ohne
    verwertbare strukturelle Änderung endete oder eine Exception warf. Gibt
    den WIP-Platz frei, sodass die nächste wartende Karte starten kann.
    Best-effort: Fehler beim Move/Kommentar werden geloggt, niemals geworfen.
    """
    try:
        moved, err = await move_card_to_status(adapter, card_id, STATUS_BLOCKED)
        if moved:
            logger.info("Deck: Karte %s automatisch nach 'blocked' verschoben (Run-Fehler).", card_id)
        else:
            logger.warning("Deck: Auto-Block für Karte %s fehlgeschlagen: %s", card_id, err)
    except Exception as exc:
        logger.warning("Deck: Auto-Block für Karte %s schlug fehl: %s", card_id, exc)
    try:
        await adapter.client.add_comment(
            card_id,
            "🤖 AUTO-BLOCKED: Der Agent-Run konnte keine verwertbare Antwort erzeugen "
            "(Token-Limit oder interner Fehler). Bitte prüfen und ggf. Reasoning anpassen.",
        )
    except Exception as exc:
        logger.warning("Deck: Auto-Block-Kommentar für Karte %s fehlgeschlagen: %s", card_id, exc)
