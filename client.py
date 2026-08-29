from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import aiohttp

logger = logging.getLogger(__name__)


class NextcloudDeckClient:
    """Client für die Nextcloud Deck REST API (OCS v2.php/apps/deck/api/v1.0)."""

    def __init__(self, base_url: str, username: str, app_password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.app_password = app_password
        self._session: Optional[aiohttp.ClientSession] = None

    def auth_header(self) -> str:
        """Abwärtskompatibles Basic Auth Header Encoding."""
        try:
            return aiohttp.BasicAuth(self.username, self.app_password).encode()
        except AttributeError:
            token = base64.b64encode(f"{self.username}:{self.app_password}".encode()).decode()
            return f"Basic {token}"

    def ocs_headers(self) -> Dict[str, str]:
        return {
            "Authorization": self.auth_header(),
            "OCS-APIRequest": "true",
            "Accept": "application/json",
        }

    async def ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def deck_url(self, path: str) -> str:
        return urljoin(f"{self.base_url}/ocs/v2.php/apps/deck/api/v1.0/", path.lstrip("/"))

    async def get_boards(self) -> List[Dict[str, Any]]:
        session = await self.ensure_session()
        async with session.get(self.deck_url("boards"), headers=self.ocs_headers()) as resp:
            if resp.status != 200:
                return []
            body = await resp.json()
            return body.get("ocs", {}).get("data", []) or []

    async def get_stacks(self, board_id: str | int) -> List[Dict[str, Any]]:
        session = await self.ensure_session()
        async with session.get(
            self.deck_url(f"boards/{board_id}/stacks"), headers=self.ocs_headers()
        ) as resp:
            if resp.status != 200:
                return []
            body = await resp.json()
            return body.get("ocs", {}).get("data", []) or []

    async def get_card(
        self, board_id: str | int, stack_id: str | int, card_id: str | int
    ) -> Optional[Dict[str, Any]]:
        session = await self.ensure_session()
        url = self.deck_url(f"boards/{board_id}/stacks/{stack_id}/cards/{card_id}")
        async with session.get(url, headers=self.ocs_headers()) as resp:
            if resp.status != 200:
                return None
            body = await resp.json()
            return body.get("ocs", {}).get("data")

    async def get_card_comments(self, card_id: str | int) -> List[Dict[str, Any]]:
        """Holt Kommentare separat ab (Card-Objekt enthält nur commentsUnread-Zähler)."""
        session = await self.ensure_session()
        url = self.deck_url(f"cards/{card_id}/comments")
        async with session.get(url, headers=self.ocs_headers()) as resp:
            if resp.status != 200:
                return []
            body = await resp.json()
            return body.get("ocs", {}).get("data", []) or []

    async def add_comment(self, card_id: str | int, message: str) -> Optional[Dict[str, Any]]:
        session = await self.ensure_session()
        url = self.deck_url(f"cards/{card_id}/comments")
        async with session.post(url, data={"message": message}, headers=self.ocs_headers()) as resp:
            if resp.status not in (200, 201):
                return None
            body = await resp.json()
            return body.get("ocs", {}).get("data")

    async def move_card(
        self, board_id: str | int, stack_id: str | int, card_id: str | int, target_stack_id: str | int, order: int = 0
    ) -> Optional[Dict[str, Any]]:
        session = await self.ensure_session()
        url = self.deck_url(f"boards/{board_id}/stacks/{stack_id}/cards/{card_id}/reorder")
        payload = {"stackId": target_stack_id, "order": order}
        async with session.put(url, json=payload, headers=self.ocs_headers()) as resp:
            if resp.status != 200:
                return None
            body = await resp.json()
            return body.get("ocs", {}).get("data")

    async def update_card(
        self,
        board_id: str | int,
        stack_id: str | int,
        card_id: str | int,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        session = await self.ensure_session()
        url = self.deck_url(f"boards/{board_id}/stacks/{stack_id}/cards/{card_id}")
        payload: Dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if description is not None:
            payload["description"] = description
        async with session.put(url, json=payload, headers=self.ocs_headers()) as resp:
            if resp.status != 200:
                return None
            body = await resp.json()
            return body.get("ocs", {}).get("data")