from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .client import NextcloudDeckClient, NextcloudDeckError
    from .identity import DeckIdentityResolver
    from .state import DeckCardSnapshot, DeckStateManager
except ImportError:  # direct test/import
    from client import NextcloudDeckClient, NextcloudDeckError
    from identity import DeckIdentityResolver
    from state import DeckCardSnapshot, DeckStateManager

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


@dataclass
class DeckRuntimeConfig:
    base_url: str
    username: str
    app_password: str
    hermes_user_id: str
    poll_interval_seconds: float
    boards: Dict[str, Dict[str, Any]]


def _env(name: str, *fallbacks: str) -> str:
    for key in (name, *fallbacks):
        value = os.getenv(key, "").strip()
        if value:
            return value
    return ""


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

    boards: Dict[str, Dict[str, Any]] = {}
    raw_boards = extra.get("boards") or []
    if isinstance(raw_boards, list):
        for item in raw_boards:
            if not isinstance(item, dict):
                continue
            board_id = str(item.get("board_id") or item.get("id") or "").strip()
            if board_id:
                boards[board_id] = dict(item)

    return DeckRuntimeConfig(
        base_url=base_url,
        username=username,
        app_password=app_password,
        hermes_user_id=hermes_user_id,
        poll_interval_seconds=max(5.0, poll),
        boards=boards,
    )


class NextcloudDeckPlatform(BasePlatformAdapter):
    """Polling platform adapter for explicitly configured Nextcloud Deck boards."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("deck"))
        self.runtime = _build_runtime_config(config)
        self.client = NextcloudDeckClient(
            self.runtime.base_url,
            self.runtime.username,
            self.runtime.app_password,
        )
        self.identity = DeckIdentityResolver(self.runtime.hermes_user_id)
        self.state = DeckStateManager()
        self._stop_event = asyncio.Event()
        self._polling_task: Optional[asyncio.Task[None]] = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return (
            self._connected
            and not self._stop_event.is_set()
            and self._polling_task is not None
            and not self._polling_task.done()
        )

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        del is_reconnect
        self._stop_event.clear()
        await self.client.ensure_session()
        await self.client.get_boards()
        self._connected = True
        if self._polling_task is None or self._polling_task.done():
            self._polling_task = asyncio.create_task(self._polling_loop())
        return True

    async def disconnect(self) -> None:
        self._stop_event.set()
        self._connected = False
        task = self._polling_task
        self._polling_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
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
        target: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        del reply_to, metadata
        card_id = self._card_id_from_target(target)
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

    def _card_is_triggered(self, card: Dict[str, Any], comments: List[Dict[str, Any]]) -> bool:
        assigned = set(self.identity.assigned_uids(card))
        if self.runtime.hermes_user_id in assigned:
            return True

        needles = {
            self.runtime.hermes_user_id.lower(),
            self.runtime.username.lower(),
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
    def _last_comment_author(comment: Dict[str, Any]) -> Optional[str]:
        for key in ("actorId", "actor", "author", "userId"):
            value = comment.get(key)
            if isinstance(value, dict):
                value = value.get("uid") or value.get("id") or value.get("primaryKey")
            if value:
                return str(value).strip()
        return None

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

        comments = await self.client.get_card_comments(card_id)
        if not self._card_is_triggered(card, comments):
            return

        last = comments[-1] if comments else {}
        last_author = self._last_comment_author(last) if last else None
        snapshot = DeckCardSnapshot(
            board_id=board_id,
            stack_id=stack_id,
            card_id=card_id,
            title=str(card.get("title") or ""),
            description=str(card.get("description") or ""),
            assigned_users=self.identity.assigned_uids(card),
            last_comment_id=str(last.get("id")) if last.get("id") else None,
            last_author=last_author,
            due_date=str(card.get("duedate")) if card.get("duedate") else None,
            done=card.get("done"),
        )

        if not self.state.should_process(snapshot):
            return

        actor_id, groups = self.identity.resolve_card_actor(card, last_author)
        self.identity.set_contextvars_identity(actor_id, groups)

        session_key = f"deck:board:{board_id}:card:{card_id}"
        source = self.build_source(
            chat_id=session_key,
            chat_name=snapshot.title or session_key,
            chat_type="deck_card",
            user_id=actor_id,
            user_name=actor_id,
            message_id=card_id,
        )
        if isinstance(source, dict):
            source["extra_headers"] = {"X-On-Behalf-Of": actor_id}

        text = f"Nextcloud Deck Karte: {snapshot.title}\nBeschreibung:\n{snapshot.description}"
        if last and last.get("message"):
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
            },
            message_id=card_id,
            user_id=actor_id,
            user_name=actor_id,
        )
        result = self.handle_message(event)
        if asyncio.iscoroutine(result):
            await result

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
            for stack in stacks:
                for card in stack.get("cards") or []:
                    await self._process_card(board, stack, card)
                    processed += 1
        return processed

    async def _polling_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Nextcloud Deck polling failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.runtime.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass


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
    if hasattr(adapter_or_config, "is_connected"):
        return bool(adapter_or_config.is_connected)
    return validate_deck_config(adapter_or_config)


def _build_adapter(config: PlatformConfig) -> NextcloudDeckPlatform:
    return NextcloudDeckPlatform(config)


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

    skills_dir = Path(__file__).parent.resolve() / "skills"
    if hasattr(ctx, "register_skill") and skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            if not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.is_file():
                skill_md = child / "skill.md"
            if skill_md.is_file():
                ctx.register_skill(
                    f"nextcloud-deck-platform:{child.name}",
                    skill_md.resolve(),
                    "Work with the Nextcloud Deck platform adapter and its configured card workflow.",
                )