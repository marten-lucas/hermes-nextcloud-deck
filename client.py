from __future__ import annotations

import base64
import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import aiohttp

logger = logging.getLogger(__name__)


class NextcloudDeckError(RuntimeError):
    pass


class NextcloudDeckClient:
    """Small, strict client for the Nextcloud Deck REST API."""

    def __init__(self, base_url: str, username: str, app_password: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/index.php"):
            self.base_url = self.base_url[:-10]
        self.username = username
        self.app_password = app_password
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    def auth_header(self) -> str:
        try:
            return aiohttp.BasicAuth(self.username, self.app_password).encode()
        except AttributeError:
            token = base64.b64encode(f"{self.username}:{self.app_password}".encode()).decode()
            return f"Basic {token}"

    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": self.auth_header(),
            "OCS-APIRequest": "true",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def deck_url(self, path: str) -> str:
        return urljoin(
            f"{self.base_url}/index.php/apps/deck/api/v1.0/",
            path.lstrip("/"),
        )

    @staticmethod
    def _unwrap(body: Any) -> Any:
        if isinstance(body, dict) and isinstance(body.get("ocs"), dict):
            meta = body["ocs"].get("meta") or {}
            status = str(meta.get("status", "")).lower()
            if status and status != "ok":
                raise NextcloudDeckError(
                    f"Nextcloud OCS error: {meta.get('message', 'unknown error')}"
                )
            return body["ocs"].get("data")
        return body

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        session = await self.ensure_session()
        url = self.deck_url(path)
        try:
            async with session.request(method, url, headers=self.headers(), **kwargs) as resp:
                raw = await resp.text()
                if resp.status >= 400:
                    raise NextcloudDeckError(
                        f"Deck API {method} {path} failed with HTTP {resp.status}: {raw[:500]}"
                    )
                if not raw:
                    return None
                try:
                    return self._unwrap(json.loads(raw))
                except json.JSONDecodeError as exc:
                    raise NextcloudDeckError(
                        f"Deck API returned non-JSON for {path}: {raw[:500]}"
                    ) from exc
        except aiohttp.ClientError as exc:
            raise NextcloudDeckError(f"Deck API connection failed for {path}: {exc}") from exc

    async def get_boards(self) -> List[Dict[str, Any]]:
        data = await self._request("GET", "boards")
        return data if isinstance(data, list) else []

    async def get_stacks(self, board_id: str | int) -> List[Dict[str, Any]]:
        data = await self._request("GET", f"boards/{board_id}/stacks")
        return data if isinstance(data, list) else []

    async def get_card_comments(self, card_id: str | int) -> List[Dict[str, Any]]:
        data = await self._request("GET", f"cards/{card_id}/comments")
        return data if isinstance(data, list) else []

    async def add_comment(self, card_id: str | int, message: str) -> Optional[Dict[str, Any]]:
        data = await self._request(
            "POST",
            f"cards/{card_id}/comments",
            json={"message": str(message)[:1000]},
        )
        return data if isinstance(data, dict) else None

    async def move_card(
        self,
        board_id: str | int,
        stack_id: str | int,
        card_id: str | int,
        target_stack_id: str | int,
        order: int = 0,
    ) -> Optional[Dict[str, Any]]:
        data = await self._request(
            "PUT",
            f"boards/{board_id}/stacks/{stack_id}/cards/{card_id}/reorder",
            json={"stackId": target_stack_id, "order": int(order)},
        )
        return data if isinstance(data, dict) else None

    async def update_card(
        self,
        board_id: str | int,
        stack_id: str | int,
        card_id: str | int,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        due_date: Optional[str] = None,
        done: Any = None,
    ) -> Optional[Dict[str, Any]]:
        payload: Dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if description is not None:
            payload["description"] = description
        if due_date is not None:
            payload["duedate"] = due_date
        if done is not None:
            payload["done"] = done
        data = await self._request(
            "PUT",
            f"boards/{board_id}/stacks/{stack_id}/cards/{card_id}",
            json=payload,
        )
        return data if isinstance(data, dict) else None