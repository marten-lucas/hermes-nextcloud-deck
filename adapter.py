from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

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

    class MessageType:  # pragma: no cover - local fallback
        TEXT = "text"

    @dataclass
    class SendResult:  # pragma: no cover - local fallback
        success: bool
        message_id: Optional[str] = None
        error: Optional[str] = None

    @dataclass
    class MessageEvent:  # pragma: no cover - local fallback
        text: str
        message_type: str
        source: Any
        raw_message: Dict[str, Any]
        message_id: Optional[str] = None
        reply_to_message_id: Optional[str] = None
        user_id: Optional[str] = None
        user_name: Optional[str] = None

    class BasePlatformAdapter:  # pragma: no cover - local fallback
        def __init__(self, config: Any, platform: str = "nextcloud_deck") -> None:
            self.config = config
            self.platform = platform

        def build_source(self, **kwargs: Any) -> Dict[str, Any]:
            return kwargs

        async def handle_message(self, event: MessageEvent) -> None:
            return None

        def _mark_disconnected(self) -> None:
            return None


@dataclass
class DeckRuntimeConfig:
    base_url: str
    username: str
    app_password: str
    hermes_user_id: str
    poll_interval_seconds: float = 30.0
    board_stack_mapping: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    observed_boards: Dict[str, Dict[str, Any]] = field(default_factory=dict)


class NextcloudDeckPlatform(BasePlatformAdapter):
    """Minimal Phase 1 Nextcloud Deck adapter using polling transport."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("nextcloud_deck"))
        self.runtime = self._build_runtime_config(config)
        self._session: Optional[aiohttp.ClientSession] = None
        self._stop_event = asyncio.Event()
        self._polling_task: Optional[asyncio.Task[None]] = None
        self._discovered_boards: Dict[str, Dict[str, Any]] = {}
        self._card_snapshots: Dict[str, str] = {}

    @staticmethod
    def _as_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _build_runtime_config(self, config: PlatformConfig) -> DeckRuntimeConfig:
        extra = getattr(config, "extra", {}) or {}
        board_stack_mapping = extra.get("board_stack_mapping") or {}
        if not isinstance(board_stack_mapping, dict):
            board_stack_mapping = {}
        observed_boards: Dict[str, Dict[str, Any]] = {}
        raw_boards = extra.get("boards") or []
        if isinstance(raw_boards, list):
            for entry in raw_boards:
                if not isinstance(entry, dict):
                    continue
                board_id = str(entry.get("board_id", "")).strip()
                if not board_id:
                    continue
                observed_boards[board_id] = dict(entry)
                stack_mapping = entry.get("stack_mapping")
                if isinstance(stack_mapping, dict):
                    board_stack_mapping[board_id] = stack_mapping
        return DeckRuntimeConfig(
            base_url=str(extra.get("base_url") or os.getenv("NEXTCLOUD_BASE_URL", "")).rstrip("/"),
            username=str(extra.get("username") or os.getenv("NEXTCLOUD_USERNAME", "")).strip(),
            app_password=str(
                extra.get("app_password")
                or getattr(config, "token", "")
                or os.getenv("NEXTCLOUD_APP_PASSWORD", "")
            ).strip(),
            hermes_user_id=str(
                extra.get("hermes_user_id") or os.getenv("NEXTCLOUD_DECK_HERMES_USER_ID", "")
            ).strip(),
            poll_interval_seconds=max(
                5.0,
                self._as_float(
                    extra.get("poll_interval_seconds")
                    or os.getenv("NEXTCLOUD_DECK_POLL_INTERVAL_SECONDS"),
                    30.0,
                ),
            ),
            board_stack_mapping=board_stack_mapping,
            observed_boards=observed_boards,
        )

    def _authorization_header(self) -> str:
        return aiohttp.encode_basic_auth(self.runtime.username, self.runtime.app_password)

    def _api_headers(self) -> Dict[str, str]:
        return {
            "Authorization": self._authorization_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "OCS-APIRequest": "true",
        }

    def _deck_url(self, path: str) -> str:
        normalized = path.lstrip("/")
        return f"{self.runtime.base_url}/index.php/apps/deck/api/v1.0/{normalized}"

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _api_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        session = await self._ensure_session()
        async with session.get(self._deck_url(path), params=params, headers=self._api_headers()) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(f"Nextcloud Deck request failed for {path}: {resp.status} {body}")
            return body

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        del is_reconnect
        self._stop_event.clear()
        await self.poll_once()
        self._start_polling_loop()
        return True

    async def disconnect(self) -> None:
        self._stop_event.set()
        if self._polling_task is not None:
            self._polling_task.cancel()
            await asyncio.gather(self._polling_task, return_exceptions=True)
            self._polling_task = None
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        self._mark_disconnected()

    def _start_polling_loop(self) -> None:
        if self._polling_task is not None and not self._polling_task.done():
            return
        self._polling_task = asyncio.create_task(self._polling_loop())

    async def _polling_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Nextcloud Deck polling error: %s", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.runtime.poll_interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def fetch_boards_once(self) -> List[Dict[str, Any]]:
        data = await self._api_get("boards")
        if not isinstance(data, list):
            raise RuntimeError(f"Expected Deck boards list, got {type(data).__name__}")
        discovered: Dict[str, Dict[str, Any]] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            board_id = str(item.get("id", "")).strip()
            if not board_id:
                continue
            discovered[board_id] = item
        self._discovered_boards = discovered
        logger.info("Nextcloud Deck discovered %d board(s)", len(discovered))
        return list(discovered.values())

    @property
    def discovered_boards(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._discovered_boards)

    async def poll_once(self) -> List[MessageEvent]:
        boards = await self.fetch_boards_once()
        if not self.runtime.observed_boards:
            return []
        events: List[MessageEvent] = []
        for board in boards:
            board_id = str(board.get("id", "")).strip()
            if board_id not in self.runtime.observed_boards:
                continue
            events.extend(await self._ingest_board_once(board))
        return events

    async def _ingest_board_once(self, board: Dict[str, Any]) -> List[MessageEvent]:
        board_id = str(board.get("id", "")).strip()
        stacks = await self._api_get(f"boards/{board_id}/stacks")
        if not isinstance(stacks, list):
            raise RuntimeError(f"Expected stacks list for board {board_id}, got {type(stacks).__name__}")
        emitted: List[MessageEvent] = []
        for stack in stacks:
            if not isinstance(stack, dict):
                continue
            for card in stack.get("cards") or []:
                if not isinstance(card, dict):
                    continue
                if not self._card_assigned_to_hermes(card):
                    continue
                payload = await self._build_card_payload(board, stack, card)
                event = self._maybe_build_card_event(payload)
                if event is None:
                    continue
                await self.handle_message(event)
                emitted.append(event)
        return emitted

    async def _build_card_payload(
        self,
        board: Dict[str, Any],
        stack: Dict[str, Any],
        card: Dict[str, Any],
    ) -> Dict[str, Any]:
        card_id = str(card.get("id", "")).strip()
        comments = await self._api_get(f"cards/{card_id}/comments")
        if not isinstance(comments, list):
            comments = []
        description = str(card.get("description") or "")
        payload = {
            "board": {
                "id": str(board.get("id", "")).strip(),
                "title": str(board.get("title") or ""),
            },
            "stack": {
                "id": str(stack.get("id", "")).strip(),
                "title": str(stack.get("title") or ""),
            },
            "card": {
                "id": card_id,
                "title": str(card.get("title") or ""),
                "description": description,
                "due_date": card.get("duedate"),
                "assigned_users": self._normalize_assigned_users(card.get("assignedUsers")),
                "checklist_items": self._extract_checklist_items(description),
                "comments": self._normalize_comments(comments),
            },
        }
        return payload

    def _maybe_build_card_event(self, payload: Dict[str, Any]) -> Optional[MessageEvent]:
        signature = self._payload_signature(payload)
        work_item_id = self._work_item_id(payload)
        if self._card_snapshots.get(work_item_id) == signature:
            return None
        self._card_snapshots[work_item_id] = signature
        source = self.build_source(
            chat_id=work_item_id,
            chat_name=payload["card"]["title"] or work_item_id,
            chat_type="work_item",
            user_id=self.runtime.hermes_user_id,
            user_name=self.runtime.hermes_user_id,
            message_id=payload["card"]["id"] or None,
        )
        text = self._format_card_prompt(payload)
        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=payload,
            message_id=payload["card"]["id"] or None,
        )

    @staticmethod
    def _payload_signature(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, sort_keys=True, ensure_ascii=True)

    @staticmethod
    def _work_item_id(payload: Dict[str, Any]) -> str:
        return f"deck:board:{payload['board']['id']}:card:{payload['card']['id']}"

    def _card_assigned_to_hermes(self, card: Dict[str, Any]) -> bool:
        for user in self._normalize_assigned_users(card.get("assignedUsers")):
            if user.get("id") == self.runtime.hermes_user_id:
                return True
        return False

    @staticmethod
    def _normalize_assigned_users(raw_users: Any) -> List[Dict[str, str]]:
        if not isinstance(raw_users, list):
            return []
        normalized: List[Dict[str, str]] = []
        for entry in raw_users:
            if not isinstance(entry, dict):
                continue
            user_id = (
                entry.get("id")
                or entry.get("uid")
                or entry.get("primaryKey")
                or entry.get("participant", {}).get("uid")
            )
            display_name = (
                entry.get("displayname")
                or entry.get("displayName")
                or entry.get("name")
                or entry.get("participant", {}).get("displayname")
                or user_id
            )
            user_id_text = str(user_id or "").strip()
            if not user_id_text:
                continue
            normalized.append({"id": user_id_text, "name": str(display_name or user_id_text)})
        return normalized

    @staticmethod
    def _normalize_comments(raw_comments: Any) -> List[Dict[str, str]]:
        if not isinstance(raw_comments, list):
            return []
        normalized: List[Dict[str, str]] = []
        for entry in raw_comments:
            if not isinstance(entry, dict):
                continue
            author = entry.get("actor") or entry.get("author") or {}
            if not isinstance(author, dict):
                author = {}
            normalized.append(
                {
                    "id": str(entry.get("id", "")).strip(),
                    "message": str(entry.get("message") or ""),
                    "author_id": str(
                        author.get("uid") or author.get("id") or author.get("primaryKey") or ""
                    ).strip(),
                    "author_name": str(
                        author.get("displayname") or author.get("displayName") or author.get("uid") or ""
                    ).strip(),
                    "created_at": str(entry.get("createdAt") or entry.get("creationDateTime") or ""),
                }
            )
        return normalized

    @staticmethod
    def _extract_checklist_items(description: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for line in str(description or "").splitlines():
            match = re.match(r"^\s*[-*]\s+\[([ xX])\]\s+(.*)$", line)
            if not match:
                continue
            items.append(
                {
                    "checked": match.group(1).lower() == "x",
                    "text": match.group(2).strip(),
                }
            )
        return items

    @staticmethod
    def _format_card_prompt(payload: Dict[str, Any]) -> str:
        checklist = payload["card"]["checklist_items"]
        checklist_text = (
            "\n".join(
                f"- [{'x' if item['checked'] else ' '}] {item['text']}"
                for item in checklist
            )
            if checklist
            else "(none)"
        )
        comments = payload["card"]["comments"]
        comments_text = (
            "\n".join(
                f"- {comment['author_name'] or comment['author_id'] or 'unknown'}: {comment['message']}"
                for comment in comments
                if comment["message"]
            )
            if comments
            else "(none)"
        )
        return "\n".join(
            [
                f"Nextcloud Deck work item from board '{payload['board']['title']}' in stack '{payload['stack']['title']}'.",
                f"Card title: {payload['card']['title']}",
                "Description:",
                payload["card"]["description"] or "(empty)",
                "Checklist:",
                checklist_text,
                "Comments:",
                comments_text,
            ]
        )

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        del chat_id, content, reply_to, metadata
        return SendResult(
            success=False,
            error="Nextcloud Deck outbound send is not implemented in phase 1; use later writeback phases.",
        )

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "work_item"}


def nextcloud_deck_deps_present() -> bool:
    return True


def validate_nextcloud_deck_config(config: PlatformConfig) -> bool:
    extra = getattr(config, "extra", {}) or {}
    base_url = extra.get("base_url") or os.getenv("NEXTCLOUD_BASE_URL", "")
    username = extra.get("username") or os.getenv("NEXTCLOUD_USERNAME", "")
    token = extra.get("app_password") or getattr(config, "token", "") or os.getenv("NEXTCLOUD_APP_PASSWORD", "")
    hermes_user_id = extra.get("hermes_user_id") or os.getenv("NEXTCLOUD_DECK_HERMES_USER_ID", "")
    return bool(
        str(base_url).strip()
        and str(username).strip()
        and str(token).strip()
        and str(hermes_user_id).strip()
    )


def env_enablement() -> dict | None:
    base_url = os.getenv("NEXTCLOUD_BASE_URL", "").strip()
    username = os.getenv("NEXTCLOUD_USERNAME", "").strip()
    app_password = os.getenv("NEXTCLOUD_APP_PASSWORD", "").strip()
    hermes_user_id = os.getenv("NEXTCLOUD_DECK_HERMES_USER_ID", "").strip()
    if not (base_url and username and app_password and hermes_user_id):
        return None
    seed = {
        "base_url": base_url,
        "username": username,
        "app_password": app_password,
        "hermes_user_id": hermes_user_id,
    }
    poll_interval = os.getenv("NEXTCLOUD_DECK_POLL_INTERVAL_SECONDS", "").strip()
    if poll_interval:
        seed["poll_interval_seconds"] = poll_interval
    return seed


def apply_yaml_config(yaml_cfg: dict, platform_cfg: dict) -> dict | None:
    del yaml_cfg
    env_map = {
        "base_url": "NEXTCLOUD_BASE_URL",
        "username": "NEXTCLOUD_USERNAME",
        "app_password": "NEXTCLOUD_APP_PASSWORD",
        "hermes_user_id": "NEXTCLOUD_DECK_HERMES_USER_ID",
        "poll_interval_seconds": "NEXTCLOUD_DECK_POLL_INTERVAL_SECONDS",
    }
    for key, env_var in env_map.items():
        if key in platform_cfg and not os.getenv(env_var):
            os.environ[env_var] = str(platform_cfg[key])
    extras: Dict[str, Any] = {}
    if "board_stack_mapping" in platform_cfg:
        extras["board_stack_mapping"] = platform_cfg["board_stack_mapping"]
    if "boards" in platform_cfg:
        extras["boards"] = platform_cfg["boards"]
    return extras or None


def _build_adapter(config: PlatformConfig) -> NextcloudDeckPlatform:
    return NextcloudDeckPlatform(config)


def register(ctx: Any) -> None:
    """Hermes plugin entry point."""
    ctx.register_platform(
        name="nextcloud_deck",
        label="Nextcloud Deck",
        adapter_factory=_build_adapter,
        check_fn=nextcloud_deck_deps_present,
        validate_config=validate_nextcloud_deck_config,
        is_connected=validate_nextcloud_deck_config,
        required_env=[
            "NEXTCLOUD_BASE_URL",
            "NEXTCLOUD_USERNAME",
            "NEXTCLOUD_APP_PASSWORD",
            "NEXTCLOUD_DECK_HERMES_USER_ID",
        ],
        install_hint="pip install aiohttp",
        env_enablement_fn=env_enablement,
        apply_yaml_config_fn=apply_yaml_config,
        platform_hint=(
            "You are processing Nextcloud Deck work items. "
            "Cards assigned to the Hermes Deck user are the relevant work items."
        ),
        max_message_length=16000,
        emoji="🗂️",
        allow_update_command=False,
    )
