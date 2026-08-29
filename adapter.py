from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .client import NextcloudDeckClient
from .identity import DeckIdentityResolver
from .reminders import DeckReminderScheduler
from .state import DeckCardSnapshot, DeckStateManager

logger = logging.getLogger(__name__)

try:
    from gateway.config import Platform, PlatformConfig  # type: ignore
    from gateway.platforms.base import (  # type: ignore
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )
except Exception:
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

    class BasePlatformAdapter:
        def __init__(self, config: Any, platform: str = "nextcloud_deck") -> None:
            self.config = config
            self.platform = platform

        def build_source(self, **kwargs: Any) -> Dict[str, Any]:
            return kwargs

        async def handle_message(self, event: MessageEvent) -> None:
            pass

        def _mark_disconnected(self) -> None:
            pass


@dataclass
class DeckRuntimeConfig:
    base_url: str
    username: str
    app_password: str
    hermes_user_id: str
    poll_interval_seconds: float


class NextcloudDeckPlatform(BasePlatformAdapter):
    """Refactored Nextcloud Deck Platform Adapter."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("nextcloud_deck"))
        extra = getattr(config, "extra", {}) or {}

        base_url = str(
            extra.get("base_url")
            or os.getenv("NEXTCLOUD_DECK_BASE_URL")
            or os.getenv("NEXTCLOUD_BASE_URL", "")
        ).rstrip("/")
        username = str(
            extra.get("username")
            or os.getenv("NEXTCLOUD_DECK_USERNAME")
            or os.getenv("NEXTCLOUD_USERNAME", "")
        )
        app_password = str(
            extra.get("app_password")
            or getattr(config, "token", "")
            or os.getenv("NEXTCLOUD_DECK_APP_PASSWORD")
            or os.getenv("NEXTCLOUD_APP_PASSWORD", "")
        )
        hermes_user_id = str(
            extra.get("hermes_user_id")
            or os.getenv("NEXTCLOUD_DECK_HERMES_USER_ID", username)
        ).strip()

        self.runtime = DeckRuntimeConfig(
            base_url=base_url,
            username=username,
            app_password=app_password,
            hermes_user_id=hermes_user_id,
            poll_interval_seconds=float(
                extra.get("poll_interval")
                or os.getenv("NEXTCLOUD_DECK_POLL_INTERVAL")
                or os.getenv("NEXTCLOUD_DECK_POLL_INTERVAL_SECONDS", 5.0)
            ),
        )

        self.client = NextcloudDeckClient(base_url, username, app_password)
        self.identity_resolver = DeckIdentityResolver(bot_user_id=hermes_user_id)
        self.state_mgr = DeckStateManager()
        self.reminder_scheduler = DeckReminderScheduler(self.client)

        self._stop_event = asyncio.Event()
        self._polling_task: Optional[asyncio.Task[None]] = None

    @property
    def is_connected(self) -> bool:
        return not self._stop_event.is_set()

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._stop_event.clear()
        await self.client.ensure_session()
        self._polling_task = asyncio.create_task(self._polling_loop())
        logger.info("Nextcloud Deck: Platform Adapter verbunden.")
        return True

    async def disconnect(self) -> None:
        self._stop_event.set()
        if self._polling_task:
            self._polling_task.cancel()
        await self.client.close()
        self._mark_disconnected()

    async def send(
        self,
        target: str,
        text: str,
        reply_to_message_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Pflichtmethode von BasePlatformAdapter: Sendet Kommentare an Deck-Karten."""
        return await self.send_message(target, text, reply_to_message_id, metadata)

    async def send_message(
        self,
        target: str,
        text: str,
        reply_to_message_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        card_id = target
        if ":" in target:
            parts = target.split(":")
            if "card" in parts:
                card_id = parts[parts.index("card") + 1]

        res = await self.client.add_comment(card_id, text)
        if res:
            comment_id = str(res.get("id", "")) if isinstance(res, dict) else None
            return SendResult(success=True, message_id=comment_id)
        return SendResult(success=False, error="Failed to post comment to Deck card")

    async def _polling_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                boards = await self.client.get_boards()
                for board in boards:
                    board_id = board.get("id")
                    if board_id:
                        stacks = await self.client.get_stacks(board_id)
                        for stack in stacks:
                            cards = stack.get("cards", [])
                            for card in cards:
                                await self._process_card(board_id, stack.get("id"), card)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Nextcloud Deck Polling Fehler: %s", exc)
            await asyncio.sleep(self.runtime.poll_interval_seconds)

    async def _process_card(self, board_id: Any, stack_id: Any, card: Dict[str, Any]) -> None:
        card_id = str(card.get("id", ""))
        if not card_id:
            return

        # Kommentare via REST API abrufen
        comments = await self.client.get_card_comments(card_id)
        last_comment = comments[-1] if comments else {}
        last_comment_id = str(last_comment.get("id", "")) if last_comment else None
        last_author = str(last_comment.get("author") or last_comment.get("actorId") or "") or None

        assignees = card.get("assignedUsers") or card.get("assignees") or []
        assigned_uids = []
        if isinstance(assignees, list):
            for a in assignees:
                uid = a.get("uid") if isinstance(a, dict) else str(a)
                if uid:
                    assigned_uids.append(str(uid))

        snapshot = DeckCardSnapshot(
            board_id=str(board_id),
            stack_id=str(stack_id),
            card_id=card_id,
            title=str(card.get("title", "")),
            description=str(card.get("description", "")),
            assigned_users=assigned_uids,
            last_comment_id=last_comment_id,
            last_author=last_author,
        )

        # Polling-Dedup: Überspringen, falls keine Änderung vorliegt
        if not self.state_mgr.should_process(snapshot):
            return

        actor_id, groups = self.identity_resolver.resolve_card_actor(
            card, comment_author=last_author
        )
        self.identity_resolver.set_contextvars_identity(actor_id, groups)

        session_key = f"deck:board:{board_id}:card:{card_id}"

        source = self.build_source(
            chat_id=session_key,
            chat_name=card.get("title", ""),
            chat_type="deck_card",
            user_id=actor_id,
            user_name=actor_id,
        )
        if isinstance(source, dict):
            source["extra_headers"] = {
                "X-On-Behalf-Of": actor_id,
            }

        msg_event = MessageEvent(
            text=card.get("description", "") or card.get("title", ""),
            message_type=MessageType.TEXT,
            source=source,
            raw_message=card,
            message_id=card_id,
            user_id=actor_id,
            user_name=actor_id,
        )
        await self.handle_message(msg_event)


def validate_deck_config(config: PlatformConfig) -> bool:
    extra = getattr(config, "extra", {}) or {}
    base_url = (
        extra.get("base_url")
        or os.getenv("NEXTCLOUD_DECK_BASE_URL")
        or os.getenv("NEXTCLOUD_BASE_URL", "")
    )
    username = (
        extra.get("username")
        or os.getenv("NEXTCLOUD_DECK_USERNAME")
        or os.getenv("NEXTCLOUD_USERNAME", "")
    )
    app_password = (
        extra.get("app_password")
        or getattr(config, "token", "")
        or os.getenv("NEXTCLOUD_DECK_APP_PASSWORD")
        or os.getenv("NEXTCLOUD_APP_PASSWORD", "")
    )
    return bool(str(base_url).strip() and str(username).strip() and str(app_password).strip())


def validate_deck_config_from_env() -> bool:
    base_url = os.getenv("NEXTCLOUD_DECK_BASE_URL") or os.getenv("NEXTCLOUD_BASE_URL", "")
    username = os.getenv("NEXTCLOUD_DECK_USERNAME") or os.getenv("NEXTCLOUD_USERNAME", "")
    app_password = os.getenv("NEXTCLOUD_DECK_APP_PASSWORD") or os.getenv("NEXTCLOUD_APP_PASSWORD", "")
    return bool(str(base_url).strip() and str(username).strip() and str(app_password).strip())


def check_is_connected(adapter_or_config: Any) -> bool:
    if hasattr(adapter_or_config, "is_connected"):
        return bool(adapter_or_config.is_connected)
    return validate_deck_config(adapter_or_config)


def _build_adapter(config: PlatformConfig) -> NextcloudDeckPlatform:
    return NextcloudDeckPlatform(config)


def register(ctx: Any) -> None:
    """Hermes Platform Plugin Registration Entrypoint."""
    ctx.register_platform(
        name="nextcloud_deck",
        label="Nextcloud Deck",
        adapter_factory=_build_adapter,
        check_fn=validate_deck_config_from_env,
        validate_config=validate_deck_config,
        is_connected=check_is_connected,
        env_enablement_fn=validate_deck_config_from_env,
        required_env=[
            "NEXTCLOUD_DECK_BASE_URL",
            "NEXTCLOUD_DECK_USERNAME",
            "NEXTCLOUD_DECK_APP_PASSWORD",
        ],
        max_message_length=16000,
        emoji="🎴",
    )

    # Skills registrieren (pathlib.Path übergibt Objekt, vermeidet AttributeError)
    skills_dir = Path(__file__).parent / "skills"
    if skills_dir.exists() and hasattr(ctx, "register_skill"):
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.exists():
                try:
                    ctx.register_skill(child.name, skill_md)
                    logger.info("Skill '%s' registriert aus %s", child.name, skill_md)
                except Exception as exc:
                    logger.warning("Fehler beim Registrieren von Skill '%s': %s", child.name, exc)