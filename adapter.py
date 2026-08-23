from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

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


@dataclass
class ReminderState:
    work_item_id: str
    board_id: str
    card_id: str
    talk_room_id: str
    patience: str
    due_at: float
    reason: str
    sent_count: int = 0


class NextcloudDeckPlatform(BasePlatformAdapter):
    """Nextcloud Deck adapter using polling transport and Hermes integration."""

    API_VERSION = "v1.0"

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("nextcloud_deck"))
        self.runtime = self._build_runtime_config(config)
        self._session: Optional[aiohttp.ClientSession] = None
        self._stop_event = asyncio.Event()
        self._polling_task: Optional[asyncio.Task[None]] = None
        self._discovered_boards: Dict[str, Dict[str, Any]] = {}
        self._board_stack_index: Dict[str, Dict[str, Dict[str, str]]] = {}
        self._card_snapshots: Dict[str, str] = {}
        self._work_item_index: Dict[str, Dict[str, Any]] = {}
        self._pending_reminders: Dict[str, ReminderState] = {}
        self._talk_sender: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None
        self._time_fn: Callable[[], float] = time.time

    @property
    def is_connected(self) -> bool:
        """Dynamic check whether the adapter has an active session and task running."""
        return bool(
            self._session
            and not self._session.closed
            and not self._stop_event.is_set()
            and (self._polling_task is not None and not self._polling_task.done())
        )

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

        # Clean base_url trimming trailing slashes and /index.php duplicates
        raw_base_url = str(extra.get("base_url") or os.getenv("NEXTCLOUD_BASE_URL", "")).rstrip("/")
        if raw_base_url.endswith("/index.php"):
            raw_base_url = raw_base_url[:-10].rstrip("/")

        return DeckRuntimeConfig(
            base_url=raw_base_url,
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
        return f"{self.runtime.base_url}/index.php/apps/deck/api/{self.API_VERSION}/{normalized}"

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _api_request(
        self,
        method: str,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        session = await self._ensure_session()
        req_fn = getattr(session, method.lower())
        async with req_fn(self._deck_url(path), json=data, params=params, headers=self._api_headers()) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if resp.status >= 400:
                raw_text = await resp.text()
                raise RuntimeError(
                    f"Nextcloud Deck request failed [{resp.status}] for {path}: {raw_text[:300]}"
                )
            if "application/json" in content_type:
                body = await resp.json(content_type=None)
            else:
                body = await resp.text()
            return self._unwrap_ocs(body)

    async def _api_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return await self._api_request("get", path, params=params)

    async def _api_post(self, path: str, data: Dict[str, Any]) -> Any:
        return await self._api_request("post", path, data=data)

    async def _api_put(self, path: str, data: Dict[str, Any]) -> Any:
        return await self._api_request("put", path, data=data)

    @staticmethod
    def _unwrap_ocs(body: Any) -> Any:
        if isinstance(body, dict) and isinstance(body.get("ocs"), dict):
            return body["ocs"].get("data")
        return body

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        del is_reconnect
        self._stop_event.clear()
        await self._ensure_session()
        await self.poll_once()
        self._start_polling_loop()
        logger.info(
            "Nextcloud Deck: Adapter connected and polling active (Interval: %ss)",
            self.runtime.poll_interval_seconds,
        )
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
        logger.info("Nextcloud Deck: Adapter disconnected cleanly")

    async def start(self) -> None:
        """Gateway lifecycle alias for connect."""
        await self.connect()

    async def stop(self) -> None:
        """Gateway lifecycle alias for disconnect."""
        await self.disconnect()

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
        logger.debug("Nextcloud Deck discovered %d board(s)", len(discovered))
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
        await self._process_due_reminders()
        return events

    async def _ingest_board_once(self, board: Dict[str, Any]) -> List[MessageEvent]:
        board_id = str(board.get("id", "")).strip()
        stacks = await self._api_get(f"boards/{board_id}/stacks")
        if not isinstance(stacks, list):
            raise RuntimeError(f"Expected stacks list for board {board_id}, got {type(stacks).__name__}")
        self._board_stack_index[board_id] = self._index_board_stacks(stacks)
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
        work_item_id = self._work_item_id(payload)
        board_config = self.runtime.observed_boards.get(payload["board"]["id"], {})
        previous = self._work_item_index.get(work_item_id, {})
        self._work_item_index[work_item_id] = {
            "board_id": payload["board"]["id"],
            "board_title": payload["board"]["title"],
            "stack_id": payload["stack"]["id"],
            "stack_title": payload["stack"]["title"],
            "card_id": payload["card"]["id"],
            "card_title": payload["card"]["title"],
            "description": payload["card"]["description"],
            "due_date": payload["card"]["due_date"],
            "payload": payload,
            "board_config": board_config,
        }
        if self._pending_reminders.get(work_item_id) and self._is_human_response(previous.get("payload"), payload):
            self._pending_reminders.pop(work_item_id, None)
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
                or entry.get("userId")
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
    def _merge_checklist_into_description(
        description: str,
        checklist_items: List[Dict[str, Any]],
    ) -> str:
        lines = str(description or "").splitlines()
        new_lines = [
            f"- [{'x' if bool(item.get('checked')) else ' '}] {str(item.get('text') or '').strip()}".rstrip()
            for item in checklist_items
            if str(item.get("text") or "").strip()
        ]
        checklist_indexes = [
            index
            for index, line in enumerate(lines)
            if re.match(r"^\s*[-*]\s+\[([ xX])\]\s+(.*)$", line)
        ]
        if not checklist_indexes:
            if not new_lines:
                return str(description or "")
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend(new_lines)
            return "\n".join(lines)
        first = checklist_indexes[0]
        last = checklist_indexes[-1]
        rebuilt = lines[:first] + new_lines + lines[last + 1 :]
        return "\n".join(rebuilt).strip("\n")

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
        work_item = self._work_item_index.get(str(chat_id))
        if not work_item:
            return SendResult(success=False, error=f"Unknown Nextcloud Deck work item: {chat_id}")
        operations_run = False
        metadata = metadata or {}
        checklist_items = metadata.get("checklist_items")
        if checklist_items is not None:
            await self.update_card_checklist(chat_id, checklist_items)
            operations_run = True
        target_status = metadata.get("target_status")
        if target_status:
            await self.move_card_to_status(chat_id, str(target_status))
            operations_run = True
        if metadata.get("await_human_response"):
            self._schedule_talk_reminder(
                chat_id,
                reason=str(metadata.get("reminder_reason") or "Awaiting human response on Deck work item."),
            )
        if content:
            comment = await self._api_post(
                f"cards/{work_item['card_id']}/comments",
                {"message": content[:1000], "parentId": reply_to},
            )
            self._record_local_comment(work_item, comment, str(content[:1000]))
            operations_run = True
            message_id = None
            if isinstance(comment, dict):
                message_id = str(comment.get("id", "") or "") or None
            return SendResult(success=True, message_id=message_id)
        if operations_run:
            return SendResult(success=True)
        return SendResult(success=False, error="No Deck writeback operation requested.")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "work_item"}

    async def move_card_to_status(self, work_item_id: str, status_key: str) -> None:
        work_item = self._require_work_item(work_item_id)
        board_id = work_item["board_id"]
        current_stack_id = work_item["stack_id"]
        target_stack_id = self._resolve_target_stack_id(board_id, status_key)
        if target_stack_id is None:
            raise RuntimeError(f"No stack mapping for status '{status_key}' on board {board_id}")
        if target_stack_id == current_stack_id:
            return
        await self._api_put(
            f"boards/{board_id}/stacks/{current_stack_id}/cards/{work_item['card_id']}/reorder",
            {"order": 0, "stackId": int(target_stack_id) if str(target_stack_id).isdigit() else target_stack_id},
        )
        stack_info = self._resolve_stack_info(board_id, target_stack_id)
        work_item["stack_id"] = target_stack_id
        work_item["stack_title"] = stack_info.get("title", work_item["stack_title"])
        payload = work_item.get("payload") or {}
        if isinstance(payload, dict):
            payload.setdefault("stack", {})
            payload["stack"]["id"] = target_stack_id
            payload["stack"]["title"] = work_item["stack_title"]
            self._card_snapshots[work_item_id] = self._payload_signature(payload)

    async def update_card_checklist(self, work_item_id: str, checklist_items: List[Dict[str, Any]]) -> None:
        work_item = self._require_work_item(work_item_id)
        rendered_description = self._merge_checklist_into_description(
            work_item.get("description", ""),
            checklist_items,
        )
        await self._api_put(
            f"boards/{work_item['board_id']}/stacks/{work_item['stack_id']}/cards/{work_item['card_id']}",
            {
                "title": work_item["card_title"],
                "description": rendered_description,
                "type": "plain",
                "order": 999,
                "duedate": work_item.get("due_date"),
            },
        )
        work_item["description"] = rendered_description
        payload = work_item.get("payload") or {}
        if isinstance(payload, dict):
            payload.setdefault("card", {})
            payload["card"]["description"] = rendered_description
            payload["card"]["checklist_items"] = self._extract_checklist_items(rendered_description)
            self._card_snapshots[work_item_id] = self._payload_signature(payload)

    def _schedule_talk_reminder(self, work_item_id: str, *, reason: str) -> None:
        work_item = self._require_work_item(work_item_id)
        board_config = work_item.get("board_config") or {}
        if not self._is_talk_reminder_enabled(board_config):
            return
        talk_room_id = str(board_config.get("talk_room_id") or "").strip()
        if not talk_room_id:
            return
        patience = str(board_config.get("patience") or "medium").strip().lower()
        due_at = self._time_fn() + self._reminder_delay_seconds(patience)
        self._pending_reminders[work_item_id] = ReminderState(
            work_item_id=work_item_id,
            board_id=work_item["board_id"],
            card_id=work_item["card_id"],
            talk_room_id=talk_room_id,
            patience=patience,
            due_at=due_at,
            reason=reason,
        )

    async def _process_due_reminders(self) -> None:
        now = self._time_fn()
        due = [state for state in self._pending_reminders.values() if state.due_at <= now and state.sent_count == 0]
        for state in due:
            ok = await self._send_talk_reminder(state)
            if ok:
                state.sent_count += 1

    async def _send_talk_reminder(self, state: ReminderState) -> bool:
        work_item = self._work_item_index.get(state.work_item_id)
        if not work_item:
            return False
        sender = self._resolve_talk_sender()
        if sender is None:
            logger.warning("Nextcloud Deck talk reminder skipped: Nextcloud Talk sender unavailable")
            return False
        content = (
            f"Erinnerung zu Deck-Karte '{work_item['card_title']}' "
            f"(Board: {work_item['board_title']}): {state.reason}"
        )
        result = await sender(
            platform="nextcloud",
            chat_id=state.talk_room_id,
            content=content,
        )
        return bool(result.get("success"))

    def _resolve_talk_sender(self) -> Optional[Callable[..., Awaitable[Dict[str, Any]]]]:
        if self._talk_sender is not None:
            return self._talk_sender
        try:
            from tools.send_message_tool import send_message as send_message_tool  # type: ignore
        except Exception:
            return None

        async def _sender(*, platform: str, chat_id: str, content: str, **kwargs: Any) -> Dict[str, Any]:
            result = send_message_tool(platform=platform, chat_id=chat_id, content=content, **kwargs)
            if asyncio.iscoroutine(result):
                return await result
            return result

        self._talk_sender = _sender
        return self._talk_sender

    @staticmethod
    def _is_talk_reminder_enabled(board_config: Dict[str, Any]) -> bool:
        return str(board_config.get("reminder_via_talk", "")).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _reminder_delay_seconds(patience: str) -> float:
        normalized = str(patience or "").strip().lower()
        if normalized == "low":
            return 300.0
        if normalized == "high":
            return 7200.0
        return 1800.0

    def _record_local_comment(self, work_item: Dict[str, Any], comment: Any, fallback_message: str) -> None:
        payload = work_item.get("payload") or {
            "board": {"id": work_item["board_id"], "title": work_item["board_title"]},
            "stack": {"id": work_item["stack_id"], "title": work_item["stack_title"]},
            "card": {
                "id": work_item["card_id"],
                "title": work_item["card_title"],
                "description": work_item.get("description", ""),
                "due_date": work_item.get("due_date"),
                "assigned_users": [],
                "checklist_items": self._extract_checklist_items(work_item.get("description", "")),
                "comments": [],
            },
        }
        if not isinstance(payload, dict):
            return
        work_item["payload"] = payload
        payload.setdefault("card", {})
        card_payload = payload["card"]
        comments = list(card_payload.get("comments") or [])
        if isinstance(comment, dict):
            comments.append(
                {
                    "id": str(comment.get("id", "")).strip(),
                    "message": str(comment.get("message") or fallback_message),
                    "author_id": self.runtime.username,
                    "author_name": self.runtime.username,
                    "created_at": str(comment.get("creationDateTime") or ""),
                }
            )
        else:
            comments.append(
                {
                    "id": "",
                    "message": fallback_message,
                    "author_id": self.runtime.username,
                    "author_name": self.runtime.username,
                    "created_at": "",
                }
            )
        card_payload["comments"] = comments
        self._card_snapshots[self._work_item_id(payload)] = self._payload_signature(payload)

    def _is_human_response(self, previous_payload: Any, current_payload: Dict[str, Any]) -> bool:
        if not isinstance(previous_payload, dict):
            return False
        if self._payload_signature(previous_payload) == self._payload_signature(current_payload):
            return False
        previous_card = previous_payload.get("card") or {}
        current_card = current_payload.get("card") or {}
        previous_stack = previous_payload.get("stack") or {}
        current_stack = current_payload.get("stack") or {}
        previous_comments = previous_card.get("comments") or []
        current_comments = current_card.get("comments") or []
        if len(current_comments) > len(previous_comments):
            latest = current_comments[-1] if current_comments else {}
            author_id = str(latest.get("author_id") or "").strip()
            if author_id and author_id not in {self.runtime.username, self.runtime.hermes_user_id}:
                return True
        if str(previous_stack.get("id") or "") != str(current_stack.get("id") or ""):
            return True
        for key in ("description", "title", "due_date", "assigned_users", "checklist_items"):
            if previous_card.get(key) != current_card.get(key):
                return True
        return False

    def _require_work_item(self, work_item_id: str) -> Dict[str, Any]:
        work_item = self._work_item_index.get(str(work_item_id))
        if not work_item:
            raise RuntimeError(f"Unknown Nextcloud Deck work item: {work_item_id}")
        return work_item

    def _resolve_target_stack_id(self, board_id: str, status_key: str) -> Optional[str]:
        board_mapping = self.runtime.board_stack_mapping.get(str(board_id), {})
        target = board_mapping.get(status_key)
        if target is None:
            return None
        target_text = str(target).strip()
        if target_text.isdigit():
            return target_text
        stack_info = self._resolve_stack_by_title(board_id, target_text)
        return stack_info.get("id") if stack_info else None

    def _resolve_stack_by_title(self, board_id: str, title: str) -> Optional[Dict[str, str]]:
        stack_index = self._board_stack_index.get(str(board_id), {})
        return stack_index.get(str(title).casefold())

    def _resolve_stack_info(self, board_id: str, stack_id: str) -> Dict[str, str]:
        stack_index = self._board_stack_index.get(str(board_id), {})
        for stack in stack_index.values():
            if stack.get("id") == str(stack_id):
                return stack
        return {"id": str(stack_id), "title": str(stack_id)}

    @staticmethod
    def _index_board_stacks(stacks: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
        indexed: Dict[str, Dict[str, str]] = {}
        for stack in stacks:
            if not isinstance(stack, dict):
                continue
            stack_id = str(stack.get("id", "")).strip()
            title = str(stack.get("title") or "").strip()
            if not stack_id or not title:
                continue
            indexed[title.casefold()] = {"id": stack_id, "title": title}
        return indexed


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


async def standalone_send(
    pconfig: PlatformConfig,
    chat_id: str,
    content: str,
    *,
    thread_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    **_: Any,
) -> Dict[str, Any]:
    """Standalone send function for out-of-band delivery from Gateway or CLI."""
    adapter = NextcloudDeckPlatform(pconfig)
    try:
        await adapter.connect()
        result = await adapter.send(chat_id, content, reply_to=thread_id, metadata=metadata)
        if result.success:
            return {"success": True, "message_id": result.message_id}
        return {"error": result.error or "unknown send error"}
    finally:
        await adapter.disconnect()


def check_is_connected(adapter_or_config: Any) -> bool:
    """Check adapter instance connection or fall back to configuration validation."""
    if hasattr(adapter_or_config, "is_connected"):
        return bool(adapter_or_config.is_connected)
    return validate_nextcloud_deck_config(adapter_or_config)


def register(ctx: Any) -> None:
    """Hermes plugin entry point."""
    ctx.register_platform(
        name="nextcloud_deck",
        label="Nextcloud Deck",
        adapter_factory=_build_adapter,
        check_fn=nextcloud_deck_deps_present,
        validate_config=validate_nextcloud_deck_config,
        is_connected=check_is_connected,
        required_env=[
            "NEXTCLOUD_BASE_URL",
            "NEXTCLOUD_USERNAME",
            "NEXTCLOUD_APP_PASSWORD",
            "NEXTCLOUD_DECK_HERMES_USER_ID",
        ],
        install_hint="pip install aiohttp",
        env_enablement_fn=env_enablement,
        apply_yaml_config_fn=apply_yaml_config,
        standalone_sender_fn=standalone_send,
        platform_hint=(
            "You are processing Nextcloud Deck work items. "
            "Cards assigned to the Hermes Deck user are the relevant work items."
        ),
        max_message_length=16000,
        emoji="🗂️",
        allow_update_command=True,
    )
