"""Formatting-Kern (aus adapter.py extrahiert, Refactoring §3).

Template-Scaffold, Quiet-Window, Speed-Heartbeat und Status-Mapping:

  - ``ensure_backlog_template`` : Vorlagenkarte im Backlog sichern + Backlog-Format prüfen.
  - ``run_speed_heartbeat``     : eigener Speed-Heartbeat während eines Turns.
  - ``fetch_speed``             : Live-Geschwindigkeit vom Ollama-Sidecar lesen.
  - ``speed_suffix``            : Sidecar-JSON → kompakten Geschwindigkeits-Text.
  - ``card_status_label``       : kompaktes Karten-Label ('Karte N · Titel') für den Status.
  - ``map_progress_status``     : Gateway-Status → (Text, Emoji) für den User-Status.

Design: Die Adapter-Instanz wird als erstes Argument übergeben — dieses Modul
importiert bewusst NICHT aus ``adapter`` (Import-Richtung bleibt strikt,
zyklenfrei). ``adapter`` importiert die Funktionen hier und stellt sie als
dünne Delegatoren bereit.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp

try:
    from .client import NextcloudDeckError
    from .workflow import (
        is_backlog_stack,
        missing_template_sections,
        template_description,
        TEMPLATE_CARD_TITLE,
    )
except ImportError:  # direct test/import
    from client import NextcloudDeckError
    from workflow import (
        is_backlog_stack,
        missing_template_sections,
        template_description,
        TEMPLATE_CARD_TITLE,
    )

logger = logging.getLogger(__name__)


async def ensure_backlog_template(
    adapter,
    board_id: str,
    board: Dict[str, Any],
    stacks: List[Dict[str, Any]],
) -> None:
    """Sichert die Referenz-Vorlagenkarte im Backlog und prüft Backlog-Format.

    Zwei Aufgaben (deterministisch, kein LLM beteiligt):

    1. **Vorlagenkarte sicherstellen:** Existiert im Backlog keine Karte mit
       dem Titel ``TEMPLATE_CARD_TITLE``, wird eine mit der
       Template-Description angelegt.

    2. **Format-Prüfung aller Backlog-Karten:** Karten im Backlog, deren
       Description NICHT dem Task-Contract entspricht, bekommen das
       Template-Skelett. Aktiv bearbeitete Karten (Ruhe-Schwelle) bleiben
       unangetastet; vorhandener menschengeschriebener Text wird bewahrt.
    """
    try:
        backlog_stacks = [
            s for s in stacks
            if is_backlog_stack(s, adapter._configured_board(board_id))
        ]
        if not backlog_stacks:
            return

        backlog_stack = backlog_stacks[0]
        backlog_stack_id = str(backlog_stack.get("id") or "").strip()
        cards = backlog_stack.get("cards") or []

        # 1. Vorlagenkarte prüfen/anlegen
        template_exists = any(
            str(c.get("title") or "").strip().lower() == TEMPLATE_CARD_TITLE.lower()
            for c in cards
        )
        if not template_exists:
            try:
                created = await adapter.client.create_card(
                    board_id,
                    backlog_stack_id,
                    title=TEMPLATE_CARD_TITLE,
                    description=template_description(adapter.runtime.template_language),
                    order=0,
                )
                if created and created.get("id"):
                    logger.info(
                        "Deck: Vorlagenkarte '%s' im Backlog von Board %s angelegt.",
                        TEMPLATE_CARD_TITLE, board_id,
                    )
                else:
                    logger.warning("Deck: Vorlagenkarte konnte nicht angelegt werden (Board %s).", board_id)
            except NextcloudDeckError as exc:
                logger.warning("Deck: Vorlagenkarte anlegen fehlgeschlagen (Board %s): %s", board_id, exc)

        # 2. Format-Prüfung aller Backlog-Karten (außer der Vorlage selbst)
        now = time.time()
        quiet = adapter.runtime.backlog_format_quiet_seconds
        for card in cards:
            title = str(card.get("title") or "").strip()
            if title.lower() == TEMPLATE_CARD_TITLE.lower():
                continue
            description = str(card.get("description") or "")

            # Aktiv bearbeitete Karten NICHT anfassen (nur wenn die
            # Ruhe-Schwelle > 0 ist): quiet_seconds == 0 deaktiviert die Prüfung.
            if quiet > 0:
                last_modified = card.get("lastModified")
                if isinstance(last_modified, (int, float)) and last_modified > 0:
                    age_seconds = now - last_modified
                    if age_seconds < quiet:
                        logger.debug(
                            "Deck: Backlog-Karte %s ('%s') wurde vor %.0fs geändert — Format-Korrektur übersprungen (aktiv bearbeitet).",
                            card.get("id"), title, age_seconds,
                        )
                        continue

            # Nur FEHLENDE Kernabschnitte ermitteln — vorhandener Text bleibt.
            missing = missing_template_sections(description)

            # Eine völlig leere Description bekommt das komplette Skelett.
            if not missing and not description.strip():
                missing = [template_description(adapter.runtime.template_language).strip()]

            if not missing:
                continue

            card_id = str(card.get("id") or "").strip()
            if not card_id:
                continue

            # Fehlende Abschnitte UNTEN anhängen (Präfix entfernt Platzhalter).
            suffix = "\n\n".join(missing)
            new_description = description.strip()
            if new_description:
                new_description = f"{new_description}\n\n{suffix}"
            else:
                new_description = suffix

            try:
                await adapter.client.update_card(
                    board_id, backlog_stack_id, card_id, description=new_description
                )
                logger.info(
                    "Deck: Backlog-Karte %s ('%s') — fehlende Abschnitte ergänzt: %s",
                    card_id,
                    title,
                    ", ".join(
                        m.splitlines()[0].lstrip("#").strip() if m.splitlines() else m
                        for m in missing
                    ),
                )
            except NextcloudDeckError as exc:
                logger.warning("Deck: Format-Fix für Karte %s fehlgeschlagen: %s", card_id, exc)
    except Exception as exc:
        logger.warning("Deck: Backlog-Template-Sicherung fehlgeschlagen (Board %s): %s", board_id, exc)


async def run_speed_heartbeat(adapter, chat_id: str, stop_event: asyncio.Event) -> None:
    """Eigener Speed-Heartbeat: setzt den Live-Status während eines Turns.

    Das Gateway ruft ``send_typing`` nur auf, wenn es Progress-Messages gibt
    (Deck sendet keine, weil es kein Typing-Konzept hat). Deshalb starten wir
    hier einen eigenen Heartbeat, der ``send_typing`` alle paar Sekunden
    aufruft, solange der Turn läuft (``stop_event`` nicht gesetzt).
    """
    if not adapter.runtime.speed_enabled:
        return
    try:
        while not stop_event.is_set():
            try:
                await adapter.send_typing(chat_id)
            except Exception as exc:
                logger.debug("Deck: Speed-Heartbeat fehlgeschlagen: %s", exc)
            # Kurz warten, dann erneut prüfen (kein fester Sleep, damit der
            # Stop schnell greift).
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        pass
    finally:
        # Turn-Ende: Status aufräumen (online + Custom-Status löschen).
        try:
            await adapter.presence_mgr.clear_custom_status_message(force=True)
            await adapter.presence_mgr.set_presence_status("online")
        except Exception as exc:
            logger.debug("Deck: Speed-Heartbeat-Cleanup fehlgeschlagen: %s", exc)
        card_id = adapter._card_id_from_target(str(chat_id or ""))
        if card_id:
            adapter._active_turns.discard(card_id)


async def fetch_speed(adapter) -> Optional[Dict[str, Any]]:
    """Liest die Live-Geschwindigkeit vom Ollama-Sidecar (über NPM-/speed).

    Kurzer Timeout + best-effort: ein toter Sidecar darf nie den Turn
    verlangsamen. Ergebnis wird kurz gecacht, damit die häufigen
    Typing-Heartbeats den Sidecar nicht fluten.
    """
    if not adapter.runtime.speed_url:
        return None
    try:
        if adapter._speed_session is None or adapter._speed_session.closed:
            adapter._speed_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=0.4)
            )
        async with adapter._speed_session.get(
            adapter.runtime.speed_url, timeout=aiohttp.ClientTimeout(total=0.4)
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            adapter._last_speed = data if isinstance(data, dict) else None
            return adapter._last_speed
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


def speed_suffix(speed: Dict[str, Any]) -> str:
    """Formatiert den Sidecar-JSON in einen kompakten Geschwindigkeits-Text.

    Generate-Phase zeigt die Generation-Geschwindigkeit (``tg``), Prompt-Phase
    zeigt den Kontext-Fortschritt UND die Prompt-Geschwindigkeit (``prompt_tps``).
    """
    phase = str(speed.get("phase", "idle"))
    if phase == "generate":
        tg = speed.get("tg")
        if isinstance(tg, (int, float)) and tg > 0:
            return f"{tg:.1f} t/s"
        return ""
    if phase == "prompt":
        progress = speed.get("prompt_progress")
        prompt_tps = speed.get("prompt_tps")
        parts: List[str] = []
        if isinstance(progress, (int, float)) and progress > 0:
            pct = int(progress * 100)
            parts.append(f"Kontext {pct}%")
        if isinstance(prompt_tps, (int, float)) and prompt_tps > 0:
            parts.append(f"⏱ {prompt_tps:.0f} t/s")
        return " · ".join(parts)
    return ""


async def card_status_label(adapter, chat_id: str) -> str:
    """Liefert ein kompaktes Karten-Label ('Karte 116 · Titel') für den Status.

    Der Titel wird pro Karte einmalig (best-effort) aufgelöst und gecacht,
    damit die häufigen Status-Updates keinen API-Spam erzeugen.
    """
    card_id = adapter._card_id_from_target(str(chat_id or ""))
    if not card_id or card_id == str(chat_id or ""):
        return ""
    cache = getattr(adapter, "_status_card_title_cache", None)
    if cache is None:
        cache = {}
        setattr(adapter, "_status_card_title_cache", cache)
    title = cache.get(card_id)
    if title is None:
        title = ""
        try:
            board_id, stack_id = await adapter._locate_card(card_id)
            card = await adapter.client.get_card(board_id, stack_id, card_id)
            if card and card.get("title"):
                title = str(card["title"]).strip()
        except Exception:
            title = ""
        # Leeren Titel als '' cachen (nicht erneut versuchen).
        cache[card_id] = title
    label = f"Karte {card_id}"
    if title:
        short = title if len(title) <= 26 else title[:25].rstrip() + "…"
        label = f"Karte {card_id} · {short}"
    return label


def map_progress_status(status_key: str, content: str) -> tuple:
    """Übersetzt Gateway-Status-Signal in (Text, Emoji) für den User-Status.

    Gleiche Semantik wie das Talk-Plugin: context laden, thinking,
    generating, tool-Ausführung.
    """
    normalized_key = str(status_key or "").strip().lower()
    normalized_content = " ".join(str(content or "").split()).strip()
    normalized_lower = normalized_content.lower()
    if "context" in normalized_lower:
        return "Liest Kontext", "📖"
    if normalized_key == "_thinking" or normalized_lower.startswith("💬 "):
        return "Denkt nach", "🤔"
    if normalized_key in ("_generating", "_responding", "llm.generating"):
        return "Antwortet", "✍️"
    if normalized_key.startswith("tool.") or "tool" in normalized_key:
        return "Fuehrt Werkzeuge aus", "🛠️"
    if normalized_content:
        return (normalized_content[:80], "💬")
    return None, None
