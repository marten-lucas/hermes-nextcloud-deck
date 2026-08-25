from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import quote, urljoin

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
        def __init__(self, config: Any, platform: str = "deck") -> None:
            self.config = config
            self.platform = platform

        def build_source(self, **kwargs: Any) -> Dict[str, Any]:
            return kwargs

        def handle_message(self, event: MessageEvent) -> None:
            return None

        def _mark_disconnected(self) -> None:
            return None


@dataclass
class DeckRuntimeConfig:
    base_url: str
    username: str
    app_password: str
    hermes_user_id: str
    home_channel: Optional[str] = None
    poll_interval_seconds: float = 30.0
    debug: bool = False
    board_stack_mapping: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    observed_boards: Dict[str, Dict[str, Any]] = field(default_factory=dict)


class NextcloudDeckPlatform(BasePlatformAdapter):
    """Nextcloud Deck adapter using polling transport."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("deck"))
        self.runtime = self._build_runtime_config(config)
        self._session: Optional[aiohttp.ClientSession] = None
        self._stop_event = asyncio.Event()
        self._polling_task: Optional[asyncio.Task[None]] = None
        self._connected_flag: bool = False
        self._discovered_boards: Dict[str, Dict[str, Any]] = {}
        self._board_stack_index: Dict[str, Dict[str, Dict[str, str]]] = {}
        self._card_snapshots: Dict[str, str] = {}
        self._work_item_index: Dict[str, Dict[str, Any]] = {}
        self._time_fn: Callable[[], float] = time.time
        self._user_groups_cache: Dict[str, List[str]] = {}

    @property
    def is_connected(self) -> bool:
        return bool(
            self._connected_flag
            and self._session
            and not self._session.closed
            and not self._stop_event.is_set()
            and (self._polling_task is not None and not self._polling_task.done())
        )

    @property
    def home_channel(self) -> Optional[str]:
        return self.runtime.home_channel

    @staticmethod
    def _as_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _build_runtime_config(self, config: PlatformConfig) -> DeckRuntimeConfig:
        extra = getattr(config, "extra", {}) or {}

        raw_url = str(
            extra.get("base_url")
            or extra.get("deck_base_url")
            or os.getenv("NEXTCLOUD_DECK_BASE_URL", "")
            or os.getenv("NEXTCLOUD_BASE_URL", "")
        ).strip()

        base_url = raw_url.rstrip("/")
        if base_url.endswith("/index.php"):
            base_url = base_url[:-10]

        username = str(
            extra.get("username")
            or extra.get("deck_username")
            or os.getenv("NEXTCLOUD_DECK_USERNAME", "")
            or os.getenv("NEXTCLOUD_USERNAME", "")
        ).strip()

        app_password = str(
            extra.get("app_password")
            or extra.get("deck_app_password")
            or getattr(config, "token", "")
            or os.getenv("NEXTCLOUD_DECK_APP_PASSWORD", "")
            or os.getenv("NEXTCLOUD_APP_PASSWORD", "")
        ).strip()

        hermes_user_id = str(
            extra.get("hermes_user_id")
            or os.getenv("NEXTCLOUD_DECK_HERMES_USER_ID", "")
        ).strip()

        home_channel = str(
            extra.get("home_channel")
            or os.getenv("NEXTCLOUD_DECK_HOME_CHANNEL", "")
            or getattr(config, "home_channel", "")
        ).strip() or None

        debug_flag = str(
            extra.get("debug")
            or os.getenv("NEXTCLOUD_DECK_DEBUG", "")
        ).strip().lower() in {"1", "true", "yes", "on"}

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
            base_url=base_url,
            username=username,
            app_password=app_password,
            hermes_user_id=hermes_user_id,
            home_channel=home_channel,
            debug=debug_flag,
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
        return f"{self.runtime.base_url}/index.php/apps/deck/api/v1.1/{normalized}"

    def _cloud_ocs_url(self, path: str) -> str:
        return urljoin(f"{self.runtime.base_url}/ocs/v1.php/cloud/", path.lstrip("/"))

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _get_user_groups(self, user_id: str) -> List[str]:
        user_id = str(user_id or "").strip()
        if not user_id:
            return []

        if user_id in self._user_groups_cache:
            return list(self._user_groups_cache[user_id])

        try:
            session = await self._ensure_session()
            encoded_user_id = quote(user_id, safe="")
            path = f"users/{encoded_user_id}/groups"
            async with session.get(
                self._cloud_ocs_url(path),
                params={"format": "json"},
                headers=self._api_headers(),
            ) as resp:
                body = await resp.json() if "application/json" in resp.headers.get("Content-Type", "") else {}

            data = self._unwrap_ocs(body)
            groups: List[str] = []
            if isinstance(data, dict):
                raw_groups = data.get("groups", [])
                if isinstance(raw_groups, dict):
                    raw_groups = raw_groups.get("element", [])
                if isinstance(raw_groups, list):
                    groups = [str(g).strip() for g in raw_groups if str(g).strip()]
            elif isinstance(data, list):
                groups = [str(g).strip() for g in data if str(g).strip()]

            self._user_groups_cache[user_id] = groups
            return list(groups)
        except Exception as exc:
            logger.warning("Konnte Gruppen für User %s nicht abfragen: %s", user_id, exc)
            return []

    async def _api_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return await self._api_request("get", path, params=params)

    async def _api_put(self, path: str, data: Dict[str, Any]) -> Any:
        return await self._api_request("put", path, json=data)

    async def _api_request(self, method: str, path: str, **kwargs: Any) -> Any:
        session = await self._ensure_session()
        request_fn = getattr(session, method)
        url = self._deck_url(path)

        if self.runtime.debug:
            logger.info("Nextcloud Deck API %s %s payload=%r", method.upper(), url, kwargs.get("json"))

        async with request_fn(url, headers=self._api_headers(), **kwargs) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw_text = await resp.text()

            if resp.status >= 400:
                logger.error(
                    "Nextcloud Deck API ERROR %s %s -> HTTP %s content_type=%s body=%r request_id=%s",
                    method.upper(),
                    url,
                    resp.status,
                    content_type,
                    raw_text[:2000],
                    resp.headers.get("x-request-id"),
                )
                raise RuntimeError(
                    f"Nextcloud Deck request failed [{resp.status}] for {path}: {raw_text[:500]}"
                )

            if "application/json" in content_type:
                try:
                    body = json.loads(raw_text)
                except json.JSONDecodeError:
                    raise RuntimeError(f"Invalid JSON from Nextcloud Deck [{path}]: {raw_text[:500]}")
            else:
                body = raw_text

            return self._unwrap_ocs(body)

    async def _fetch_card_comments(self, card_id: str) -> List[Dict[str, Any]]:
        session = await self._ensure_session()
        url = f"{self.runtime.base_url}/remote.php/dav/comments/deckCard/{card_id}/"
        headers = self._api_headers()
        headers["Content-Type"] = "application/xml"

        try:
            async with session.request("PROPFIND", url, headers=headers) as resp:
                if resp.status >= 400:
                    return []
                xml_data = await resp.text()
                return self._parse_webdav_comments_xml(xml_data)
        except Exception:
            return []

    @staticmethod
    def _parse_webdav_comments_xml(xml_string: str) -> List[Dict[str, Any]]:
        comments: List[Dict[str, Any]] = []
        try:
            root = ET.fromstring(xml_string)
            ns = {"d": "DAV:", "oc": "http://owncloud.org/ns"}
            for response in root.findall("d:response", ns):
                propstat = response.find("d:propstat", ns)
                if propstat is None:
                    continue
                prop = propstat.find("d:prop", ns)
                if prop is None:
                    continue
                message_node = prop.find("oc:message", ns)
                if message_node is None or not message_node.text:
                    continue
                cid = prop.findtext("oc:id", default="", namespaces=ns)
                comments.append({
                    "id": cid,
                    "message": message_node.text,
                    "actor": {
                        "id": prop.findtext("oc:actorId", default="", namespaces=ns),
                        "displayname": prop.findtext("oc:actorDisplayName", default="", namespaces=ns),
                    },
                    "creationDateTime": prop.findtext("oc:creationDateTime", default="", namespaces=ns),
                })
        except Exception:
            pass

        comments.sort(key=lambda c: int(c["id"]) if str(c.get("id", "")).isdigit() else 0)
        return comments

    async def _post_card_comment(self, card_id: str, message: str) -> Optional[str]:
        session = await self._ensure_session()
        url = f"{self.runtime.base_url}/remote.php/dav/comments/deckCard/{card_id}/"
        payload = {
            "actorType": "users",
            "actorId": self.runtime.username,
            "message": message[:1000],
            "objectType": "deckCard",
            "objectId": str(card_id),
            "verb": "comment",
        }
        async with session.post(url, json=payload, headers=self._api_headers()) as resp:
            if resp.status >= 400:
                raw_text = await resp.text()
                raise RuntimeError(f"WebDAV Comment POST failed [{resp.status}]: {raw_text[:200]}")
            location = resp.headers.get("Content-Location", "")
            return location.split("/")[-1] if location else None

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
        return True

    async def disconnect(self) -> None:
        self._stop_event.set()
        self._connected_flag = False
        if self._polling_task is not None:
            self._polling_task.cancel()
            await asyncio.gather(self._polling_task, return_exceptions=True)
            self._polling_task = None
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        self._mark_disconnected()

    async def start(self) -> None:
        await self.connect()

    async def stop(self) -> None:
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
        return list(discovered.values())

    async def poll_once(self) -> List[MessageEvent]:
        try:
            boards = await self.fetch_boards_once()
            self._connected_flag = True
        except Exception as exc:
            self._connected_flag = False
            raise exc

        events: List[MessageEvent] = []
        for board in boards:
            board_id = str(board.get("id", "")).strip()
            if self.runtime.observed_boards and board_id not in self.runtime.observed_boards:
                continue
            _, _, board_events = await self._ingest_board_once(board)
            events.extend(board_events)
        return events

    async def _ingest_board_once(self, board: Dict[str, Any]) -> tuple[int, int, List[MessageEvent]]:
        board_id = str(board.get("id", "")).strip()
        board_title = str(board.get("title") or board_id)
        stacks = await self._api_get(f"boards/{board_id}/stacks")
        if not isinstance(stacks, list):
            raise RuntimeError(f"Expected stacks list for board {board_id}")
        self._board_stack_index[board_id] = self._index_board_stacks(stacks)
        
        emitted: List[MessageEvent] = []
        cards_scanned = 0
        cards_matched = 0
        active_work_item_ids: set[str] = set()

        for stack in stacks:
            if not isinstance(stack, dict):
                continue
            for card in stack.get("cards") or []:
                if not isinstance(card, dict):
                    continue
                cards_scanned += 1
                card_id = str(card.get("id", "")).strip()
                work_item_id = f"deck:board:{board_id}:card:{card_id}"
                active_work_item_ids.add(work_item_id)

                comments = await self._fetch_card_comments(card_id)

                if not self._card_should_process(card, comments, board_title):
                    continue

                cards_matched += 1
                payload = await self._build_card_payload(board, stack, card, comments)
                event = await self._maybe_build_card_event(payload)
                if event is not None:
                    await self.handle_message(event)
                    emitted.append(event)

        tracked_ids = [
            wid for wid, data in list(self._work_item_index.items())
            if data.get("board_id") == board_id
        ]
        for wid in tracked_ids:
            if wid not in active_work_item_ids:
                logger.info("Nextcloud Deck: Karte %s auf Board %s wurde gelöscht. Bereinige State.", wid, board_id)
                self._work_item_index.pop(wid, None)
                self._card_snapshots.pop(wid, None)

        return cards_scanned, cards_matched, emitted

    def _card_should_process(self, card: Dict[str, Any], comments: List[Dict[str, Any]], board_title: str) -> bool:
        if self._card_assigned_to_hermes(card):
            return True
        targets = {self.runtime.hermes_user_id.lower(), self.runtime.username.lower(), "ki_assistent", "ki gerda"}
        desc = str(card.get("description") or "").lower()
        if any(t in desc for t in targets):
            return True
        for c in comments:
            if any(t in str(c.get("message", "")).lower() for t in targets):
                return True
        return False

    async def _build_card_payload(
        self,
        board: Dict[str, Any],
        stack: Dict[str, Any],
        card: Dict[str, Any],
        comments: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        card_id = str(card.get("id", "")).strip()
        description = str(card.get("description") or "")
        
        owner_info = card.get("owner")
        owner_id = ""
        if isinstance(owner_info, dict):
            owner_id = str(owner_info.get("primaryKey") or owner_info.get("uid") or "").strip()
        elif owner_info:
            owner_id = str(owner_info).strip()

        payload = {
            "board": {"id": str(board.get("id", "")).strip(), "title": str(board.get("title") or "")},
            "stack": {"id": str(stack.get("id", "")).strip(), "title": str(stack.get("title") or "")},
            "card": {
                "id": card_id,
                "title": str(card.get("title") or ""),
                "description": description,
                "due_date": card.get("duedate"),
                "order": card.get("order", 0),
                "owner": owner_id,
                "assigned_users": self._normalize_assigned_users(card.get("assignedUsers")),
                "comments": self._normalize_comments(comments),
            },
        }
        work_item_id = self._work_item_id(payload)
        
        self._work_item_index[work_item_id] = {
            "board_id": payload["board"]["id"],
            "board_title": payload["board"]["title"],
            "stack_id": payload["stack"]["id"],
            "stack_title": payload["stack"]["title"],
            "card_id": payload["card"]["id"],
            "card_title": payload["card"]["title"],
            "description": payload["card"]["description"],
            "due_date": payload["card"]["due_date"],
            "order": card.get("order", 0),
            "owner": owner_id,
            "payload": payload,
        }
        return payload

    async def _maybe_build_card_event(self, payload: Dict[str, Any]) -> Optional[MessageEvent]:
        signature = self._payload_signature(payload)
        work_item_id = self._work_item_id(payload)
        if self._card_snapshots.get(work_item_id) == signature:
            return None
        
        comments = payload["card"]["comments"]
        last_comment = comments[-1] if comments else None
        last_author = last_comment["author_id"] if last_comment else (payload["card"]["owner"] or self.runtime.hermes_user_id)

        bot_ids = {self.runtime.username.lower(), self.runtime.hermes_user_id.lower(), "ki_assistent", "ki gerda"}
        if last_author.lower() in bot_ids:
            self._card_snapshots[work_item_id] = signature
            return None

        self._card_snapshots[work_item_id] = signature

        source = self.build_source(
            chat_id=work_item_id,
            chat_name=payload["card"]["title"] or work_item_id,
            chat_type="work_item",
            user_id=last_author,
            user_name=last_author,
            message_id=payload["card"]["id"] or None,
        )

        event_text = f"Nextcloud Deck Karte: {payload['card']['title']}\nBeschreibung:\n{payload['card']['description']}"
        if last_comment and last_comment.get("message"):
            event_text += f"\n\nNeuester Kommentar von {last_comment.get('author_name', last_author)}:\n{last_comment['message']}"

        return MessageEvent(
            text=event_text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=payload,
            message_id=payload["card"]["id"] or None,
            user_id=last_author,
            user_name=last_author,
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
        normalized = []
        for entry in raw_users:
            if not isinstance(entry, dict):
                continue
            participant = entry.get("participant") if isinstance(entry.get("participant"), dict) else {}
            uid = (
                participant.get("uid")
                or participant.get("primaryKey")
                or entry.get("userId")
                or (entry.get("uid") if isinstance(entry.get("uid"), str) else None)
            )
            name = participant.get("displayname") or uid or ""
            if uid:
                normalized.append({"id": str(uid), "name": str(name)})
        return normalized

    @staticmethod
    def _normalize_comments(raw_comments: Any) -> List[Dict[str, str]]:
        if not isinstance(raw_comments, list):
            return []
        normalized = []
        for entry in raw_comments:
            if not isinstance(entry, dict):
                continue
            author = entry.get("actor") or {}
            normalized.append({
                "id": str(entry.get("id", "")),
                "message": str(entry.get("message", "")),
                "author_id": str(author.get("id") or author.get("uid") or ""),
                "author_name": str(author.get("displayname") or author.get("id") or ""),
            })
        return normalized

    async def update_card_description(self, work_item_id: str, new_description: str) -> None:
        work_item = self._require_work_item(work_item_id)
        board_id = int(work_item["board_id"])
        stack_id = int(work_item["stack_id"])
        card_id = int(work_item["card_id"])

        try:
            await self._api_put(
                f"boards/{board_id}/stacks/{stack_id}/cards/{card_id}?format=json",
                {
                    "title": work_item["card_title"],
                    "description": new_description,
                    "type": "plain",
                    "order": work_item.get("order", 0),
                    "owner": work_item.get("owner", self.runtime.username),
                },
            )
            work_item["description"] = new_description
        except RuntimeError as exc:
            if "[404]" in str(exc):
                logger.warning("Nextcloud Deck: Update fehlgeschlagen, Karte %s existiert nicht mehr.", work_item_id)
                self._work_item_index.pop(work_item_id, None)
                self._card_snapshots.pop(work_item_id, None)
            raise exc

    async def move_card_to_status(self, work_item_id: str, status_key: str) -> None:
        work_item = self._require_work_item(work_item_id)
        board_id = int(work_item["board_id"])
        current_stack_id = int(work_item["stack_id"])
        card_id = int(work_item["card_id"])

        target_stack_raw = self._resolve_target_stack_id(board_id, status_key)
        if not target_stack_raw:
            return
        target_stack_id = int(target_stack_raw)

        if target_stack_id == current_stack_id:
            return

        try:
            await self._api_put(
                f"boards/{board_id}/stacks/{current_stack_id}/cards/{card_id}/reorder?format=json",
                {
                    "order": 0,
                    "stackId": target_stack_id,
                },
            )
            work_item["stack_id"] = str(target_stack_id)
            work_item["order"] = 0
        except RuntimeError as exc:
            if "[404]" in str(exc):
                logger.warning("Nextcloud Deck: Move fehlgeschlagen, Karte %s existiert nicht mehr.", work_item_id)
                self._work_item_index.pop(work_item_id, None)
                self._card_snapshots.pop(work_item_id, None)
            raise exc

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        work_item = self._work_item_index.get(str(chat_id))
        if not work_item:
            return SendResult(success=False, error=f"Unknown work item: {chat_id}")
        metadata = metadata or {}
        
        if metadata.get("description") or metadata.get("new_description"):
            await self.update_card_description(chat_id, str(metadata.get("description") or metadata.get("new_description")))
        if metadata.get("target_status"):
            await self.move_card_to_status(chat_id, str(metadata.get("target_status")))

        if content:
            msg_id = await self._post_card_comment(work_item["card_id"], content)
            return SendResult(success=True, message_id=msg_id)
        return SendResult(success=True)

    def _require_work_item(self, work_item_id: str) -> Dict[str, Any]:
        item = self._work_item_index.get(str(work_item_id))
        if not item:
            raise RuntimeError(f"Unknown work item: {work_item_id}")
        return item

    def _resolve_target_stack_id(self, board_id: str | int, status_key: str) -> Optional[str]:
        mapping = self.runtime.board_stack_mapping.get(str(board_id), {})
        target = mapping.get(status_key)
        if not target:
            return None
        return str(target).strip()

    @staticmethod
    def _index_board_stacks(stacks: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
        indexed = {}
        for s in stacks:
            sid = str(s.get("id", ""))
            title = str(s.get("title", ""))
            if sid and title:
                indexed[title.casefold()] = {"id": sid, "title": title}
        return indexed

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "work_item"}


def register(ctx: Any) -> None:
    ctx.register_platform(
        name="deck",
        label="Nextcloud Deck",
        adapter_factory=lambda cfg: NextcloudDeckPlatform(cfg),
        check_fn=lambda: True,
        validate_config=lambda cfg: True,
        is_connected=lambda cfg: True,
        required_env=["NEXTCLOUD_DECK_BASE_URL", "NEXTCLOUD_DECK_USERNAME", "NEXTCLOUD_DECK_APP_PASSWORD", "NEXTCLOUD_DECK_HERMES_USER_ID"],
    )