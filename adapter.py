from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

try:
    from .client import NextcloudDeckClient, NextcloudDeckError
    from .identity import DeckIdentityResolver
    from .outbound import categorize_gateway_message
    from .presence import DeckPresenceManager
    from .state import DeckCardSnapshot, DeckStateManager
    from .workflow import (
        LABEL_PREFIX_PHASE,
        PHASE_PLAN,
        PHASE_EXECUTE,
        APPROVAL_APPROVED,
        APPROVAL_REQUIRED,
        build_capabilities_prompt,
        canonical_label_key,
        check_agent_label_gate,
        check_agent_status_gate,
        configure_friendly_labels,
        friendly_label_title,
        has_workflow_label_change,
        TEMPLATE_CARD_TITLE,
        template_description,
        AGENT_WORKSPACE_MARKER,
        split_agent_workspace,
        DEFAULT_TEMPLATE_LANGUAGE,
        TEMPLATE_LANGUAGES,
        missing_template_sections,
        FRIENDLY_LABELS,
        STATUS_REVIEW,
        STATUS_RUNNING,
        STATUS_BLOCKED,
        compile_destructive_patterns,
        current_deck_context,
        current_deck_action_count,
        check_destructive_gate,
        DeckWorkflowContext,
        DestructiveToolBlocked,
        analyze_board_suitability,
        extract_hermes_labels,
        is_backlog_stack,
        parse_subtasks,
    )
except ImportError:  # direct test/import
    from client import NextcloudDeckClient, NextcloudDeckError
    from identity import DeckIdentityResolver
    from outbound import categorize_gateway_message
    from presence import DeckPresenceManager
    from state import DeckCardSnapshot, DeckStateManager
    from workflow import (
        LABEL_PREFIX_PHASE,
        PHASE_PLAN,
        PHASE_EXECUTE,
        APPROVAL_APPROVED,
        APPROVAL_REQUIRED,
        build_capabilities_prompt,
        canonical_label_key,
        check_agent_label_gate,
        check_agent_status_gate,
        configure_friendly_labels,
        friendly_label_title,
        has_workflow_label_change,
        TEMPLATE_CARD_TITLE,
        template_description,
        AGENT_WORKSPACE_MARKER,
        split_agent_workspace,
        DEFAULT_TEMPLATE_LANGUAGE,
        TEMPLATE_LANGUAGES,
        missing_template_sections,
        FRIENDLY_LABELS,
        STATUS_REVIEW,
        STATUS_RUNNING,
        STATUS_BLOCKED,
        compile_destructive_patterns,
        current_deck_context,
        current_deck_action_count,
        check_destructive_gate,
        DeckWorkflowContext,
        DestructiveToolBlocked,
        analyze_board_suitability,
        extract_hermes_labels,
        is_backlog_stack,
        parse_subtasks,
    )

try:
    from gateway.config import Platform, PlatformConfig  # type: ignore
    from gateway.platforms.base import (  # type: ignore
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )
except Exception:  # local test fallback
    Platform = lambda name: name  # type: ignore
    PlatformConfig = Any  # type: ignore

    class MessageType:
        TEXT = "text"

    @dataclass
    class SendResult:
        success: bool
        message_id: Optional[str] = None
        error: Optional[str] = None

    @dataclass
    class MessageEvent:
        text: str
        message_type: str
        source: Any
        raw_message: Dict[str, Any]
        message_id: Optional[str] = None
        user_id: Optional[str] = None
        user_name: Optional[str] = None

    class BasePlatformAdapter:
        def __init__(self, config: Any, platform: str = "deck") -> None:
            self.config = config
            self.platform = platform

        def build_source(self, **kwargs: Any) -> Dict[str, Any]:
            return kwargs

        async def handle_message(self, event: MessageEvent) -> None:
            return None

        def _mark_disconnected(self) -> None:
            return None


logger = logging.getLogger(__name__)

# Modul-Global: Live-Adapter-Referenz für den Deck-Card-Action-Tool-Handler.
# Der Handler läuft im Tool-Worker-Thread, muss aber auf die Adapter-Instanz
# zugreifen, die auf dem Gateway-Loop lebt. Wird im __init__ gesetzt.
_LIVE_ADAPTER_REF: Optional["NextcloudDeckPlatform"] = None

# Ruhe-Schwelle (Sekunden) für die Backlog-Format-Korrektur: Karten, deren
# ``lastModified`` jünger als dieser Wert ist, werden als "aktiv bearbeitet"
# betrachtet und NICHT angefasst — so wird eine laufende menschliche
# Bearbeitung nicht durch eine automatische Template-Ergänzung gestört.
BACKLOG_FORMAT_QUIET_SECONDS = 300.0


@dataclass
class DeckRuntimeConfig:
    base_url: str
    username: str
    app_password: str
    hermes_user_id: str
    poll_interval_seconds: float
    boards: Dict[str, Dict[str, Any]]
    home_channel: Optional[str] = None
    bot_aliases: tuple[str, ...] = ()
    destructive_tool_patterns: List[str] = None  # type: ignore[assignment]
    backlog_format_quiet_seconds: float = BACKLOG_FORMAT_QUIET_SECONDS
    max_in_progress: int = 0
    template_language: str = DEFAULT_TEMPLATE_LANGUAGE
    speed_enabled: bool = False
    speed_url: str = ""


def _load_dotenv_fallback() -> Dict[str, str]:
    """Liest ~/.hermes/.env als Fallback ein (einmalig gecached).

    Der Gateway-Prozess lädt die .env via dotenv in os.environ — aber
    Status-Konsumenten (Dashboard, `hermes status`) sind separate Prozesse.
    Damit is_connected()/check_is_connected() dort dieselbe Antwort liefern
    wie zur Gateway-Startzeit, wird die .env direkt gelesen.
    """
    global _DOTENV_CACHE
    if _DOTENV_CACHE is not None:
        return _DOTENV_CACHE
    values: Dict[str, str] = {}
    try:
        env_path = Path.home() / ".hermes" / ".env"
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, raw = line.partition("=")
                key = key.strip()
                raw = raw.strip().strip('"').strip("'")
                if key and raw:
                    values.setdefault(key, raw)
    except Exception:
        pass
    _DOTENV_CACHE = values
    return values


_DOTENV_CACHE: Optional[Dict[str, str]] = None


def _env(name: str, *fallbacks: str) -> str:
    dotenv = _load_dotenv_fallback()
    for key in (name, *fallbacks):
        value = os.getenv(key, "").strip()
        if value:
            return value
        value = dotenv.get(key, "").strip()
        if value:
            return value
    return ""


def _env_bool(name: str, *fallbacks: str, default: bool = False) -> bool:
    value = _env(name, *fallbacks).strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on", "y", "ja", "an")


def _build_runtime_config(config: PlatformConfig) -> DeckRuntimeConfig:
    extra = getattr(config, "extra", {}) or {}
    base_url = str(
        extra.get("base_url")
        or extra.get("deck_base_url")
        or _env("NEXTCLOUD_DECK_BASE_URL", "NEXTCLOUD_BASE_URL")
    ).strip().rstrip("/")
    if base_url.endswith("/index.php"):
        base_url = base_url[:-10]

    username = str(
        extra.get("username")
        or extra.get("deck_username")
        or _env("NEXTCLOUD_DECK_USERNAME", "NEXTCLOUD_USERNAME")
    ).strip()

    app_password = str(
        extra.get("app_password")
        or extra.get("deck_app_password")
        or getattr(config, "token", "")
        or _env("NEXTCLOUD_DECK_APP_PASSWORD", "NEXTCLOUD_APP_PASSWORD")
    ).strip()

    hermes_user_id = str(
        extra.get("hermes_user_id")
        or _env("NEXTCLOUD_DECK_HERMES_USER_ID", "NEXTCLOUD_HERMES_USER_ID")
        or username
    ).strip()

    try:
        poll = float(
            extra.get("poll_interval_seconds")
            or extra.get("poll_interval")
            or _env("NEXTCLOUD_DECK_POLL_INTERVAL_SECONDS", "NEXTCLOUD_DECK_POLL_INTERVAL")
            or 30
        )
    except (TypeError, ValueError):
        poll = 30.0

    # Ruhe-Schwelle für die Backlog-Format-Korrektur: Karten, deren
    # ``lastModified`` jünger als dieser Wert ist, gelten als "aktiv bearbeitet"
    # und werden NICHT angefasst. ``0`` deaktiviert die Prüfung komplett
    # (immer korrigieren, keine Wartezeit). Negativ-Werte werden wie 0 behandelt.
    try:
        backlog_format_quiet_seconds = float(
            extra.get("backlog_format_quiet_seconds")
            or _env(
                "NEXTCLOUD_DECK_BACKLOG_FORMAT_QUIET_SECONDS",
                "NEXTCLOUD_DECK_BACKLOG_FORMAT_QUIET_SECONDS",
            )
            or BACKLOG_FORMAT_QUIET_SECONDS
        )
    except (TypeError, ValueError):
        backlog_format_quiet_seconds = BACKLOG_FORMAT_QUIET_SECONDS
    if backlog_format_quiet_seconds < 0:
        backlog_format_quiet_seconds = 0.0

    # WIP-Limit: maximale Anzahl gleichzeitig "in Arbeit" befindlicher Karten
    # (todo/ready/running). ``0`` = kein Limit (Default). ``1`` erzwingt
    # strikt sequenzielle Abarbeitung — der Agent startet keine neue Karte,
    # solange eine bereits in einer Arbeits-Spalte liegt (Multitasking-Schutz).
    try:
        max_in_progress = int(
            extra.get("max_in_progress")
            or _env("NEXTCLOUD_DECK_MAX_IN_PROGRESS", "NEXTCLOUD_DECK_MAX_IN_PROGRESS")
            or 0
        )
    except (TypeError, ValueError):
        max_in_progress = 0
    if max_in_progress < 0:
        max_in_progress = 0

    # Template-Sprache (de/en). Bestimmt die Sprache der automatisch angelegten
    # Vorlagenkarte und der Format-Gerüste.
    template_language = str(
        extra.get("template_language")
        or _env("NEXTCLOUD_DECK_TEMPLATE_LANGUAGE", "NEXTCLOUD_DECK_TEMPLATE_LANGUAGE")
        or DEFAULT_TEMPLATE_LANGUAGE
    ).strip().lower()
    if template_language not in TEMPLATE_LANGUAGES:
        template_language = DEFAULT_TEMPLATE_LANGUAGE

    # Live-Token-Geschwindigkeit (t/s) im User-Status. Quelle ist der Ollama
    # Sidecar (ct101) über NPM. Standardmäßig AUS; URL ist die NPM-/speed-Location.
    speed_enabled = bool(
        extra.get("speed_enabled")
        or _env_bool("NEXTCLOUD_DECK_SPEED_ENABLED", default=False)
    )
    speed_url = str(
        extra.get("speed_url")
        or _env("NEXTCLOUD_DECK_SPEED_URL")
    ).strip().rstrip("/")

    boards: Dict[str, Dict[str, Any]] = {}
    raw_boards = extra.get("boards") or []
    if isinstance(raw_boards, list):
        for item in raw_boards:
            if not isinstance(item, dict):
                continue
            board_id = str(item.get("board_id") or item.get("id") or "").strip()
            if board_id:
                boards[board_id] = dict(item)

    # Deck hat keinen eigenen Home-Channel im Talk-Sinne. Der globale
    # NEXTCLOUD_HOME_CHANNEL (Talk-Raum) darf NICHT als Deck-Home-Channel
    # interpretiert werden — sonst erscheint beim Agent die irreführende
    # "No home channel"-Notice bzw. Cron-Ergebnisse würden in einen Talk-Raum
    # geleitet.
    #
    # WICHTIG: Das Gateway prüft die Home-Channel-Notice über das Standard-
    # Schema ``<PLATFORM>_HOME_CHANNEL`` = ``DECK_HOME_CHANNEL`` (nicht
    # ``NEXTCLOUD_DECK_HOME_CHANNEL``). Beide werden gelesen. Der besondere
    # Wert "log" leitet Cron-/Cross-Platform-Zustellung in ein Logfile statt
    # auf eine Karte.
    home_channel = str(
        extra.get("home_channel")
        or _env("NEXTCLOUD_DECK_HOME_CHANNEL", "DECK_HOME_CHANNEL")
    ).strip() or None

    raw_aliases = (
        extra.get("bot_aliases")
        or _env("NEXTCLOUD_DECK_BOT_ALIASES", "NEXTCLOUD_BOT_ALIASES")
    )
    bot_aliases: tuple[str, ...] = ()
    if isinstance(raw_aliases, str):
        bot_aliases = tuple(a.strip().lower() for a in raw_aliases.split(",") if a.strip())
    elif isinstance(raw_aliases, list):
        bot_aliases = tuple(str(a).strip().lower() for a in raw_aliases if str(a).strip())

    raw_destructive = extra.get("destructive_tool_patterns")
    destructive_patterns: List[str] = []
    if isinstance(raw_destructive, str):
        destructive_patterns = [p.strip() for p in raw_destructive.split(",") if p.strip()]
    elif isinstance(raw_destructive, list):
        destructive_patterns = [str(p).strip() for p in raw_destructive if str(p).strip()]

    # Friendly-Label-Mapping aus der Config (platforms.deck.extra.label_mapping).
    # Fehlt es, gilt das Default-Mapping in workflow.py. Wird global angewendet,
    # damit alle Adapter-Instanzen (und der Prompt-Bau) dasselbe Mapping nutzen.
    label_mapping = extra.get("label_mapping")
    if isinstance(label_mapping, dict) and label_mapping:
        try:
            configure_friendly_labels(label_mapping)
        except Exception as exc:
            logger.warning("Deck: label_mapping aus Config konnte nicht angewendet werden: %s", exc)

    return DeckRuntimeConfig(
        base_url=base_url,
        username=username,
        app_password=app_password,
        hermes_user_id=hermes_user_id,
        poll_interval_seconds=max(5.0, poll),
        boards=boards,
        home_channel=home_channel,
        bot_aliases=bot_aliases,
        destructive_tool_patterns=destructive_patterns,
        backlog_format_quiet_seconds=backlog_format_quiet_seconds,
        max_in_progress=max_in_progress,
        template_language=template_language,
        speed_enabled=speed_enabled,
        speed_url=speed_url,
    )


class NextcloudDeckPlatform(BasePlatformAdapter):
    """Polling platform adapter for explicitly configured Nextcloud Deck boards."""

    @staticmethod
    def _resolve_platform():
        """Platform-Member robust auflösen (auch ohne Plugin-Discovery).

        ``Platform("deck")`` erzeugt nur dann dynamisch ein Pseudo-Member,
        wenn das Plugin gebündelt ist oder die Registry den Namen kennt.
        Sonst ValueError — dann Fallback auf ein eingebautes Member.
        """
        try:
            return Platform("deck")
        except ValueError:
            try:
                from gateway.platform_registry import platform_registry

                if platform_registry.is_registered("deck"):
                    return Platform("deck")
            except Exception:
                pass
            return Platform("matrix")

    def __init__(self, config: PlatformConfig):
        super().__init__(config, self._resolve_platform())
        global _LIVE_ADAPTER_REF
        _LIVE_ADAPTER_REF = self
        self.runtime = _build_runtime_config(config)
        self.client = NextcloudDeckClient(
            self.runtime.base_url,
            self.runtime.username,
            self.runtime.app_password,
        )
        self.identity = DeckIdentityResolver(
            self.runtime.hermes_user_id,
            client=self.client,
            bot_aliases=self.runtime.bot_aliases,
        )
        self.state = DeckStateManager()
        self.presence_mgr = DeckPresenceManager(self.client)
        self.destructive_patterns = compile_destructive_patterns(
            self.runtime.destructive_tool_patterns
        )
        self._speed_session: Optional[aiohttp.ClientSession] = None
        self._last_speed: Optional[Dict[str, Any]] = None
        # In-Flight-Tracking: Karten, deren Turn gerade aktiv läuft (via
        # send_typing/stop_typing). Wird in die WIP-Zählung einbezogen, damit
        # das WIP-Limit auch BEREITS LAUFENDE Turns berücksichtigt — nicht nur
        # den Stack-Zustand. Sonst laufen mehrere freigegebene Karten parallel
        # (jede hat einen eigenen Session-Key, das Gateway serialisiert nur
        # pro Karte, nicht über Karten hinweg).
        self._active_turns: set[str] = set()
        self._stop_event = asyncio.Event()
        self._polling_task: Optional[asyncio.Task[None]] = None
        self._connected = False
        self._gateway_loop = None

    @property
    def is_connected(self) -> bool:
        return (
            self._connected
            and not self._stop_event.is_set()
            and self._polling_task is not None
            and not self._polling_task.done()
        )

    @property
    def home_channel(self) -> Optional[str]:
        """Default-Ziel für Cron/scheduled Zustellung."""
        return self.runtime.home_channel

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        del is_reconnect
        self._stop_event.clear()
        # Gateway-Loop für Cross-Thread-Dispatch des Card-Action-Tools merken.
        try:
            self._gateway_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._gateway_loop = None
        await self.client.ensure_session()
        await self.client.get_boards()
        self._connected = True
        if self._polling_task is None or self._polling_task.done():
            self._polling_task = asyncio.create_task(self._polling_loop())
        # Board-Eignung prüfen und bei Problemen laut loggen (kein Abbruch —
        # ein ungeeignetes Board darf das Gateway nicht blockieren).
        await self.check_board_suitability()
        # Presence: Bot-User als online melden + ggf. altes Custom-Status-Reset.
        try:
            await self.presence_mgr.set_presence_status("online")
        except Exception as exc:
            logger.debug("Deck: Presence online setzen fehlgeschlagen: %s", exc)
        return True

    async def _call_on_gateway_loop(self, coro_factory):
        """Führt eine Coroutine auf dem Gateway-Loop aus (Cross-Thread-sicher).

        Das Card-Action-Tool läuft in einem Worker-Thread/-Loop; die
        aiohttp-Session des Adapters ist aber an den Gateway-Loop gebunden.
        """
        loop = getattr(self, "_gateway_loop", None)
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if loop is None or (current is not None and current is loop):
            return await coro_factory()
        if loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
            return await asyncio.wrap_future(fut)
        return await coro_factory()

    async def check_board_suitability(self) -> List[Any]:
        """Prüft alle konfigurierten Boards auf Workflow-Eignung und loggt Reports.

        Rückgabe: Liste der BoardSuitabilityResult-Objekte.
        """
        results: List[Any] = []
        try:
            boards = await self.client.get_boards()
        except NextcloudDeckError as exc:
            logger.warning("Deck: Board-Eignungsprüfung übersprungen (Boards nicht ladbar): %s", exc)
            return results

        for board in boards if isinstance(boards, list) else []:
            board_id = str(board.get("id") or "").strip()
            if not board_id or (self.runtime.boards and board_id not in self.runtime.boards):
                continue
            board_title = str(board.get("title") or board_id)
            try:
                stacks = await self.client.get_stacks(board_id)
            except NextcloudDeckError as exc:
                logger.warning("Deck: Spalten von Board %s nicht ladbar: %s", board_id, exc)
                continue

            config = self._configured_board(board_id)
            result = analyze_board_suitability(board_id, board_title, stacks, config)
            results.append(result)
            if result.is_suitable:
                logger.info("Deck Board-Eignung OK:\n%s", result.format_report())
            else:
                logger.warning("Deck Board NICHT geeignet (fehlende Pflicht-Spalten):\n%s", result.format_report())
        return results

    async def setup_test_card(
        self,
        board_id: str,
        title: str,
        description: str = "",
        stack_title: str = "Todo",
        label_titles: Optional[List[str]] = None,
        assignee: Optional[str] = None,
    ) -> Optional[str]:
        """Erstellt eine Test-Karte in einem Schritt (Karte + Labels + Assignee).

        Nützlich für automatisierte E2E-Tests: legt die Karte in der Spalte
        ``stack_title`` an, weist die Labels per Titel zu (erstellt fehlende
        Labels automatisch) und setzt optional den Assignee.

        Rückgabe: die neue Karten-ID, oder None bei Fehler.
        """
        label_titles = label_titles or []
        try:
            stacks = await self.client.get_stacks(board_id)
        except NextcloudDeckError as exc:
            logger.warning("Deck: setup_test_card: Spalten nicht ladbar: %s", exc)
            return None

        stack_id = None
        for s in stacks if isinstance(stacks, list) else []:
            if str(s.get("title") or "").strip().casefold() == stack_title.strip().casefold():
                stack_id = str(s.get("id") or "").strip()
                break
        if not stack_id:
            logger.warning("Deck: setup_test_card: Spalte '%s' nicht gefunden", stack_title)
            return None

        try:
            card = await self.client.create_card(
                board_id, stack_id, title=title, description=description
            )
        except NextcloudDeckError as exc:
            logger.warning("Deck: setup_test_card: Karte konnte nicht erstellt werden: %s", exc)
            return None
        if not card or not card.get("id"):
            return None
        card_id = str(card["id"])

        # Labels per Titel zuweisen (ggf. anlegen)
        for label_title in label_titles:
            try:
                await self._apply_label_to_card(card_id, str(label_title))
            except Exception as exc:
                logger.warning("Deck: setup_test_card: Label '%s' nicht gesetzt: %s", label_title, exc)

        # Assignee setzen
        if assignee:
            try:
                await self._assign_user_to_card(card_id, str(assignee))
            except Exception as exc:
                logger.warning("Deck: setup_test_card: Assignee '%s' nicht gesetzt: %s", assignee, exc)

        logger.info("Deck: setup_test_card: Karte %s ('%s') erstellt in '%s'", card_id, title, stack_title)
        return card_id

    async def disconnect(self) -> None:
        self._stop_event.set()
        self._connected = False
        task = self._polling_task
        self._polling_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # Presence: offline setzen + Custom-Status löschen (best-effort).
        try:
            await self.presence_mgr.set_presence_status("offline")
            await self.presence_mgr.clear_custom_status_message(force=True)
        except Exception as exc:
            logger.debug("Deck: Presence offline setzen fehlgeschlagen: %s", exc)
        if self._speed_session and not self._speed_session.closed:
            await self._speed_session.close()
        self._speed_session = None
        await self.client.close()
        self._mark_disconnected()

    async def get_chat_info(self, target: str) -> Dict[str, Any]:
        """Returns metadata for the target Deck card session."""
        card_id = self._card_id_from_target(target)
        return {
            "id": card_id,
            "target": target,
            "type": "deck_card",
        }

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        # Basis-Signatur: send(chat_id, content, reply_to, metadata). Der
        # Gateway ruft mit chat_id=... auf; der alte Positional-Name 'target'
        # bleibt als chat_id kompatibel (alt: send(target, text)).
        target = chat_id
        del reply_to
        card_id = self._card_id_from_target(target)
        metadata = metadata or {}

        # Log-Sink: Ist der Home-Channel auf "log" gesetzt (kein Karten-Target),
        # landet Cron-/Cross-Platform-Zustellung in einem Logfile statt auf einer
        # Karte. Kein Deck-Kommentar, keine Fehlermeldung an den Agent.
        if self.runtime.home_channel == "log" and target == "log":
            await self._write_home_log(content)
            return SendResult(success=True)

        # Loop-Prävention: interne Gateway-Meldungen nicht als Deck-Kommentar
        # spiegeln. Lifecycle → still verwerfen (Deck hat kein Presence-Konzept),
        # suppress → still verwerfen, error → als Kommentar mit Fehler-Präfix.
        category, details = categorize_gateway_message(content)
        if category == "suppress":
            logger.debug("Deck: suppressed internal gateway message (%s)", details.get("state", "noise"))
            return SendResult(success=True)
        if category == "lifecycle":
            logger.info("Deck: gateway lifecycle notice suppressed (%s)", details.get("state"))
            return SendResult(success=True)
        if category == "error":
            content = f"🚫 **Fehler**\n\n{content}"

        # Karten-Aktionen via metadata (Beschreibung-Update, Status-Move, Labels, Assignee)
        new_description = metadata.get("description") or metadata.get("new_description")
        target_status = metadata.get("target_status")
        assign_labels = metadata.get("assign_labels") or metadata.get("assign_label")
        remove_labels = metadata.get("remove_labels") or metadata.get("remove_label")
        assign_user = metadata.get("assign_user") or metadata.get("assign_users")
        unassign_user = metadata.get("unassign_user") or metadata.get("unassign_users")

        card_action_taken = False

        # Deterministische Workflow-Durchsetzung (b): Schreibt der Agent eine
        # neue Beschreibung (Plan/Ergebnis), OHNE target_status anzugeben, wird
        # die Karte automatisch nach 'review' verschoben. Begründung: Der
        # Agent vergisst erfahrungsgemäß target_status im selben Call — der
        # Adapter erzwingt damit den Vertrags-Übergang (Plan fertig → Review)
        # zuverlässig, ohne auf das LLM-Verhalten zu vertrauen.
        auto_move_reason = None
        if new_description and not target_status:
            auto_move_reason = await self._auto_move_review_check(card_id)
            if auto_move_reason:
                target_status = STATUS_REVIEW

        if target_status:
            moved, err = await self._move_card_to_status(card_id, str(target_status))
            if not moved:
                # Bei einem Gate-Fehler posten wir den Grund als Kommentar, damit der Agent/User weiß, was passiert ist
                if err and "Gate" in err:
                    try:
                        await self.client.add_comment(card_id, f"⚠️ **Workflow-Gate:** {err}")
                    except Exception:
                        pass
                return SendResult(success=False, error=err or f"Could not move card {card_id} to status '{target_status}'")
            card_action_taken = True

        if auto_move_reason:
            logger.info(
                "Deck: Auto-Move Karte %s nach 'review' (%s) — Agent hat target_status weggelassen.",
                card_id,
                auto_move_reason,
            )

        if new_description:
            updated = await self._update_card_description(card_id, str(new_description))
            if not updated:
                return SendResult(success=False, error=f"Could not update description of card {card_id}")
            card_action_taken = True

        if assign_labels:
            labels_list = [assign_labels] if isinstance(assign_labels, str) else list(assign_labels)
            for lbl in labels_list:
                applied, err = await self._apply_label_to_card(card_id, str(lbl))
                if not applied and err:
                    logger.warning("Deck: Label '%s' konnte nicht gesetzt werden: %s", lbl, err)
                elif applied:
                    card_action_taken = True

        if remove_labels:
            labels_list = [remove_labels] if isinstance(remove_labels, str) else list(remove_labels)
            for lbl in labels_list:
                removed = await self._remove_label_from_card(card_id, str(lbl))
                if removed:
                    card_action_taken = True

        if assign_user:
            users_list = [assign_user] if isinstance(assign_user, str) else list(assign_user)
            for usr in users_list:
                assigned = await self._assign_user_to_card(card_id, str(usr))
                if assigned:
                    card_action_taken = True

        if unassign_user:
            users_list = [unassign_user] if isinstance(unassign_user, str) else list(unassign_user)
            for usr in users_list:
                unassigned = await self._unassign_user_from_card(card_id, str(usr))
                if unassigned:
                    card_action_taken = True

        if content:
            try:
                result = await self.client.add_comment(card_id, content)
            except NextcloudDeckError as exc:
                logger.warning("Deck comment write failed for card %s: %s", card_id, exc)
                return SendResult(success=False, error=str(exc))
            return SendResult(
                success=result is not None,
                message_id=str(result.get("id")) if isinstance(result, dict) and result.get("id") else None,
                error=None if result is not None else "Deck did not return a comment",
            )

        # Nur Karten-Aktionen, kein Kommentar-Inhalt
        if card_action_taken:
            return SendResult(success=True)
        return SendResult(success=False, error="Empty message and no card action metadata")

    async def _write_home_log(self, content: str) -> None:
        """Schreibt eine Home-Channel-Meldung in ein dediziertes Logfile.

        Wird genutzt, wenn ``home_channel: "log"`` konfiguriert ist — Cron-
        Ergebnisse und Cross-Platform-Meldungen werden dann nicht auf eine Karte
        geschrieben, sondern nur protokolliert.
        """
        log_path = Path.home() / ".hermes" / "logs" / "deck-home.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            ts = __import__("datetime").datetime.now().isoformat(timespec="seconds")
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"[{ts}] {content}\n\n")
            logger.info("Deck: Home-Channel-Meldung in %s protokolliert", log_path)
        except Exception as exc:
            logger.warning("Deck: Konnte Home-Log nicht schreiben (%s): %s", log_path, exc)

    async def _auto_move_review_check(self, card_id: str) -> Optional[str]:
        """Prüft, ob ein Auto-Move nach 'review' gerechtfertigt ist (b-Fix).

        Bedingungen: Die Karte ist auffindbar, befindet sich in einer
        Vor-Stufe (todo/ready/running) und der Agent liefert eine
        Beschreibung ohne target_status. Gibt die Begründung zurück (oder
        None, wenn kein Auto-Move erfolgen soll — z. B. Karte schon in
        review/blocked/done, oder Gate würde den Move blockieren).
        """
        try:
            location = await self._locate_card(card_id)
            if location is None:
                return None
            board_id, stack_id = location
            current_card = await self.client.get_card(board_id, stack_id, card_id)
            if not current_card:
                return None

            # Aktueller Stack-Titel → Status-Key normalisieren
            stacks = await self.client.get_stacks(board_id)
            current_status = None
            for st in stacks or []:
                if str(st.get("id")) == str(stack_id):
                    title = str(st.get("title") or "").strip().lower()
                    # Kanonische Status-Keys: backlog|triage|todo|ready|running|review|blocked|done
                    aliases = {
                        "to do": "todo", "in progress": "running",
                        "in bearbeitung": "running", "arbeit": "running",
                        "prüfung": "review", "pruefung": "review",
                        "erledigt": "done", "fertig": "done",
                    }
                    current_status = aliases.get(title, title)
                    break
            if current_status is None:
                return None

            # Nur aus Vor-Stufen nach review schieben
            if current_status not in {"todo", "ready", "running"}:
                return None

            # Gate-Prüfung (gleiche Regeln wie beim expliziten Move)
            hermes_labels = extract_hermes_labels(current_card)
            phase = hermes_labels.get("phase") or PHASE_PLAN
            approval = hermes_labels.get("approval")
            task_type = hermes_labels.get("type")
            risk = hermes_labels.get("risk")
            allowed, reason = check_agent_status_gate(
                STATUS_REVIEW, phase, approval, task_type, risk
            )
            if not allowed:
                logger.info(
                    "Deck: Auto-Move nach 'review' für Karte %s durch Gate blockiert: %s",
                    card_id,
                    reason,
                )
                return None
            return f"Beschreibung geschrieben in '{current_status}' ohne target_status"
        except Exception as exc:
            logger.warning("Deck: Auto-Move-Check für Karte %s fehlgeschlagen: %s", card_id, exc)
            return None

    async def _update_card_description(self, card_id: str, description: str) -> bool:
        """Aktualisiert den AGENT-WORKSPACE-Teil der Karten-Beschreibung.

        Der Agent sendet nur den Inhalt seiner Agent-Sektionen (die ``##``-
        Blöcke unterhalb des ``# Agent Workspace``-Markers). Dieser Handler
        ersetzt ausschließlich den Teil AB dem Marker — der Mensch-Teil
        (Objective/Context/Constraints/Acceptance Criteria) bleibt byte-genau
        erhalten. Ist noch kein Marker vorhanden (z. B. alte Karte), wird er
        zusammen mit dem Agent-Inhalt angehängt.

        Truncation-Absicherung: Ist der neue Agent-Teil ein bloßes Präfix des
        bestehenden Agent-Teils (Abruch mitten im Schreiben), wird er verworfen.
        """
        location = await self._locate_card(card_id)
        if location is None:
            return False
        board_id, stack_id = location

        new_agent = (description or "").strip()

        try:
            current_card = await self.client.get_card(board_id, stack_id, card_id)
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
            # Kein Agent-Workspace-Marker vorhanden (Alt-Karte im alten Template):
            # Marker explizit einfügen, damit Folge-Updates den Agent-Teil
            # sauber finden und ersetzen können statt erneut anzuhängen.
            new_description = f"{human_part.rstrip()}\n\n{marker_line}\n\n{new_agent}".rstrip()
        elif human_part:
            # human_part enthält bereits die Marker-Zeile (aus split_agent_workspace).
            new_description = f"{human_part.rstrip()}\n\n{new_agent}".rstrip()
        else:
            new_description = f"{marker_line}\n\n{new_agent}".rstrip()

        if not new_description:
            return False

        try:
            result = await self.client.update_card(board_id, stack_id, card_id, description=new_description)
            return result is not None
        except NextcloudDeckError as exc:
            logger.warning("Deck description update failed for card %s: %s", card_id, exc)
            return False

    async def _move_card_to_status(self, card_id: str, status_key: str) -> tuple[bool, Optional[str]]:
        """Verschiebt die Karte in den Ziel-Stack mit Berücksichtigung von Workflow-Gates."""
        location = await self._locate_card(card_id)
        if location is None:
            return False, f"Card {card_id} could not be located"
        board_id, stack_id = location

        # Karte laden, um aktuelle Phase / Approval-Status zu prüfen
        current_card = await self.client.get_card(board_id, stack_id, card_id)
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
        target_stack_id = await self._resolve_target_stack_id(board_id, status_key)
        if not target_stack_id:
            logger.warning("Deck: kein Ziel-Stack für Status '%s' in Board %s konfiguriert", status_key, board_id)
            return False, f"No target stack found for status '{status_key}'"

        try:
            result = await self.client.move_card(board_id, stack_id, card_id, target_stack_id)
            if result is None:
                return False, f"Move of card {card_id} to stack {target_stack_id} failed (empty response)"
            # Verifikation: Die Deck-API kann Moves still fehlschlagen lassen
            # (HTTP 200, aber keine Änderung). Daher den tatsächlichen Stack
            # nach dem Move prüfen (curl-verifiziert 2026-09-09).
            verify_location = await self._locate_card(card_id)
            if verify_location is not None:
                _, actual_stack_id = verify_location
                if str(actual_stack_id) != str(target_stack_id):
                    logger.warning(
                        "Deck: Move-Verifikation fehlgeschlagen für Karte %s: erwartet Stack %s, tatsächlich %s",
                        card_id, target_stack_id, actual_stack_id,
                    )
                    return False, f"Card {card_id} did not move to stack {target_stack_id} (silent API failure)"
            # Konzept 2 (I3): Ein Move nach 'review' bedeutet "wartet auf
            # (erneute) Freigabe". Deterministsch den Alt-Zustand bereinigen:
            # phase:execute entfernen (Karte ist nicht mehr aktiv in Umsetzung)
            # und approval:approved entfernen + approval:required setzen — die
            # alte Freigabe ist verbraucht, es wird eine neue angefordert.
            # Dadurch trägt eine Review-Karte nie mehr "🚀 In Umsetzung" oder
            # "✔️ Freigabe erteilt" zusätzlich zum "⌛ Freigabe nötig".
            status_norm = str(status_key or "").strip().lower()
            if status_norm == STATUS_REVIEW:
                try:
                    await self._remove_label_from_card(card_id, f"{LABEL_PREFIX_PHASE}{PHASE_EXECUTE}")
                    await self._apply_label_to_card(card_id, f"approval:{APPROVAL_REQUIRED}")
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

    # ------------------------------------------------------------------
    # Label-Gruppen-Invarianten (Konzept 2).
    # Zwei Gruppen sind jeweils gegenseitig exklusiv (XOR):
    #   - Phase:     phase:plan  XOR  phase:execute
    #   - Approval:  approval:required  XOR  approval:approved
    # Es gibt bewusst NUR ein Approval-Label — ob damit ein Plan oder ein
    # Ergebnis freigegeben wird, ergibt sich aus dem Kontext/der Phase
    # (plan -> Plan-Freigabe, execute/fertig -> Ergebnis-Abnahme).
    # Es wird deshalb KEIN separates "acceptance"-Label verwendet.
    # ------------------------------------------------------------------
    _LABEL_XOR_GROUPS = (
        (f"{LABEL_PREFIX_PHASE}{PHASE_PLAN}", f"{LABEL_PREFIX_PHASE}{PHASE_EXECUTE}"),
        (f"approval:{APPROVAL_REQUIRED}", f"approval:{APPROVAL_APPROVED}"),
    )

    def _xor_group_of(self, canonical_key: Optional[str]) -> Optional[tuple[str, str]]:
        """Liefert die XOR-Gruppe (Paar kanonischer Keys), zu der ``canonical_key``
        gehört, oder None, wenn der Key keiner Gruppe angehört."""
        if not canonical_key:
            return None
        for group in self._LABEL_XOR_GROUPS:
            if canonical_key in group:
                return group
        return None

    async def _remove_xor_siblings(
        self,
        card_id: str,
        canonical_key: str,
        except_sibling: Optional[str] = None,
    ) -> None:
        """Entfernt die Schwester-Labels derselben XOR-Gruppe von einer Karte.

        Setzt man z. B. ``phase:execute``, wird ``phase:plan`` entfernt; setzt
        man ``approval:required``, wird ``approval:approved`` entfernt. Dadurch
        kann auf einer Karte nie mehr als EIN Label pro Gruppe liegen — das
        Label-Chaos (🚀 + ⌛ + ✔️ zugleich) ist strukturell ausgeschlossen.
        Best-effort: Fehler werden geloggt, niemals geworfen.
        """
        group = self._xor_group_of(canonical_key)
        if not group:
            return
        for sibling in group:
            if sibling == canonical_key:
                continue
            if except_sibling and sibling == except_sibling:
                continue
            try:
                removed = await self._remove_label_from_card(card_id, sibling)
                if removed:
                    logger.info(
                        "Deck: XOR-Invariante — '%s' entfernt (Konflikt mit '%s') auf Karte %s.",
                        sibling, canonical_key, card_id,
                    )
            except Exception as exc:
                logger.debug("Deck: XOR-Entfernung '%s' auf Karte %s fehlgeschlagen: %s", sibling, card_id, exc)

    async def _normalize_label_conflicts(self, card_id: str) -> None:
        """Poll-seitige Normalisierung (Konzept 2, Punkt 3): erkennt und bereinigt
        widersprüchliche Label-Paare auf einer Karte IDEMPOTENT, ohne auf einen
        Agent-Lauf warten zu müssen.

        Regel (Best-effort, deterministisch): Der aktuelle/menschliche Zustand
        gewinnt. Bei einem Konflikt in der Phase-Gruppe gewinnt ``execute``;
        bei der Approval-Gruppe gewinnt ``required`` (eine erneute Freigabe-
        Anforderung invalidiert die alte Freigabe).
        """
        location = await self._locate_card(card_id)
        if location is None:
            return
        board_id, stack_id = location
        try:
            current_card = await self.client.get_card(board_id, stack_id, card_id)
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
            await self._remove_label_from_card(card_id, phase_plan)
            logger.info("Deck: Label-Konflikt bereinigt — 'phase:plan' entfernt (execute gewinnt) auf Karte %s.", card_id)
        # Approval: required gewinnt gegen approved
        approval_required = f"approval:{APPROVAL_REQUIRED}"
        approval_approved = f"approval:{APPROVAL_APPROVED}"
        if approval_required in label_keys and approval_approved in label_keys:
            await self._remove_label_from_card(card_id, approval_approved)
            logger.info("Deck: Label-Konflikt bereinigt — 'approval:approved' entfernt (required gewinnt) auf Karte %s.", card_id)

    async def _apply_label_to_card(self, card_id: str, label_title: str) -> tuple[bool, Optional[str]]:
        """Weist der Karte ein Label zu (erstellt das Label bei Bedarf auf dem Board).

        Akzeptiert Friendly-Titel ("⌛ Freigabe nötig") und kanonische Keys
        ("approval:required") sowie die alte technische Form ("hermes/approval:required").
        Im Board wird das Label immer im Friendly-Format mit Mapping-Farbe angelegt.
        """
        # Kanonischen Key auflösen; unbekannte Labels unverändert durchreichen
        canonical_key = canonical_label_key(label_title)
        board_title = friendly_label_title(canonical_key) if canonical_key else label_title

        location = await self._locate_card(card_id)
        if location is None:
            return False, "Card could not be located"
        board_id, stack_id = location

        current_card = await self.client.get_card(board_id, stack_id, card_id)
        if current_card:
            # Idempotenz: Ist das Label (in irgendeiner Schreibweise) bereits
            # auf der Karte, ist die Zuweisung ein No-Op.
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

        board_labels = await self.client.get_board_labels(board_id)
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
                new_lbl = await self.client.create_board_label(board_id, board_title, color=color)
            except NextcloudDeckError as exc:
                # Z. B. HTTP 403: Der Bot-User hat nur permissionEdit (nicht
                # permissionManage) und darf keine Board-Labels anlegen. Das darf
                # den Poll-Zyklus NICHT crashen — sauber als Fehlschlag melden.
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
            res = await self.client.assign_label(board_id, stack_id, card_id, int(target_label_id))
        except NextcloudDeckError as exc:
            # Wenn bereits zugewiesen, als Erfolg werten
            if "already assigned" in str(exc).lower():
                return True, None
            logger.warning("Deck assign_label failed for card %s: %s", card_id, exc)
            return False, str(exc)

        # XOR-Invariante (Konzept 2): Nach erfolgreicher Zuweisung die
        # Schwester-Labels derselben Gruppe entfernen, damit z. B. beim Setzen
        # von 'approval:required' ein altes 'approval:approved' sofort
        # verschwindet — kein Label-Chaos möglich.
        if canonical_key:
            await self._remove_xor_siblings(card_id, canonical_key)

        return res is not None, None

    async def _remove_label_from_card(self, card_id: str, label_title: str) -> bool:
        """Entfernt ein Label von der Karte (Friendly-Titel oder kanonischer Key)."""
        location = await self._locate_card(card_id)
        if location is None:
            return False
        board_id, stack_id = location

        canonical_key = canonical_label_key(label_title)
        board_labels = await self.client.get_board_labels(board_id)
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
            res = await self.client.remove_label(board_id, stack_id, card_id, int(target_label_id))
            return res is not None
        except NextcloudDeckError as exc:
            logger.warning("Deck remove_label failed for card %s: %s", card_id, exc)
            return False

    async def _assign_user_to_card(self, card_id: str, user_id: str) -> bool:
        """Weist einen Benutzer der Karte zu (löst Username -> UID auf)."""
        location = await self._locate_card(card_id)
        if location is None:
            return False
        board_id, stack_id = location

        # Username/Display-Name -> Nextcloud-UID auflösen (Handoff an Menschen).
        resolved = await self.identity.resolve_user_uid(user_id)
        if resolved:
            user_id = resolved

        try:
            res = await self.client.assign_user(board_id, stack_id, card_id, user_id)
            return res is not None
        except NextcloudDeckError as exc:
            if "already assigned" in str(exc).lower():
                return True
            logger.warning("Deck assign_user failed for card %s: %s", card_id, exc)
            return False

    async def _unassign_user_from_card(self, card_id: str, user_id: str) -> bool:
        """Entfernt einen Benutzer von der Karte."""
        location = await self._locate_card(card_id)
        if location is None:
            return False
        board_id, stack_id = location
        try:
            res = await self.client.unassign_user(board_id, stack_id, card_id, user_id)
            return res is not None
        except NextcloudDeckError as exc:
            logger.warning("Deck unassign_user failed for card %s: %s", card_id, exc)
            return False

    async def _locate_card(self, card_id: str) -> Optional[tuple[str, str]]:
        """Findet (board_id, stack_id) einer Karte über die konfigurierten Boards."""
        try:
            boards = await self.client.get_boards()
        except NextcloudDeckError as exc:
            logger.warning("Deck: Boards konnten nicht geladen werden: %s", exc)
            return None
        for board in boards if isinstance(boards, list) else []:
            board_id = str(board.get("id") or "").strip()
            if not board_id or (self.runtime.boards and board_id not in self.runtime.boards):
                continue
            try:
                stacks = await self.client.get_stacks(board_id)
            except NextcloudDeckError:
                continue
            for stack in stacks if isinstance(stacks, list) else []:
                stack_id = str(stack.get("id") or "").strip()
                for card in stack.get("cards") or []:
                    if str(card.get("id") or "") == card_id:
                        return board_id, stack_id
        return None

    async def _resolve_target_stack_id(self, board_id: str, status_key: str) -> Optional[str]:
        """Löst einen Status-Schlüssel zu einer Stack-ID auf (Board-Config 'status_mapping' oder Stack-Titel)."""
        config = self._configured_board(board_id) or {}
        mapping = config.get("status_mapping") or config.get("stack_mapping") or {}
        if isinstance(mapping, dict):
            target = mapping.get(status_key) or mapping.get(status_key.lower())
            if target:
                return str(target).strip()

        # Fallback: Stack-Titel-Match (case-insensitive)
        try:
            stacks = await self.client.get_stacks(board_id)
        except NextcloudDeckError:
            return None
        for stack in stacks if isinstance(stacks, list) else []:
            title = str(stack.get("title") or "").strip().casefold()
            if title == status_key.strip().casefold():
                return str(stack.get("id") or "").strip() or None
        return None

    async def send_message(
        self,
        target: str,
        text: str,
        reply_to_message_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return await self.send(target, text, reply_to_message_id, metadata)

    @staticmethod
    def _card_id_from_target(target: str) -> str:
        parts = str(target).split(":")
        if "card" in parts:
            idx = parts.index("card")
            if idx + 1 < len(parts):
                return parts[idx + 1]
        return str(target).strip()

    def _configured_board(self, board_id: str) -> Optional[Dict[str, Any]]:
        if not self.runtime.boards:
            return None
        return self.runtime.boards.get(board_id)

    # Aktive Arbeits-Spalten (für WIP-Zählung und WIP-Gate). Als Modul-/
    # Klassenkonstante definiert, damit _count_active_cards und der WIP-Guard
    # in _process_card dieselbe Definition nutzen (keine Drift).
    # "triage" zählt mit: eine dort zugewiesene Karte wird getriggert, soll aber
    # wie jede andere aktive Karte gegen das globale WIP-Limit gesperrt werden
    # (sonst startet sie parallel zu einer laufenden Karte — siehe Karte 121).
    _ACTIVE_STACK_TITLES = {
        "todo", "ready", "running", "to do", "in progress", "in bearbeitung",
        "triage",
    }

    async def _count_active_cards(self, exclude_card_id: Optional[str] = None) -> int:
        """Zählt Karten in Arbeits-Spalten (todo/ready/running/triage) über ALLE
        Boards.

        Wird für das GLOBALE WIP-Limit genutzt: Karten, die der Mensch aktiv in
        den Workflow gegeben hat (nicht Backlog/Review/Blocked/Done), gelten als
        "in Arbeit" — unabhängig davon, auf welchem Board sie liegen.
        ``exclude_card_id`` blendet die aktuell betrachtete Karte aus, damit
        deren eigener Status nicht die Zählung verfälscht.

        Wichtig: Karten mit dem "Waiting"-Label werden NICHT mitgezählt. Sie
        warten selbst auf freie WIP-Kapazität; würden sie zählen, blockierten
        sich mehrere Waiting-Karten in Triage gegenseitig (Deadlock, siehe
        Karten 120/121).
        """
        if not self.runtime.max_in_progress:
            return 0
        waiting_title = friendly_label_title("waiting").lower()
        total = 0
        for board_id in self.runtime.boards:
            try:
                stacks = await self.client.get_stacks(board_id)
            except NextcloudDeckError:
                continue
            for stack in stacks or []:
                title = str(stack.get("title") or "").strip().lower()
                if title not in self._ACTIVE_STACK_TITLES:
                    continue
                for card in stack.get("cards") or []:
                    cid = str(card.get("id") or "").strip()
                    if exclude_card_id and cid == exclude_card_id:
                        continue
                    labels = self._card_label_titles(card)
                    if any(str(l).strip().lower() == waiting_title for l in labels):
                        continue
                    total += 1
        # In-Flight-Turns einbeziehen: Karten, deren Turn gerade aktiv läuft
        # (send_typing gesehen, stop_typing noch nicht), zählen ebenfalls gegen
        # das WIP-Limit — auch wenn sie (noch) nicht in einer aktiven Spalte
        # liegen. So verhindern wir, dass mehrere freigegebene Karten parallel
        # anlaufen (jede hat einen eigenen Session-Key; das Gateway serialisiert
        # nur pro Karte, nicht über Karten hinweg).
        for cid in self._active_turns:
            if exclude_card_id and cid == exclude_card_id:
                continue
            total += 1
        return total

    def _card_is_triggered(self, card: Dict[str, Any], comments: List[Dict[str, Any]]) -> bool:
        assigned = set(self.identity.assigned_uids(card))
        if self.runtime.hermes_user_id in assigned:
            return True

        needles = {
            self.runtime.hermes_user_id.lower(),
            self.runtime.username.lower(),
            *self.runtime.bot_aliases,
        }
        description = str(card.get("description") or "").lower()
        if any(needle and needle in description for needle in needles):
            return True

        for comment in comments:
            message = str(comment.get("message") or "").lower()
            if any(needle and needle in message for needle in needles):
                return True
        return False

    @staticmethod
    def _card_label_titles(card: Dict[str, Any]) -> List[str]:
        """Alle Label-Titel einer Karte (sortiert deterministisch)."""
        titles = [
            str(lbl.get("title") or "")
            for lbl in (card.get("labels") or [])
            if isinstance(lbl, dict) and lbl.get("title")
        ]
        return sorted(titles)

    @staticmethod
    def _last_comment_author(comment: Dict[str, Any]) -> Optional[str]:
        for key in ("actorId", "actor", "author", "userId"):
            value = comment.get(key)
            if isinstance(value, dict):
                value = value.get("uid") or value.get("id") or value.get("primaryKey")
            if value:
                return str(value).strip()
        return None

    def _comment_id(self, comment: Dict[str, Any]) -> Optional[int]:
        try:
            return int(str(comment.get("id") or ""))
        except (ValueError, TypeError):
            return None

    def _new_comments_since_baseline(
        self, board_id: str, card_id: str, comments: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Liefert Kommentare mit ID > letzter verarbeiteter ID (aufsteigend).

        Während eine Karte auf Waiting/Freigabe wartet, sammeln sich mehrere
        menschliche Kommentare an. Diese sollen dem Agenten beim nächsten Lauf
        ALLE als Kontext übergeben werden — nicht nur der allerletzte. Eigene
        (Agent-)Kommentare zwischen den menschlichen werden ausgefiltert.
        """
        baseline = self.state.last_processed_comment_id(board_id, card_id)
        result: List[Dict[str, Any]] = []
        for c in comments:
            cid = self._comment_id(c)
            if cid is None:
                continue
            if baseline is not None and cid <= baseline:
                continue
            result.append(c)
        return result

    async def _process_card(
        self,
        board: Dict[str, Any],
        stack: Dict[str, Any],
        card: Dict[str, Any],
    ) -> None:
        board_id = str(board.get("id") or "").strip()
        stack_id = str(stack.get("id") or "").strip()
        card_id = str(card.get("id") or "").strip()
        if not board_id or not stack_id or not card_id:
            return

        # Backlog-Filter: Karten in der Backlog-Spalte sind reine Ideensammlungen
        # und werden vom Agenten bewusst ignoriert, bis ein Mensch sie nach Triage/Todo zieht.
        board_config = self._configured_board(board_id)
        if is_backlog_stack(stack, board_config):
            logger.debug("Nextcloud Deck: Karte %s liegt im Backlog ('%s') und wird ignoriert.", card_id, stack.get("title"))
            return

        comments = await self.client.get_card_comments(card_id)
        if not self._card_is_triggered(card, comments):
            return

        # Deck-API liefert Kommentare absteigend (neueste zuerst) — für eine
        # deterministische "neuester Kommentar"-Logik explizit aufsteigend nach
        # numerischer ID sortieren (gleicher Bug-Typ wie Talk-Fix f9f057b).
        comments = sorted(
            comments,
            key=lambda c: int(str(c.get("id") or 0) or 0) if str(c.get("id") or 0).isdigit() else 0,
        )
        last = comments[-1] if comments else {}
        last_author = self._last_comment_author(last) if last else None

        # --- LOOPS & SYSTEM-MESSAGE FILTER ---
        # 1. Ignoriere eigene Nachrichten, leere Absender sowie reservierte System-Accounts
        #    AUSNAHME: Hat ein Mensch seit dem letzten Lauf Workflow-Labels geändert
        #    (z. B. Freigabe erteilt), wird der Filter übersprungen — die Label-
        #    Änderung ist ein echter Trigger, auch wenn der Agent zuletzt kommentierte.
        label_titles = self._card_label_titles(card)
        snapshot = DeckCardSnapshot(
            board_id=board_id,
            stack_id=stack_id,
            card_id=card_id,
            title=str(card.get("title") or ""),
            description=str(card.get("description") or ""),
            assigned_users=self.identity.assigned_uids(card),
            labels=label_titles,
            last_comment_id=str(last.get("id")) if last.get("id") else None,
            last_author=last_author,
            last_comment_message=str(last.get("message") or "") if last else None,
            due_date=str(card.get("duedate")) if card.get("duedate") else None,
            done=card.get("done"),
        )
        human_label_change = (
            self.state.label_changed_since_baseline(snapshot)
            and has_workflow_label_change(
                self.state._last_labels.get(f"{board_id}:{card_id}", []),
                label_titles,
            )
        )
        if human_label_change:
            logger.info(
                "Deck: Workflow-Label-Änderung auf Karte %s erkannt (%s) — Trigger trotz Agent-eigenem Kommentar.",
                card_id,
                ", ".join(label_titles),
            )
        elif last_author and (
            last_author == self.runtime.username
            or last_author == self.runtime.hermes_user_id
            or last_author.lower() in {*self.runtime.bot_aliases, "system", "changelog", "sample"}
        ):
            logger.debug("Nextcloud Deck: Ignoriere eigenen oder System-Kommentar (Author: %s)", last_author)
            return

        # 2. Native Nextcloud System-Message / System-Event Flags auswerten
        if last and (last.get("systemMessage") or last.get("system_message")):
            logger.debug("Nextcloud Deck: Ignoriere native SystemMessage auf Karte %s", card_id)
            return

        # 3. Ignoriere systemgenerierte Textnachrichten & Platzhalter
        last_message = str(last.get("message") or "") if last else ""
        if (
            "{actor}" in last_message
            or "Das System hat" in last_message
            or "Gesprächseinstellungen verwalten" in last_message
            or "Unterhaltungsinformationen bearbeiten" in last_message
        ):
            logger.debug("Nextcloud Deck: Ignoriere automatische System-Textnachricht auf Karte %s", card_id)
            return
        # -------------------------------------

        if not self.state.should_process(snapshot):
            return

        # WIP-Limit: Ist max_in_progress > 0 und bereits so viele ANDERE Karten
        # in Arbeits-Spalten (todo/ready/running) über ALLE Boards, wie das
        # Limit erlaubt, wird diese Karte NICHT gestartet — der nächste
        # Poll-Zyklus prüft erneut, sobald eine laufende Karte nach
        # Review/Blocked/Done geschoben wurde. Die wartende Karte bekommt das
        # Label "Waiting", damit sichtbar ist, dass der Adapter sie gesehen hat.
        # Fix 2a: Das WIP-Limit gilt NUR für Karten, die selbst in einer aktiven
        # Arbeits-Spalte (todo/ready/running/triage) liegen. Karten in Review/
        # Blocked/Done warten auf den Menschen oder sind beendet — sie dürfen
        # NICHT mit 'Waiting' belegt und NICHT gegen das WIP-Limit gesperrt
        # werden, sonst entsteht ein endloser WIP-Spam (siehe Karte 117 in Review).
        current_stack_title = str(stack.get("title") or "").strip().lower()
        if (
            self.runtime.max_in_progress > 0
            and current_stack_title in self._ACTIVE_STACK_TITLES
        ):
            active = await self._count_active_cards(exclude_card_id=card_id)
            if active >= self.runtime.max_in_progress:
                logger.info(
                    "Deck: WIP-Limit erreicht (%d >= %d) — Karte %s ('%s') noch nicht gestartet.",
                    active, self.runtime.max_in_progress, card_id, snapshot.title,
                )
                await self._apply_label_to_card(card_id, friendly_label_title("waiting"))
                # Fix 2b: Den Zustand als "gesehen" markieren, damit diese Karte
                # nicht in jedem Poll-Zyklus erneut getriggert wird (sonst
                # Dauer-Spam alle poll_interval Sekunden). WICHTIG: mark_seen
                # (nicht mark_processed), damit die Kommentar-Baseline NICHT
                # fortgeschrieben wird — die während des Wartens auflaufenden
                # Kommentare müssen dem Agenten später erhalten bleiben.
                self.state.mark_seen(snapshot)
                return

        # Konzept 2 (Punkt 3): Label-Konflikte idempotent bereinigen, sobald die
        # Karte tatsächlich (re-)verarbeitet wird — bevor der Agent läuft. So
        # wird z. B. ein gleichzeitiges "phase:plan"+"phase:execute" oder
        # "approval:required"+"approval:approved" deterministisch aufgelöst,
        # ohne auf das LLM-Verhalten zu vertrauen.
        await self._normalize_label_conflicts(card_id)

        # Karte wird jetzt tatsächlich gestartet — ein evtl. gesetztes
        # "Waiting"-Label (aus einem früheren WIP-Block) wieder entfernen.
        await self._remove_label_from_card(card_id, friendly_label_title("waiting"))

        actor_id, groups, is_fallback = await self.identity.resolve_card_actor(card, last_author)

        principal = self.identity.build_principal(
            user_id=actor_id,
            groups=groups,
            board_id=str(board_id),
            card_id=str(card_id),
            is_fallback=is_fallback,
        )

        session_key = f"deck:board:{board_id}:card:{card_id}"
        source = self.build_source(
            chat_id=session_key,
            chat_name=snapshot.title or session_key,
            chat_type="deck_card",
            user_id=actor_id,
            user_name=actor_id,
            message_id=card_id,
        )
        headers = self.identity.principal_headers(principal) or {
            "X-On-Behalf-Of": actor_id,
            "X-User-Groups": ",".join(str(g).strip().lower() for g in groups),
        }
        # extra_headers plattformneutral transportieren:
        # - dict-Sources (Test-Fallback-Base) → Key-Setzung
        # - reale SessionSource-Objekte (Hermes >= 0.20) → Attribut
        #   (Dataclass ist nicht frozen; das x-on-behalf-Plugin liest
        #   getattr(source, "extra_headers"))
        if isinstance(source, dict):
            source["extra_headers"] = headers
        else:
            try:
                setattr(source, "extra_headers", headers)
            except Exception:
                logger.warning(
                    "Deck: Konnte extra_headers nicht an SessionSource hängen."
                )

        stack_title = str(stack.get("title") or "").strip()
        hermes_labels = extract_hermes_labels(card)
        phase = hermes_labels.get("phase") or PHASE_PLAN
        task_type = hermes_labels.get("type")
        risk = hermes_labels.get("risk")
        approval = hermes_labels.get("approval")

        # Phase-Auto-Set: Trägt die Karte noch KEIN hermes/phase:* Label, wird
        # 'hermes/phase:plan' einmalig gesetzt, damit der Zustand im Deck-UI
        # sichtbar ist (konzeptionell ist "kein Label" = Plan-Phase). Loop-sicher,
        # weil die Re-Baseline nach dem Lauf die neue Baseline übernimmt.
        if not hermes_labels.get("phase"):
            try:
                applied, _ = await self._apply_label_to_card(card_id, f"{LABEL_PREFIX_PHASE}{PHASE_PLAN}")
                if applied:
                    logger.info("Deck: Karte %s: 'hermes/phase:plan' automatisch gesetzt.", card_id)
            except Exception as exc:
                logger.debug("Deck: Auto-Set phase:plan fehlgeschlagen: %s", exc)

        # Phase-Auto-Transition: Liegt eine menschliche Freigabe vor, die Karte
        # sich aber noch in 'plan' befindet, vollzieht der ADAPTER (nicht der
        # Agent) den Wechsel nach 'execute': Label phase:plan entfernen,
        # phase:execute setzen und die Karte nach 'running' schieben. Das macht
        # den aktiven Bearbeitungsstatus sofort im Deck-UI sichtbar (🚀 In
        # Umsetzung + Spalte Running) und stellt sicher, dass der Agent ab dem
        # nächsten Lauf den Execute-Prompt statt des Plan-Prompts bekommt.
        # Loop-sicher: nach dem Wechsel trägt die Karte kein phase:plan mehr,
        # die Re-Baseline übernimmt den neuen Zustand.
        if (
            approval == APPROVAL_APPROVED
            and phase == PHASE_PLAN
        ):
            try:
                applied_phase, _ = await self._apply_label_to_card(card_id, f"{LABEL_PREFIX_PHASE}{PHASE_EXECUTE}")
                if applied_phase:
                    await self._remove_label_from_card(card_id, f"{LABEL_PREFIX_PHASE}{PHASE_PLAN}")
                    logger.info(
                        "Deck: Karte %s: Freigabe liegt vor → Phase auf 'execute' gewechselt (🚀 In Umsetzung).",
                        card_id,
                    )
                    # Karte in die aktive Spalte schieben, sofern sie noch nicht
                    # dort liegt (Running/Ready/In Bearbeitung).
                    await self._move_card_to_status(card_id, STATUS_RUNNING)
                    # Phase-Variable für den aktuellen Prompt aktualisieren
                    phase = PHASE_EXECUTE
            except Exception as exc:
                logger.debug("Deck: Auto-Transition plan→execute fehlgeschlagen: %s", exc)

        subtask_progress = parse_subtasks(snapshot.description)
        capabilities_prompt = build_capabilities_prompt(
            phase=phase,
            task_type=task_type,
            risk=risk,
            approval=approval,
        )

        all_label_titles = self._card_label_titles(card)

        text = (
            f"Nextcloud Deck Karte: {snapshot.title}\n"
            f"Karten-ID (card_id): {card_id}\n"
            f"Aktuelle Spalte: {stack_title}\n"
            f"Labels: {', '.join(all_label_titles) if all_label_titles else 'keine'}\n"
            f"Subtasks: {subtask_progress.summary()}\n\n"
            f"{capabilities_prompt}\n\n"
            f"Beschreibung:\n{snapshot.description}"
        )

        # Neue Kommentare seit der letzten Verarbeitung (nicht nur der aller-
        # letzte). Während eine Karte wartet (Waiting/Freigabe), sammeln sich
        # mehrere menschliche Hinweise an — die bekommt der Agent jetzt ALLE.
        new_comments = self._new_comments_since_baseline(board_id, card_id, comments)
        if new_comments:
            blocks = []
            for c in new_comments:
                author = self._last_comment_author(c) or "unbekannt"
                msg = str(c.get("message") or "").strip()
                if not msg:
                    continue
                blocks.append(f"[{author}]: {msg}")
            if blocks:
                text += "\n\nNeue Kommentare seit dem letzten Lauf:\n" + "\n\n".join(blocks)
        elif last and last.get("message"):
            text += f"\n\nLetzter Kommentar von {last_author or 'unbekannt'}:\n{last['message']}"

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message={
                "board": board,
                "stack": stack,
                "card": card,
                "comments": comments,
                "hermes_workflow": {
                    "phase": phase,
                    "task_type": task_type,
                    "risk": risk,
                    "approval": approval,
                    "subtask_progress": {
                        "total": subtask_progress.total,
                        "completed": subtask_progress.completed,
                        "percentage": subtask_progress.percentage,
                    },
                },
            },
            message_id=card_id,
            user_id=actor_id,
            user_name=actor_id,
        )
        result = None
        run_error: Optional[str] = None
        workflow_ctx = DeckWorkflowContext(
            board_id=str(board_id),
            card_id=str(card_id),
            phase=phase,
            risk=risk or "low",
            approval=approval,
        )
        token = current_deck_context.set(workflow_ctx)
        action_token = current_deck_action_count.set(0)
        try:
            if principal is not None:
                with self.identity.principal_context(principal):
                    result = self.handle_message(event)
            else:
                result = self.handle_message(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            run_error = str(exc)
            logger.warning(
                "Deck: Agent-Run für Karte %s schlug fehl: %s", card_id, exc
            )
        finally:
            current_deck_context.reset(token)
            action_count = current_deck_action_count.get()
            current_deck_action_count.reset(action_token)

        # Auto-Block bei Run-Fehler: Produziert der Agent keine verwertbare
        # strukturelle Änderung (deck_card_action) und signalisiert die Antwort
        # einen Token-Limit-/Fehlschlag, verschiebt der ADAPTER die Karte nach
        # 'blocked'. Sonst würde eine feststeckende Karte dauerhaft in Running
        # hängen und das globale WIP-Limit blockieren (nächste Karte bleibt
        # 'Waiting'). Loop-sicher: nach dem Auto-Block wird die Dedup-Baseline
        # unten neu gesetzt → kein Selbst-Trigger.
        #
        # Konzept 1: Zusätzlich wird INHALTLICH registriert, wenn der Agent ein
        # Block-Intent-Signal produziert (z. B. "🤖 BLOCKED" / "PLAN CHANGE
        # REQUESTED" im Ergebnis oder Kommentar), ohne dass er die Karte selbst
        # strukturell nach 'blocked' verschoben hat. Auch dann übernimmt der
        # ADAPTER den deterministischen Block — unabhängig vom LLM-Verhalten.
        block_intent = self._run_has_block_intent(result)
        if run_error is not None or (
            action_count == 0 and self._run_has_failure_signal(result)
        ) or block_intent:
            await self._auto_block_card(card_id)
            return

        # Fix 1 (Adapter-Fallback): Ein Execute-Turn, der OHNE strukturelle
        # Änderung endet, aber ein "warte auf menschliche Entscheidung"-Signal
        # trägt, wird deterministisch nach 'review' verschoben — statt ewig in
        # 'running' zu hängen und den WIP-Slot zu blockieren (der 119-Bug).
        if (
            phase == PHASE_EXECUTE
            and action_count == 0
            and self._run_has_review_intent(result)
        ):
            moved, _ = await self._move_card_to_status(card_id, STATUS_REVIEW)
            if moved:
                logger.info(
                    "Deck: Karte %s nach 'review' verschoben — Agent wartet auf neue menschliche Freigabe (Review-Intent erkannt).",
                    card_id,
                )
            await self._rebaseline_card(board_id, stack_id, card_id)
            return

        # Fix 2 (Adapter-Fallback, Plan-Phase): Ein Plan-Lauf, der OHNE
        # strukturelle Änderung endet (kein deck_card_action), hat den Plan
        # nicht in die Description geschrieben. Das ist fast immer ein
        # "Agent hat Rückfragen / konnte nicht abschließen"-Fall. Statt die
        # Karte still in Triage/Running hängen zu lassen (und den WIP-Slot zu
        # blockieren), wird sie nach 'review' zurückgegeben — der Mensch sieht
        # die offenen Fragen im Kommentar und kann antworten.
        if phase == PHASE_PLAN and action_count == 0:
            moved, _ = await self._move_card_to_status(card_id, STATUS_REVIEW)
            if moved:
                logger.info(
                    "Deck: Karte %s (Phase 'plan') ohne deck_card_action nach 'review' verschoben — Plan nicht strukturell abgeschlossen (vermutlich offene Rückfragen).",
                    card_id,
                )
            await self._rebaseline_card(board_id, stack_id, card_id)
            return

        # Diagnose: Hat der Agent den Lauf ohne deck_card_action beendet, obwohl
        # die Phase strukturelle Änderungen erwarten würde? Unsichtbares
        # "nur Kommentar"-Muster frühzeitig sichtbar machen.
        if action_count == 0:
            logger.warning(
                "Deck: Lauf für Karte %s (Phase '%s') endete ohne deck_card_action-Aufruf — "
                "der Agent hat vermutlich nur einen Text-Kommentar geschrieben statt die Karte "
                "strukturell zu verändern.",
                card_id, phase,
            )

        # Nach erfolgreicher Verarbeitung die Dedup-Baseline auf den *aktuellen*
        # Karten-Zustand setzen. Der Agent kann während handle_message selbst
        # Labels/Description geändert haben — diese Änderungen sollen NICHT als
        # neuer Trigger wirken, ein späterer menschenseitiger Wechsel aber schon.
        await self._rebaseline_card(board_id, stack_id, card_id)

    _RUN_FAILURE_MARKERS = (
        "no visible answer",
        "output-token limit",
        "output token limit",
        "max_tokens",
        "hit its output-token",
        "reasoning consumed",
    )

    # Konzept 1: Inhaltliche Block-Intent-Signale des Agenten. Produziert der
    # Agent eine dieser Phrasen (im Ergebnis oder als Kommentar), ohne die Karte
    # selbst nach 'blocked' zu verschieben, übernimmt der Adapter den Block
    # deterministisch.
    _BLOCK_INTENT_MARKERS = (
        "🤖 blocked",
        "plan change requested",
        "blocked —",
        "blocked --",
        "nicht lösbar",
        "nicht loesbar",
        "not possible",
        "hard blocker",
        "can't proceed",
        "cannot proceed",
    )

    # Fix 1: Review-Intent-Signale. Der Agent pausiert und delegiert die nächste
    # Entscheidung an den Menschen („warte auf deine Freigabe", „du sagst mir…").
    # Erkennt der Adapter ein solches Signal in einem Execute-Turn ohne
    # strukturelle Änderung, verschiebt er die Karte deterministisch nach 'review'.
    _REVIEW_INTENT_MARKERS = (
        "warte auf deine",
        "warten auf deine",
        "waiting for your",
        "awaiting your",
        "waiting on your",
        "brauche deine",
        "need your",
        "deine freigabe",
        "deine bestätigung",
        "deine entscheidung",
        "your approval",
        "your confirmation",
        "your decision",
        "du sagst",
        "bitte bestätig",
        "please confirm",
        "halte dann an",
        "halt an",
    )

    @classmethod
    def _run_has_failure_signal(cls, result: Any) -> bool:
        """Erkennt Fehlschlag-Signale im Run-Ergebnis.

        Prüft den Text des Ergebnisses auf bekannte Token-Limit-/Fehler-Marker
        (z. B. „No visible answer was produced ... output-token limit"). Gibt
        True zurück, wenn der Run offensichtlich ohne verwertbare Antwort
        gescheitert ist.
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
        return any(m in low for m in cls._RUN_FAILURE_MARKERS)

    @classmethod
    def _run_has_review_intent(cls, result: Any) -> bool:
        """Erkennt ein Review-Intent-Signal des Agenten (Fix 1).

        Prüft auf semantische Muster „ich pausiere und warte auf eine menschliche
        Entscheidung/Freigabe". Wird genutzt, um eine Karte ohne strukturelle
        Änderung deterministisch nach 'review' zu verschieben statt sie in
        'running' hängen zu lassen.
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
        return any(m in low for m in cls._REVIEW_INTENT_MARKERS)

    @classmethod
    def _run_has_block_intent(cls, result: Any) -> bool:
        """Erkennt ein inhaltliches Block-Intent-Signal des Agenten (Konzept 1).

        Anders als ``_run_has_failure_signal`` (technischer Token-/Fehler-Marker)
        prüft dies auf ein semantisches „ich kann das nicht lösen / Plan-Change
        nötig"-Signal. Rückgabe True = der Adapter soll die Karte nach 'blocked'
        verschieben, sofern der Agent es nicht bereits selbst getan hat.
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
        return any(m in low for m in cls._BLOCK_INTENT_MARKERS)

    async def _auto_block_card(self, card_id: str) -> None:
        """Verschiebt eine fehlgeschlagene Karte nach 'blocked' + Kommentar.

        Wird vom Adapter (nicht vom Agenten) ausgelöst, wenn ein Run ohne
        verwertbare strukturelle Änderung endete oder eine Exception warf. Gibt
        den WIP-Platz frei, sodass die nächste wartende Karte starten kann.
        Best-effort: Fehler beim Move/Kommentar werden geloggt, niemals geworfen.
        """
        try:
            moved, err = await self._move_card_to_status(card_id, STATUS_BLOCKED)
            if moved:
                logger.info("Deck: Karte %s automatisch nach 'blocked' verschoben (Run-Fehler).", card_id)
            else:
                logger.warning("Deck: Auto-Block für Karte %s fehlgeschlagen: %s", card_id, err)
        except Exception as exc:
            logger.warning("Deck: Auto-Block für Karte %s schlug fehl: %s", card_id, exc)
        try:
            await self.client.add_comment(
                card_id,
                "🤖 AUTO-BLOCKED: Der Agent-Run konnte keine verwertbare Antwort erzeugen "
                "(Token-Limit oder interner Fehler). Bitte prüfen und ggf. Reasoning anpassen.",
            )
        except Exception as exc:
            logger.warning("Deck: Auto-Block-Kommentar für Karte %s fehlgeschlagen: %s", card_id, exc)

    async def _rebaseline_card(self, board_id: str, stack_id: str, card_id: str) -> None:
        """Setzt die Dedup-Baseline auf den aktuellen Karten-Zustand neu.

        WICHTIG: ``stack_id`` kann nach einem Move der AEHRE Stack sein (z. B.
        die Karte wurde gerade von Triage nach Review verschoben). Deshalb wird
        die aktuelle Position zuerst per _locate_card neu ermittelt — sonst
        liefert get_card mit dem alten Stack None, die Baseline wird NICHT
        gesetzt, und die Karte erscheint beim nächsten Poll als "verändert"
        (Re-Trigger-Schleife nach einem Review-Move).
        """
        # Aktuelle Position (board_id + stack_id) neu auflösen, falls vorhanden.
        location = await self._locate_card(card_id)
        if location is not None:
            board_id, stack_id = location
        try:
            current = await self.client.get_card(board_id, stack_id, card_id)
        except Exception:
            return
        if not current:
            return
        comments = await self.client.get_card_comments(card_id)
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
            assigned_users=self.identity.assigned_uids(current),
            labels=self._card_label_titles(current),
            last_comment_id=str(last.get("id")) if last.get("id") else None,
            last_author=self._last_comment_author(last) if last else None,
            last_comment_message=str(last.get("message") or "") if last else None,
            due_date=str(current.get("duedate")) if current.get("duedate") else None,
            done=current.get("done"),
        )
        self.state.mark_processed(fresh)

    async def poll_once(self) -> int:
        boards = await self.client.get_boards()
        processed = 0
        for board in boards:
            board_id = str(board.get("id") or "").strip()
            if not board_id:
                continue
            config = self._configured_board(board_id)
            if config is None:
                continue
            stacks = await self.client.get_stacks(board_id)
            # Vorlagen-/Format-Sicherung im Backlog (pro Board, pro Poll-Zyklus)
            await self._ensure_backlog_template(board_id, board, stacks)
            for stack in stacks:
                for card in stack.get("cards") or []:
                    await self._process_card(board, stack, card)
                    processed += 1
        return processed

    async def _ensure_backlog_template(
        self,
        board_id: str,
        board: Dict[str, Any],
        stacks: List[Dict[str, Any]],
    ) -> None:
        """Sichert die Referenz-Vorlagenkarte im Backlog und prüft Backlog-Format.

        Zwei Aufgaben (deterministisch, kein LLM beteiligt):

        1. **Vorlagenkarte sicherstellen:** Existiert im Backlog keine Karte
           mit dem Titel ``TEMPLATE_CARD_TITLE``, wird eine mit der
           Template-Description angelegt. Der Mensch kann sie duplizieren —
           dupliziert er sie (Titel geändert), legt der nächste Poll wieder
           eine frische Vorlage an, da die alte "verbraucht" wurde.

        2. **Format-Prüfung aller Backlog-Karten:** Karten im Backlog, deren
           Description NICHT dem Task-Contract entspricht, bekommen das
           Template-Skelett — so ist sichergestellt, dass jede Backlog-Karte
           im richtigen Format liegt, bevor sie in den Workflow gezogen wird.
        """
        try:
            backlog_stacks = [
                s for s in stacks
                if is_backlog_stack(s, self._configured_board(board_id))
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
                    created = await self.client.create_card(
                        board_id,
                        backlog_stack_id,
                        title=TEMPLATE_CARD_TITLE,
                        description=template_description(self.runtime.template_language),
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
            quiet = self.runtime.backlog_format_quiet_seconds
            for card in cards:
                title = str(card.get("title") or "").strip()
                if title.lower() == TEMPLATE_CARD_TITLE.lower():
                    continue
                description = str(card.get("description") or "")

                # Aktiv bearbeitete Karten NICHT anfassen (nur wenn die
                # Ruhe-Schwelle > 0 ist): wurde die Karte erst kürzlich
                # geändert, wird von einer laufenden menschlichen Bearbeitung
                # ausgegangen. quiet_seconds == 0 deaktiviert diese Prüfung.
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

                # Nur FEHLENDE Kernabschnitte ermitteln — der vorhandene,
                # menschengeschriebene Text bleibt unangetastet.
                missing = missing_template_sections(description)

                # Zusätzlich: eine völlig leere Description bekommt das komplette
                # Skelett (hier gibt es nichts zu bewahren); sonst nur ergänzen.
                if not missing and not description.strip():
                    missing = [template_description(self.runtime.template_language).strip()]

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
                    await self.client.update_card(
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

    async def _polling_loop(self) -> None:
        # Zähler für Health-Log (Fix 3): macht einen stillen Loop-Tod sichtbar.
        poll_count = 0
        while not self._stop_event.is_set():
            try:
                await self.poll_once()
                poll_count += 1
                # Health-Log alle 20 Zyklen (~10 min bei 30s-Intervall), damit
                # ein lebender Loop beobachtbar bleibt.
                if poll_count % 20 == 0:
                    logger.info("Deck: Polling-Loop aktiv (%d Zyklen).", poll_count)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Nextcloud Deck polling failed")
                # Fix 2: Session nach Fehler zurücksetzen, damit ein einzelner
                # Timeout/Connection-Error nicht den Loop dauerhaft lähmt.
                try:
                    await self.client.reset_session()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.runtime.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    # ── User-Status-Contract (wie Talk-Plugin) ─────────────────────────
    # Der Hermes-Gateway ruft diese Methoden während eines Turns auf, sofern
    # sie existieren: mark_turn_started/finished (busy), send_or_update_status
    # (Custom-Status mit Icon), send_typing/stop_typing sind für Deck bewusst
    # No-Ops (Deck hat kein Typing-Konzept). Da Talk und Deck das Modell nie
    # gleichzeitig nutzen, ist der User-Status global eindeutig.

    async def mark_turn_started(self) -> None:
        """Presence-Marker: ein Turn ist aktiv (Referenzgezählt → busy)."""
        try:
            await self.presence_mgr.set_busy()
        except Exception as exc:
            logger.debug("Deck: set_busy fehlgeschlagen: %s", exc)

    async def mark_turn_finished(self) -> None:
        """Presence-Marker: Turn beendet — zurück auf online, wenn letzter."""
        try:
            await self.presence_mgr.clear_busy()
        except Exception as exc:
            logger.debug("Deck: clear_busy fehlgeschlagen: %s", exc)

    async def send_or_update_status(
        self,
        chat_id: str,
        status_key: str,
        content: str,
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Status-Contract: Fortschritt als Custom-Presence statt Kartentext.

        Der Karten-Bezug (Nummer + Titel) wird zusätzlich in den Status-Text
        eingefügt, damit beim Hover sichtbar ist, WELCHE Karte gerade arbeitet.
        """
        del metadata
        message, icon = self._map_progress_status(status_key, content)
        if message:
            card_label = await self._card_status_label(chat_id)
            if card_label:
                message = f"{card_label} · {message}"
            try:
                await self.presence_mgr.set_custom_status_message(message, icon)
            except Exception as exc:
                logger.debug("Deck: Custom-Status setzen fehlgeschlagen: %s", exc)
        return SendResult(success=True)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Typing-Heartbeat: aktualisiert bei aktivem Speed-Tracking den Status
        mit der Live-Geschwindigkeit (Sidecar) — Deck hat sonst kein Typing.

        Die AKTION (Liest Kontext / Antwortet) und das Icon werden aus der
        Sidecar-Phase abgeleitet, NICHT aus dem Gateway-Status-Text. So bleibt
        beim Phasenübergang prompt→generate die Anzeige konsistent (kein
        "Liest Kontext ⚡ 11.9 t/s" mehr, wenn längst generiert wird).
        """
        del metadata
        if not self.runtime.speed_enabled:
            return
        # In-Flight-Tracking: Diese Karte läuft gerade aktiv. Wird in die
        # WIP-Zählung einbezogen (siehe _count_active_cards).
        card_id = self._card_id_from_target(str(chat_id or ""))
        if card_id:
            self._active_turns.add(card_id)
        speed = await self._fetch_speed()
        if not speed:
            return
        suffix = self._speed_suffix(speed)
        try:
            # Presence auf "busy" setzen — das Gateway ruft mark_turn_started/
            # finished NICHT auf, daher ist send_typing der einzige zuverlässige
            # Lifecycle-Hook während eines Turns. Zusammen mit dem Custom-Status
            # ergibt das die sichtbare "arbeitet gerade"-Präsenz.
            await self.presence_mgr.set_presence_status("busy")
            # Karten-Label IMMER direkt aus chat_id auflösen (eigener Cache pro
            # Karte). Ein globaler _status_card_label würde bei parallelen oder
            # aufeinanderfolgenden Turns das Label der VORHERIGEN Karte zeigen.
            card_label = await self._card_status_label(chat_id)
            phase = str(speed.get("phase", "idle")).lower()
            if phase == "generate":
                action, icon = "Antwortet", "✍️"
            else:
                action, icon = "Liest Kontext", "📖"
            prefix = f"{card_label} · {action}" if card_label else action
            # Speed-Suffix nur anhängen, wenn der Sidecar eine aktive Phase
            # (prompt/generate) liefert. Bei "idle" (z.B. Stale nach 30s oder
            # Heartbeat außerhalb des kurzen prompt-Fensters) bleibt der Status
            # trotzdem auf der Aktion stehen — sonst überschreibt der
            # send_or_update_status-Callback ("Liest Kontext" ohne Speed) den
            # Status dauerhaft und die Prozent/Geschwindigkeit fehlen.
            message = f"{prefix} ⚡ {suffix}" if suffix else prefix
            await self.presence_mgr.set_custom_status_message(message, icon)
        except Exception as exc:
            logger.debug("Deck: Speed-Typing-Update fehlgeschlagen: %s", exc)

    async def stop_typing(self, chat_id: str) -> None:
        """Turn-Ende: Live-Status (Aktion + Speed) aufräumen.

        Deck hat kein Typing, aber der send_typing-Heartbeat hat einen Custom-
        Status ("Karte N · Antwortet ⚡ …") gesetzt. Der muss beim Turn-Ende
        entfernt werden, sonst klebt "Antwortet ⚡ 8.1 t/s" dauerhaft am User,
        obwohl keine Karte mehr läuft. Wir setzen den User auf "online" (busy
        war via mark_turn_started/finished referenzgezählt) und löschen den
        Custom-Status.
        """
        try:
            await self.presence_mgr.clear_custom_status_message(force=True)
            # Zurück auf "online" — das Gateway ruft mark_turn_finished nicht
            # auf, daher stellt stop_typing die Presence wieder auf "online".
            await self.presence_mgr.set_presence_status("online")
        except Exception as exc:
            logger.debug("Deck: Custom-Status beim stop_typing löschen fehlgeschlagen: %s", exc)
        finally:
            # In-Flight-Tracking: Turn dieser Karte ist beendet.
            card_id = self._card_id_from_target(str(chat_id or ""))
            if card_id:
                self._active_turns.discard(card_id)

    async def _fetch_speed(self) -> Optional[Dict[str, Any]]:
        """Liest die Live-Geschwindigkeit vom Ollama-Sidecar (über NPM-/speed).

        Kurzer Timeout + best-effort: ein toter Sidecar darf nie den Turn
        verlangsamen. Ergebnis wird kurz gecacht, damit die häufigen
        Typing-Heartbeats den Sidecar nicht fluten.
        """
        if not self.runtime.speed_url:
            return None
        try:
            if self._speed_session is None or self._speed_session.closed:
                self._speed_session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=0.4)
                )
            async with self._speed_session.get(
                self.runtime.speed_url, timeout=aiohttp.ClientTimeout(total=0.4)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                self._last_speed = data if isinstance(data, dict) else None
                return self._last_speed
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None

    @staticmethod
    def _speed_suffix(speed: Dict[str, Any]) -> str:
        """Formatiert den Sidecar-JSON in einen kompakten Geschwindigkeits-Text.

        Generate-Phase zeigt die Generation-Geschwindigkeit (``tg``, ~10-20 t/s),
        Prompt-Phase zeigt BOTH den Kontext-Fortschritt UND die Prompt-Geschwindigkeit
        (``prompt_tps``, ~100 t/s) — so ist die schnelle Token-Verarbeitung beim
        Kontext-Einlesen sichtbar.
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

    async def _card_status_label(self, chat_id: str) -> str:
        """Liefert ein kompaktes Karten-Label ('Karte 116 · Titel') für den Status.

        Der Titel wird pro Karte einmalig (best-effort) aufgelöst und gecacht,
        damit die häufigen Status-Updates keinen API-Spam erzeugen. Fällt die
        Auflösung aus, bleibt nur die Karten-Nummer.
        """
        card_id = self._card_id_from_target(str(chat_id or ""))
        if not card_id or card_id == str(chat_id or ""):
            return ""
        cache = getattr(self, "_status_card_title_cache", None)
        if cache is None:
            cache = {}
            setattr(self, "_status_card_title_cache", cache)
        title = cache.get(card_id)
        if title is None:
            title = ""
            try:
                board_id, stack_id = await self._locate_card(card_id)
                card = await self.client.get_card(board_id, stack_id, card_id)
                if card and card.get("title"):
                    title = str(card["title"]).strip()
            except Exception:
                title = ""
            # Leeren Titel als '' cachen (nicht erneut versuchen) — aber ein
            # Lookup-Fehler (exception) soll beim nächsten Mal neu versuchen.
            cache[card_id] = title
        label = f"Karte {card_id}"
        if title:
            short = title if len(title) <= 26 else title[:25].rstrip() + "…"
            label = f"Karte {card_id} · {short}"
        return label

    @staticmethod
    def _map_progress_status(status_key: str, content: str) -> tuple[Optional[str], Optional[str]]:
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


def validate_deck_config(config: PlatformConfig) -> bool:
    runtime = _build_runtime_config(config)
    return bool(
        runtime.base_url
        and runtime.username
        and runtime.app_password
        and runtime.hermes_user_id
    )


def validate_deck_config_from_env() -> bool:
    return bool(
        _env("NEXTCLOUD_DECK_BASE_URL", "NEXTCLOUD_BASE_URL")
        and _env("NEXTCLOUD_DECK_USERNAME", "NEXTCLOUD_USERNAME")
        and _env("NEXTCLOUD_DECK_APP_PASSWORD", "NEXTCLOUD_APP_PASSWORD")
    )


def env_enablement() -> Optional[Dict[str, Any]]:
    if not validate_deck_config_from_env():
        return None
    base_url = _env("NEXTCLOUD_DECK_BASE_URL", "NEXTCLOUD_BASE_URL")
    username = _env("NEXTCLOUD_DECK_USERNAME", "NEXTCLOUD_USERNAME")
    password = _env("NEXTCLOUD_DECK_APP_PASSWORD", "NEXTCLOUD_APP_PASSWORD")
    try:
        poll = float(
            _env("NEXTCLOUD_DECK_POLL_INTERVAL_SECONDS", "NEXTCLOUD_DECK_POLL_INTERVAL")
            or 30
        )
    except ValueError:
        poll = 30.0
    return {
        "base_url": base_url,
        "username": username,
        "app_password": password,
        "hermes_user_id": _env("NEXTCLOUD_DECK_HERMES_USER_ID", "NEXTCLOUD_HERMES_USER_ID") or username,
        "poll_interval_seconds": max(5.0, poll),
    }


def check_is_connected(adapter_or_config: Any) -> bool:
    """Verbindungsanzeige für Dashboard/Status.

    Wird vom Gateway mit einem PlatformConfig (noch nicht verbunden) ODER mit
    der aktiven Adapter-Instanz aufgerufen. Stehen die Credentials (wie in
    diesem Deployment) nur in der .env und nicht in der PlatformConfig, fällt
    die Prüfung auf die Env-Validierung zurück — sonst filtert das Dashboard
    die Plattform fälschlich aus der Status-Anzeige heraus.
    """
    if hasattr(adapter_or_config, "is_connected"):
        return bool(adapter_or_config.is_connected)
    if validate_deck_config(adapter_or_config):
        return True
    # Credentials kommen aus der .env (env_enablement-Pfad) — PlatformConfig
    # trägt sie nicht. Gleiche Prüfung wie beim Gateway-Start.
    return validate_deck_config_from_env()


def _build_adapter(config: PlatformConfig) -> NextcloudDeckPlatform:
    return NextcloudDeckPlatform(config)


DECK_CARD_ACTION_SCHEMA = {
    "name": "deck_card_action",
    "description": (
        "Führe eine strukturelle Aktion auf einer Nextcloud-Deck-Karte aus: "
        "Beschreibung aktualisieren, Karte in eine Zielspalte verschieben, "
        "Labels zuweisen/entfernen oder einen Benutzer zuweisen/entfernen. "
        "Nutze dieses Tool statt einen Kommentar zu schreiben, wenn du den "
        "Workflow-Vertrag erfüllen willst (Plan in die Description schreiben, "
        f"nach 'review' schieben, '{friendly_label_title('approval:required')}' setzen)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "card_id": {
                "type": "string",
                "description": "Die Karten-ID (aus dem Kontext: 'Karten-ID (card_id)').",
            },
            "target_status": {
                "type": "string",
                "description": "Zielspalte (backlog|triage|todo|ready|running|review|blocked|done).",
            },
            "description": {
                "type": "string",
                "description": "Neuer vollständiger Description-Text (Markdown, inkl. Subtasks).",
            },
            "assign_labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Labels, die der Karte zugewiesen werden sollen. Friendly-Titel oder "
                    f"kanonische Keys, z. B. ['{friendly_label_title('approval:required')}'] "
                    "oder ['approval:required']."
                ),
            },
            "remove_labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Labels, die entfernt werden sollen, z. B. "
                    f"['{friendly_label_title('phase:plan')}'] oder ['phase:plan']."
                ),
            },
            "assign_user": {
                "type": "string",
                "description": "Benutzer (UID oder Username), der der Karte zugewiesen werden soll.",
            },
            "unassign_user": {
                "type": "string",
                "description": "Benutzer (UID oder Username), der von der Karte entfernt werden soll.",
            },
            "comment": {
                "type": "string",
                "description": "Optionaler Kommentartext, der zusammen mit der Aktion auf der Karte gepostet wird.",
            },
        },
        "required": ["card_id"],
    },
}


def _make_deck_card_action_handler() -> Any:
    """Erzeugt den (async) Tool-Handler für `deck_card_action`.

    Der Handler wird mit ``is_async=True`` registriert; er reicht die
    eigentliche Aktion über die Live-Adapter-Referenz an den Gateway-Loop
    durch (aiohttp-Session-Bindung) und awaitet das Ergebnis.
    """

    async def _handle_deck_card_action(args: Dict[str, Any] | None = None, **kwargs: Any) -> str:
        args = args or {}
        adapter = _LIVE_ADAPTER_REF
        if adapter is None:
            return json.dumps({"success": False, "error": "Deck-Adapter nicht verbunden"}, ensure_ascii=False)

        card_id = str(args.get("card_id") or "").strip()
        if not card_id:
            return json.dumps({"success": False, "error": "card_id fehlt"}, ensure_ascii=False)

        target = args.get("target_status")
        description = args.get("description")
        assign_labels = args.get("assign_labels")
        remove_labels = args.get("remove_labels")
        assign_user = args.get("assign_user")
        unassign_user = args.get("unassign_user")
        comment = args.get("comment")

        metadata: Dict[str, Any] = {}
        if target:
            metadata["target_status"] = str(target)
        if description is not None:
            metadata["description"] = str(description)
        if assign_labels:
            metadata["assign_labels"] = assign_labels if isinstance(assign_labels, list) else [assign_labels]
        if remove_labels:
            metadata["remove_labels"] = remove_labels if isinstance(remove_labels, list) else [remove_labels]
        if assign_user:
            metadata["assign_user"] = assign_user
        if unassign_user:
            metadata["unassign_user"] = unassign_user

        content = str(comment) if comment else ""
        if not metadata and not content:
            return json.dumps({"success": False, "error": "Keine Aktion angegeben"}, ensure_ascii=False)

        async def _do():
            return await adapter.send(
                chat_id=f"deck:board:unknown:card:{card_id}",
                content=content,
                metadata=metadata,
            )

        try:
            result = await adapter._call_on_gateway_loop(_do)
        except Exception as exc:
            return json.dumps({"success": False, "error": f"Deck-Aktion fehlgeschlagen: {exc}"}, ensure_ascii=False)

        if result.success:
            return json.dumps({"success": True}, ensure_ascii=False)
        return json.dumps({"success": False, "error": result.error or "Deck-Aktion fehlgeschlagen"}, ensure_ascii=False)

    return _handle_deck_card_action


def _destructive_tool_hook(tool_name: str = "", args: Any = None, **kwargs: Any) -> Any:
    """pre_tool_call-Hook: Gate 3 (Block) + deck_card_action-Diagnose-Zähler.

    Liest den aktiven Deck-Karten-Kontext (ContextVar) und blockt destruktive
    Tool-Aufrufe, wenn die Karte risk:high trägt und keine Freigabe vorliegt.
    Zählt außerdem deck_card_action-Aufrufe im aktuellen Turn (ContextVar),
    damit nach dem Lauf sichtbar ist, ob der Agent strukturell aktiv war.
    """
    # deplumen Zähler unabhängig vom Kontext (deck_card_action kann auch ohne
    # Karten-Kontext aufgerufen werden, z. B. via Tool ohne gesetzten Context).
    if str(tool_name or "").strip() == "deck_card_action":
        try:
            current_deck_action_count.set(current_deck_action_count.get() + 1)
        except Exception:
            pass

    workflow_ctx = current_deck_context.get()
    if workflow_ctx is None:
        return args if args is not None else kwargs.get("request") or kwargs.get("payload") or kwargs

    patterns = compile_destructive_patterns()
    allowed, reason = check_destructive_gate(tool_name, workflow_ctx, patterns)
    if allowed:
        return args if args is not None else kwargs.get("request") or kwargs.get("payload") or kwargs

    logger.warning("Deck: %s", reason)
    raise DestructiveToolBlocked(reason)


def register(ctx: Any) -> None:
    ctx.register_platform(
        name="deck",
        label="Nextcloud Deck",
        adapter_factory=_build_adapter,
        check_fn=validate_deck_config_from_env,
        validate_config=validate_deck_config,
        is_connected=check_is_connected,
        env_enablement_fn=env_enablement,
        required_env=[
            "NEXTCLOUD_DECK_BASE_URL",
            "NEXTCLOUD_DECK_USERNAME",
            "NEXTCLOUD_DECK_APP_PASSWORD",
        ],
        max_message_length=16000,
        emoji="🎴",
    )

    # Gate 3: destruktive Tool-Aufrufe bei risk:high technisch blocken
    if hasattr(ctx, "register_hook"):
        try:
            ctx.register_hook("pre_tool_call", _destructive_tool_hook)
            logger.info("Deck: 'pre_tool_call'-Hook für Gate 3 registriert.")
        except Exception as exc:
            logger.warning("Deck: Konnte Gate-3-Hook nicht registrieren: %s", exc)

    # Card-Action-Tool: der einzige Weg, wie der Agent strukturelle
    # Karten-Aktionen (target_status/description/labels/assignee) auslösen kann,
    # da das Gateway send_message-Tool kein metadata durchreicht.
    if hasattr(ctx, "register_tool"):
        try:
            ctx.register_tool(
                name="deck_card_action",
                toolset="nextcloud-deck-platform",
                schema=DECK_CARD_ACTION_SCHEMA,
                handler=_make_deck_card_action_handler(),
                is_async=True,
            )
            logger.info("Deck: Tool 'deck_card_action' registriert.")
        except Exception as exc:
            logger.warning("Deck: Konnte 'deck_card_action' nicht registrieren: %s", exc)

    skills_dir = Path(__file__).parent / "skills"
    if skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.is_file():
                ctx.register_skill(child.name, skill_md)